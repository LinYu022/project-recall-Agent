"""Observed provider usage; unknown response fields and prices stay unknown."""

import json


def usage_for_cell(path, cell_id):
    if not path.exists():
        return {
            "requests_reserved": 0,
            "prompt_tokens": None,
            "completion_tokens": None,
            "unknown_responses": 0,
            "currency_cost": None,
            "price_status": "not_verified",
        }
    entries = [e for e in json.loads(path.read_bytes())["entries"] if e["cell"] == cell_id]
    result = {
        "requests_reserved": len(entries),
        "unknown_responses": sum(e["state"] == "reserved" for e in entries),
        "currency_cost": None,
        "price_status": "not_verified",
    }
    for name in ["prompt_tokens", "completion_tokens", "total_tokens"]:
        values = [(e.get("usage") or {}).get(name) for e in entries]
        result[name] = sum(values) if all(type(v) is int and v >= 0 for v in values) else None
    return result
