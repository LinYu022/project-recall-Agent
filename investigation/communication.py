"""User-facing bounded stops, shared by fixed, agent and interactive control paths."""

PERMANENT_SAFETY = "即使确认个车适用并完成召回补救，也不能据此证明车辆永久安全。"
COMPLAINT_LIMIT = (
    "允许的召回资料不含投诉全集，也未提供车辆数量及观察时长或里程等暴露量分母；"
    "不能据此给出投诉总数或计算故障发生率。"
)


def clarification_prompt(fields, *, intent):
    prompt = "请补充必要条件或明确接受固定资料的时间范围：" + "、".join(fields) + "。"
    if intent == "applicability":
        prompt += "目前不能确认您的车是否适用召回。" + PERMANENT_SAFETY
    return prompt


def bounded_stop(disposition, *, reason, intent, missing=()):
    if disposition == "needs_clarification":
        return clarification_prompt(missing, intent=intent)
    if disposition == "unsupported_time":
        return "当前固定发布不支持所请求的时间范围；请明确是否接受固定资料的时间范围。"
    if disposition == "no_matching_records":
        return (
            "当前允许的冻结资料、时间及车型范围中结构化召回查询为零；"
            "不能据此断言现实中没有其他召回。"
        )
    if disposition != "insufficient_evidence":
        raise ValueError("unsupported_bounded_stop")
    if reason in {"no_complaint_source_allowed", "no_allowed_complaint_source"}:
        return COMPLAINT_LIMIT
    if intent == "complaint_statistics":
        return (
            "本次未能取得完整、可核查的投诉全集和暴露量分母，不能给出投诉总数或故障发生率。"
            "工具失败或额度耗尽不表示投诉为零。"
        )
    return "本次允许资料不足以回答问题；缺失或未取得的证据不等于没有相关记录。"
