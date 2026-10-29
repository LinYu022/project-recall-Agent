"""Historical development review packets; no new execution, scoring or model call."""

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from ingestion.records import content_hash
from reviewing.corpus import public_packet, read, write_new
from reviewing.policy import implementation
from runtime.artifacts import ArtifactStore


def main():
    prior = read("docs/experiments/P1-09-results.json")
    ledger = read(".local/P1-09/request-allowance.json")
    assert content_hash(ledger) == prior["verification"]["record_content_sha256"]["request_ledger"]
    final = next(r for r in prior["rounds"] if r["round"] == "candidate_precondition_repair")
    references = {
        identifier: read(Path("evals/investigation-v0.2") / (identifier + ".json"))
        for identifier in final["profile"]["case_ids"]
    }
    dependencies = implementation()
    producer = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    calibration_id = content_hash(
        {
            "prior_summary": content_hash(prior),
            "round": final["round"],
            "reports": [r["report_content_sha256"] for r in final["runs"]],
            "producer": producer,
            "analysis_dependencies": dependencies,
        }
    )
    root = Path(".local/P1-16/development-review-calibration") / calibration_id
    items = []
    for run in final["runs"]:
        digest = run["report_artifact"]["sha256"]
        found = subprocess.run(
            [
                "rg",
                "--files",
                "--hidden",
                "--no-ignore",
                ".local/P1-09/runs/private-runs",
                "-g",
                digest + ".json",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert len(found) == 1
        path = Path(found[0])
        report = ArtifactStore(path.parent.parent).read_json(digest)
        assert content_hash(report) == run["report_content_sha256"]
        reference = references[run["case_id"]]
        assert content_hash(reference) == run["score"]["reference_sha256"]
        assert reference["split"] == "development"
        blind = (
            "DEVBLIND-"
            + content_hash({"report": content_hash(report), "reference": content_hash(reference)})[
                :12
            ]
        )
        packet = public_packet(
            {
                "observation": {"blind_id": blind},
                "report": report,
                "case": SimpleNamespace(),
                "inputs": {},
            },
            "historical-development-calibration/0.1",
        )
        packet |= {
            "classification": "historical_development_output_not_new_execution_or_current_frozen_system_baseline",
            "input_visibility_method": "not_verified_for_this_historical_packet_do_not_attribute_omissions_to_model_without_input_check",
        }
        write_new(root / "packets" / (blind + ".json"), packet)
        lines = [
            "# 历史开发样例盲审 " + blind,
            "",
            "本包隐藏方法/模型/用量，只作独立审查与评分校准。它不是当前系统的新运行，也不是未见测试。",
            "",
            "原始来源文本可读；本包尚未核查每段文字在当时模型输入中的可见性，因此遗漏不能直接归因于模型。",
            "",
            "## 题目",
            "",
            packet["question"],
            "",
            "范围：" + json.dumps(packet["scope"], ensure_ascii=False),
            "",
            "## 待审答案",
            "",
            "处置：" + str((packet.get("proposal") or {}).get("disposition")),
            "",
        ]
        for claim in (packet.get("proposal") or {}).get("claims", []):
            lines += [
                "### " + claim["claim_id"],
                "",
                claim["statement"],
                "",
                "支持引用：" + ", ".join(claim["support_ids"]),
                "反证引用：" + (", ".join(claim["counter_ids"]) or "无"),
                "",
            ]
        lines += [
            "## 原始证据",
            "",
            "必需上下文关系仅由程序核对，不等于内容直接支持。完整字段与定位保存在同名JSON中。",
            "",
        ]
        for card in packet["evidence"]:
            payload = card["payload"]
            lines += [
                "### " + card["alias"] + " / " + card["kind"] + " / " + card["scope_channel"],
                "",
                "来源：" + ", ".join(card["source_ids"]),
                "必需上下文：" + (", ".join(card["required_context"]) or "无"),
                "",
            ]
            if card["kind"] == "text":
                lines += [payload.get("text", json.dumps(payload, ensure_ascii=False)), ""]
            else:
                compact = {k: v for k, v in payload.items() if k not in {"rows", "assessment"}}
                lines += ["```json", json.dumps(compact, ensure_ascii=False, indent=2), "```", ""]
                if payload.get("assessment"):
                    lines += ["关系原文与条件见同名JSON的assessment字段。", ""]
        lines += [
            "## 实际审查记录（未填）",
            "",
            "逐条给出支持/部分支持/夸大或不支持/无法评价，说明适用条件、遗漏和引用角色。未读写未审。",
            "",
            "最后判断处置是否合适、整题是否满足全部要求；正确停止与完整回答分别判断。参考标签未向审查者展示。",
            "",
        ]
        path = root / "packets" / (blind + ".md")
        content = "\n".join(lines)
        if path.exists():
            assert path.read_text(encoding="utf-8") == content
        else:
            path.write_text(content, encoding="utf-8")
        items.append(
            {
                "blind_id": blind,
                "case_id": run["case_id"],
                "method_private": run["method"],
                "historical_round": final["round"],
                "original_report_artifact": run["report_artifact"],
                "report_content_sha256": content_hash(report),
                "reference_sha256": content_hash(reference),
                "packet_content_sha256": content_hash(packet),
            }
        )
    manifest = {
        "schema_version": "historical-development-calibration/0.1",
        "source_round": final["round"],
        "source_record_content_sha256": content_hash(prior),
        "producer_sha256": producer,
        "analysis_dependencies": dependencies,
        "calibration_id": calibration_id,
        "selection": "all four reports from the final previously inspected development round; no outcome-based exclusion",
        "items": items,
        "actual_new_human_reviews": 0,
        "new_provider_requests": 0,
        "not_current_system_quality_baseline": True,
        "not_unseen_test": True,
    }
    write_new(root / "manifest-private.json", manifest)
    print(
        json.dumps(
            {
                "packets_prepared": len(items),
                "new_provider_requests": 0,
                "first_packet": (root / "packets" / (items[0]["blind_id"] + ".md")).as_posix(),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
