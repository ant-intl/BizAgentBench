"""Pure customer-scope projection shared by state readers and independent verifiers."""

from copy import deepcopy


def project(world, scope):
    """Evaluator's customer slice; access control is independently enforced by tools."""
    out = {}
    for key, value in world.items():
        if key == "_closure_state":
            out[key] = project(value, scope)
            continue
        if key == "_session_scope":
            continue
        if isinstance(value, dict) and not key.startswith("_"):
            out[key] = {
                id: deepcopy(r)
                for id, r in value.items()
                if not isinstance(r, dict) or r.get("scope", scope) in (scope, "ARCHIVE")
            }
        else:
            out[key] = deepcopy(value)
    if "_event_payloads" in out:
        out["_event_payloads"] = {
            id: v for id, v in out["_event_payloads"].items() if id in out.get("events", {})
        }
    return out
