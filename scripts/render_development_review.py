"""Render saved answer/evidence only; never invent human grades or expose reference labels."""

import argparse
import json
from pathlib import Path

from reviewing.corpus import read


def compact_payload(card):
    payload = card["payload"]
    if card["kind"] == "structured_lookup":
        return {k: v for k, v in payload.items() if k != "rows"}
    if card["kind"] == "relationship" and payload.get("assessment"):
        assessment = payload["assessment"]
        reduced = {
            k: v for k, v in assessment.items() if k not in {"left_scope", "right_scope", "links"}
        }
        reduced["links"] = [
            {k: v for k, v in link.items() if k not in {"evidence", "supporting_evidence"}}
            for link in assessment["links"]
        ]
        for side in ("left_scope", "right_scope"):
            scope = assessment[side]
            reduced[side] = {
                k: v for k, v in scope.items() if k in {"vehicles", "coverage", "official_id"}
            }
            reduced[side]["facts"] = [
                {k: v for k, v in fact.items() if k != "evidence"} for fact in scope["facts"]
            ]
        return {k: reduced if k == "assessment" else v for k, v in payload.items()}
    return payload


def render(packet, *, compact=False):
    lines = [
        "# 开发答案审查包 " + packet["blind_id"],
        "",
        "这里只展示题目、实际答案与原始证据，不含参考标签或预填评分。",
        "",
        "本批是已见开发资料的小试，已读单方法方案的人可能知道方法；不声称严格方法盲法。",
        "",
        "## 题目",
        "",
        packet["question"],
        "",
        "范围：" + json.dumps(packet["scope"], ensure_ascii=False),
        "",
        "## 实际答案",
        "",
    ]
    proposal = packet.get("proposal")
    if proposal:
        lines += ["处置：" + proposal["disposition"], ""]
        for claim in proposal["claims"]:
            lines += [
                "### " + claim["claim_id"],
                "",
                claim["statement"],
                "",
                "支持引用：" + ", ".join(claim["support_ids"]),
                "反证引用：" + (", ".join(claim["counter_ids"]) or "无"),
                "",
            ]
    else:
        lines += ["当前没有可审的答案提案；不能补造一个。", ""]
        for guard in packet.get("guard", []):
            lines += [guard.get("prompt", ""), "字段：" + ", ".join(guard.get("fields", [])), ""]
    lines += [
        "## 证据与输入可见性",
        "",
        "required_context为程序要求的上下文角色，不自动代表它直接支持每个论点。",
        "精确匹配未找到只代表可见性未解决，不能断言未输入。",
        "",
    ]
    if compact:
        lines += [
            "这是阅读版：结构化查询不重复展示逐行rows，关系卡保留全部关系类型/条件/未知字段及来源事实，省略重复定位元数据。",
            "[完整原始证据JSON]("
            + packet["blind_id"]
            + ".json)与原阅读包保留；本版未新增或修改语义判断。",
            "",
        ]
    for card in packet["evidence"]:
        lines += [
            "### " + card["alias"] + " / " + card["kind"] + " / " + card["scope_channel"],
            "",
            "来源：" + ", ".join(card["source_ids"]),
            "必需上下文：" + (", ".join(card["required_context"]) or "无"),
            "",
        ]
        payload = card["payload"]
        if card["kind"] == "text":
            lines += [payload["text"], ""]
        else:
            shown = compact_payload(card) if compact else payload
            lines += ["```json", json.dumps(shown, ensure_ascii=False, indent=2), "```", ""]
        visibility = card.get("input_visibility")
        if visibility:
            lines += ["生成输入核查：" + visibility["status"], ""]
            if visibility.get("text_differs_from_raw_source"):
                lines += ["实际匹配的最小化文本：", "", visibility["minimized_text"], ""]
            if visibility.get("raw_fields_not_in_matched_payload"):
                lines += [
                    "原始字段未进入匹配payload："
                    + ", ".join(visibility["raw_fields_not_in_matched_payload"]),
                    "",
                ]
    lines += [
        "## 实际审查（未填）",
        "",
        "逐条判断支持/部分支持/夸大或不支持/无法评价，并说明范围、条件、反证、遗漏与引用角色。未读写未审。",
        "最后分别判断处置是否合适、核心任务是否完成、全部必需条件是否满足。",
        "未核查生成输入时，不把遗漏直接归因于模型。有效阅读用时可实际计时或自报，未测量留空。",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    data = render(read(args.packet), compact=args.compact)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        if args.output.read_text(encoding="utf-8") != data:
            raise ValueError("review_packet_exists_with_different_content")
    else:
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(data)
    print(json.dumps({"output": args.output.as_posix(), "human_judgments_created": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
