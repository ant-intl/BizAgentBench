"""Product state transitions. No witness, verifier, admission or plan imports."""

from copy import deepcopy


def _case(e, cid, kind):
    c = e.row("service_cases", cid)
    e.check(c["kind"] == kind, "SERVICE_CONTRACT_MISMATCH")
    return c


def _book(e, c, action, data, before, receipt=None, fee=0):
    return e.book(
        c,
        action,
        contract=c["kind"],
        arguments=deepcopy(data),
        receipt=receipt or {},
        cash_delta_minor=c["cash_minor"] - before,
        cash_after_minor=c["cash_minor"],
        fee_minor=fee,
    )


def _event_book(e, c, event, receipt, before):
    p = e.w["_event_payloads"][event["id"]]
    return e.book(
        c,
        "bank_event",
        event_kind=p["event_kind"],
        event_id=event["id"],
        receipt=receipt,
        cash_delta_minor=c["cash_minor"] - before,
        cash_after_minor=c["cash_minor"],
    )


def _pay(e, c, r, n):
    e.positive(n)
    e.cash(c, -n)
    r["paid_minor"] += n
    r["payments"].append([e.w["minute"], n])


class PvPOperations:
    def settlement_currency(self, c, ccy, n):
        if ccy == "USD":
            self.cash(c, n)
        else:
            r = self.byref(c, ccy)
            self.check(r["balance_minor"] + n >= 0, "INSUFFICIENT_CURRENCY_CASH")
            r["balance_minor"] += n

    def pvp_instruction(self, case_id, action, data):
        c = _case(self, case_id, "queued_pvp_liquidity")
        before = c["cash_minor"]
        before_fees = c["fees_minor"]
        if action in ("settle", "release_gross"):
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "pvp_trade")
            self.check(r["status"] == "QUEUED" and r["counterparty_ready"], "TRADE_NOT_READY")
            self.settlement_currency(c, r["sell_currency"], -r["sell_minor"])
            r.update(
                rail="PVP" if action == "settle" else "GROSS",
                status="SETTLED" if action == "settle" else "OUTBOUND_FINAL",
                settled_minute=self.w["minute"] if action == "settle" else None,
                gross_pending=action != "settle",
            )
            if action == "settle":
                self.settlement_currency(c, r["buy_currency"], r["buy_minor"])
        elif action == "fund":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "fx_funding_ticket")
            self.check(
                not r["used"] and r["earliest_minute"] <= self.w["minute"] <= r["last_minute"],
                "FUNDING_UNAVAILABLE",
            )
            self.settlement_currency(c, r["sell_currency"], -r["sell_minor"])
            self.settlement_currency(c, r["buy_currency"], r["buy_minor"])
            self.cash(c, -r.get("usd_fee_minor", 0))
            c["fees_minor"] += r.get("usd_fee_minor", 0)
            r["used"] = True
        elif action == "pay":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "settlement_bill")
            n = data["amount_minor"]
            self.positive(n)
            self.settlement_currency(c, r["currency"], -n)
            r["paid_minor"] += n
            r["payments"].append([self.w["minute"], n])
        elif action == "place_term":
            self.args(data, "amount_minor")
            self.positive(data["amount_minor"])
            self.check(self.w["minute"] == 0, "PLACEMENT_WINDOW_CLOSED")
            self.cash(c, -data["amount_minor"])
            self.byref(c, "LOCK")["locked_minor"] += data["amount_minor"]
        else:
            self.check(False, "UNKNOWN_ACTION")
        return _book(self, c, action, data, before, fee=c["fees_minor"] - before_fees)

    def pvp_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        before = c["cash_minor"]
        p = self.w["_event_payloads"][event["id"]]
        out = {}
        if p["event_kind"] == "counterparty_confirmation":
            r = self.byref(c, "CONFIRM")
            actual = r["_actual_gbp_receipt_minor"]
            r.update(
                status="FINAL",
                actual_gbp_receipt_minor=actual,
                eur_incoming_minor=r["_actual_eur_incoming_minor"],
            )
            self.byref(c, "FX_B").update(buy_minor=actual, counterparty_ready=True)
            self.settlement_currency(c, "EUR", r["eur_incoming_minor"])
            out = dict(actual_gbp_receipt_minor=actual, eur_incoming_minor=r["eur_incoming_minor"])
        if p["event_kind"] in ("counterparty_confirmation", "payment_deadline", "term_release"):
            for r in self.records(c, "pvp_trade"):
                if r["gross_pending"]:
                    self.settlement_currency(c, r["buy_currency"], r["buy_minor"])
                    r.update(status="SETTLED", gross_pending=False, settled_minute=self.w["minute"])
        if p["event_kind"] == "term_release":
            r = self.byref(c, "LOCK")
            out["released_minor"] = r["locked_minor"]
            interest = r["locked_minor"] * r["interest_bp"] // 10000
            out["interest_minor"] = interest
            self.cash(c, r["locked_minor"] + interest)
            r["locked_minor"] = 0
            r["interest_paid_minor"] += interest
        return _event_book(self, c, event, out, before)


class DefaultOperationsBase:
    def default_instruction(self, case_id, action, data):
        c = _case(self, case_id, "participant_default_waterfall")
        before = c["cash_minor"]
        loss = self.byref(c, "LOSS")
        cover = self.byref(c, "COVER")
        bridge = self.byref(c, "BRIDGE")
        note = self.byref(c, "NOTE")
        if action == "pay":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"])
            self.check(
                r["kind"] in ("surviving_trade", "settlement_bill") and not r["closed_out"],
                "OBLIGATION_NOT_OPEN",
            )
            _pay(self, c, r, data["amount_minor"])
        elif action == "release_cover":
            self.args(data)
            self.check(self.w["minute"] == 0 and not cover["released"], "COVER_RELEASE_UNAVAILABLE")
            self.cash(c, cover["remaining_minor"])
            cover["returned_minor"] += cover["remaining_minor"]
            cover.update(remaining_minor=0, released=True)
            bridge["cancelled"] = True
        elif action == "contribute":
            self.args(data, "source", "amount_minor")
            n = data["amount_minor"]
            self.positive(n)
            self.check(
                loss["status"] == "ASSESSED" and n <= loss["assessment_minor"] - loss["paid_minor"],
                "ASSESSMENT_NOT_PAYABLE",
            )
            if data["source"] == "cash":
                self.cash(c, -n)
                loss["cash_paid_minor"] += n
            elif data["source"] == "fund":
                self.check(n <= cover["remaining_minor"], "INSUFFICIENT_POSTED_FUND")
                cover["remaining_minor"] -= n
                cover["used_minor"] += n
                loss["fund_paid_minor"] += n
            elif data["source"] == "note":
                self.check(
                    not cover["released"] and n <= note["remaining_minor"],
                    "INDEMNITY_NOTE_UNAVAILABLE",
                )
                note["remaining_minor"] -= n
                note["used_minor"] += n
                loss["note_paid_minor"] += n
            else:
                self.check(False, "INVALID_CONTRIBUTION_SOURCE")
            loss["paid_minor"] += n
        elif action == "restore_note":
            self.args(data, "amount_minor")
            n = data["amount_minor"]
            self.positive(n)
            self.check(n <= note["face_minor"] - note["remaining_minor"], "NOTE_ALREADY_WHOLE")
            self.cash(c, -n)
            note["remaining_minor"] += n
            note["replenished_minor"] += n
        elif action == "replacement_margin":
            self.args(data, "amount_minor")
            n = data["amount_minor"]
            self.positive(n)
            self.check(
                note["used_minor"] > 0
                and note["margin_paid_minor"] + n <= note["replacement_margin_minor"],
                "MARGIN_NOT_REQUIRED",
            )
            self.cash(c, -n)
            note["margin_paid_minor"] += n
        elif action in ("draw", "repay"):
            self.args(data, "amount_minor")
            n = data["amount_minor"]
            self.positive(n)
            if action == "draw":
                self.check(
                    self.w["minute"] >= bridge["available_from"]
                    and n <= bridge["limit_minor"] - bridge["drawn_minor"]
                    and (not bridge["cancelled"] or not bridge["requires_retained_cover"]),
                    "BRIDGE_UNAVAILABLE",
                )
                self.cash(c, n)
                bridge["drawn_minor"] += n
            else:
                self.check(n <= bridge["drawn_minor"] - bridge["repaid_minor"], "OVER_REPAYMENT")
                self.cash(c, -n)
                bridge["repaid_minor"] += n
        elif action == "accept_closeout":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "surviving_trade")
            self.check(
                r["closeout_receipt_minor"] > 0 and not r["closed_out"] and not r["paid_minor"],
                "CLOSEOUT_UNAVAILABLE",
            )
            r["closed_out"] = True
            self.cash(c, r["closeout_receipt_minor"])
        else:
            self.check(False, "UNKNOWN_ACTION")
        return _book(self, c, action, data, before)

    def default_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        before = c["cash_minor"]
        p = self.w["_event_payloads"][event["id"]]
        loss = self.byref(c, "LOSS")
        cover = self.byref(c, "COVER")
        note = self.byref(c, "NOTE")
        out = {}
        if p["event_kind"] == "liquidation_notice":
            gross = loss["_actual_gross_loss_minor"]
            assessment = (
                max(0, gross - loss["defaulter_collateral_minor"])
                * loss["member_weight"]
                // loss["total_survivor_weight"]
            )
            loss.update(
                status="ASSESSED", actual_gross_loss_minor=gross, assessment_minor=assessment
            )
            out = dict(gross_loss_minor=gross, assessment_minor=assessment)
        elif p["event_kind"] == "income_and_cover_release":
            recovery = self.byref(c, "RECOVERY")
            n = min(recovery["_actual_recovery_minor"], loss["paid_minor"])
            total = loss["paid_minor"]
            refunds = {
                s: (n * loss[s + "_paid_minor"] // total if total else 0)
                for s in ("cash", "fund", "note")
            }
            refunds["cash"] += n - sum(refunds.values())
            recovery.update(status="FINAL", actual_recovery_minor=n, refunds=refunds)
            self.cash(c, refunds["cash"])
            cover["remaining_minor"] += refunds["fund"]
            note["remaining_minor"] += refunds["note"]
            note["restored_minor"] += refunds["note"]
            income = self.byref(c, "INCOME")
            self.cash(c, income["amount_minor"])
            income["settled"] = True
            complete = (
                all(
                    r["paid_minor"] == r["amount_minor"]
                    or (r["closed_out"] and self.byref(c, "TERMS")["allow_cash_closeout"])
                    for r in self.records(c)
                    if r["kind"] in ("surviving_trade", "settlement_bill")
                )
                and loss["paid_minor"] == loss["assessment_minor"]
            )
            released = cover["remaining_minor"] if complete else 0
            margin = note["margin_paid_minor"] if complete else 0
            self.cash(c, released + margin)
            cover["remaining_minor"] -= released
            cover["returned_minor"] += released
            note["margin_returned_minor"] += margin
            out = dict(
                refunds=refunds,
                income_minor=income["amount_minor"],
                cover_released_minor=released,
                margin_released_minor=margin,
            )
        return _event_book(self, c, event, out, before)


class AssignmentOperationsBase:
    def assignment_instruction(self, case_id, action, data):
        c = _case(self, case_id, "documentary_proceeds_priority")
        before = c["cash_minor"]
        if action in ("assign", "transfer_performance"):
            self.args(data, "record_id", "recipient_id", "cap_minor")
            r = self.local(c, data["record_id"], "documentary_proceeds")
            recipient = self.local(c, data["recipient_id"])
            self.check(
                recipient["kind"] in ("proceeds_recipient", "supplier_debt")
                and r["assignment"] is None
                and self.w["minute"] <= r["assignment_cutoff_minute"],
                "ASSIGNMENT_WINDOW_OR_RECIPIENT",
            )
            self.positive(data["cap_minor"])
            self.check(data["cap_minor"] <= r["face_minor"], "CAP_EXCEEDS_FACE")
            r["assignment"] = dict(
                recipient_ref=recipient["reference"],
                cap_minor=data["cap_minor"],
                registered_minute=self.w["minute"],
            )
            if action == "transfer_performance":
                r.update(presentation_owner=recipient["name"], performance_transferred=True)
        elif action == "present":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "documentary_proceeds")
            self.check(
                not r["presented"]
                and r["presentation_owner"] == "Wren Exports"
                and self.w["minute"] < r["honor_minute"],
                "PRESENTATION_RIGHT_OR_DEADLINE",
            )
            r.update(presented=True, presentation_minute=self.w["minute"])
        elif action == "revoke_old":
            self.args(data)
            r = self.byref(c, "OLD_TRANSFER")
            self.check(
                r["revocable"] and not r["revoked"] and self.w["minute"] == 0,
                "REVOCATION_UNAVAILABLE",
            )
            r.update(revoked=True, accelerated_due_minute=0)
        elif action == "pay_lender":
            self.args(data, "amount_minor")
            r = self.byref(c, "OLD_TRANSFER")
            self.check(r["revoked"], "LENDER_CASH_NOT_DUE")
            n = data["amount_minor"]
            self.positive(n)
            self.cash(c, -n)
            r["cash_payoff_minor"] += n
            self.byref(c, "OLD_LENDER")["received_minor"] += n
        elif action == "pay":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"])
            self.check(r["kind"] in ("supplier_debt", "settlement_bill"), "WRONG_PAYABLE")
            _pay(self, c, r, data["amount_minor"])
        else:
            self.check(False, "UNKNOWN_ACTION")
        return _book(self, c, action, data, before)

    def assignment_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        before = c["cash_minor"]
        p = self.w["_event_payloads"][event["id"]]
        r = self.byref(c, p["document_ref"])
        actual = r["_actual_honor_minor"] if r["presented"] else 0
        r.update(
            actual_honor_minor=actual, status="HONORED" if r["presented"] else "NO_PRESENTATION"
        )
        remaining = actual
        distributions = []

        def send(ref, n):
            nonlocal remaining
            if not n:
                return
            payee = self.byref(c, ref)
            remaining -= n
            distributions.append(dict(recipient_ref=ref, amount_minor=n))
            if payee["kind"] == "supplier_debt":
                payee["paid_minor"] += n
                payee["payments"].append([self.w["minute"], n])
            else:
                payee["received_minor"] += n
                if payee["beneficiary_type"] == "company":
                    self.cash(c, n)

        if r["senior_recipient_ref"]:
            send(r["senior_recipient_ref"], min(remaining, r["senior_minor"]))
        old = self.byref(c, "OLD_TRANSFER")
        if old["document_ref"] == r["reference"] and not old["revoked"]:
            send(old["recipient_ref"], min(remaining, old["cap_minor"]))
        if r["assignment"]:
            send(r["assignment"]["recipient_ref"], min(remaining, r["assignment"]["cap_minor"]))
        send("COMPANY", remaining)
        r["distributed"] = distributions
        return _event_book(
            self,
            c,
            event,
            dict(
                document_ref=r["reference"], actual_honor_minor=actual, distributions=distributions
            ),
            before,
        )


class PoolOperationsBase:
    def pool_instruction(self, case_id, action, data):
        c = _case(self, case_id, "beneficial_sweep_restitution")
        before = c["cash_minor"]

        def move(src, dst, n):
            self.positive(n)
            s = self.byref(c, src)
            d = self.byref(c, dst)
            self.check(s["balance_minor"] >= n, "INSUFFICIENT_OWNER_CASH")
            s["balance_minor"] -= n
            d["balance_minor"] += n

        if action == "reverse":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "pool_sweep")
            n = data["amount_minor"]
            self.check(
                r["status"] == "POSTED" and n <= r["amount_minor"] - r["reversed_minor"],
                "SWEEP_NOT_REVERSIBLE",
            )
            move(r["target_ref"], r["source_ref"], n)
            r["reversed_minor"] += n
        elif action in ("lend", "repay"):
            self.args(data, "amount_minor")
            r = self.byref(c, "LOAN")
            n = data["amount_minor"]
            if action == "lend":
                self.check(
                    (not r["single_draw"] or not r["disbursed"])
                    and n <= r["limit_minor"] - r["drawn_minor"],
                    "LOAN_AUTHORITY_UNAVAILABLE",
                )
                move(r["lender_ref"], r["borrower_ref"], n)
                r["drawn_minor"] += n
                r["disbursed"] = True
            else:
                self.check(n <= r["drawn_minor"] - r["repaid_minor"], "OVER_REPAYMENT")
                move(r["borrower_ref"], r["lender_ref"], n)
                r["repaid_minor"] += n
        elif action == "pay":
            self.args(data, "record_id", "funding_id", "amount_minor")
            r = self.local(c, data["record_id"], "owned_bill")
            owner = self.local(c, data["funding_id"], "beneficial_ledger")
            n = data["amount_minor"]
            self.positive(n)
            self.check(owner["balance_minor"] >= n, "INSUFFICIENT_OWNER_CASH")
            owner["balance_minor"] -= n
            self.cash(c, -n)
            r["paid_minor"] += n
            r["payments"].append([self.w["minute"], n, owner["reference"]])
        elif action == "return_refund":
            self.args(data, "recipient_id", "amount_minor")
            owner = self.local(c, data["recipient_id"], "beneficial_ledger")
            r = self.byref(c, "REFUND")
            n = data["amount_minor"]
            self.positive(n)
            self.check(
                r["status"] == "CONFIRMED"
                and n <= c["state"]["suspense_minor"]
                and r["paid_minor"] == 0,
                "SUSPENSE_UNAVAILABLE",
            )
            self.cash(c, -n)
            c["state"]["suspense_minor"] -= n
            r["paid_minor"] += n
            r["recipient_ref"] = owner["reference"]
        else:
            self.check(False, "UNKNOWN_ACTION")
        return _book(self, c, action, data, before)

    def pool_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        before = c["cash_minor"]
        p = self.w["_event_payloads"][event["id"]]
        out = {}
        if p["event_kind"] == "pending_sweep_confirmation":
            r = self.byref(c, "PENDING")
            outcome = r["_actual_outcome"]
            r["status"] = outcome
            if outcome == "POSTED":
                self.byref(c, r["source_ref"])["balance_minor"] -= r["amount_minor"]
                self.byref(c, r["target_ref"])["balance_minor"] += r["amount_minor"]
            out = dict(sweep_ref=r["reference"], outcome=outcome)
        elif p["event_kind"] == "refund_source_confirmation":
            r = self.byref(c, "REFUND")
            r.update(status="CONFIRMED", actual_owner_ref=r["_actual_owner_ref"])
            self.cash(c, r["amount_minor"])
            c["state"]["suspense_minor"] += r["amount_minor"]
            out = dict(amount_minor=r["amount_minor"], owner_ref=r["actual_owner_ref"])
        elif p["event_kind"] == "borrower_income":
            r = self.byref(c, "INCOME")
            self.cash(c, r["amount_minor"])
            self.byref(c, r["owner_ref"])["balance_minor"] += r["amount_minor"]
            r["settled"] = True
            out = dict(owner_ref=r["owner_ref"], amount_minor=r["amount_minor"])
        return _event_book(self, c, event, out, before)


class DefaultOperations:
    def default_instruction(self, case_id, action, data):
        if action not in ("elect_recovery_cash", "apply_recovery"):
            return self.next_handler(DefaultOperations).default_instruction(case_id, action, data)
        c = _case(self, case_id, "participant_default_waterfall")
        before = c["cash_minor"]
        r = self.byref(c, "CERTIFICATE")
        if action == "elect_recovery_cash":
            self.args(data)
            self.check(
                r["mode"] is None and r["status"] in ("PENDING", "CONFIRMED"),
                "RECOVERY_ELECTION_CLOSED",
            )
            r["mode"] = "CASH"
        else:
            self.args(data, "record_id", "amount_minor")
            claim = self.local(c, data["record_id"])
            n = data["amount_minor"]
            self.positive(n)
            self.check(
                r["status"] == "CONFIRMED"
                and r["mode"] in (None, "SETTLEMENT")
                and claim["reference"] == r["actual_claim_ref"],
                "RECOVERY_CLAIM_NOT_ELIGIBLE",
            )
            self.check(
                n <= r["amount_minor"] - r["applied_minor"]
                and n <= claim["amount_minor"] - claim["paid_minor"],
                "RECOVERY_AMOUNT_UNAVAILABLE",
            )
            r["mode"] = "SETTLEMENT"
            r["applied_minor"] += n
            claim["paid_minor"] += n
            claim["payments"].append([self.w["minute"], n])
        return _book(self, c, action, data, before)

    def default_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        r = self.byref(c, "CERTIFICATE")
        if p["event_kind"] == "settlement_deadline":
            before = c["cash_minor"]
            r.update(status="CONFIRMED", actual_claim_ref=r["_actual_claim_ref"])
            return _event_book(
                self,
                c,
                event,
                dict(
                    recovery_claim_ref=r["actual_claim_ref"],
                    recovery_amount_minor=r["amount_minor"],
                ),
                before,
            )
        result = self.next_handler(DefaultOperations).default_event(event)
        if p["event_kind"] == "income_and_cover_release":
            n = r["amount_minor"] - r["applied_minor"]
            self.cash(c, n)
            r.update(cash_received_minor=n, status="REDEEMED")
            booking = self.w["service_bookings"][result["id"]]
            booking["receipt"]["certificate_cash_minor"] = n
            booking["cash_delta_minor"] += n
            booking["cash_after_minor"] = c["cash_minor"]
            return self.public(booking)
        return result


class AssignmentOperations:
    def assignment_instruction(self, case_id, action, data):
        if (
            action == "pay"
            and data.get("record_id")
            == self.byref(self.row("service_cases", case_id), "SUPPLIER")["id"]
        ):
            return self.assignment_instruction(
                case_id,
                "pay_claim",
                dict(recipient_id=data["record_id"], amount_minor=data["amount_minor"]),
            )
        if action != "pay_claim":
            return self.next_handler(AssignmentOperations).assignment_instruction(
                case_id, action, data
            )
        c = _case(self, case_id, "documentary_proceeds_priority")
        before = c["cash_minor"]
        self.args(data, "recipient_id", "amount_minor")
        recipient = self.local(c, data["recipient_id"])
        sup = self.byref(c, "SUPPLIER")
        n = data["amount_minor"]
        self.positive(n)
        self.check(
            recipient["reference"] in ("SUPPLIER", "FACTOR")
            and n <= self.byref(c, "TERMS")["supplier_cash_limit_minor"] - sup["cash_paid_minor"],
            "CASH_MANDATE_LIMIT",
        )
        self.cash(c, -n)
        sup["cash_paid_minor"] += n
        if recipient["reference"] == sup["current_holder_ref"]:
            sup["paid_minor"] += n
            sup["payments"].append([self.w["minute"], n])
        if recipient["reference"] == "FACTOR":
            recipient["received_minor"] += n
        return _book(self, c, action, data, before)

    def assignment_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        before = c["cash_minor"]
        p = self.w["_event_payloads"][event["id"]]
        r = self.byref(c, p["document_ref"])
        sup = self.byref(c, "SUPPLIER")
        notice = self.byref(c, "SUPPLIER_NOTICE")
        out = {}
        if r["reference"] == "DOC_A":
            credit = notice["_actual_credit_minor"]
            holder = notice["_actual_holder_ref"]
            notice.update(status="FINAL", actual_credit_minor=credit, actual_holder_ref=holder)
            sup.update(
                amount_minor=sup["original_amount_minor"] - credit,
                credit_note_minor=credit,
                current_holder_ref=holder,
            )
            out["supplier_notice"] = dict(credit_minor=credit, holder_ref=holder)
        actual = r["_actual_honor_minor"] if r["presented"] else 0
        r.update(
            actual_honor_minor=actual, status="HONORED" if r["presented"] else "NO_PRESENTATION"
        )
        remaining = actual
        alloc = []
        claims = []
        if r["senior_recipient_ref"]:
            claims.append((r["senior_recipient_ref"], r["senior_minor"]))
        old = self.byref(c, "OLD_TRANSFER")
        if old["document_ref"] == r["reference"] and not old["revoked"]:
            claims.append((old["recipient_ref"], old["cap_minor"]))
        if r["assignment"]:
            claims.append((r["assignment"]["recipient_ref"], r["assignment"]["cap_minor"]))
        claims.append(("COMPANY", remaining))
        for ref, cap in claims:
            n = min(cap, remaining)
            remaining -= n
            if not n:
                continue
            alloc.append(dict(recipient_ref=ref, amount_minor=n))
            payee = self.byref(c, ref)
            if ref in ("SUPPLIER", "FACTOR"):
                if ref == sup["current_holder_ref"]:
                    sup["paid_minor"] += n
                    sup["payments"].append([self.w["minute"], n])
                if ref == "FACTOR":
                    payee["received_minor"] += n
            else:
                payee["received_minor"] += n
                if ref == "COMPANY":
                    self.cash(c, n)
        r["distributed"] = alloc
        out.update(document_ref=r["reference"], actual_honor_minor=actual, distributions=alloc)
        return _event_book(self, c, event, out, before)


class PoolOperations:
    def pool_instruction(self, case_id, action, data):
        if action == "reverse":
            return self.next_handler(PoolOperations).pool_instruction(case_id, action, data)
        c = _case(self, case_id, "beneficial_sweep_restitution")
        before = c["cash_minor"]
        fee = 0
        if action in ("lend", "repay"):
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "bilateral_loan")
            n = data["amount_minor"]
            self.positive(n)
            if action == "lend":
                self.check(
                    (not r["single_draw"] or not r["disbursed"])
                    and n <= r["limit_minor"] - r["drawn_minor"],
                    "LOAN_AUTHORITY_UNAVAILABLE",
                )
                src, dst = r["lender_ref"], r["borrower_ref"]
                r["drawn_minor"] += n
                r["disbursed"] = True
            else:
                self.check(n <= r["drawn_minor"] - r["repaid_minor"], "OVER_REPAYMENT")
                src, dst = r["borrower_ref"], r["lender_ref"]
                r["repaid_minor"] += n
            s = self.byref(c, src)
            target = self.byref(c, dst)
            self.check(s["balance_minor"] >= n, "INSUFFICIENT_OWNER_CASH")
            s["balance_minor"] -= n
            target["balance_minor"] += n
        elif action == "pay":
            self.args(data, "record_id", "funding_id", "amount_minor")
            r = self.local(c, data["record_id"])
            self.check(r["kind"] in ("owned_bill", "owned_bank_debt"), "PAYABLE_TYPE")
            owner = self.local(c, data["funding_id"], "beneficial_ledger")
            n = data["amount_minor"]
            self.positive(n)
            self.check(owner["balance_minor"] >= n, "INSUFFICIENT_OWNER_CASH")
            owner["balance_minor"] -= n
            self.cash(c, -n)
            r["paid_minor"] += n
            r["payments"].append([self.w["minute"], n, owner["reference"]])
        elif action == "return_refund":
            self.args(data, "recipient_id", "amount_minor", "rail")
            recipient = self.local(c, data["recipient_id"], "beneficial_ledger")
            r = self.byref(c, "REFUND")
            owner = self.byref(c, r["actual_owner_ref"])
            n = data["amount_minor"]
            self.positive(n)
            self.check(
                r["status"] == "CONFIRMED"
                and r["rail"] is None
                and data["rail"] in ("STANDARD", "EXPRESS"),
                "RETURN_NOT_AVAILABLE",
            )
            fee = r["express_fee_minor"] if data["rail"] == "EXPRESS" else 0
            self.check(owner["balance_minor"] >= n + fee, "INSUFFICIENT_OWNER_CASH")
            owner["balance_minor"] -= n + fee
            r.update(rail=data["rail"], recipient_ref=recipient["reference"], fee_paid_minor=fee)
            if data["rail"] == "STANDARD":
                c["state"]["suspense_minor"] += n
                r.update(reserved_minor=n, status="RETURN_QUEUED")
            else:
                self.cash(c, -n - fee)
                c["fees_minor"] += fee
                r.update(paid_minor=n, status="RETURNED", paid_minute=self.w["minute"])
        else:
            self.check(False, "UNKNOWN_ACTION")
        return _book(self, c, action, data, before, fee=fee)

    def pool_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        before = c["cash_minor"]
        p = self.w["_event_payloads"][event["id"]]
        out = {}
        if p["event_kind"] == "pending_sweep_confirmation":
            return self.next_handler(PoolOperations).pool_event(event)
        if p["event_kind"] == "refund_source_confirmation":
            r = self.byref(c, "REFUND")
            r.update(status="CONFIRMED", actual_owner_ref=r["_actual_owner_ref"])
            self.cash(c, r["amount_minor"])
            self.byref(c, r["actual_owner_ref"])["balance_minor"] += r["amount_minor"]
            out = dict(amount_minor=r["amount_minor"], owner_ref=r["actual_owner_ref"])
        elif p["event_kind"] == "borrower_income":
            r = self.byref(c, "INCOME")
            n = r["installment_minor"]
            self.cash(c, n)
            self.byref(c, r["owner_ref"])["balance_minor"] += n
            r["received_minor"] += n
            r["settled"] = r["received_minor"] == r["amount_minor"]
            out = dict(owner_ref=r["owner_ref"], amount_minor=n)
            refund = self.byref(c, "REFUND")
            if refund["status"] == "RETURN_QUEUED":
                amount = refund["reserved_minor"]
                self.cash(c, -amount)
                c["state"]["suspense_minor"] -= amount
                refund.update(
                    reserved_minor=0,
                    paid_minor=amount,
                    status="RETURNED",
                    paid_minute=self.w["minute"],
                )
                out["buyer_return_minor"] = amount
        else:
            self.check(False, "UNKNOWN_EVENT")
        return _event_book(self, c, event, out, before)
