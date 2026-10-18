"""Independent per-person accumulation and quote-version reconstruction."""

from payment.runtime.products.services.collection_support import (
    records,
    bookings,
    protocol,
    outcome,
)


def verify(world, c):
    checks = []

    def ck(n, ok, zh, **kw):
        checks.append(dict(name=n, passed=bool(ok), description_zh=zh, evidence=kw))

    try:
        w = world.get("_closure_state") or world
        ini = c["initial_world"]
        rs = records(c)
        ids = {z["id"]: k for k, z in rs.items()}
        cash = ini["service_cases"][c["case_id"]]["cash_minor"]
        fees = 0
        valid = True
        scope = True
        timely = True
        events = set()
        for b in bookings(w, c):
            before = cash
            fee = 0
            out = {}
            m = b["minute"]
            if b.get("contract") == "revised_tolerances":
                d = b["arguments"]
                z = rs[ids[d["invoice_id"]]]
                letters = [rs[ids[i]] for i in d["authority_ids"]]
                gross = z["principal_minor"] + z["fee_minor"]
                valid &= (
                    b["kind"] == "pay"
                    and z["kind"] == "authorized_invoice"
                    and d["quote_version"] == z["quote_version"]
                    and len(letters) == 2
                    and len({a["natural_person"] for a in letters}) == 2
                )
                for a in letters:
                    scope &= (
                        a["kind"] == "corporate_authority"
                        and a["status"] == "SIGNED"
                        and z["reference"] in a["invoice_refs"]
                        and z["beneficiary_version"] in a["beneficiary_versions"]
                        and z["fee_minor"] <= a["fee_ceiling_minor"]
                        and a["effective_from"] <= m <= a["effective_through"]
                        and a["used_gross_minor"] + gross <= a["total_gross_limit_minor"]
                    )
                    a["used_gross_minor"] += gross
                z["paid_minor"] += z["principal_minor"]
                cash -= gross
                fee = z["fee_minor"]
                timely &= m <= z["due_minute"]
                out = dict(
                    invoice_ref=z["reference"],
                    principal_minor=z["principal_minor"],
                    fee_minor=z["fee_minor"],
                    beneficiary_version=z["beneficiary_version"],
                    authority_refs=[a["reference"] for a in letters],
                )
                valid &= b["receipt"] == out and b["fee_minor"] == fee
            elif b["kind"] == "bank_event":
                eid = b["event_id"]
                e = ini["events"][eid]
                valid &= (
                    eid not in events
                    and e["due_minute"] == m
                    and w["events"][eid]["status"] == "COMPLETED"
                    and b["event_kind"] == "quote_revision"
                )
                events.add(eid)
                for z in rs.values():
                    if z["kind"] == "authorized_invoice" and z["quote_revision_minute"] == m:
                        valid &= z["_revision"] in z["possible_revisions"]
                        z.update(**z["_revision"], quote_version=z["quote_version"] + 1)
                        out[z["reference"]] = dict(
                            fee_minor=z["fee_minor"],
                            beneficiary_version=z["beneficiary_version"],
                            quote_version=z["quote_version"],
                        )
                valid &= b["receipt"] == out
            else:
                valid = False
            fees += fee
            valid &= (
                cash >= 0
                and b["cash_delta_minor"] == cash - before
                and b["cash_after_minor"] == cash
            )
        ck(
            "payments.full_current_version",
            timely
            and events == set(ini["events"])
            and all(
                z["paid_minor"] == z["principal_minor"]
                for z in rs.values()
                if z["kind"] == "authorized_invoice"
            ),
            "Verify each ticket to complete a single full payment and deadline per the current quote and collection version.. ",
        )
        ck(
            "authority.independent_people_and_limits",
            scope,
            "Calculate the quota based on each natural person’s actual total deduction for the full batch, Receipt version, Fee tolerance and validity interval. ",
        )
        ck(
            "fees.batch_limit",
            fees <= 5500,
            "The full batch service fee does not exceed the user’s. USD 55 Cap; Single-ticket authorization tolerance does not increase the total budget. ",
            actual_minor=fees,
        )
        protocol(world, c, rs, cash, fees, {}, valid, checks)
    except (KeyError, TypeError, ValueError, StopIteration) as e:
        ck("state.well_formed", False, "Lack of independent accounting facts.. ", error=str(e))
    return outcome(checks)
