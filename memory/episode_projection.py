"""Deterministic extractive history: retain attempts/counterevidence; exclude final claim text."""

from investigation.flow import redact


def report_events(report):
    result = []
    for call in report.get("tool_calls", []):
        result.append(
            {
                "kind": "tool",
                "tool": call["tool"],
                "status": call["status"],
                "attempt": call.get("attempt"),
                "error_code": call.get("error_code", call.get("error_type")),
                "query": redact(call.get("input", {}).get("request", {}).get("query", ""))[:300],
            }
        )
    for decision in report.get("planner_revisions", []):
        for hypothesis in decision.get("hypotheses", []):
            result.append(
                {
                    "kind": "hypothesis",
                    "hypothesis_id": hypothesis["hypothesis_id"],
                    "statement": redact(hypothesis["statement"]),
                    "status": hypothesis["status"],
                    "evidence_ids": [
                        report["aliases"][a]
                        for a in hypothesis.get("support_ids", [])
                        if a in report["aliases"]
                    ],
                    "counterevidence_ids": [
                        report["aliases"][a]
                        for a in hypothesis.get("counter_ids", [])
                        if a in report["aliases"]
                    ],
                    "is_semantic_verdict": False,
                }
            )
    counters = [
        report.get("aliases", {})[a]
        for a in report.get("recorded_counterevidence_aliases", [])
        if a in report.get("aliases", {})
    ]
    if counters:
        result.append(
            {
                "kind": "counterevidence",
                "evidence_ids": list(dict.fromkeys(counters)),
                "omission_is_not_absence": True,
            }
        )
    for branch in report.get("branch_results", []):
        result.append(
            {
                "kind": "branch",
                "role": branch["delegation_id"],
                "status": branch["status"],
                "error_code": branch.get("error_code"),
                "unresolved_fields": branch.get("unresolved_fields", []),
            }
        )
    for conflict in report.get("conflicts", []):
        result.append(
            {
                "kind": "conflict",
                "issue": conflict["issue"],
                "subject": conflict["subject"],
                "status": conflict["status"],
                "is_semantic_verdict": False,
            }
        )
    result.append(
        {
            "kind": "outcome",
            "execution_status": report["execution_status"],
            "disposition": (report.get("proposal") or {}).get("disposition"),
            "error_code": report.get("error_code"),
            "termination": report.get("termination"),
            "quality_status": report.get("quality_status"),
            "unresolved_fields": (report.get("proposal") or {}).get("unresolved_fields", []),
            "is_semantic_verdict": False,
        }
    )
    return result


def summary(body):
    events = body["events"]
    return {
        "task_id": body["task_id"],
        "task_state": body["task_state"],
        "scope": body["scope"],
        "release_id": body["release_id"],
        "time": body["time"],
        "allowed_source_ids": body["allowed_source_ids"],
        "question": body["question"],
        "query": body["query"],
        "attempted_tools": [e["tool"] for e in events if e["kind"] == "tool"],
        "outcome": next(
            (e for e in reversed(events) if e["kind"] == "outcome"),
            {"task_state": body["task_state"]},
        ),
        "failure_labels": list(
            dict.fromkeys(e["error_code"] for e in events if e.get("error_code"))
        ),
        "counterevidence_count": len(
            {i for e in events for i in e.get("counterevidence_ids", [])}
            | {i for e in events if e["kind"] == "counterevidence" for i in e["evidence_ids"]}
        ),
        "negative_hypotheses": [
            {"statement": e["statement"], "status": e["status"], "is_semantic_verdict": False}
            for e in events
            if e["kind"] == "hypothesis" and e["status"] == "contradicted"
        ],
        "event_count": len(events),
        "is_business_evidence": False,
        "historical_conclusions_are_not_current_facts": True,
    }
