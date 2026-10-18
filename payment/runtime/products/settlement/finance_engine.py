"""Contract execution. This module never imports a strategy or verifier."""

from copy import deepcopy


class FinanceOperations:
    def _finance_start(self, case_id, kind):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == kind, "SERVICE_CONTRACT_MISMATCH")
        return c, c["cash_minor"], c["fees_minor"]

    def _finance_book(self, c, action, data, before, fees):
        return self.book(
            c,
            action,
            contract=c["kind"],
            arguments=deepcopy(data),
            receipt=deepcopy(data),
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
            fee_minor=c["fees_minor"] - fees,
        )

    def _finance_pay(self, c, data):
        self.args(data, "record_id", "amount_minor")
        r = self.local(c, data["record_id"], "finance_bill")
        n = data["amount_minor"]
        self.positive(n)
        self.cash(c, -n)
        r["paid_minor"] += n
        r["payments"].append([self.w["minute"], n])

    def jv_instruction(self, case_id, action, data):
        c, b, f = self._finance_start(case_id, "jv_default_equalization")
        m = self.w["minute"]
        terms = self.byref(c, "TERMS")
        if action == "fund_rescue":
            self.args(data, "record_id", "amount_minor", "mode")
            r = self.local(c, data["record_id"], "rescue_commitment")
            n = data["amount_minor"]
            mode = data["mode"]
            self.positive(n)
            member = self.byref(c, r["creditor_ref"])
            debtor = self.byref(c, r["debtor_ref"])
            allrows = self.records(c, "rescue_commitment")
            self.check(
                m == 0
                and r["funded_minor"] == 0
                and n % terms["funding_increment_minor"] == 0
                and mode in ("LONG", "SHORT", "CAPITAL"),
                "RESCUE_UNAVAILABLE",
            )
            self.check(
                n
                + sum(x["funded_minor"] for x in allrows if x["creditor_ref"] == r["creditor_ref"])
                <= member["limit_minor"]
                and n
                + sum(x["funded_minor"] for x in allrows if x["debtor_ref"] == r["debtor_ref"])
                <= debtor["call_minor"],
                "SOURCE_OR_CALL_CAPACITY_EXCEEDED",
            )
            r.update(funded_minor=n, mode=mode)
            self.cash(c, n)
            if mode == "LONG":
                self.charge(c, r["fee_minor"])
            elif mode == "SHORT":
                self.cash(c, r["rebate_minor"])
            else:
                member["issued_units"] += n // 1000
                member["capital_paid_minor"] += n
        elif action == "return_bridge":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "rescue_commitment")
            n = data["amount_minor"]
            self.positive(n)
            self.check(
                r["mode"] in ("LONG", "SHORT")
                and n <= r["funded_minor"] - r["returned_minor"] - r["assigned_minor"]
                and not r["novations"],
                "BRIDGE_RETURN_EXCEEDS_DEBT",
            )
            self.cash(c, -n)
            r["returned_minor"] += n
        elif action == "novate_claim":
            self.args(data, "record_id", "creditor_record_id", "amount_minor")
            r = self.local(c, data["record_id"], "rescue_commitment")
            owner = self.local(c, data["creditor_record_id"], "rescue_member")
            n = data["amount_minor"]
            self.positive(n)
            self.check(
                r["mode"] == "LONG"
                and self.byref(c, r["debtor_ref"])["status"] == "FINAL"
                and owner["reference"] != r["creditor_ref"]
                and not r["assigned_minor"]
                and n <= r["funded_minor"] - r["returned_minor"] - sum(r["novations"].values()),
                "NOVATION_UNAVAILABLE",
            )
            self.charge(c, terms["novation_fee_minor"])
            r["novations"][owner["reference"]] = r["novations"].get(owner["reference"], 0) + n
        elif action == "assign_member_debt":
            self.args(data, "record_id", "creditor_record_id", "amount_minor")
            r = self.local(c, data["record_id"], "rescue_commitment")
            owner = self.local(c, data["creditor_record_id"], "rescue_member")
            n = data["amount_minor"]
            self.positive(n)
            debtor = self.byref(c, r["debtor_ref"])
            ref = owner["reference"]
            entitled = (
                r["funded_minor"] - r["returned_minor"] - sum(r["novations"].values())
                if ref == r["creditor_ref"]
                else r["novations"].get(ref, 0)
            )
            self.check(
                r["mode"] == "LONG"
                and debtor["status"] == "FINAL"
                and n <= entitled - r["assignments"].get(ref, 0)
                and n + debtor["debts"].get(ref, 0)
                <= owner["claim_limits_minor"][debtor["reference"]],
                "ASSIGNMENT_UNAVAILABLE",
            )
            r["assigned_minor"] += n
            r["assignments"][ref] = r["assignments"].get(ref, 0) + n
            debtor["debts"][ref] = debtor["debts"].get(ref, 0) + n
        elif action == "restore_equity":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "rescue_commitment")
            member = self.byref(c, r["creditor_ref"])
            self.check(
                terms["equity_restoration_allowed"] and r["mode"] == "CAPITAL",
                "EQUITY_RESCISSION_UNAVAILABLE",
            )
            member["issued_units"] -= r["funded_minor"] // 1000
            member["capital_paid_minor"] -= r["funded_minor"]
            r["mode"] = "LONG"
        elif action == "pay":
            self._finance_pay(c, data)
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self._finance_book(c, action, data, b, f)

    def jv_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        before = c["cash_minor"]
        out = {}
        if p["event_kind"] == "capital_cure":
            notice = self.byref(c, "CURES")
            cures = notice["possible_profiles"][notice["_profile"]]
            notice["status"] = "FINAL"
            for ref, n in cures.items():
                self.byref(c, ref).update(actual_cure_minor=n, capital_paid_minor=n, status="FINAL")
            inc = self.byref(c, "INCOME")
            inc["received_minor"] = inc["amount_minor"]
            self.cash(c, sum(cures.values()) + inc["amount_minor"])
            out = dict(cures_minor=deepcopy(cures), operating_minor=inc["amount_minor"])
        else:
            self.check(
                p["event_kind"] in ("project_deadline", "equalization_deadline"), "UNKNOWN_EVENT"
            )
        return self.book(
            c,
            "bank_event",
            event_id=event["id"],
            event_kind=p["event_kind"],
            receipt=out,
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
        )

    def syndicated_instruction(self, case_id, action, data):
        c, b, f = self._finance_start(case_id, "syndicated_transfer_allocation")
        m = self.w["minute"]
        loan = self.byref(c, "LOAN")
        if action == "elect_support":
            self.args(data, "record_id", "mode")
            r = self.local(c, data["record_id"], "participation_transfer")
            mode = data["mode"]
            self.check(
                m == 0 and r["mode"] is None and mode in ("STANDBY", "MARGIN", "RELEASE"),
                "SUPPORT_ELECTION_UNAVAILABLE",
            )
            r["mode"] = mode
            if mode == "STANDBY":
                self.charge(c, r["standby_fee_minor"])
            elif mode == "MARGIN":
                self.cash(c, -r["margin_minor"])
                self.charge(c, r["margin_fee_minor"])
                r["margin_locked_minor"] = r["margin_minor"]
            else:
                self.cash(c, r["release_rebate_minor"])
        elif action == "accelerate_transfer":
            self.args(data, "record_id", "fee_minor")
            r = self.local(c, data["record_id"], "participation_transfer")
            self.check(
                m == 1
                and r["registered_effective_minute"] == 3
                and not r["expedited"]
                and r["mode"] in ("STANDBY", "MARGIN")
                and data["fee_minor"] == r["expedite_fee_minor"],
                "EXPEDITED_SETTLEMENT_UNAVAILABLE",
            )
            self.charge(c, data["fee_minor"])
            r.update(expedited=True, effective_minute=2, expedite_paid_minor=data["fee_minor"])
        elif action == "fund_draw":
            self.args(data, "record_id", "amount_minor", "draw", "source")
            r = self.local(c, data["record_id"], "syndicate_lender")
            n = data["amount_minor"]
            self.positive(n)
            draw = data["draw"]
            src = data["source"]
            self.check(
                draw in ("FIRST", "SECOND")
                and ((draw == "FIRST" and m == 0) or (draw == "SECOND" and m == 2))
                and src in ("CAPITAL", "STANDBY"),
                "DRAW_WINDOW_OR_SOURCE_INVALID",
            )
            field = "funded_" + draw.lower() + "_minor"
            cap = loan[draw.lower() + "_minor"]
            self.check(loan[field] + n <= cap, "DRAW_EXCEEDS_CONTRACT")
            if src == "CAPITAL":
                self.check(n <= r["capacity_minor"] - r["funded_minor"], "LENDER_CAPACITY_EXCEEDED")
                r["funded_minor"] += n
                self.cash(c, n)
            elif src == "STANDBY":
                tr = next(
                    x
                    for x in self.records(c, "participation_transfer")
                    if x["seller_ref"] == r["reference"]
                )
                self.check(
                    draw == "SECOND"
                    and tr["mode"] in ("STANDBY", "MARGIN")
                    and n <= tr["standby_limit_minor"] - r["bridge_funded_minor"]
                    and self.byref(c, "POOL")["used_minor"] + n
                    <= self.byref(c, "POOL")["limit_minor"],
                    "STANDBY_UNAVAILABLE",
                )
                r["bridge_funded_minor"] += n
                self.byref(c, "POOL")["used_minor"] += n
                self.cash(c, n)
            loan[field] += n
            loan["principal_outstanding_minor"] += n
        elif action in ("pay_principal", "pay_fee"):
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "syndicate_lender")
            n = data["amount_minor"]
            self.positive(n)
            self.cash(c, -n)
            r["principal_paid_minor" if action == "pay_principal" else "fee_paid_minor"] += n
        elif action == "pay":
            self._finance_pay(c, data)
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self._finance_book(c, action, data, b, f)

    def syndicated_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        ek = self.w["_event_payloads"][event["id"]]["event_kind"]
        m = self.w["minute"]
        before = c["cash_minor"]
        out = {}
        loan = self.byref(c, "LOAN")
        if ek == "consent_register":
            dates = {}
            for tr in self.records(c, "participation_transfer"):
                tr["effective_minute"] = tr["_effective_minute"]
                tr["registered_effective_minute"] = tr["_effective_minute"]
                tr["expedite_fee_minor"] = self.byref(c, "QUOTES")["possible_profiles"][
                    self.byref(c, "QUOTES")["_profile"]
                ][tr["reference"]]
                dates[tr["reference"]] = tr["effective_minute"]
                if tr["effective_minute"] == 1:
                    self.byref(c, tr["seller_ref"])["units"] -= tr["units"]
                    self.byref(c, tr["buyer_ref"])["units"] += tr["units"]
                    tr["settled"] = True
            self.byref(c, "QUOTES")["status"] = "FINAL"
            out = dict(
                effective_minutes=dates,
                expedite_quotes_minor={
                    tr["reference"]: tr["expedite_fee_minor"]
                    for tr in self.records(c, "participation_transfer")
                },
            )
        elif ek == "second_draw_deadline":
            settled = []
            for tr in self.records(c, "participation_transfer"):
                if not tr["settled"] and tr["effective_minute"] == 2:
                    self.byref(c, tr["seller_ref"])["units"] -= tr["units"]
                    self.byref(c, tr["buyer_ref"])["units"] += tr["units"]
                    tr["settled"] = True
                    settled.append(tr["reference"])
            out = dict(expedited_settlements=sorted(settled))
        elif ek == "transfer_completion":
            bridges = {}
            margin = 0
            for tr in self.records(c, "participation_transfer"):
                if not tr["settled"]:
                    self.byref(c, tr["seller_ref"])["units"] -= tr["units"]
                    self.byref(c, tr["buyer_ref"])["units"] += tr["units"]
                    tr["settled"] = True
                seller = self.byref(c, tr["seller_ref"])
                seller["bridge_repaid_minor"] = seller["bridge_funded_minor"]
                tr["bridge_reimbursed_minor"] = seller["bridge_funded_minor"]
                bridges[tr["reference"]] = tr["bridge_reimbursed_minor"]
                margin += tr["margin_locked_minor"]
                tr["margin_locked_minor"] = 0
            self.byref(c, "POOL")["repaid_minor"] = self.byref(c, "POOL")["used_minor"]
            self.cash(c, margin)
            out = dict(bridge_reimbursements=bridges, margin_released_minor=margin)
        elif ek == "borrower_payoff":
            fees = {r["reference"]: 0 for r in self.records(c, "syndicate_lender")}
            for minute in range(4):
                holdings = {
                    r["reference"]: r["initial_units"] for r in self.records(c, "syndicate_lender")
                }
                for tr in self.records(c, "participation_transfer"):
                    if tr["effective_minute"] <= minute:
                        holdings[tr["seller_ref"]] -= tr["units"]
                        holdings[tr["buyer_ref"]] += tr["units"]
                principal = loan["funded_first_minor"] + (
                    loan["funded_second_minor"] if minute >= 2 else 0
                )
                for ref, units in holdings.items():
                    fees[ref] += principal * units * loan["fee_rate_bp_per_minute"] // 1000000
            n = loan["principal_outstanding_minor"]
            loan.update(
                principal_outstanding_minor=0,
                borrower_repaid_minor=n,
                fee_received_minor=sum(fees.values()),
                _fee_due=fees,
            )
            self.cash(c, n + sum(fees.values()))
            out = dict(principal_minor=n, fee_total_minor=sum(fees.values()))
        else:
            self.check(ek == "second_draw_deadline", "UNKNOWN_EVENT")
        return self.book(
            c,
            "bank_event",
            event_id=event["id"],
            event_kind=ek,
            receipt=out,
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
        )

    def _borrowing_base(self, c):
        buckets = {}
        caps = {}
        for r in self.records(c, "borrowing_receivable"):
            buckets[r["debtor"]] = (
                buckets.get(r["debtor"], 0) + r["face_minor"] * r["advance_bp"] // 10000
            )
            caps[r["debtor"]] = r["debtor_cap_minor"]
        return max(
            0,
            sum(min(n, caps[d]) for d, n in buckets.items())
            - self.byref(c, "NOTICE")["reserve_minor"],
        )

    def borrowing_instruction(self, case_id, action, data):
        c, b, f = self._finance_start(case_id, "borrowing_base_retirement")
        m = self.w["minute"]
        new = self.byref(c, "NEW")
        if action == "draw":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "borrowing_line")
            n = data["amount_minor"]
            self.positive(n)
            self.check(not r["retired"], "FACILITY_RETIRED")
            if r["reference"] == "NEW":
                self.check(
                    m <= r["draw_close_minute"] and r["debt_minor"] + n <= self._borrowing_base(c),
                    "BORROWING_BASE_OR_DRAW_WINDOW_EXCEEDED",
                )
            else:
                self.check(n <= r["limit_minor"] - r["drawn_minor"], "OLD_DRAW_EXCEEDED")
                r["lock_through_minute"] = r["redraw_lock_minute"]
            r["drawn_minor"] += n
            r["debt_minor"] += n
            self.cash(c, n)
        elif action == "repay_cash":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "borrowing_line")
            n = data["amount_minor"]
            self.positive(n)
            self.check(n <= r["debt_minor"], "EXCESS_REPAYMENT")
            self.cash(c, -n)
            r["debt_minor"] -= n
            r["repaid_minor"] += n
        elif action == "retire_old":
            self.args(data)
            r = self.byref(c, "OLD")
            self.check(
                r["debt_minor"] == 0 and not r["retired"] and m >= r["lock_through_minute"],
                "LEGACY_RETIREMENT_UNAVAILABLE",
            )
            r.update(retired=True, retired_minute=m)
            self.byref(c, "TERMS")["old_lien_released"] = True
        elif action == "apply_supplier_credit":
            self.args(data, "record_id", "bill_record_id", "amount_minor")
            r = self.local(c, data["record_id"], "supplier_credit")
            bill = self.local(c, data["bill_record_id"], "finance_bill")
            n = data["amount_minor"]
            self.positive(n)
            self.check(
                r["bill_ref"] == bill["reference"]
                and n <= r["received_minor"] - r["applied_minor"],
                "SUPPLIER_CREDIT_UNAVAILABLE",
            )
            r["applied_minor"] += n
        elif action == "sell_receivable":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "borrowing_receivable")
            self.check(
                m == 0 and not r["sold"] and r["sale_minor"] > 0, "RECEIVABLE_SALE_UNAVAILABLE"
            )
            r["sold"] = True
            r["face_minor"] -= r["sale_minor"]
            self.cash(c, r["sale_price_minor"])
        elif action in ("apply_collection", "release_collection"):
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "borrowing_receivable")
            n = data["amount_minor"]
            self.positive(n)
            self.check(n <= r["controlled_minor"], "CONTROLLED_PROCEEDS_EXCEEDED")
            r["controlled_minor"] -= n
            if action == "apply_collection":
                self.check(n <= new["debt_minor"], "EXCESS_REPAYMENT")
                new["debt_minor"] -= n
                new["repaid_minor"] += n
                r["applied_minor"] += n
            else:
                self.cash(c, n)
                if r["released_minor"] == 0:
                    self.charge(c, r["release_fee_minor"])
                r["released_minor"] += n
        elif action == "pay":
            self._finance_pay(c, data)
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self._finance_book(c, action, data, b, f)

    def borrowing_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        ek = self.w["_event_payloads"][event["id"]]["event_kind"]
        before = c["cash_minor"]
        out = {}
        if ek == "debtor_collections":
            got = {}
            for r in self.records(c, "borrowing_receivable"):
                n = r["collection_minor"] - (r["sale_minor"] if r["sold"] else 0)
                r["face_minor"] -= n
                r["collected_minor"] = n
                r["controlled_minor"] = n
                got[r["reference"]] = n
            notice = self.byref(c, "NOTICE")
            notice.update(reserve_minor=notice["_actual_reserve_minor"], status="FINAL")
            self.byref(c, "CREDIT")["received_minor"] = notice["reserve_minor"]
            out = dict(
                collections_minor=got,
                reserve_minor=notice["reserve_minor"],
                supplier_credit_minor=notice["reserve_minor"],
            )
        else:
            self.check(ek in ("inventory_deadline", "covenant_deadline"), "UNKNOWN_EVENT")
        return self.book(
            c,
            "bank_event",
            event_id=event["id"],
            event_kind=ek,
            receipt=out,
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
        )
