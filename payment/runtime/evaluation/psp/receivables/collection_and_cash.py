ACTIVE = {32}


def records(w, table):
    return [r for r in w.get("xp_a_" + table, {}).values() if r["customer_id"] == w["_customer_id"]]


def verify(world, contract, previous):
    result = previous(world, contract)
    n = contract["number"]
    if n not in ACTIVE:
        return result
    checks = result["checks"]
    doc = "PX-A-RECALL-SALE"

    def check(key, passed, detail, zh):
        checks.append(
            dict(
                name=f"a.{n}.{key}",
                passed=bool(passed),
                detail=detail,
                explanation=dict(check_zh=zh, rule_documents=[doc]),
            )
        )

    if n in (32,):
        from payment.runtime.evaluation.psp.receivables.collection_checks import (
            verify as extra_verify,
        )

        extra_verify(world, contract, check)
    return dict(success=all((x["passed"] for x in checks)), checks=checks)


def wrap(previous):
    return lambda world, contract: verify(world, contract, previous)
