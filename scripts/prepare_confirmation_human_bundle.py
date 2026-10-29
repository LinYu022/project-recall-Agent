"""Make the predeclared human audit navigable without filling any human grades."""

import argparse
import subprocess
import sys
from pathlib import Path

from ingestion.records import content_hash
from reviewing.corpus import read, write_new
from scripts.render_confirmation_stages import render


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = read(args.snapshot)
    output = args.output.resolve()
    if output.exists():
        raise ValueError("human_bundle_output_must_be_new")
    output.mkdir(parents=True)
    stage_counts = render(args.snapshot, output / "stages.md")
    rows = []
    generated = []
    failures = []
    for row in snapshot["packets"]:
        path = args.snapshot.parent / "packets" / (row["blind_id"] + ".json")
        packet = read(path)
        if content_hash(packet) != row["packet_sha256"]:
            raise ValueError("review_packet_changed")
        if not row["mandatory_human_review"]:
            continue
        label = row["blind_id"]
        if (packet.get("proposal") or {}).get("claims"):
            target = output / (label + "-reading.md")
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.render_confirmation_review",
                    "--packet",
                    str(path),
                    "--output",
                    str(target),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            generated.append({**row, "reading": target.as_posix()})
        elif packet["execution_failed"]:
            failures.append({**row, "packet": packet})
        rows.append(row)
    lines = [
        "# 预先登记人工核查导航",
        "",
        "本目录保留每个实际输出及来源，不含实现代理代填的人类评分。固定方法全35项与其余固定抽样45项共80项；其他关键边界可另加。",
        "本批方法已知，隐藏方法名称只是减轻呈现干扰，不称严格方法盲审。",
        "",
        f"当前快照已包含 {len(rows)} 个登记项；生成答案 {len(generated)} 份，无答案失败项 {len(failures)} 个。",
        "",
        "请分别评价生成答案、停止/有限来源阶段与失败未作答；状态 completed 不等于核心任务完成。",
        "",
        f"[停止与有限来源输出]({(output / 'stages.md').as_posix()})，共 {stage_counts['groups']} 组 / {stage_counts['cells']} 格（含登记项以外相同阶段，逐格列明）。",
        "",
        "## 登记的生成答案",
        "",
    ]
    for row in generated:
        lines.append("- [" + row["blind_id"] + "](" + row["reading"] + ")")
    lines += [
        "",
        "## 登记的失败未作答项",
        "",
        "以下运行均未向用户形成答案；不把空答案算成错误事实原子，也不自动填为任务成功。可按实际阅读批量判断核心任务与全部条件。",
        "",
    ]
    for row in failures:
        packet = row["packet"]
        lines += [
            "### " + row["blind_id"],
            "",
            "题目：" + packet["question"],
            "",
            "范围：`" + str(packet["scope"]) + "`",
            "",
            "实际答案："
            + str(packet["actual_answer"])
            + "；处置："
            + str((packet.get("proposal") or {}).get("disposition"))
            + "。",
            "",
            "执行错误类型：`" + str(packet.get("execution_error_code")) + "`。",
            "",
            "原始包：[JSON]("
            + (args.snapshot.parent / "packets" / (row["blind_id"] + ".json")).resolve().as_posix()
            + ")",
            "",
        ]
    with (output / "index.md").open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines))
    write_new(
        output / "index.json",
        {
            "experiment_id": snapshot["experiment_id"],
            "snapshot_path": args.snapshot.resolve().as_posix(),
            "snapshot_sha256": content_hash(snapshot),
            "predeclared_human_total": 80,
            "observed_predeclared": len(rows),
            "generated": generated,
            "failures": failures,
            "stage_reading": (output / "stages.md").as_posix(),
            "human_judgment": None,
            "new_provider_calls": 0,
        },
    )
    print(
        {
            "observed_predeclared": len(rows),
            "generated": len(generated),
            "failures": len(failures),
            "index": (output / "index.md").as_posix(),
        }
    )


if __name__ == "__main__":
    main()
