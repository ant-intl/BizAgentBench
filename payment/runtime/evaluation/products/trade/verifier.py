"""Independent economic and event predicates. Does not import execution or solver code."""

from copy import deepcopy
from payment.runtime.evaluation.products.trade.source_verifier import verify as inherited_verify
from payment.runtime.products.trade.records import TABLES


def verify(world, c):
    if not c.get("trade_service"):
        return inherited_verify(world, c)
    checks = []

    def check(name, ok, zh, detail=None):
        checks.append(dict(name=name, passed=bool(ok), description_zh=zh, detail=detail))

    try:
        initial = c["initial_world"]
        case_id = c["case_id"]
        scope = c["scope"]
        state = world.get("_closure_state") or world
        start = initial["service_cases"][case_id]
        case = state["service_cases"][case_id]
        kind = case["kind"]
        s = case["state"]
        records = {
            r["reference"]: r for r in state["service_records"].values() if r["case_id"] == case_id
        }
        original = {
            r["reference"]: r
            for r in initial["service_records"].values()
            if r["case_id"] == case_id
        }
        rr = next(r for r in original.values() if r["kind"] == "external_correspondence")
        source = rr["_response"]
        books = [b for b in state["service_bookings"].values() if b["case_id"] == case_id]
        calls = world["audit"]
        closes = [a for a in calls if a["tool"] == "record_case_outcome"]
        check(
            "closure",
            len(closes) == 1
            and closes[0]["result"]["ok"]
            and world["case_outcome"]["outcome"] == "COMPLETED",
            "Exactly once genuine completion and closure; Other business predicates are still checked independently. ",
        )
        reqs = [
            r
            for r in state["requests"].values()
            if r.get("scope") == scope and r.get("status") == "EXECUTED"
        ]
        approvals = True
        for r in reqs:
            decisions = [
                a
                for a in calls
                if a["tool"] == "decide_request"
                and a["arguments"]["request_id"] == r["id"]
                and a["result"]["ok"]
                and a["arguments"]["decision"] == "APPROVE"
            ]
            approvals &= bool(decisions) and r.get("approval_seq", 10**10) < r.get(
                "executed_seq", -1
            )
        check(
            "approval",
            approvals,
            "All executed requests have prior genuine user approval and a matching request identifier. ",
        )
        # Protect other customer scopes, archived source papers and every inherited table.
        exempt = {
            "audit",
            "unlocked",
            "case_outcome",
            "_closure_state",
            "_executing_approver",
            "minute",
            "requests",
            "events",
            "_event_payloads",
            *TABLES,
        }
        frame = all(state.get(t) == v for t, v in initial.items() if t not in exempt)
        for t in TABLES:
            for rid, r in initial[t].items():
                if r.get("scope") != scope or t == "service_documents":
                    frame &= state[t].get(rid) == r
        check(
            "frame",
            frame,
            "Other customers, Original contract, Historical records and other business accounts remain unchanged. ",
        )
        obligations = [r for r in records.values() if r["kind"] == "obligation"]
        for r in obligations:
            ps = [b for b in books if b["kind"] == "payment" and b["record_id"] == r["id"]]
            check(
                "obligation." + r["reference"],
                r["paid_minor"] == r["amount_minor"] == sum(b["amount_minor"] for b in ps)
                and all(b["minute"] <= r["due_minute"] for b in ps),
                "Verify against the actual total amount paid "
                + r["name"]
                + " The full amount, No duplication and arrival deadline. ",
            )
        # Independent cash reconstruction: do not trust recorded cash_delta_minor.
        cash = start["cash_minor"]
        eur = start["state"].get("eur_minor", 0)
        fees = 0
        if kind == "forward" and records[rr["reference"]]["receipt_status"] == "READY":
            cash += source["credited_minor"]
        if kind == "debit":
            cash += sum(
                r["amount_minor"]
                for r in records.values()
                if r["kind"] == "debit_instruction" and r["state"] == "SETTLED"
            )
        for b in books:
            a = b["kind"]
            d = b.get("arguments", {})
            if a == "payment":
                r = next(r for r in records.values() if r["id"] == b["record_id"])
                if not r.get("payer"):
                    if r.get("currency") == "EUR":
                        eur -= b["amount_minor"]
                    else:
                        cash -= b["amount_minor"]
            elif kind == "escrow" and a == "distribute":
                cash -= d["amount_minor"]
            elif kind == "factoring":
                if a == "repay_senior":
                    cash -= d["amount_minor"]
                elif a == "assign":
                    o = initial["service_records"][d["offer_id"]]
                    f = (d["face_minor"] * o["fee_bp"] + 9999) // 10000
                    cash += d["face_minor"] * o["advance_bp"] // 10000 - f
                    fees += f
            elif kind == "credit_letter":
                f = (
                    1000
                    if a == "amend"
                    else (
                        initial["service_records"][d["route_id"]]["fee_minor"]
                        if a == "present"
                        else 0
                    )
                )
                cash -= f
                fees += f
                if a == "present" and s.get("presentation_status") == "PAID":
                    cash += d["amount_minor"]
            elif kind == "guarantee":
                if a == "issue":
                    o = initial["service_records"][d["product_id"]]
                    f = o["fee_minor"]
                    fees += f
                    cash -= f
                # Principal is accounted below using conservation of the collateral pool.
            elif kind == "forward":
                if a == "deliver":
                    cash += start["state"]["margin_minor"] - start["state"]["deliver_minor"]
                    eur += start["state"]["receive_minor"]
                if a == "closeout":
                    cash += start["state"]["margin_minor"] - 15000
                    fees += 15000
            elif kind == "debit" and a == "refund":
                cash -= d["amount_minor"]
            elif kind == "representment":
                if a == "defend":
                    cash -= 1000
                    fees += 1000
            elif kind == "freight":
                if a == "pay_lien":
                    cash -= d["amount_minor"]
                if a == "release" and b["minute"] > 5:
                    cash -= 3000
                    fees += 3000
            elif kind == "rolling_reserve" and a == "withdraw":
                r = original[next(x for x in original if original[x]["id"] == d["record_id"])]
                f = 2000 if b["minute"] < r["matures_minute"] else 0
                cash += d["amount_minor"] - f
                fees += f
            elif kind == "netting" and a == "fund":
                cash -= d["amount_minor"]
            elif kind == "collateral" and a == "repay":
                cash -= d["amount_minor"]
            elif kind == "insurance" and a == "sell_salvage":
                cash += source["salvage_offer_minor"]
            elif kind == "term_deposit" and a == "redeem":
                r = initial["service_records"][d["record_id"]]
                early = b["minute"] < r["matures_minute"]
                f = r["break_fee_minor"] if early else 0
                interest = 0 if early else r["interest_minor"]
                prior_registration = any(
                    x["kind"] == "register_tax" and x["minute"] <= b["minute"]
                    for x in books[: books.index(b)]
                )
                tax = (
                    0
                    if prior_registration and source["effective_minute"] <= r["matures_minute"]
                    else interest * 2000 // 10000
                )
                cash += r["principal_minor"] + interest - f - tax
                fees += f
                actual = state["service_records"][d["record_id"]]
                check(
                    "deposit_tax." + r["reference"],
                    actual["tax_minor"] == tax and actual["interest_paid_minor"] == interest,
                    "Independently calculate this interest and withholding tax according to the contract maturity date, Late withdrawal cannot be used for retroactive tax exemption.. ",
                )
            elif kind == "export_license":
                if a == "register_return":
                    cash += source["refundable_bond_minor"]
                if a == "declare":
                    cash -= 10 * d["kilograms"]
        if kind == "guarantee":
            cash -= s["collateral_minor"] - start["state"]["collateral_minor"]
        if kind == "representment":
            cash += s["defended_minor"]
        if kind == "insurance":
            cash += sum(r["paid_minor"] for r in records.values() if r["kind"] == "policy")
        check(
            "cash_conservation",
            cash == case["cash_minor"]
            and eur == s.get("eur_minor", 0)
            and fees == case["fees_minor"],
            "From initial principal, Original rate, Recalculate each currency’s cash and fees based on each instruction and actual event; Do not use tool self-reported balance changes. ",
            dict(
                expected_cash_minor=cash,
                actual_cash_minor=case["cash_minor"],
                expected_fees_minor=fees,
            ),
        )
        if kind == "escrow":
            deliveries = [r for r in original.values() if r["kind"] == "delivery"]
            accepted = sum(
                source["accepted_units"][r["shipment_ref"]] * r["unit_price_minor"]
                for r in deliveries
            )
            total = sum(r["ordered_units"] * r["unit_price_minor"] for r in deliveries)
            retained = accepted * start["state"]["retention_bp"] // 10000
            paid = lambda who: sum(
                b["arguments"]["amount_minor"]
                for b in books
                if b["kind"] == "distribute" and b["arguments"]["beneficiary"] == who
            )
            check(
                "escrow_entitlements",
                s.get("declared_units") == source["accepted_units"]
                and paid("supplier") == accepted - retained
                and paid("buyer") == total - accepted
                and case["cash_minor"] == retained,
                "Independently reconcile supplier from latest receipt quantity and unit price, Buyer and warranty three-party rights. ",
            )
        elif kind == "factoring":
            good = True
            for r in records.values():
                if r["kind"] == "receivable" and r["assigned"]:
                    credit = sum(
                        n["amount_minor"]
                        for n in source["credit_notes"]
                        if n["debtor"] == r["debtor"] and n["status"] == "ISSUED"
                    )
                    good &= r["assigned_face_minor"] <= r["face_minor"] - credit and r["notified"]
            check(
                "factoring_scope",
                good and not records["ORION"]["assigned"] and s["senior_debt_minor"] == 0,
                "Verify the actual transferable face amount after the credit note., Delivered debtor notice, Old debt settlement and Orion Unpledged. ",
            )
        elif kind == "credit_letter":
            check(
                "credit_remaining",
                s.get("presentation_status") == "PAID"
                and s["presentation"]["amount_minor"]
                == start["state"]["face_minor"] - start["state"]["prior_advance_minor"]
                and case["fees_minor"] <= 2000,
                "Verify the actual paid negotiated payment, Remaining obligation after deducting historical prepayments and the customer bank fee cap. ",
            )
        elif kind == "guarantee":
            old = True
            new = 0
            continuous = True
            for b in books:
                if b["kind"] == "issue":
                    new = b["arguments"]["amount_minor"]
                if b["kind"] == "return_old":
                    old = False
                continuous &= new >= source["replacement_amount_minor"] if not old else True
            check(
                "guarantee_continuity",
                continuous
                and not s["old_active"]
                and s["new_active"]
                and s["new_amount_minor"] == source["replacement_amount_minor"],
                "Verify guarantee continuity against each issuance and return event; Final reissue cannot conceal the prior safeguard gap. ",
            )
        elif kind == "forward":
            check(
                "forward_delivery",
                s["forward_status"] == "DELIVERED"
                and s["margin_minor"] == 0
                and s["eur_minor"] >= 10000,
                "Forward is actually delivered and margin is released., EUR reserve still remains after payment. ",
            )
        elif kind == "debit":
            refund = max(
                0,
                original["FIRST-COLLECTION"]["amount_minor"]
                + source["prior_bank_transfer_minor"]
                - source["invoice_minor"],
            )
            check(
                "debit_scope",
                records["FIRST-COLLECTION"]["refunded_minor"] == refund
                and records["SECOND-COLLECTION"]["refunded_minor"] == 0
                and not records["OLD-MANDATE"]["active"]
                and records["NEW-MANDATE"]["active"]
                and records["OLD-PENDING"]["state"] == "RECALLED",
                "Verify the overcharge using the transfer and debit on the same bill; Old accepted instructions must be withdrawn separately., New authorization remains valid. ",
            )
        elif kind == "representment":
            valid = max(
                0,
                (source["ordered_units"] - source["delivered_units"]) * source["unit_price_minor"]
                - source["posted_refunds_minor"],
            )
            check(
                "merchant_defense",
                s["conceded_minor"] == valid
                and s["defended_minor"] == start["state"]["disputed_minor"] - valid
                and s["held_minor"] == 0
                and s.get("defense_status") == "ACCEPTED",
                "Reconcile merchant concessions and defense amounts against actual stockouts and received refunds, and the defense has been resolved and funds released. ",
            )
        elif kind == "freight":
            good = all(
                records[ref]["released_cartons"] == n
                and records[ref]["endorsed_to"] == source["consignee"]
                and records[ref]["freight_paid_minor"] == records[ref]["freight_minor"]
                for ref, n in source["accepted_cartons"].items()
            )
            check(
                "cargo_title",
                good,
                "Each bill of lading releases only the accepted portion to the designated warehouse, And after deducting historical freight, pay off the lien amount exactly. ",
            )
            endorsed = {
                r["id"]: r.get("endorsed_to") for r in original.values() if r["kind"] == "cargo"
            }
            actual_title = True
            for b in books:
                if b["kind"] == "endorse":
                    endorsed[b["arguments"]["record_id"]] = b["arguments"]["consignee"]
                if b["kind"] == "release":
                    actual_title &= endorsed[b["arguments"]["record_id"]] == source["consignee"]
            check(
                "cargo_recipient_at_release",
                actual_title,
                "Verify against the endorsee at the time of actual title release.; After-the-fact correction to the correct warehouse cannot change the goods title already transferred. ",
            )
        elif kind == "rolling_reserve":
            good = all(
                r["held_minor"] >= r["open_exposure_minor"]
                for r in records.values()
                if r["kind"] == "reserve_cohort"
            )
            good &= records[source["refund_cohort"]]["refunds_minor"] == source[
                "refund_minor"
            ] and all(
                r["refunds_minor"] == 0
                for r in records.values()
                if r["kind"] == "reserve_cohort" and r["reference"] != source["refund_cohort"]
            )
            check(
                "cohort_principal",
                good,
                "Refund offsets the original sales batch; Each batch still retains the principal required for its outstanding chargebacks, Cannot be substituted across batches. ",
            )
        elif kind == "netting":
            expected = {r["reference"]: 0 for r in original.values() if r["kind"] == "participant"}
            for ref in source["approved_invoice_refs"]:
                r = original[ref]
                expected[r["debtor"]] -= r["amount_minor"]
                expected[r["creditor"]] += r["amount_minor"]
            correct = (
                s.get("novation") == expected
                and all(records[ref]["settled"] for ref in source["approved_invoice_refs"])
                and all(not records[ref]["settled"] for ref in source["disputed_invoice_refs"])
            )
            for ref, n in expected.items():
                payments = sum(
                    b["amount_minor"]
                    for b in books
                    if b["kind"] == "payment"
                    and state["service_records"][b["record_id"]].get("payer") == ref
                )
                correct &= records[ref]["cash_minor"] == max(n, 0) - payments
            check(
                "entity_net_positions",
                correct and s["clearing_minor"] == 0,
                "Independently calculate each entity’s net position from the undisputed invoice graph, And deduct its external payments.; Total balance cannot substitute for correct entity. ",
            )
        elif kind == "collateral":
            # Reconstruct every effective valuation and collateral transaction,
            # so a final repayment cannot erase an earlier coverage breach.
            av = {
                r["id"]: deepcopy(r) for r in original.values() if r["kind"] == "collateral_asset"
            }
            debt = start["state"]["principal_minor"]
            continuous = True
            breaches = []
            effective_bookings = set()
            for call in calls:
                if not call["result"]["ok"]:
                    continue
                if call["tool"] == "wait_for_update":
                    delivered = call["result"]["data"].get("events", [])
                    if any(
                        ev["kind"] == "trade_source_receipt" and ev["target_id"] == rr["id"]
                        for ev in delivered
                    ):
                        ar = next(
                            r
                            for r in av.values()
                            if r["reference"] == source["valuation_reference"]
                        )
                        ar["market_minor"] = source["market_minor"]
                if call["tool"] == "execute_request":
                    b = call["result"]["data"]
                    if (
                        b.get("contract") == "collateral"
                        and b.get("case_id") == case_id
                        and b["id"] not in effective_bookings
                    ):
                        effective_bookings.add(b["id"])
                        d = b["arguments"]
                        if b["kind"] == "repay":
                            debt -= d["amount_minor"]
                        elif b["kind"] == "pledge":
                            av[d["record_id"]]["pledged"] = True
                        elif b["kind"] == "release":
                            av[d["record_id"]]["pledged"] = False
                held = sum(
                    r["market_minor"] * (10000 - r["haircut_bp"]) // 10000
                    for r in av.values()
                    if r["pledged"]
                )
                required_now = (debt * start["state"]["margin_bp"] + 9999) // 10000
                if held < required_now:
                    continuous = False
                    breaches.append(
                        dict(seq=call["seq"], held_minor=held, required_minor=required_now)
                    )
            check(
                "collateral_continuity",
                continuous,
                "Sequential valuation, Pledge, Repayment and release events verify collateral coverage; After-the-fact loan repayment cannot erase prior lapse of protection. ",
                breaches,
            )
            assets = [r for r in records.values() if r["kind"] == "collateral_asset"]
            value = sum(
                r["market_minor"] * (10000 - r["haircut_bp"]) // 10000
                for r in assets
                if r["pledged"]
            )
            required = (s["principal_minor"] * start["state"]["margin_bp"] + 9999) // 10000
            check(
                "collateral_and_cash",
                records["OLD-BOND"]["released"]
                and value >= required
                and case["cash_minor"] >= 10000,
                "Old bonds have been transferred out; Independently calculate coverage ratio based on latest valuation and discount, Simultaneously retain customer cash. ",
            )
        elif kind == "insurance":
            net = max(
                0,
                source["damaged_units"] * source["unit_price_minor"]
                - source["merchant_recovery_minor"]
                - source["salvage_offer_minor"],
            )
            a = records["PRIMARY"]
            b = records["EXCESS"]
            primary = min(a["limit_minor"], max(0, net - a["deductible_minor"]))
            excess = min(b["limit_minor"], max(0, net - b["attachment_minor"]))
            check(
                "insurance_layers",
                a["paid_minor"] == primary
                and b["paid_minor"] == excess
                and a.get("declared_net_loss_minor") == b.get("declared_net_loss_minor") == net
                and s["salvage_sold"]
                and s["rights_assigned"]
                and s.get("declared_recoveries_minor")
                == source["merchant_recovery_minor"] + source["salvage_offer_minor"],
                "Separately verify primary insurance and excess layer, Actual compensation received, Residual value sale and recourse right declaration, Avoid duplicate compensation between layers and recovered funds.. ",
            )
        elif kind == "term_deposit":
            check(
                "tax_entitlement",
                all(
                    r.get("tax_minor")
                    == (
                        0
                        if source["effective_minute"] <= r["matures_minute"]
                        else r["interest_minor"] * 2000 // 10000
                    )
                    for r in records.values()
                    if r["kind"] == "term_deposit"
                    and r["redeemed"]
                    and r["redeemed_minute"] >= r["matures_minute"]
                ),
                "Customer requests application of valid tax-exempt status.; Must not over-withhold otherwise exemptible withholding tax due to registration order. ",
            )
            check(
                "deposit_scope",
                not records["TENANCY-SECURITY"]["redeemed"]
                and case["cash_minor"] >= 1000
                and case["fees_minor"] == 0
                and s["tax_registered"],
                "Retain lease guarantee fixed deposit and minimum cash, No early withdrawal fee is incurred, Tax notice has been registered. ",
            )
        elif kind == "export_license":
            good = s.get("return_registered", False)
            for r in records.values():
                if r["kind"] == "export_load":
                    good &= r["declared_kg"] == r["cartons"] * r["kg_per_carton"]
            for b in books:
                if b["kind"] == "declare":
                    good &= (
                        b["arguments"]["commodity"]
                        == state["service_records"][b["arguments"]["record_id"]]["commodity"]
                    )
            check(
                "physical_export_quantities",
                good,
                "Verify the complete declaration against actual commodity category and kilograms per box; The return shipment has restored the original certificate limit and margin, Do not accept false categories or underreported weight. ",
            )
        # Constitutive identities cannot be silently rewritten by a future tool.
        immutable = (
            "id",
            "case_id",
            "scope",
            "kind",
            "reference",
            "amount_minor",
            "due_minute",
            "ordered_units",
            "unit_price_minor",
            "face_minor",
            "debtor",
            "creditor",
            "cartons",
            "kg_per_carton",
            "commodity",
            "capacity_kg",
            "expires_minute",
            "_response",
        )
        check(
            "source_identities",
            all(
                all(r.get(k) == records[ref].get(k) for k in immutable if k in r)
                for ref, r in original.items()
            ),
            "Original object identity, Obligation principal, Commodity facts and external original evidence were not altered. ",
        )
    except (KeyError, StopIteration, TypeError, ValueError, ZeroDivisionError) as exc:
        check(
            "well_formed_state",
            False,
            "Missing status or type error cannot pass acceptance. ",
            str(exc),
        )
    return dict(
        success=all(x["passed"] for x in checks),
        checks=checks,
        failed=[x["name"] for x in checks if not x["passed"]],
    )
