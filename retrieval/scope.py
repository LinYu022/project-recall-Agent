"""Three-valued exact scope checks, preserving vehicle-position conjunctions."""

from datetime import date

from entities.normalize import lexical
from retrieval.contracts import SearchScope


def classify_scope(metadata: dict, scope: SearchScope) -> tuple[str, list[str]]:
    global_unknown: list[str] = []
    global_mismatch: list[str] = []
    if scope.campaign_number:
        actual = metadata.get("campaign_number")
        if actual is None:
            global_unknown.append("campaign_unknown")
        elif actual != scope.campaign_number:
            global_mismatch.append("campaign_mismatch")
    if scope.component:
        components = metadata.get("components", [])
        if lexical(scope.component) not in components:
            if not components or None in components:
                global_unknown.append("component_unknown")
            else:
                global_mismatch.append("component_mismatch")
    vehicle_constraints = any(
        v is not None for v in [scope.make, scope.model, scope.model_year, scope.production_date]
    ) or bool(scope.configurations)
    if global_mismatch:
        return "excluded", global_mismatch
    if not vehicle_constraints:
        return ("pending", global_unknown) if global_unknown else ("matched", [])
    alternatives: list[tuple[list[str], list[str]]] = []
    for vehicle in metadata.get("vehicles") or [{}]:
        unknown, mismatch = list(global_unknown), []
        for field in ("make", "model", "model_year"):
            expected = getattr(scope, field)
            if expected is None:
                continue
            actual = vehicle.get(field)
            expected = lexical(expected) if isinstance(expected, str) else expected
            if actual is None:
                unknown.append(field + "_unknown")
            elif actual != expected:
                mismatch.append(field + "_mismatch")
        if scope.production_date is not None:
            interval = vehicle.get("production_range")
            if interval is None:
                unknown.append("production_date_unknown")
            elif (
                not date.fromisoformat(interval["start"])
                <= scope.production_date
                <= date.fromisoformat(interval["end"])
            ):
                mismatch.append("production_date_mismatch")
        for key, value in scope.configurations.items():
            actual = vehicle.get("configurations", {}).get(key)
            if actual is None:
                unknown.append("configuration_unknown:" + key)
            elif actual != lexical(value):
                mismatch.append("configuration_mismatch:" + key)
        if not mismatch and not unknown:
            return "matched", []
        alternatives.append((mismatch, unknown))
    possible = [unknown for mismatch, unknown in alternatives if not mismatch]
    if possible:
        fewest: list[str] = min(possible, key=lambda reasons: len(reasons))
        return "pending", sorted(set(fewest))
    return "excluded", sorted({reason for mismatch, _ in alternatives for reason in mismatch})
