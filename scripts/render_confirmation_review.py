"""Render one actual frozen answer without a method label or prefilled grades."""

import argparse
import json
import re
from pathlib import Path

from ingestion.records import content_hash
from reviewing.corpus import read, write_new


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet = read(args.packet)
    lines = [
        "# 冻结确认试验：" + packet["blind_id"],
        "",
        "题目：" + packet["question"],
        "",
        "请按实际阅读标注每个导航片段并给出必要合并/拆分；片段不是预判原子。请核查实际引用对范围/条件/各命题的支持，及题目全部义务。不要将运行完成或模型自称完成当作语义通过。",
        "",
        "处置：" + str((packet.get("proposal") or {}).get("disposition")),
        "",
        "## 实际呈现文本（如已记录）",
        "",
        packet.get("actual_answer") or "（此版审查包未单独记录呈现文本；原结论如下。）",
        "",
        "## 实际答案",
        "",
    ]
    claims = []
    for c in (packet.get("proposal") or {}).get("claims", []):
        text = c["statement"]
        cuts = [0] + [
            m.end() for m in re.finditer(r"[；;，、]|(?<!\d),(?!\d)", text) if m.end() < len(text)
        ]
        cuts = sorted(set(cuts))
        atoms = []
        lines += [
            "**" + c["claim_id"] + "**：" + text,
            "",
            "实际支持：" + ", ".join(c["support_ids"]) + "；反证：" + ", ".join(c["counter_ids"]),
            "",
        ]
        for i, (start, end) in enumerate(zip(cuts, cuts[1:] + [len(text)], strict=True), 1):
            key = c["claim_id"] + f"-A{i:02}"
            quote = text[start:end]
            lines.append(f"- `{key}`：{quote}")
            atoms.append(
                {"atom_id": key, "start": start, "end": end, "quote": quote, "verdict": None}
            )
        claims.append({"claim": c, "candidate_units": atoms})
        lines.append("")
    lines += ["## 评分义务（只供审查，未进入模型生成输入）", ""]
    for o in packet["reference_obligations"]:
        lines += ["- `" + o["reference_claim_id"] + "`：" + o["statement"]]
    for o in packet["stage_obligations"]:
        lines += ["- `" + o["obligation_id"] + "`：" + o["statement"]]
    lines += ["", "## 实际证据与模型输入可见性", ""]
    for e in packet["evidence"]:
        lines += [
            "### " + e["alias"] + " / " + ", ".join(e["source_ids"]),
            "",
            "通道：" + e["scope_channel"] + "；必需上下文：" + str(e.get("required_context", [])),
            "",
            "```text",
            e["payload"].get("text") or json.dumps(e["payload"], ensure_ascii=False, indent=2),
            "```",
            "",
        ]
        if e.get("input_visibility"):
            lines += [
                "输入核查：`" + e["input_visibility"]["status"] + "`；仅作归因，不改变原答案评分。",
                "",
            ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    write_new(
        args.output.with_suffix(".json"),
        {
            "schema_version": "confirmation-human-atom-candidates/0.1",
            "blind_id": packet["blind_id"],
            "packet_sha256": content_hash(packet),
            "packet_path": args.packet.as_posix(),
            "claims": claims,
            "human_judgment": None,
        },
    )
    print(
        {
            "blind_id": packet["blind_id"],
            "claims": len(claims),
            "candidates": sum(len(c["candidate_units"]) for c in claims),
            "reading": args.output.as_posix(),
        }
    )


if __name__ == "__main__":
    main()
