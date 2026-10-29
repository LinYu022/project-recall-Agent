"""Faithful transcription of already received human judgments; no new grading."""

from ingestion.records import content_hash
from reviewing.corpus import template, verified
from reviewing.scoring import context_links


def existing_human_review(pointer):
    _, identity, _, rows, registry = verified(pointer)
    names = [
        "docs/experiments/P1-16-human-review-C001.json",
        "docs/experiments/P1-16-human-review-C002-C004.json",
        "docs/experiments/P1-16-human-review-complete.json",
    ]
    first, middle, complete = [registry[name] for name in names]
    row = next(
        r
        for r in rows
        if r["observation"] is not None
        and content_hash(r["observation"]) == complete["observation_sha256"]
    )
    value = template(row, identity["experiment_id"])
    claims = {c["claim_id"]: c for c in row["report"]["proposal"]["claims"]}
    required = {
        (link["claim_id"], alias)
        for link in context_links(row["report"])
        for alias in link["context_aliases"]
    }
    citations = [
        {
            "claim_id": "C001",
            "evidence_alias": "E001",
            "role": "direct",
            "direct_verdict": first["citation_pair"]["verdict"],
            "evaluated_aspect": "完整C001的有限冻结来源查询计数和不外推全球缺失的边界",
            "reason": first["human_reason_verbatim"],
        }
    ]
    aspects = {
        "C002": "完整复合结论的覆盖：缺陷文本与结构化匹配元结论，后者引用不足",
        "C003": "巡航无法解除时的速度维持或加速及碰撞风险；不包含未匹配元结论",
        "C004": "经销商免费软件检查/刷新、召回日期和更新前限制；不包含匹配元结论及未提及的应急步骤",
    }
    reasons = {"C001": first["human_reason_verbatim"]}
    scopes = {
        "C001": "correct",
        "C002": "incorrect",
        "C003": "correct",
        "C004": "mostly_correct",
        "C005": "mostly_correct",
        "C006": "partial",
    }
    for claim in middle["reviewed_claims"]:
        identifier = claim["claim_id"]
        reasons[identifier] = claim["human_reason"]
        for citation in claim["citations"]:
            role = citation["role"]
            item = {
                "claim_id": identifier,
                "evidence_alias": citation["alias"],
                "role": "both"
                if role == "direct" and (identifier, citation["alias"]) in required
                else role,
                "reason": claim["human_reason"],
            }
            if role == "direct":
                item |= {
                    "direct_verdict": citation["support"],
                    "evaluated_aspect": aspects[identifier],
                }
            citations.append(item)
    for identifier, aspect in [
        ("C005", "NHTSA联系渠道与该Notes文本不含永久安全承诺；不包含程序匹配状态元结论"),
        ("C006", "有限来源匹配状态及单项缺陷补救的证据边界；不包含无法核对推断的结论"),
    ]:
        finding = complete[identifier]
        reasons[identifier] = finding["coverage_gap"]
        for alias in finding["direct_evidence"]:
            citations.append(
                {
                    "claim_id": identifier,
                    "evidence_alias": alias,
                    "role": "both" if (identifier, alias) in required else "direct",
                    "direct_verdict": "supported",
                    "evaluated_aspect": aspect,
                    "reason": finding["coverage_gap"],
                }
            )
    actual_pairs = {
        (c["claim_id"], a) for c in claims.values() for a in c["support_ids"] + c["counter_ids"]
    }
    if {(c["claim_id"], c["evidence_alias"]) for c in citations} != actual_pairs:
        raise ValueError("legacy_judgments_do_not_cover_the_exact_saved_citation_pairs")
    input_sha = "c4f7793b391eac66e9bae099dbfe2f583ee6227ecd745d592752fc558327a567"
    value |= {
        "reviewer_kind": "human",
        "reviewer_id": complete["reviewer_id"],
        "source_kind": "actual_human_reply",
        "sources": [
            {"name": name, "content_sha256": content_hash(registry[name])} for name in names
        ],
        "transcribed_by": "implementation_agent_from_existing_actual_human_records_no_new_judgments",
        "complete_task_reading": complete["complete_task_reading"],
        "task_success": complete["task_success"],
        "disposition_correct": complete["disposition_correct"],
        "claims": [
            {
                "claim_id": identifier,
                "verdict": verdict,
                "scope_verdict": scopes[identifier],
                "reason": reasons[identifier],
            }
            for identifier, verdict in complete["claim_verdicts"].items()
        ],
        "citations": citations,
        "assertions": [],
        "assertions_exhaustive": False,
        "obligations": [],
        "relations": [],
        "input_visibility": {
            "input_artifact_sha256": input_sha,
            "input_content_sha256": content_hash(row["inputs"][input_sha]),
            "check": "exact_input_checked",
            "note": "Implementation-agent provenance check, not a new human verdict: numbers were minimized; defect conditions and emergency paragraph were visible; E001 retained counts/scope but omitted detailed rows and make_unknown/model_unknown reasons.",
        },
        "notes": "Post hoc transcription of one completed human reading. Direct metrics concern only named aspects of compound claims. Original human verdicts unchanged; atomization and reference-obligation mapping were not requested and stay unreviewed. Both role combines the user's direct judgment with a mechanically proven context dependency. Do not call citation-aspect support whole-claim correctness or a population score.",
    }
    return value
