def rr(w, t):
    return [r for r in w.get("xp_a_" + t, {}).values() if r["customer_id"] == w["_customer_id"]]


def check(checks, n, key, ok, detail, why, docs):
    checks.append(
        dict(
            name=f"a.{n}.reasoning_{key}",
            passed=bool(ok),
            detail=detail,
            explanation=dict(check_zh=why, rule_documents=docs),
        )
    )
