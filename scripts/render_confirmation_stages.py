"""Render exact no-claim stage groups for actual human review, without scores."""

import argparse
import json
from pathlib import Path

from ingestion.records import content_hash
from reviewing.corpus import read, write_new


def render(snapshot_path, output, *, campaign=None):
    snapshot = read(snapshot_path)
    groups: list[dict] = []
    lines = [
        "# 确认试验：实际停止与有限来源输出",
        "",
        "每组只有题目、条件、证据与实际呈现相同才合并导航。请逐组判断处置、阶段义务与全部条件；不预填分数。",
        "WAITING_INPUT 的 proposal/answer 为空表示尚无答案提案；请评价已发生的澄清是否充分，不视为完整调查。",
        "状态 completed 不代表题目已完整回答。下方文字直接取自运行报告，没有为审查补写解释。",
        "",
    ]
    for group in snapshot["stage_groups"]:
        packet = group["representative_packet"]
        if packet["execution_failed"] or not packet["stage_obligations"]:
            continue
        if campaign and packet["scope"].get("campaign_number") != campaign:
            continue
        key = f"S{len(groups) + 1:02}"
        groups.append({"review_id": key, **group})
        lines += [
            "## " + key,
            "",
            "原题：" + packet["question"],
            "",
            "范围：`" + json.dumps(packet["scope"], ensure_ascii=False) + "`",
            "",
            "状态：`"
            + str(packet["task_state"])
            + "`；处置：`"
            + str((packet.get("proposal") or {}).get("disposition"))
            + "`",
            "",
            "实际向用户呈现的答案：",
            "",
            packet["actual_answer"] if packet["actual_answer"] is not None else "（尚无答案）",
            "",
        ]
        if packet.get("guard"):
            lines += [
                "实际澄清（原文）：",
                "",
                "```json",
                json.dumps(packet["guard"], ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        lines += ["冻结阶段义务（仅供评价）：", ""]
        for item in packet["stage_obligations"]:
            lines.append("- `" + item["obligation_id"] + "`：" + item["statement"])
        lines += ["", "实际证据：", ""]
        if not packet["evidence"]:
            lines += ["（此阶段尚未查询证据。）", ""]
        for evidence in packet["evidence"]:
            lines += [
                "### " + key + " / " + evidence["alias"],
                "",
                "```json",
                json.dumps(evidence["payload"], ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        lines += ["本组覆盖的实际运行项（仅本组，不推广至别组）：", ""]
        for cell, digest in zip(group["cell_ids"], group["observation_sha256s"], strict=True):
            lines.append("- `" + cell + "`；observation `" + digest + "`")
        lines += ["", "分组指纹：`" + group["group_id"] + "`", ""]
    result = {
        "schema_version": "confirmation-stage-reading/0.1",
        "experiment_id": snapshot["experiment_id"],
        "snapshot_path": snapshot_path.as_posix(),
        "snapshot_sha256": content_hash(snapshot),
        "groups": groups,
        "human_judgment": None,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines))
    write_new(output.with_suffix(".json"), result)
    return {"groups": len(groups), "cells": sum(len(g["cell_ids"]) for g in groups)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--campaign")
    args = parser.parse_args()
    print(render(args.snapshot, args.output, campaign=args.campaign))


if __name__ == "__main__":
    main()
