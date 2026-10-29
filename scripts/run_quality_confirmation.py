"""Execute a newly authorized frozen trial; failed reviewer models are not run here."""

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evaluation.contracts import request_ceiling
from evaluation.executor import CellExecutor
from evaluation.freeze import load
from evaluation.records import MeteredModel, ProviderLedger, RunRecords, exclusive
from evaluation.session import session
from models.chat_api import ChatJSONModel, local_configuration
from portal.postgres import portable_postgres
from releases.bundle import BundleStore
from releases.catalog import Catalog
from releases.contracts import PublishRequest
from reviewing.corpus import read, write_new


def validate_approval(approval, identity, protocol):
    if (
        approval.get("experiment_id") != identity["experiment_id"]
        or approval.get("maximum_requests") != request_ceiling(protocol)
        or approval.get("status") != "explicit_user_approved"
        or approval.get("source_kind") != "actual_human_reply"
        or not approval.get("human_reply")
        or approval.get("model") != protocol.model_id
        or approval.get("endpoint") != protocol.endpoint
    ):
        raise ValueError("new_confirmation_has_no_matching_authorization")


def main():
    from scripts.freeze_quality_confirmation import wrapper_identity

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--bin-dir", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    identity = read(args.experiment)
    body, protocol = load(identity)
    if protocol.quality_targets["new_wrapper_implementation"] != wrapper_identity():
        raise ValueError("confirmation_wrapper_changed_after_freeze")
    validate_approval(read(args.approval), identity, protocol)
    root = args.root
    root.mkdir(parents=True, exist_ok=True)
    # Persist the private DB for a resumable new run, without using old experiment schemas.
    # portable_postgres is deliberately one lifecycle: subsequent continuation must use
    # saved observations rather than silently recreate completed cells.
    if (root / "runs.json").exists():
        raise ValueError("confirmation_already_started_inspect_before_resume")
    model = ChatJSONModel(local_configuration() | {"RECALL_MODEL_ID": protocol.model_id})
    limits = {
        c["cell_id"]: 0
        if next(t for t in protocol.cases if t.case_id == c["case_id"]).category
        in {"missing_conditions", "time_boundary", "scope_exclusion", "insufficient"}
        else 1
        if c["method"] == "fixed"
        else protocol.profile.max_model_calls
        for c in body["cells"]
    }
    ledger = ProviderLedger(
        root / "provider-requests.json",
        experiment_id=identity["experiment_id"],
        cells=limits,
        identity=model.identity,
    )
    records = RunRecords(root, identity, body, mode="live")
    provider = records.store.__class__(root / "provider")
    location = read(args.labels / "release-location.json")
    with exclusive(root / "writer.json"):
        try:
            with portable_postgres(args.bin_dir.resolve(), root / "database") as url:
                with session(url, identity["experiment_id"], mode="live") as (engine, _):
                    catalog = Catalog(engine, BundleStore(Path(location["root"])))
                    catalog.publish(
                        PublishRequest(
                            release_id=protocol.dataset_release_id,
                            manifest_sha256=protocol.manifest_sha256,
                            expected_default=None,
                            retain_until=datetime.now(UTC) + timedelta(days=30),
                        )
                    )
                    executor = CellExecutor(
                        catalog,
                        root / "tasks",
                        protocol,
                        body["prefix"],
                        model_factory=lambda cell: MeteredModel(
                            model, ledger, cell["cell_id"], provider
                        ),
                        label_root=args.labels,
                    )
                    by_id = {cell["cell_id"]: cell for cell in body["cells"]}
                    order = protocol.quality_targets["execution_order"]
                    if len(order) != len(by_id) or set(order) != set(by_id):
                        raise ValueError("confirmation_execution_order_changed")
                    for identifier in order:
                        cell = by_id[identifier]
                        if not records.claim(cell["cell_id"]):
                            continue
                        observation = executor.run(cell)
                        saved = records.finish(cell, observation)
                        print(
                            {
                                "cell": cell["cell_id"],
                                "state": saved["state"],
                                "error": saved.get("error_code"),
                            },
                            flush=True,
                        )
        finally:
            # This file describes shutdown evidence, not a successful experiment.
            write_new(
                root / "lifecycle.json",
                {
                    "experiment_id": identity["experiment_id"],
                    "database_pid_exists": (root / "database/data/postmaster.pid").exists(),
                    "provider_requests": len(read(root / "provider-requests.json")["entries"]),
                    "formal_quality_acceptance": "pending_actual_human_adjudication",
                    "original_270_cells_changed": False,
                },
            )


if __name__ == "__main__":
    main()
