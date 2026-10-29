"""Read finished units from a running frozen batch; no model or database access."""

import argparse
from collections import defaultdict
from pathlib import Path

from evaluation.freeze import load
from ingestion.records import content_hash
from reviewing.corpus import public_packet, read, write_new
from runtime.artifacts import ArtifactStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    identity = read(args.experiment)
    _, protocol = load(identity)
    index = read(args.root / "runs.json")
    records = ArtifactStore(args.root)
    provider = ArtifactStore(args.root / "provider")
    ledger = read(args.root / "provider-requests.json")
    input_map: dict[str, dict[str, dict]] = defaultdict(dict)
    for entry in ledger["entries"]:
        if not entry.get("artifact"):
            continue
        saved = provider.read_json(entry["artifact"]["sha256"])
        ref = saved["input_artifact"]
        input_map[entry["cell"]][ref["sha256"]] = provider.read_json(ref["sha256"])
    by_case = {c.case_id: c for c in protocol.cases}
    packets = []
    groups = defaultdict(list)
    for cell_id, row in index["rows"].items():
        if not row.get("record"):
            continue
        observation = records.read_json(row["record"]["sha256"])
        report = None
        if observation.get("report_reference"):
            ref = observation["report_reference"]
            report = ArtifactStore(Path(ref["artifact_root"])).read_json(
                ref["report_artifact"]["sha256"]
            )
            if content_hash(report) != observation["report_content_sha256"]:
                raise ValueError("confirmation_report_changed")
        case = by_case[row["case_id"]]
        packet = public_packet(
            {
                "observation": observation,
                "report": report,
                "case": case,
                "inputs": input_map[cell_id],
            },
            identity["experiment_id"],
        )
        packet |= {
            # Empty-claim proposals can still have a user-facing answer. Keep the
            # exact text so a disposition alone cannot stand in for its explanation.
            "actual_answer": (report or {}).get("answer"),
            "task_state": observation.get("task_state"),
            "execution_status": (report or {}).get("execution_status"),
            "execution_error_code": (report or {}).get("error_code"),
            "stage_obligations": protocol.quality_targets["stage_obligations"].get(
                case.case_id, []
            ),
            "reference_obligations": read(args.labels / "references" / (case.case_id + ".json"))[
                "required_claims"
            ],
            "human_review": None,
        }
        if not packet.get("proposal") or not packet["proposal"]["claims"]:
            canonical = {
                "question": packet["question"],
                "scope": packet["scope"],
                "guard": packet.get("guard"),
                "proposal": packet.get("proposal"),
                "actual_answer": packet["actual_answer"],
                "task_state": packet["task_state"],
                "execution_status": packet["execution_status"],
                "execution_error_code": packet["execution_error_code"],
                "allowed_source_ids": packet.get("allowed_source_ids"),
                "time": packet.get("time"),
                "evidence": [
                    {
                        k: e.get(k)
                        for k in (
                            "alias",
                            "kind",
                            "payload",
                            "scope_channel",
                            "source_ids",
                            "required_context",
                        )
                    }
                    for e in packet["evidence"]
                ],
                "stage_obligations": packet["stage_obligations"],
                "execution_failed": packet["execution_failed"],
            }
            groups[content_hash(canonical)].append(
                {
                    "cell_id": cell_id,
                    "packet": packet,
                    "observation_sha256": content_hash(observation),
                }
            )
        write_new(args.output / "packets" / (packet["blind_id"] + ".json"), packet)
        packets.append(
            {
                "cell_id": cell_id,
                "blind_id": packet["blind_id"],
                "state": row["state"],
                "packet_sha256": content_hash(packet),
                "observation_sha256": content_hash(observation),
                "mandatory_human_review": cell_id
                in protocol.quality_targets["mandatory_human_cells"],
            }
        )
    grouped = []
    for group_id, items in sorted(groups.items()):
        grouped.append(
            {
                "group_id": group_id,
                "cell_ids": [i["cell_id"] for i in items],
                "observation_sha256s": [i["observation_sha256"] for i in items],
                "identical_semantic_stage_payload": True,
                "representative_packet": items[0]["packet"],
                "human_judgment": None,
            }
        )
    result = {
        "experiment_id": identity["experiment_id"],
        "snapshot_index_sha256": content_hash(index),
        "observed_units": len(packets),
        "total_frozen_units": len(index["rows"]),
        "packets": packets,
        "stage_groups": grouped,
        "new_provider_calls": 0,
        "partial_snapshot": len(packets) != len(index["rows"]),
    }
    write_new(args.output / "snapshot.json", result)
    print(
        {
            "observed": len(packets),
            "total": len(index["rows"]),
            "no_claim_stage_groups": len(grouped),
            "output": args.output.as_posix(),
        }
    )


if __name__ == "__main__":
    main()
