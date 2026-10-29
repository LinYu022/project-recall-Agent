"""Transactional projections; source records and previous rules remain immutable."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from database.schema import entity_components, entity_projections, entity_vehicles, record_versions
from entities.normalize import EntityRules, project_record


def project_versions(engine: sa.Engine, version_ids: list[str], rules: EntityRules) -> dict:
    created = 0
    with engine.begin() as connection:
        for identifier in sorted(set(version_ids)):
            row = (
                connection.execute(
                    sa.select(record_versions).where(record_versions.c.version_id == identifier)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ValueError("source version must be imported before entity projection")
            projection = project_record(dict(row), rules)
            claim = (
                insert(entity_projections)
                .values(
                    version_id=identifier,
                    rules_version=rules.rules_version,
                    rules_sha256=rules.sha256,
                    projection=projection,
                )
                .on_conflict_do_nothing(index_elements=["version_id", "rules_version"])
                .returning(entity_projections.c.version_id)
            )
            if connection.scalar(claim) is None:
                saved = connection.scalar(
                    sa.select(entity_projections.c.projection).where(
                        entity_projections.c.version_id == identifier,
                        entity_projections.c.rules_version == rules.rules_version,
                    )
                )
                if saved != projection:
                    raise ValueError(
                        "entity rules or projection changed without a new rules_version"
                    )
                continue
            created += 1
            for vehicle in projection["vehicles"]:
                connection.execute(
                    entity_vehicles.insert().values(
                        version_id=identifier,
                        rules_version=rules.rules_version,
                        position=vehicle["position"],
                        make=vehicle["make"]["canonical"],
                        model=vehicle["model"]["canonical"],
                        model_year=vehicle["model_year"]["canonical"],
                        production_date=vehicle["production_date"]["canonical"],
                    )
                )
            for index, component in enumerate(projection["components"]):
                connection.execute(
                    entity_components.insert().values(
                        version_id=identifier,
                        rules_version=rules.rules_version,
                        position=index,
                        canonical=component["canonical"],
                        raw_label=component["raw"] if isinstance(component["raw"], str) else None,
                    )
                )
    return {
        "status": "projected",
        "rules_version": rules.rules_version,
        "rules_sha256": rules.sha256,
        "source_versions": len(set(version_ids)),
        "new_projections": created,
    }
