"""One predefined read-only count over pinned, time-filtered complaint versions."""

from pathlib import Path
from typing import Annotated

import sqlalchemy as sa
from pydantic import Field

from database.schema import entity_components, entity_projections, entity_vehicles, record_versions
from domain.tasks import Contract, EventInterval, Text
from entities.errors import ProjectionNotReady, QueryDatabaseUnavailable
from entities.normalize import EntityRules, lexical
from ingestion.records import prepare_import
from ingestion.temporal import TemporalRequest, select_versions


class ComplaintCountRequest(Contract):
    temporal: TemporalRequest
    make: Text
    model: Text
    model_year: Annotated[int, Field(strict=True, ge=1886, le=2100)]
    components: list[Text] = []
    production_interval: EventInterval | None = None


def count_complaints(
    engine: sa.Engine | None, root: Path, request: ComplaintCountRequest, rules: EntityRules
) -> dict:
    make, model = lexical(request.make), lexical(request.model)
    components = sorted(set(lexical(label) for label in request.components))
    if model not in rules.models_by_make.get(make, []) or not set(components) <= set(
        rules.component_labels
    ):
        raise ValueError("query requires exact mapped entity labels, not fuzzy candidates")
    selection = select_versions(root, request.temporal)
    context = {
        "rules_version": rules.rules_version,
        "rules_sha256": rules.sha256,
        "selection_sha256": selection["selection_sha256"],
        "input_set_sha256": selection["input_set_sha256"],
        "inputs": selection["inputs"],
        "mapping_version": selection["mapping_version"],
        "query": {
            "make": make,
            "model": model,
            "model_year": request.model_year,
            "components": components,
        },
        "scope": "selected_archived_responses_only",
        "count_unit": "distinct_odi",
        "population_completeness": "not_established",
        "production_interval": request.production_interval.model_dump(mode="json")
        if request.production_interval
        else None,
        "dataset_release": None,
        "time": request.temporal.time.model_dump(mode="json"),
        "analysis_label": selection["analysis_label"],
        "temporal_status": selection["status"],
    }
    if selection["status"] not in {"selected", "empty"}:
        return context | {
            "status": "unsupported",
            "reason": "temporal_selection_incomplete",
            "distinct_complaints": None,
        }
    identifiers = [
        entry["version_id"]
        for entry in selection["selected"]
        if entry["source_kind"] == "complaint_api"
    ]
    # A zero result requires selected complaint-source evidence, not merely a recall/PDF input set.
    if not any(
        prepare_import(root, identifier).snapshot.source.kind == "complaint_api"
        for identifier in request.temporal.snapshot_ids
    ):
        return context | {
            "status": "unsupported",
            "reason": "no_complaint_source_in_input",
            "distinct_complaints": None,
        }
    if engine is None:
        raise QueryDatabaseUnavailable("count queries require a configured database")
    with (
        engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection,
        connection.begin(),
    ):
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        projected = connection.execute(
            sa.select(entity_projections.c.version_id, entity_projections.c.rules_sha256).where(
                entity_projections.c.version_id.in_(identifiers),
                entity_projections.c.rules_version == rules.rules_version,
            )
        ).all()
        if len(projected) != len(identifiers) or any(
            row.rules_sha256 != rules.sha256 for row in projected
        ):
            raise ProjectionNotReady(
                "all selected complaint versions require the exact entity projection"
            )
        source = record_versions
        vehicles, parts = entity_vehicles, entity_components
        vehicle_conditions = [
            vehicles.c.version_id == source.c.version_id,
            vehicles.c.rules_version == rules.rules_version,
            vehicles.c.make == make,
            vehicles.c.model == model,
            vehicles.c.model_year == request.model_year,
        ]
        component_conditions = [
            parts.c.version_id == source.c.version_id,
            parts.c.rules_version == rules.rules_version,
        ]
        if components:
            component_conditions.append(parts.c.canonical.in_(components))
        vehicle_match = sa.exists(sa.select(1).where(*vehicle_conditions))
        component_match = (
            sa.exists(sa.select(1).where(*component_conditions)) if components else sa.true()
        )
        base = sa.select(source.c.version_id, source.c.record_key).where(
            source.c.version_id.in_(identifiers),
            source.c.source_kind == "complaint_api",
            vehicle_match,
            component_match,
        )
        unknown_dates: int | None = None
        if request.production_interval:
            interval = request.production_interval
            unknown_dates = (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(
                        base.where(
                            sa.exists(
                                sa.select(1).where(
                                    *vehicle_conditions, vehicles.c.production_date.is_(None)
                                )
                            )
                        ).subquery()
                    )
                )
                or 0
            )
            base = base.where(
                sa.exists(
                    sa.select(1).where(
                        *vehicle_conditions,
                        vehicles.c.production_date.between(interval.start, interval.end),
                    )
                )
            )
        matched = base.subquery()
        versions, complaints = connection.execute(
            sa.select(
                sa.func.count(), sa.func.count(sa.distinct(matched.c.record_key))
            ).select_from(matched)
        ).one()
        join_condition = sa.and_(
            parts.c.version_id == matched.c.version_id, parts.c.rules_version == rules.rules_version
        )
        if components:
            join_condition = sa.and_(join_condition, parts.c.canonical.in_(components))
        component_rows = connection.scalar(
            sa.select(sa.func.count()).select_from(matched.join(parts, join_condition))
        )
    return context | {
        "status": "partial" if unknown_dates else "counted",
        "distinct_complaints": complaints,
        "matched_versions": versions,
        "matching_component_rows": component_rows,
        "unknown_production_date_versions": unknown_dates,
        "production_filter_status": "not_requested"
        if request.production_interval is None
        else ("incomplete_dates" if unknown_dates else "evaluated"),
        "count_interpretation": "confirmed_by_known_production_dates"
        if request.production_interval
        else "matching_structured_labels",
        "query_operation": "predefined_exists_and_distinct_count",
    }
