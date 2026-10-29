"""Candidate atom boundaries for actual human calibration; no prefilled semantic scores."""

import argparse
import re
from pathlib import Path

from ingestion.records import content_hash
from reviewing.corpus import read, write_new


def spans(text):
    # Navigation candidates, not a claim that punctuation guarantees logical atomicity.
    starts = [0]
    starts += [m.end() for m in re.finditer(r"[；;，,]|、", text) if m.end() < len(text)]
    starts = sorted(set(starts))
    return [(a, b, text[a:b]) for a, b in zip(starts, starts[1:] + [len(text)], strict=True)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    summary = read("docs/verification/2026-10-29-P1-16-development-review-complete.json")
    plan = read("evals/development-baseline-v0.1/prepared-plan.json")
    selected = {"DEV2-PRIUS-RECALL", "DEV2-PRIUS-JOINT", "DEV2-FORD-PAIR"}
    bundle = {
        "schema_version": "human-component-candidate-packet/0.1",
        "judgments": None,
        "items": [],
    }
    lines = [
        "# 当前开发基线：原子与义务人工校准包",
        "",
        "这些是原句导航切分候选，不是已确认的原子；遇到拆开后丢失条件的片段可合并，复合片段可再拆。所有分数留空。",
        "",
        "请对每个A项评价支持/部分支持/不支持/无法评价，同时确认其S引文；可按范围批量写同一评价，但请实际阅读。对范围限定、数字、日期、条件和边界分别检查。",
        "",
        "每题最后确认：候选断言是否覆盖原答案所有实质内容；有无必须再拆/合并；题目义务满足/部分/缺失；全部必需条件满足/有轻微瑕疵/不满足。引用只支持部分原子时，不必支持整个复合句。",
        "",
    ]
    for row in summary["rows"]:
        case_id = row["cell"]["case_id"]
        if case_id not in selected:
            continue
        packet = read(row["review_packet_path"])
        claims = []
        lines += [
            "## " + case_id,
            "",
            "题目：" + packet["question"],
            "",
            "### 原答案与候选断言",
            "",
        ]
        for claim in packet["proposal"]["claims"]:
            units = []
            lines += [
                "**" + claim["claim_id"] + "**：" + claim["statement"],
                "",
                "实际支持引文：" + ", ".join(claim["support_ids"]),
                "",
            ]
            for i, (start, end, text) in enumerate(spans(claim["statement"]), 1):
                key = claim["claim_id"] + f"-A{i:02}"
                units.append(
                    {"atom_id": key, "start": start, "end": end, "quote": text, "verdict": None}
                )
                lines.append(f"- `{key}`：{text}")
            lines.append("")
            claims.append({"claim": claim, "candidate_units": units})
        lines += ["### 题目必需义务（逐项核对原问题）", ""]
        obligations = plan["references"][case_id]["required_claims"]
        for o in obligations:
            lines.append(f"- `{o['reference_claim_id']}`：{o['statement']}")
        lines += [
            "",
            "若参考义务遗漏题目必需条件，请指出；不能只按标签通过。",
            "",
            "### 完整来源与引用角色",
            "",
        ]
        seen = set()
        for e in packet["evidence"]:
            if e["alias"] not in {
                a for c in packet["proposal"]["claims"] for a in c["support_ids"]
            }:
                continue
            payload = e["payload"]
            text = payload.get("text")
            if text is None:
                import json

                text = json.dumps(payload, ensure_ascii=False, indent=2)
            key = content_hash({"kind": e["kind"], "source": e["source_ids"], "payload": payload})
            lines += [
                "#### " + e["alias"] + " / " + ", ".join(e["source_ids"]),
                "",
                "通道：" + e["scope_channel"],
                "",
            ]
            if key in seen:
                lines += ["与上文同文；完整JSON仍保留本别名与定位。", ""]
            else:
                lines += ["```text", text, "```", ""]
                seen.add(key)
        item = {
            "case_id": case_id,
            "packet_sha256": content_hash(packet),
            "source_packet": row["review_packet_path"],
            "claims": claims,
            "obligations": obligations,
            "human_review": None,
        }
        bundle["items"].append(item)
    write_new(args.root / "packet.json", bundle)
    args.root.mkdir(parents=True, exist_ok=True)
    path = args.root / "reading.md"
    with path.open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    print(
        {
            "packet": path.as_posix(),
            "cases": len(bundle["items"]),
            "candidates": sum(
                len(c["candidate_units"]) for x in bundle["items"] for c in x["claims"]
            ),
        }
    )


if __name__ == "__main__":
    main()
