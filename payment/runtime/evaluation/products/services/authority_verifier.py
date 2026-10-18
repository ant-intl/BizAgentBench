"""Independent temporal authority join over the evidence used for each payment."""

from copy import deepcopy


def verify(world, c):
    checks = []

    def ck(name, ok, zh, **kw):
        checks.append(dict(name=name, passed=bool(ok), description_zh=zh, evidence=kw))

    try:
        initial = c["initial_world"]
        w = world.get("_closure_state") or world
        cid = c["case_id"]
        old = {
            r["reference"]: r
            for r in initial["service_records"].values()
            if r.get("case_id") == cid
        }
        now = {r["reference"]: r for r in w["service_records"].values() if r.get("case_id") == cid}
        r = deepcopy(old)
        ids = {v["id"]: k for k, v in old.items()}
        policy = r["POLICY"]
        cash = {
            "USD": initial["service_cases"][cid]["cash_minor"],
            "EUR": initial["service_cases"][cid]["state"]["eur_minor"],
        }
        fees = {"USD": 0, "EUR": 0}
        valid = True
        deadline = True
        dest = True
        daily_ok = True
        authority = []
        bookings = sorted(
            (
                b
                for b in w["service_bookings"].values()
                if b.get("case_id") == cid and b["id"] not in initial["service_bookings"]
            ),
            key=lambda b: (b["minute"], b["id"]),
        )
        for b in bookings:
            delta = {"USD": 0, "EUR": 0}
            out = {}
            minute = b["minute"]
            if b.get("contract") == "versioned_authority":
                d = b["arguments"]
                invoice = r[ids[d["record_id"]]]
                action = b["kind"]
                if action == "attach_signatures":
                    refs = [ids[i] for i in d["signature_ids"]]
                    valid &= len(refs) == len(set(refs)) and all(
                        r[k]["status"] == "AVAILABLE" and r[k]["verified_genuine"] for k in refs
                    )
                    invoice["attached_signature_refs"] = refs
                elif action == "pay_invoice":
                    n = d["amount_minor"]
                    currency = invoice["currency"]
                    fee = invoice["fee_minor"] - invoice["fee_paid_minor"]
                    out = dict(
                        currency=currency,
                        supplier_minor=n,
                        fee_minor=fee,
                        bank_version=d["bank_version"],
                        signature_refs=deepcopy(invoice["attached_signature_refs"]),
                    )
                    valid &= (
                        invoice["amendment_status"] != "PENDING"
                        and d["bank_version"] in invoice["registered_bank_versions"]
                    )
                    dest &= d["bank_version"] == invoice["bank_version"]
                    deadline &= minute <= invoice["due_minute"]
                    gross = (
                        invoice["face_minor"] - invoice["prior_paid_minor"] + invoice["fee_minor"]
                    )
                    required = (
                        policy["upper_tier_people"]
                        if gross > policy["threshold_minor"][currency]
                        else policy["ordinary_people"]
                    )
                    qualified = []
                    reasons = {}
                    for ref in invoice["attached_signature_refs"]:
                        copy = r[ref]
                        s = copy.get("body")
                        why = []
                        if copy["status"] != "AVAILABLE" or not copy["verified_genuine"] or not s:
                            why.append("copy_not_available")
                        else:
                            person = r[s["person"]]
                            if s["payment"] != invoice["reference"]:
                                why.append("other_invoice")
                            if s["currency"] != currency or currency not in person["currencies"]:
                                why.append("currency_outside_authority")
                            if s["bank_version"] != d["bank_version"]:
                                why.append("bank_binding_changed")
                            if gross > min(s["ceiling_minor"], person["ceiling_minor"]):
                                why.append("gross_debit_above_ceiling")
                            if s["capacity"] not in person["capacities"]:
                                why.append("capacity_not_held")
                            if not (
                                person["effective_from"]
                                <= s["signed_minute"]
                                <= minute
                                < person["effective_until"]
                            ):
                                why.append("appointment_not_effective_at_signature_and_payment")
                        if why:
                            reasons[ref] = why
                        else:
                            qualified.append(s)
                    people = {s["person"] for s in qualified}
                    capacities = {s["capacity"] for s in qualified}
                    ok = (
                        len(people) >= required and set(policy["required_capacities"]) <= capacities
                    )
                    out["qualifying_person_refs"] = sorted(people)
                    for person_ref in sorted(people):
                        person = r[person_ref]
                        if invoice["reference"] not in person["committed_invoice_refs"]:
                            person["committed_invoice_refs"].append(invoice["reference"])
                            person["committed_minor"][currency] += gross
                        daily_ok &= (
                            person["committed_minor"][currency]
                            <= person["daily_limit_minor"][currency]
                        )
                    authority.append(
                        dict(
                            booking_id=b["id"],
                            invoice=invoice["reference"],
                            minute=minute,
                            gross_instruction_minor=gross,
                            required_people=required,
                            qualified_people=sorted(people),
                            qualified_capacities=sorted(capacities),
                            inapplicable_copies=reasons,
                            passed=ok,
                        )
                    )
                    delta[currency] = -n - fee
                    fees[currency] += fee
                    invoice["paid_minor"] += n
                    invoice["fee_paid_minor"] += fee
                else:
                    valid = False
                valid &= b["receipt"] == out
            elif b["kind"] == "bank_event" and b.get("event_kind") in (
                "authority_amendment",
                "authority_window",
            ):
                ev = w["events"][b["event_id"]]
                valid &= ev["status"] == "COMPLETED" and ev["due_minute"] == minute
                if b["event_kind"] == "authority_amendment":
                    invoice = r["EQUIPMENT"]
                    valid &= invoice["amendment_status"] == "PENDING"
                    invoice["amendment_status"] = "CONFIRMED"
                    amend = old["EQUIPMENT"]["_amendment"]
                    if amend == "BANK_DETAILS":
                        invoice["bank_version"] = 2
                    else:
                        invoice["remittance_note"] = "Updated supplier reference"
                    delivered = []
                    for ref, s in r.items():
                        if s["kind"] != "signed_instrument" or s["status"] != "AWAITING_COPY":
                            continue
                        body = deepcopy(old[ref]["_body"])
                        if body["payment"] == "EQUIPMENT":
                            body["bank_version"] = invoice["bank_version"]
                        s.update(status="AVAILABLE", body=body)
                        delivered.append(ref)
                    out = dict(
                        amendment=amend,
                        bank_version=invoice["bank_version"],
                        delivered_signature_refs=sorted(delivered),
                    )
                valid &= b["receipt"] == out
            else:
                continue
            for currency in cash:
                cash[currency] += delta[currency]
            valid &= (
                b["usd_delta_minor"] == delta["USD"]
                and b["eur_delta_minor"] == delta["EUR"]
                and b["usd_after_minor"] == cash["USD"]
                and b["eur_after_minor"] == cash["EUR"]
                and min(cash.values()) >= 0
            )
        invoices = [x for x in r.values() if x["kind"] == "controlled_invoice"]
        ck(
            "payments.exact_remaining",
            all(
                x["paid_minor"] == x["face_minor"] and x["fee_paid_minor"] == x["fee_minor"]
                for x in invoices
            ),
            "Historical actual payments count toward commercial obligations., Each remaining balance and one-time fee is actually completed. ",
            invoices={
                x["reference"]: dict(paid=x["paid_minor"], face=x["face_minor"]) for x in invoices
            },
        )
        ck(
            "payments.current_destination_and_deadline",
            dest and deadline,
            "Each actual payment uses the bank version confirmed at the time, And meet the invoice deadline. ",
        )
        ck(
            "authority.independent_people_and_scope",
            all(x["passed"] for x in authority) and len(authority) >= len(invoices),
            "Verify the real natural person of the evidence used one by one, Valid appointment, Currency, Responsibility, Fee-inclusive limit and version binding. ",
            payments=authority,
        )
        ck(
            "authority.cumulative_company_allowance",
            daily_ok,
            "Each actual qualified signer has cumulative authorized use under the entire commercial instruction; Split payments are not deducted repeatedly, Changing signatory afterwards does not return used limit. ",
            people={
                p["reference"]: dict(
                    used=p["committed_minor"],
                    limits=p["daily_limit_minor"],
                    invoices=p["committed_invoice_refs"],
                )
                for p in r.values()
                if p["kind"] == "authority_person"
            },
        )
        ck(
            "ledger.actual_cash_and_fees",
            valid
            and cash["USD"] == w["service_cases"][cid]["cash_minor"]
            and cash["EUR"] == w["service_cases"][cid]["state"]["eur_minor"]
            and fees["USD"] == w["service_cases"][cid]["fees_minor"]
            and fees["EUR"] == w["service_cases"][cid]["state"]["eur_fees_minor"],
            "Independently rebuild two wallets, First transaction fee, Payment receipt and evidence change, Do not score by the referenced signatory list. ",
        )
        frame = now == r
        for table, values in initial.items():
            if (
                isinstance(values, dict)
                and not table.startswith("_")
                and table not in ("events", "requests")
            ):
                frame &= all(
                    w[table].get(rid) == v
                    for rid, v in values.items()
                    if isinstance(v, dict) and v.get("scope") != c["scope"]
                )
        ck(
            "frame.records_and_history",
            frame,
            "Retain real employment, Signed original, Historical actual payments, Company policies and other customer records.. ",
        )
        ck(
            "events.completed",
            all(
                e["status"] == "COMPLETED"
                for e in w["events"].values()
                if e.get("scope") == c["scope"] and e.get("event_category") == "authority_amendment"
            ),
            "Actual supplier confirmation completed; A deadline marker with no state change need not be waited for. ",
        )
        closes = [a for a in world["audit"] if a["tool"] == "record_case_outcome"]
        ck(
            "process.single_completion",
            len(closes) == 1
            and closes[0]["result"]["ok"]
            and closes[0]["arguments"]["outcome"] == "COMPLETED",
            "Close case only once after completing business outcome.. ",
        )
        ck(
            "process.owner_approval",
            all(
                q["approval_seq"] < q["executed_seq"]
                and any(
                    a["tool"] == "decide_request"
                    and a["arguments"]["request_id"] == q["id"]
                    and a["arguments"]["decision"] == "APPROVE"
                    and a["result"]["ok"]
                    for a in world["audit"]
                )
                for q in w["requests"].values()
                if q.get("scope") == c["scope"] and q["status"] == "EXECUTED"
            ),
            "Verify account holder’s bank instruction authorization and company independent signature requirement separately. ",
        )
    except (KeyError, TypeError, ValueError, StopIteration) as e:
        ck(
            "state.well_formed",
            False,
            "Missing fields required for independent accounting. ",
            error=str(e),
        )
    failed = [x["name"] for x in checks if not x["passed"]]
    return dict(success=not failed, checks=checks, failed=failed, allowed_terminals=["COMPLETED"])
