"""Freeze, run, inspect and review one complete-denominator experiment."""

import argparse
import json
import os
from pathlib import Path

from evaluation.analysis import summarize
from evaluation.executor import CellExecutor, ScriptedModel
from evaluation.freeze import load, seal
from evaluation.records import MeteredModel, ProviderLedger, RunRecords, atomic_json, exclusive
from evaluation.session import session
from ingestion.records import content_hash
from models.chat_api import ChatJSONModel
from releases.bundle import BundleStore
from releases.catalog import Catalog
from releases.contracts import PublishRequest
from runtime.artifacts import ArtifactStore


def observations(records, protocol):
    cases = {c.case_id: c for c in protocol.cases}
    result = []
    for row in records.snapshot()["rows"].values():
        cell = {
            k: row[k]
            for k in [
                "cell_id",
                "arm_id",
                "case_id",
                "repeat",
                "family",
                "method",
                "event_cluster_id",
            ]
        }
        value = (
            records.store.read_json(row["record"]["sha256"])
            if row["record"]
            else {"cell": cell, "state": row["state"], "category": cases[row["case_id"]].category}
        )
        result.append(value)
    return result


def analyze(identity, body, protocol, root, mode):
    records = RunRecords(root, identity, body, mode=mode)
    rows = observations(records, protocol)
    from evaluation.costs import usage_for_cell

    for row in rows:
        if mode == "live":
            row["observed_provider_usage"] = usage_for_cell(
                Path(".local/P1-16/provider-request-allowance.json"), row["cell"]["cell_id"]
            )
    review_index = root / "reviews.json"
    if review_index.exists():
        index = json.loads(review_index.read_bytes())
        for row in rows:
            pointer = index.get(row["cell"]["cell_id"])
            if pointer:
                saved = records.store.read_json(pointer["sha256"])
                original = records.store.read_json(
                    records.snapshot()["rows"][row["cell"]["cell_id"]]["record"]["sha256"]
                )
                if saved["observation_sha256"] != content_hash(original):
                    raise ValueError("review_does_not_match_current_observation")
                row |= saved["result"]
    result = {
        "experiment_id": identity["experiment_id"],
        "mode": mode,
        "summary": summarize(protocol, rows),
        "rows": rows,
    }
    atomic_json(root / "analysis.json", result)
    return result


def run(args, parser, identity, body, protocol, root, records):
    if args.maximum_cells is not None and args.maximum_cells < 1:
        parser.error("maximum-cells must be positive")
    if args.arms and not set(args.arms) <= {a.arm_id for a in protocol.arms}:
        parser.error("unknown arm")
    # This acknowledgement flag cannot itself grant human permission. An agent must
    # first receive explicit authorization for the concrete proposal from the user.
    if args.mode == "live" and args.approved_experiment != identity["experiment_id"]:
        parser.error("live requests require the exact approved frozen experiment ID")
    if args.mode == "live":
        approval = json.loads((args.label_root / "approval.json").read_bytes())
        if (
            approval["status"] != "explicit_user_approved"
            or approval["protocol_content_sha256"] != content_hash(protocol.model_dump(mode="json"))
            or approval["maximum_requests"] != body["maximum_provider_requests"]
            or approval["model"] != protocol.model_id
            or approval["endpoint"] != protocol.endpoint + "/chat/completions"
        ):
            raise ValueError("live_approval_does_not_cover_this_protocol")
    for case in protocol.cases:
        from investigation.contracts import ReferenceCase

        reference = ReferenceCase.model_validate_json(
            (args.label_root / "references" / (case.case_id + ".json")).read_bytes()
        )
        if content_hash(reference.model_dump(mode="json")) != case.reference_sha256:
            raise ValueError("reference_changed_before_evaluation_execution")
    selected = [c for c in body["cells"] if not args.arms or c["arm_id"] in args.arms]
    model = ChatJSONModel() if args.mode == "live" else ScriptedModel()
    if isinstance(model, ChatJSONModel) and (
        model.model_id != protocol.model_id or model.base != protocol.endpoint
    ):
        raise ValueError("configured_provider_differs_from_freeze")
    by_id = {c.case_id: c for c in protocol.cases}
    limits = {
        c["cell_id"]: 0
        if by_id[c["case_id"]].category
        in {"missing_conditions", "time_boundary", "scope_exclusion", "insufficient"}
        else 1
        if c["method"] == "fixed"
        else protocol.profile.max_model_calls
        for c in body["cells"]
    }
    ledger = (
        ProviderLedger(
            Path(".local/P1-16/provider-request-allowance.json"),
            experiment_id=identity["experiment_id"],
            cells=limits,
            identity=model.identity,
        )
        if args.mode == "live"
        else None
    )

    def factory(cell):
        return (
            MeteredModel(model, ledger, cell["cell_id"], ArtifactStore(root / "provider"))
            if ledger
            else model
        )

    url = os.environ.get("RECALL_EVAL_DATABASE_URL")
    if not url:
        raise ValueError("dedicated_RECALL_EVAL_DATABASE_URL_required")
    with (
        exclusive(root / "execution.json"),
        session(url, identity["experiment_id"], mode=args.mode) as (engine, _),
    ):
        catalog = Catalog(engine, BundleStore(Path(".local/releases")))
        from datetime import UTC, datetime, timedelta

        with catalog.transaction() as connection:
            import sqlalchemy as sa

            from database.schema import release_defaults

            current = connection.scalar(sa.select(release_defaults.c.release_id))
        if current is None:
            catalog.publish(
                PublishRequest(
                    release_id=protocol.dataset_release_id,
                    manifest_sha256=protocol.manifest_sha256,
                    expected_default=None,
                    retain_until=datetime.now(UTC) + timedelta(days=30),
                )
            )
        elif current != protocol.dataset_release_id:
            raise ValueError("evaluation_schema_has_another_release")
        executor = CellExecutor(
            catalog,
            Path(".local/e16") / identity["experiment_id"][:12] / args.mode[0],
            protocol,
            body["prefix"],
            model_factory=factory,
            label_root=args.label_root,
        )
        count = 0
        for cell in selected:
            arm = next(a for a in protocol.arms if a.arm_id == cell["arm_id"])
            if arm.family == "memory_sequential":
                prior = [
                    c
                    for c in body["cells"]
                    if c["arm_id"] == cell["arm_id"]
                    and c["repeat"] == cell["repeat"]
                    and arm.cases.index(c["case_id"]) < arm.cases.index(cell["case_id"])
                ]
                if prior and (arm.arm_id, cell["repeat"]) not in executor.sequences:
                    # Do not reconstruct a different learned history or resend earlier model calls.
                    break
            if not records.claim(cell["cell_id"]):
                continue
            observation = executor.run(cell)
            records.finish(cell, observation)
            print(json.dumps({"cell": cell["cell_id"], "state": observation["state"]}), flush=True)
            count += 1
            if args.maximum_cells and count >= args.maximum_cells:
                break


def review(args, records, protocol, root):
    from evaluation.review import apply_review

    row = records.snapshot()["rows"][args.cell]
    if not row["record"]:
        raise ValueError("cannot_review_unobserved_cell")
    observation = records.store.read_json(row["record"]["sha256"])
    if args.command == "review-template":
        if args.file.exists():
            raise ValueError("review_template_already_exists")
        template = {
            "schema_version": "evaluation-review/0.1",
            "blind_id": observation["blind_id"],
            "observation_sha256": content_hash(observation),
            "reviewer_kind": None,
            "reviewer_id": "",
            "complete": False,
            "task_success": False,
            "disposition_correct": False,
            "dispute_status": "none",
            "citations": [],
            "relations": [],
            "semantic": None,
            "notes": "UNFILLED. Actual independent judgments are required; never mark complete automatically.",
        }
        atomic_json(args.file, template)
        print(json.dumps({"template": args.file.as_posix(), "status": "unfilled"}))
        return
    result = apply_review(
        observation,
        json.loads(args.file.read_bytes()),
        next(c for c in protocol.cases if c.case_id == row["case_id"]),
        args.label_root,
    )
    with exclusive(root / "reviews.json"):
        path = root / "reviews.json"
        index = json.loads(path.read_bytes()) if path.exists() else {}
        index[args.cell] = records.store.put_json(
            {
                "observation_sha256": content_hash(observation),
                "review": json.loads(args.file.read_bytes()),
                "result": result,
            }
        )
        atomic_json(path, index)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze")
    freeze.add_argument(
        "--protocol", type=Path, default=Path("evals/comprehensive-v0.1/protocol.json")
    )
    freeze.add_argument("--prefix", type=Path, default=Path("evals/comprehensive-v0.1/prefix.json"))
    freeze.add_argument("--root", type=Path, default=Path(".local/P1-16/experiments"))
    for name in ["run", "analyze", "review-template", "import-review"]:
        command = sub.add_parser(name)
        command.add_argument("--experiment", type=Path, required=True)
        command.add_argument("--mode", choices=["scripted", "live"], default="scripted")
        command.add_argument("--label-root", type=Path, default=Path("evals/comprehensive-v0.1"))
        if name == "run":
            command.add_argument("--arms", nargs="*")
            command.add_argument("--maximum-cells", type=int)
            command.add_argument("--approved-experiment")
        if name in {"review-template", "import-review"}:
            command.add_argument("--cell", required=True)
            command.add_argument("--file", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        identity = seal(
            json.loads(args.protocol.read_bytes()), args.root, json.loads(args.prefix.read_bytes())
        )
        print(json.dumps(identity))
        return 0
    identity = json.loads(args.experiment.read_bytes())
    body, protocol = load(identity, require_current=args.command == "run")
    root = Path(identity["artifact_root"]) / args.mode
    records = RunRecords(root, identity, body, mode=args.mode)
    if args.command == "run":
        run(args, parser, identity, body, protocol, root, records)
    elif args.command in {"review-template", "import-review"}:
        review(args, records, protocol, root)
        if args.command == "review-template":
            return 0
    result = analyze(identity, body, protocol, root, args.mode)
    print(
        json.dumps(
            {
                "experiment_id": identity["experiment_id"],
                "unresolved_cells": result["summary"]["unresolved_cells"],
                "quality_claim_allowed": result["summary"]["quality_cost_claim_allowed"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
