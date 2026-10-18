"""Independent invoice discharge, company cash and instruction-signature audit."""

from copy import deepcopy
import hashlib
import json
from payment.runtime.products.services.netting_bound import residual, receipt_net


def signature_fingerprint(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def qualifying(rows, cycle, when):
    eligible = {ref: dict(people=set(), capacities=set()) for ref in cycle["participant_refs"]}
    fee = rows["TERMS"]["fee_per_net_debtor_minor"] if cycle["payload"]["type"] == "INTERNAL" else 0
    for ref in cycle["signature_refs"]:
        s = rows[ref]
        p = rows[s["person_ref"]]
        login = rows[s["login_ref"]]
        if not (
            s["kind"] == "clearing_signature"
            and s["verified_genuine"]
            and s["source"] == "registered_staff_signature_service"
            and s["cycle_ref"] == cycle["reference"]
            and s["instruction_digest"] == signature_fingerprint(cycle["payload"])
        ):
            continue
        if not (
            p["currency"] == cycle["payload"]["currency"]
            and p["effective_from"] <= s["signed_minute"] <= when < p["effective_until"]
            and s["capacity"] in p["capacities"]
            and login["person_ref"] == p["reference"]
            and login["capacity"] == s["capacity"]
        ):
            continue
        for company in eligible:
            amount = max(0, -cycle["net_minor"][company]) + (
                fee if cycle["net_minor"][company] < 0 else 0
            )
            if (
                company in s["company_refs"]
                and company in p["company_refs"]
                and amount <= p["debit_ceiling_minor"]
            ):
                eligible[company]["people"].add(p["reference"])
                eligible[company]["capacities"].add(s["capacity"])
    return {
        k: {field: sorted(value) for field, value in result.items()}
        for k, result in eligible.items()
    }


def verify(world, c):
    checks = []

    def ck(name, ok, zh, **kw):
        checks.append(dict(name=name, passed=bool(ok), description_zh=zh, evidence=kw))

    try:
        w = world.get("_closure_state") or world
        initial = c["initial_world"]
        cid = c["case_id"]
        original = {
            r["reference"]: deepcopy(r)
            for r in initial["service_records"].values()
            if r.get("case_id") == cid
        }
        rows = deepcopy(original)
        ids = {r["id"]: ref for ref, r in rows.items()}
        allocated = set(initial["service_records"])
        fees = initial["service_cases"][cid]["fees_minor"]
        valid = True
        seen = set()
        external_total = 0
        control_records = []

        def kind(k):
            return [r for r in rows.values() if r["kind"] == k]

        def obj(a, key, k):
            r = rows[ids[a[key]]]
            assert r["kind"] == k
            return r

        def create(ref, name, k, **values):
            number = len(allocated) + 1
            while "SERVICE_RECORDS-" + format(number, "04") in allocated:
                number += 1
            rid = "SERVICE_RECORDS-" + format(number, "04")
            allocated.add(rid)
            r = dict(
                id=rid,
                scope=c["scope"],
                case_id=cid,
                reference=ref,
                name=name,
                kind=k,
                customer_access=True,
                **values,
            )
            rows[ref] = r
            ids[rid] = ref
            return r

        def internal(refs, minute):
            amounts = {ref: residual(rows, ref, minute) for ref in sorted(refs)}
            basis = [dict(invoice_ref=ref, amount_minor=n) for ref, n in amounts.items()]
            parts = sorted(
                {x for ref in refs for x in [rows[ref]["debtor_ref"], rows[ref]["creditor_ref"]]}
            )
            allnet = receipt_net(rows, amounts)
            payload = dict(type="INTERNAL", currency="USD", basis=basis)
            return dict(
                payload=payload,
                instruction_digest=signature_fingerprint(payload),
                net_minor={p: allnet[p] for p in parts},
                participant_refs=parts,
            )

        def newcycle(values, m):
            number = len(kind("clearing_cycle")) + 1
            return create(
                "CYCLE-" + str(number),
                "Prepared clearing instruction " + str(number),
                "clearing_cycle",
                status="PREPARED",
                revision=1,
                prepared_minute=m,
                executed_minute=None,
                signature_refs=[],
                **values,
            )

        books = sorted(
            (
                b
                for b in w["service_bookings"].values()
                if b.get("case_id") == cid and b["id"] not in initial["service_bookings"]
            ),
            key=lambda b: (b["minute"], b["id"]),
        )
        for b in books:
            before = sum(r["cash_minor"] for r in kind("clearing_account"))
            oldfees = fees
            receipt = {}
            m = b["minute"]
            if b.get("contract") == "revised_netting":
                a = b["arguments"]
                action = b["kind"]
                if action == "prepare_cycle":
                    listed = a["invoice_ids"]
                    assert type(listed) is list and listed and len(listed) == len(set(listed))
                    refs = [ids[rid] for rid in listed]
                    assert all(rows[ref]["kind"] == "intercompany_invoice" for ref in refs)
                    cycle = newcycle(internal(refs, m), m)
                    receipt = dict(cycle=deepcopy(cycle))
                elif action == "prepare_external":
                    o = obj(a, "obligation_id", "clearing_external_obligation")
                    payer = obj(a, "payer_account_id", "clearing_account")
                    recipient = obj(a, "recipient_id", "clearing_recipient")
                    n = a["amount_minor"]
                    assert type(n) is int and n > 0
                    valid &= (
                        recipient["status"] == "VERIFIED"
                        and recipient["currency"] == payer["currency"] == "USD"
                    )
                    payload = dict(
                        type="EXTERNAL",
                        currency="USD",
                        obligation_ref=o["reference"],
                        payer_ref=payer["reference"],
                        recipient_ref=recipient["reference"],
                        amount_minor=n,
                    )
                    cycle = newcycle(
                        dict(
                            payload=payload,
                            instruction_digest=signature_fingerprint(payload),
                            net_minor={payer["reference"]: -n},
                            participant_refs=[payer["reference"]],
                        ),
                        m,
                    )
                    receipt = dict(cycle=deepcopy(cycle))
                elif action == "refresh_cycle":
                    cycle = obj(a, "cycle_id", "clearing_cycle")
                    valid &= (
                        cycle["status"] == "PREPARED" and cycle["payload"]["type"] == "INTERNAL"
                    )
                    cycle.update(internal([v["invoice_ref"] for v in cycle["payload"]["basis"]], m))
                    cycle["revision"] += 1
                    receipt = dict(cycle=deepcopy(cycle))
                elif action == "collect_signature":
                    cycle = obj(a, "cycle_id", "clearing_cycle")
                    login = obj(a, "login_id", "clearing_staff_login")
                    p = rows[login["person_ref"]]
                    parts = sorted(set(cycle["participant_refs"]) & set(p["company_refs"]))
                    fee = (
                        rows["TERMS"]["fee_per_net_debtor_minor"]
                        if cycle["payload"]["type"] == "INTERNAL"
                        else 0
                    )
                    valid &= (
                        cycle["status"] == "PREPARED"
                        and login["status"] == "ACTIVE"
                        and p["effective_from"] <= m < p["effective_until"]
                        and login["capacity"] in p["capacities"]
                        and p["currency"] == cycle["payload"]["currency"]
                        and bool(parts)
                        and all(
                            max(0, -cycle["net_minor"][r])
                            + (fee if cycle["net_minor"][r] < 0 else 0)
                            <= p["debit_ceiling_minor"]
                            for r in parts
                        )
                    )
                    number = len(kind("clearing_signature")) + 1
                    s = create(
                        "SIGNATURE-" + str(number),
                        "Registered staff signature " + str(number),
                        "clearing_signature",
                        cycle_ref=cycle["reference"],
                        person_ref=p["reference"],
                        login_ref=login["reference"],
                        capacity=login["capacity"],
                        company_refs=parts,
                        instruction_digest=cycle["instruction_digest"],
                        signed_minute=m,
                        verified_genuine=True,
                        source="registered_staff_signature_service",
                    )
                    cycle["signature_refs"].append(s["reference"])
                    receipt = dict(signature=deepcopy(s), cycle_ref=cycle["reference"])
                elif action == "execute":
                    cycle = obj(a, "cycle_id", "clearing_cycle")
                    payload = cycle["payload"]
                    valid &= cycle["status"] == "PREPARED" and cycle[
                        "instruction_digest"
                    ] == signature_fingerprint(payload)
                    if payload["type"] == "INTERNAL":
                        amounts = {v["invoice_ref"]: v["amount_minor"] for v in payload["basis"]}
                        computed = receipt_net(rows, amounts)
                        valid &= (
                            len(amounts) == len(payload["basis"])
                            and all(type(n) is int for n in amounts.values())
                            and cycle["net_minor"]
                            == {ref: computed[ref] for ref in cycle["participant_refs"]}
                        )
                        rate = rows["TERMS"]["fee_per_net_debtor_minor"]
                    else:
                        valid &= payload["type"] == "EXTERNAL" and cycle["net_minor"] == {
                            payload["payer_ref"]: -payload["amount_minor"]
                        }
                        rate = 0
                    applicable = qualifying(rows, cycle, m)
                    control = rows["TERMS"]["control"]
                    compliant = all(
                        len(z["people"]) >= control["minimum_people"]
                        and set(control["required_capacities"]) <= set(z["capacities"])
                        for z in applicable.values()
                    )
                    control_records.append(
                        dict(
                            cycle_ref=cycle["reference"],
                            minute=m,
                            signature_refs=deepcopy(cycle["signature_refs"]),
                            instruction_digest=cycle["instruction_digest"],
                            applicable=applicable,
                            compliant=compliant,
                        )
                    )
                    charges = {ref: rate if n < 0 else 0 for ref, n in cycle["net_minor"].items()}
                    for ref, n in cycle["net_minor"].items():
                        r = rows[ref]
                        valid &= (
                            r["kind"] == "clearing_account"
                            and r["status"] == "OPEN"
                            and r["currency"] == payload["currency"]
                        )
                        r["cash_minor"] += n - charges[ref]
                        valid &= r["cash_minor"] >= 0
                    fees += sum(charges.values())
                    if payload["type"] == "INTERNAL":
                        for ref, n in amounts.items():
                            r = rows[ref]
                            r["netting_settled_minor"] += n
                            r["settlements"].append(
                                dict(cycle_ref=cycle["reference"], amount_minor=n, minute=m)
                            )
                    else:
                        valid &= rows[payload["recipient_ref"]]["status"] == "VERIFIED"
                        o = rows[payload["obligation_ref"]]
                        n = payload["amount_minor"]
                        o["paid_minor"] += n
                        external_total += n
                        o["payments"].append(
                            dict(
                                payer_ref=payload["payer_ref"],
                                recipient_ref=payload["recipient_ref"],
                                amount_minor=n,
                                minute=m,
                                cycle_ref=cycle["reference"],
                            )
                        )
                    cycle.update(status="EXECUTED", executed_minute=m)
                    receipt = dict(
                        cycle_ref=cycle["reference"],
                        instruction_digest=cycle["instruction_digest"],
                        net_minor=deepcopy(cycle["net_minor"]),
                        charges_minor=charges,
                        signature_refs=deepcopy(cycle["signature_refs"]),
                    )
                else:
                    valid = False
            elif b["kind"] == "bank_event":
                eid = b["event_id"]
                ev = initial["events"][eid]
                valid &= (
                    eid not in seen
                    and ev["due_minute"] == m
                    and w["events"][eid]["status"] == "COMPLETED"
                    and b["event_kind"]
                    == initial["_event_payloads"][eid]["event_kind"]
                    == "credit_confirmation"
                )
                seen.add(eid)
                changes = []
                for note in kind("clearing_credit_note"):
                    if note["status"] == "PENDING":
                        n = note["_actual_amount_minor"]
                        valid &= (
                            n in note["possible_amounts_minor"] and m == note["confirmation_minute"]
                        )
                        note.update(status="FINAL", amount_minor=n)
                        changes.append(dict(note_ref=note["reference"], amount_minor=n))
                receipt = dict(credits=changes)
            else:
                valid = False
            cash = sum(r["cash_minor"] for r in kind("clearing_account"))
            valid &= (
                b["receipt"] == receipt
                and b["cash_delta_minor"] == cash - before
                and b["cash_after_minor"] == cash
                and b["fee_minor"] == fees - oldfees
            )
        invoices = kind("intercompany_invoice")
        agreed = [r for r in invoices if r["agreement_status"] == "AGREED"]
        left = {r["reference"]: residual(rows, r["reference"], w["minute"]) for r in agreed}
        ck(
            "invoices.final_individual_discharge",
            all(n == 0 for n in left.values()),
            "Final balance of each confirmed invoice is zeroed out, Must not reconcile only group totals or each party’s net amount. ",
            remaining_minor=left,
        )
        due = []
        for r in agreed:
            if r["due_minute"] == 0:
                paid = sum(
                    p["amount_minor"] for p in r["settlements"] if p["minute"] <= r["due_minute"]
                )
                required = residual(original, r["reference"], 0)
                due.append(
                    dict(
                        invoice_ref=r["reference"],
                        required_minor=required,
                        paid_by_cutoff_minor=paid,
                        passed=paid >= required,
                    )
                )
        ck(
            "invoices.original_due_amounts",
            all(z["passed"] for z in due),
            "Initial maturing debt is fully performed at the amount then in effect, Subsequent credits cannot retroactively recognize prior missed payments. ",
            invoices=due,
        )
        ck(
            "invoices.disputed_scope_untouched",
            all(
                r == original[r["reference"]]
                for r in invoices
                if r["agreement_status"] == "DISPUTED"
            ),
            "Disputed invoices retained by the customer have not been settled or rewritten. ",
        )
        outside = kind("clearing_external_obligation")
        ck(
            "external.correct_source_recipient_and_deadline",
            all(
                o["paid_minor"] == o["amount_minor"]
                and all(
                    p["payer_ref"] == o["payer_ref"]
                    and p["recipient_ref"] == o["recipient_ref"]
                    and p["minute"] <= o["due_minute"]
                    for p in o["payments"]
                )
                for o in outside
            ),
            "External brokerage funds actually paid in full by the designated company to the correct payee., And complete on schedule. ",
            payments={o["reference"]: o["payments"] for o in outside},
        )
        ck(
            "control.independent_current_signatures",
            all(r["compliant"] for r in control_records),
            "Each actual instruction had a matching amount at the time of execution, Independent natural person signature for scope and authority. ",
            instructions=control_records,
        )
        ck(
            "charges.customer_total_limit",
            fees <= c["fee_cap_minor"],
            "Total actual payer fees for all executed clearing cycles do not exceed the customer budget. ",
            actual_minor=fees,
            cap_minor=c["fee_cap_minor"],
        )
        cash = sum(r["cash_minor"] for r in kind("clearing_account"))
        expected = deepcopy(initial["service_cases"][cid])
        expected.update(cash_minor=cash, fees_minor=fees)
        conserved = (
            cash
            == initial["service_cases"][cid]["cash_minor"]
            - (fees - initial["service_cases"][cid]["fees_minor"])
            - external_total
        )
        ck(
            "ledger.company_cash_and_conservation",
            valid and conserved and expected == w["service_cases"][cid],
            "Independently reconstruct each company’s actual receipts and payments, Per-payer charging and group fund conservation. ",
            company_cash_minor={r["reference"]: r["cash_minor"] for r in kind("clearing_account")},
            actual_external_outflow_minor=external_total,
        )
        closing_window = rows["TERMS"]["late_window_minute"]
        final_notes = [n for n in kind("clearing_credit_note") if n["status"] == "FINAL"]
        invoice_settlements = [payment for invoice in agreed for payment in invoice["settlements"]]
        window_met = (
            bool(seen)
            and all(n["status"] != "PENDING" for n in kind("clearing_credit_note"))
            and all(n["confirmation_minute"] <= closing_window for n in final_notes)
            and all(payment["minute"] <= closing_window for payment in invoice_settlements)
        )
        ck(
            "events.final_confirmation_and_window",
            window_met,
            "Actual credit deduction confirmation and bill-by-bill repayment must be completed within the settlement window; Other transactions or conversations after completion do not change the completed payment time. ",
            closing_window_minute=closing_window,
            credit_confirmation_minutes=[n["confirmation_minute"] for n in final_notes],
            settlement_minutes=[p["minute"] for p in invoice_settlements],
        )
        actual = {
            r["reference"]: r for r in w["service_records"].values() if r.get("case_id") == cid
        }
        frame = rows == actual
        for table, values in initial.items():
            if (
                isinstance(values, dict)
                and not table.startswith("_")
                and table not in ("events", "requests")
            ):
                frame &= all(
                    w[table].get(k) == v
                    for k, v in values.items()
                    if isinstance(v, dict) and v.get("scope") != c["scope"]
                )
        ck(
            "frame.source_and_generated_records",
            frame,
            "Verify original documents., Actual signing source, Modification boundaries for actual newly created drafts and other business records. ",
        )
        closes = [a for a in world["audit"] if a["tool"] == "record_case_outcome"]
        ck(
            "process.single_completion",
            len(closes) == 1
            and closes[0]["result"]["ok"]
            and closes[0]["arguments"]["outcome"] == "COMPLETED",
            "Close only once after final business reconciliation. ",
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
            "Each bank instruction has customer approval with bound parameters, Verify institutional signing and customer approval separately. ",
        )
    except (KeyError, TypeError, ValueError, StopIteration, AssertionError) as e:
        ck(
            "state.well_formed",
            False,
            "Lack valid data required for independent per-ticket settlement or genuine signature. ",
            error=str(e),
        )
    failed = [z["name"] for z in checks if not z["passed"]]
    return dict(success=not failed, failed=failed, checks=checks, allowed_terminals=["COMPLETED"])
