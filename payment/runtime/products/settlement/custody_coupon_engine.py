from copy import deepcopy


class CustodyCouponOperations:
    def custody_coupon_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "custody_coupon_entitlements", "SERVICE_CONTRACT_MISMATCH")
        before = c["cash_minor"]
        fees = c["fees_minor"]
        out = {}
        m = self.w["minute"]
        notice = self.byref(c, "ISSUER")
        if action == "elect_claim":
            self.args(data, "record_id", "mode")
            r = self.local(c, data["record_id"], "manufactured_claim")
            mode = data["mode"]
            self.check(
                mode in ("FINAL", "CONTINGENT")
                and r["election"] is None
                and m <= r["claim_deadline"],
                "CLAIM_ELECTION_UNAVAILABLE",
            )
            r.update(election=mode, elected_minute=m)
            if mode == "CONTINGENT":
                self.charge(c, r["contingent_fee_minor"])
            elif r["final_receipt_minute"] <= m:
                r["received_minor"] = r["units"] * notice["provisional_rate_minor"]
                self.cash(c, r["received_minor"])
            out = dict(claim_ref=r["reference"], mode=mode, received_minor=r["received_minor"])
        elif action in ("draw_advance", "repay_advance"):
            self.args(data, "amount_minor")
            r = self.byref(c, "ADVANCE")
            n = data["amount_minor"]
            self.positive(n)
            if action == "draw_advance":
                self.check(
                    m == 0 and r["drawn_minor"] == 0 and n <= r["limit_minor"],
                    "ADVANCE_UNAVAILABLE",
                )
                self.cash(c, n)
                self.charge(c, r["fee_minor"])
                r["drawn_minor"] = n
            else:
                self.check(n <= r["drawn_minor"] - r["repaid_minor"], "EXCESS_ADVANCE_REPAYMENT")
                self.cash(c, -n)
                r["repaid_minor"] += n
            out = dict(amount_minor=n)
        elif action == "settle_purchase":
            self.args(data, "mode")
            r = self.byref(c, "DELIVERY")
            mode = data["mode"]
            self.check(
                mode in ("STANDARD", "EXPRESS") and r["paid_minor"] == 0 and m == 0,
                "PURCHASE_WINDOW_CLOSED",
            )
            n = r["principal_minor"]
            self.cash(c, -n)
            self.charge(
                c, r["standard_fee_minor"] if mode == "STANDARD" else r["express_fee_minor"]
            )
            r.update(paid_minor=n, mode=mode)
            if mode == "EXPRESS":
                r.update(settled=True, settled_minute=m)
            out = dict(principal_minor=n, mode=mode)
        elif action == "realize_collateral":
            self.args(data, "record_id", "allocations")
            asset = self.local(c, data["record_id"], "borrower_collateral")
            self.check(
                not asset["realized"]
                and asset["returned_minute"] is None
                and notice["status"] == "FINAL",
                "COLLATERAL_UNAVAILABLE",
            )
            alloc = {}
            total = 0
            self.check(
                type(data["allocations"]) is dict and bool(data["allocations"]),
                "EMPTY_COLLATERAL_ALLOCATION",
            )
            for rid, n in data["allocations"].items():
                claim = self.local(c, rid, "manufactured_claim")
                self.positive(n)
                self.check(
                    claim["reference"] in asset["eligible_claims"]
                    and n <= claim["arrears_minor"] - claim["collateral_recovered_minor"],
                    "COLLATERAL_SCOPE_OR_ARREARS_EXCEEDED",
                )
                alloc[claim["reference"]] = n
                total += n
            self.check(total <= asset["face_minor"], "COLLATERAL_PROCEEDS_EXCEEDED")
            for ref, n in alloc.items():
                self.byref(c, ref)["collateral_recovered_minor"] += n
            asset.update(realized=True, allocations_minor=alloc)
            self.cash(c, total)
            out = dict(
                security_ref=asset["reference"],
                allocations_minor=alloc,
                applied_minor=total,
                segregated_remainder_minor=asset["face_minor"] - total,
            )
        elif action == "return_collateral":
            self.args(data, "record_id")
            asset = self.local(c, data["record_id"], "borrower_collateral")
            self.check(asset["returned_minute"] is None, "COLLATERAL_ALREADY_RETURNED")
            asset["returned_minute"] = m
            if asset["realized"]:
                asset["cash_returned_minor"] = asset["face_minor"] - sum(
                    asset["allocations_minor"].values()
                )
            else:
                asset["returned_intact"] = True
            out = dict(
                security_ref=asset["reference"],
                returned_intact=asset["returned_intact"],
                cash_returned_minor=asset["cash_returned_minor"],
            )
        elif action in ("pay_coupon", "recover_coupon"):
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "coupon_investor")
            n = data["amount_minor"]
            self.positive(n)
            if action == "pay_coupon":
                self.cash(c, -n)
                r["paid_minor"] += n
                r["last_payment_minute"] = m
            else:
                self.check(
                    notice["status"] == "FINAL"
                    and n <= r["recovery_limit_minor"] - r["recovered_minor"]
                    and n <= r["paid_minor"] - r["recovered_minor"],
                    "RECOVERY_MANDATE_EXCEEDED",
                )
                self.cash(c, n)
                r["recovered_minor"] += n
                r["last_recovery_minute"] = m
            out = dict(investor_ref=r["reference"], amount_minor=n)
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract=c["kind"],
            arguments=deepcopy(data),
            receipt=out,
            fee_minor=c["fees_minor"] - fees,
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
        )

    def custody_coupon_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        kind = self.w["_event_payloads"][event["id"]]["event_kind"]
        before = c["cash_minor"]
        out = {}
        notice = self.byref(c, "ISSUER")
        if kind == "purchase_settlement":
            r = self.byref(c, "DELIVERY")
            if r["paid_minor"] == r["principal_minor"] and r["mode"] == "STANDARD":
                r.update(settled=True, settled_minute=self.w["minute"])
            paid = []
            for r in self.records(c, "manufactured_claim"):
                if r["election"] == "FINAL" and r["received_minor"] == 0:
                    r["received_minor"] = r["units"] * notice["provisional_rate_minor"]
                    self.cash(c, r["received_minor"])
                    paid.append(r["reference"])
            out = dict(
                purchase_settled=self.byref(c, "DELIVERY")["settled"], final_claims_collected=paid
            )
        elif kind == "coupon_correction":
            rate = notice["_final_rate_minor"]
            delta = notice["direct_custody_units"] * (rate - notice["provisional_rate_minor"])
            net = delta
            notice.update(
                final_rate_minor=rate,
                status="FINAL",
                issuer_received_minor=notice["direct_custody_units"] * rate,
                actual_borrower_shortfalls=deepcopy(notice["_borrower_shortfalls"]),
            )
            paid = {}
            for r in self.records(c, "manufactured_claim"):
                if r["election"] == "CONTINGENT":
                    short = notice["_borrower_shortfalls"].get(r["reference"], 0)
                    self.check(0 <= short <= r["units"] * rate, "INVALID_BORROWER_COLLECTION")
                    n = r["units"] * rate - short
                    net += n
                    r["received_minor"] = n
                    r["arrears_minor"] = short
                    paid[r["reference"]] = n
            r = self.byref(c, "OPERATING")
            net += r["amount_minor"]
            self.cash(c, net)
            r["received_minor"] = r["amount_minor"]
            out = dict(
                final_rate_minor=rate,
                issuer_adjustment_minor=delta,
                manufactured_collections=paid,
                borrower_shortfalls=deepcopy(notice["_borrower_shortfalls"]),
                operating_received_minor=r["amount_minor"],
            )
        elif kind != "servicing_deadline":
            self.check(False, "UNKNOWN_EVENT")
        return self.book(
            c,
            "bank_event",
            event_id=event["id"],
            event_kind=kind,
            receipt=out,
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
        )
