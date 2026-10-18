from copy import deepcopy


class CustodyNdfOperations:
    def custody_ndf_cover(self, c, key):
        return self.byref(c, "MARGIN_" + key)["cash_minor"] + sum(
            r["collateral_value_minor"]
            for r in self.records(c, "collateral_bond")
            if r["pledged_to"] == key and r["confirmed"]
        )

    def custody_ndf_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "custody_ndf_segregation", "SERVICE_CONTRACT_MISMATCH")
        cash = c["cash_minor"]
        fees = c["fees_minor"]
        m = self.w["minute"]
        out = {}
        if action in ("pledge", "release_bond", "deliver_bond"):
            self.args(data, *(["record_id", "set_id"] if action == "pledge" else ["record_id"]))
            r = self.local(c, data["record_id"], "collateral_bond")
            if action == "pledge":
                s = self.local(c, data["set_id"], "ndf_netting_set")
                self.check(
                    r["settled"]
                    and r["pledged_to"] is None
                    and not r["repo"]
                    and not r["delivered"]
                    and (r["registered"] or m <= r["registration_deadline"]),
                    "BOND_UNAVAILABLE_OR_REGISTRATION_CLOSED",
                )
                r.update(
                    pledged_to=s["set_key"], confirmed=m >= 1, pledged_minute=m, registered=True
                )
            elif action == "release_bond":
                key = r["pledged_to"]
                self.check(key is not None and m >= r["release_minute"], "BOND_RELEASE_LOCKED")
                margin = self.byref(c, "MARGIN_" + key)
                self.check(
                    m >= margin["release_minute"]
                    or margin["settlement_release_status"] == "AVAILABLE"
                    or self.custody_ndf_cover(c, key) - r["collateral_value_minor"]
                    >= margin["required_minor"],
                    "INSUFFICIENT_CONTINUING_COVER",
                )
                r.update(pledged_to=None, confirmed=False)
            else:
                self.check(
                    r["settled"]
                    and r["pledged_to"] is None
                    and not r["repo"]
                    and not r["delivered"],
                    "DELIVERY_BOND_UNAVAILABLE",
                )
                r.update(delivered=True, delivered_minute=m)
            out = dict(
                bond_ref=r["reference"], pledged_to=r["pledged_to"], delivered=r["delivered"]
            )
        elif action in ("deposit_margin", "withdraw_margin"):
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "segregated_margin")
            n = data["amount_minor"]
            self.positive(n)
            if action == "deposit_margin":
                self.cash(c, -n)
                r["cash_minor"] += n
            else:
                self.check(
                    m >= r["confirmation_minute"]
                    and n <= r["cash_minor"]
                    and (
                        m >= r["release_minute"]
                        or r["settlement_release_status"] == "AVAILABLE"
                        or self.custody_ndf_cover(c, r["set_key"]) - n >= r["required_minor"]
                    ),
                    "MARGIN_NOT_RELEASEABLE",
                )
                self.cash(c, n)
                r["cash_minor"] -= n
                r["withdrawn_minor"] += n
            out = dict(set_key=r["set_key"], amount_minor=n)
        elif action in ("draw_repo", "repay_repo"):
            self.args(data)
            r = self.byref(c, "REPO")
            bond = self.byref(c, r["asset_ref"])
            if action == "draw_repo":
                self.check(
                    not r["drawn"]
                    and bond["pledged_to"] is None
                    and not bond["delivered"]
                    and m == 0,
                    "REPO_UNAVAILABLE",
                )
                self.cash(c, r["advance_minor"])
                self.charge(c, r["fee_minor"])
                r["drawn"] = True
                bond["repo"] = True
            else:
                self.check(r["drawn"] and not r["repaid"], "REPO_NOT_OUTSTANDING")
                self.cash(c, -r["advance_minor"])
                r["repaid"] = True
                bond["repo"] = False
            out = dict(advance_minor=r["advance_minor"])
        elif action in ("draw_bridge", "repay_bridge"):
            self.args(data, "amount_minor")
            n = data["amount_minor"]
            self.positive(n)
            r = self.byref(c, "BRIDGE")
            if action == "draw_bridge":
                self.check(
                    m >= r["open_minute"] and not r["drawn_minor"] and n <= r["limit_minor"],
                    "BRIDGE_UNAVAILABLE",
                )
                self.cash(c, n)
                r["drawn_minor"] = n
                r["fee_charged_minor"] = r["fee_minor"]
            else:
                self.check(
                    m >= r["repayment_minute"]
                    and n == r["drawn_minor"] - r["repaid_minor"]
                    and n > 0,
                    "INVALID_BRIDGE_REPAYMENT",
                )
                self.cash(c, -n)
                self.charge(c, r["fee_charged_minor"])
                r["repaid_minor"] += n
            out = dict(amount_minor=n)
        elif action == "pay_settlement":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "ndf_netting_set")
            n = data["amount_minor"]
            self.positive(n)
            self.cash(c, -n)
            r["paid_minor"] += n
            r["paid_minute"] = m
            if r["status"] == "FINAL" and r["paid_minor"] == r["settlement_due_minor"]:
                r["release_notice"] = r["_release_style"]
                self.byref(c, "MARGIN_" + r["set_key"])["settlement_release_status"] = (
                    "AVAILABLE" if r["_release_style"] == "SAME_DAY" else "HELD"
                )
            out = dict(
                set_key=r["set_key"], amount_minor=n, collateral_release_notice=r["release_notice"]
            )
        elif action == "pay_bill":
            self.args(data, "amount_minor")
            n = data["amount_minor"]
            self.positive(n)
            self.cash(c, -n)
            r = self.byref(c, "BILL")
            r["paid_minor"] += n
            r["paid_minute"] = m
            out = dict(amount_minor=n)
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract=c["kind"],
            arguments=deepcopy(data),
            receipt=out,
            fee_minor=c["fees_minor"] - fees,
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
        )

    def custody_ndf_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        k = self.w["_event_payloads"][event["id"]]["event_kind"]
        cash = c["cash_minor"]
        out = {}
        if k == "substitution_confirmation":
            refs = []
            for r in self.records(c, "collateral_bond"):
                if r["pledged_to"] is not None:
                    r["confirmed"] = True
                    refs.append(r["reference"])
            out = dict(confirmed_bonds=refs)
        elif k == "corrected_fixing":
            notice = self.byref(c, "FIXING")
            pair = deepcopy(notice["_fixing_pair"])
            notice.update(actual_fixing_pair=pair, status="FINAL")
            totals = {"A": 0, "B": 0}
            for r in self.records(c, "ndf_leg"):
                fixing = pair[r["set_key"]]
                n = r["payer_sign"] * (
                    r["foreign_notional_minor"] * r["rate_denominator"] // r["strike_numerator"]
                    - r["foreign_notional_minor"] * r["rate_denominator"] // fixing
                )
                r.update(final_fixing_numerator=fixing, final_signed_minor=n)
                totals[r["set_key"]] += n
            for key, n in totals.items():
                self.byref(c, "SET_" + key).update(settlement_due_minor=n, status="FINAL")
                self.byref(c, "MARGIN_" + key)["required_minor"] = n
            r = self.byref(c, "LATE")
            r["settled"] = True
            out = dict(
                fixing_pair=pair, net_settlements_minor=totals, settled_bond_ref=r["reference"]
            )
        elif k == "later_receipt":
            self.byref(c, "TARGET")["liquidity_at_deadline_minor"] = c["cash_minor"]
            r = self.byref(c, "RECEIVABLE")
            self.cash(c, r["amount_minor"])
            r["received_minor"] = r["amount_minor"]
            out = dict(
                liquidity_at_deadline_minor=cash, independent_receipt_minor=r["amount_minor"]
            )
        else:
            self.check(False, "UNKNOWN_EVENT")
        return self.book(
            c,
            "bank_event",
            event_id=event["id"],
            event_kind=k,
            receipt=out,
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
        )
