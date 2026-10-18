from payment.runtime.evaluation.psp.accounts.observations import initial_record


def scoped(w, c, table):
    cid = c["initial_world"].get(
        "_customer_id", c["initial_world"]["xp_cases"][c["id"]]["customer_id"]
    )
    return [r for r in w.get(table, {}).values() if r.get("customer_id") == cid]


def checker(c, base, remove=()):
    checks = [x for x in base["checks"] if x["name"] not in remove]

    def add(k, ok, detail, zh):
        checks.append(
            dict(
                name=f"BR-{c['number']}-{k}",
                passed=bool(ok),
                detail=detail,
                explanation=dict(
                    check_zh=zh, rule_documents=c["reasoning_revision"]["public_documents"]
                ),
            )
        )

    return (checks, add)


def result(checks):
    return dict(success=all((x["passed"] for x in checks)), checks=checks)
