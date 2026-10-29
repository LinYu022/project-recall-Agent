"""Human reference-label audit before new model generation or test freeze."""

import argparse
from pathlib import Path

from ingestion.contracts import Snapshot
from ingestion.snapshots import SnapshotStore
from reviewing.corpus import read, write_new


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = read(args.candidate)
    source_root = Path(".local/q16/heldout-candidates-v1/raw")
    store = SnapshotStore(source_root)
    by_code = {s["source"]["selection"]["campaign_number"]: s for s in value["sources"]}
    lines = [
        "# 新确认试验的来源与参考义务审查（尚无模型输出）",
        "",
        "请只评价标签、任务、范围/时间与事件隔离是否符合原始公开来源；本包没有新答案或预填分数。",
        "新来源候选来自五组2025车型目录的固定筛选，未根据模型输出选题；与旧召回编号无重叠不等于事件绝对独立。",
        "每组的FACTS、COUNTER、PAIR属于回答任务；CONDITIONS、YEAR、TIME、INSUFFICIENT分别考核澄清、有限零匹配、历史范围冲突和投诉证据不足。",
        "题目义务不是要求照抄全部其他品牌/车型列表，而是准确保留该查询范围所需的certain、配置/触发条件、机制、风险和补救；不能把来源总体描述缩成无条件个车适用。",
        "完整候选JSON保留每个原文字段和字节/引用定位，本文是阅读视图。",
        "外部来源不得代替冻结来源；电话联系方式的可选省略与必要行车应急条件的遗漏应区分。",
        "",
    ]
    for group, codes in value["groups"].items():
        lines += ["## " + group, ""]
        for code in codes:
            snap = Snapshot.model_validate(by_code[code])
            import json

            rows = json.loads(store.raw_bytes(snap.raw))["results"]
            fact = next(
                c
                for c in value["cases"]
                if c["event_cluster_id"] == group and c["case_id"].endswith("FACTS")
            )
            # Pair source vehicle facts are located in the original data, not inferred from prose.
            scope = fact["scope"]
            matching = [
                r
                for r in rows
                if r["Make"] == scope["make"]
                and r["Model"] == scope["model"]
                and int(r["ModelYear"]) == scope["model_year"]
            ]
            lines += [
                "### " + code,
                "",
                "查询车辆："
                + str(scope["model_year"])
                + " "
                + scope["make"]
                + " "
                + scope["model"],
                "",
                "结构化匹配原始行数：" + str(len(matching)),
                "",
            ]
            for row in matching[:1]:
                for field in ("Component", "Summary", "Consequence", "Remedy", "Notes"):
                    lines += ["**" + field + "**", "", row.get(field) or "（原字段为空）", ""]
        lines += ["### 该组题目与预定义务", ""]
        for case in [c for c in value["cases"] if c["event_cluster_id"] == group]:
            ref = value["references"][case["case_id"]]
            lines += [
                "- `" + case["case_id"] + "`：" + case["request"]["question"],
                "  - 年款："
                + str(case["scope"]["model_year"])
                + "；允许处置："
                + ", ".join(ref["acceptable_dispositions"])
                + "；义务："
                + ", ".join(o["reference_claim_id"] for o in ref["required_claims"]),
            ]
        lines += [
            "",
            "请确认：是否保留关键适用/触发/风险/补救条件？PAIR的关系应如何限制？负向/澄清题标签是否正确？与既有事件或组间有无应合并的事件关系？",
            "",
        ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    write_new(
        args.output.with_suffix(".json"),
        {
            "candidate": args.candidate.as_posix(),
            "reference_review": None,
            "model_generation_requests": 0,
        },
    )
    print(
        {
            "reading": args.output.as_posix(),
            "characters": len("\n".join(lines)),
            "new_model_calls": 0,
        }
    )


if __name__ == "__main__":
    main()
