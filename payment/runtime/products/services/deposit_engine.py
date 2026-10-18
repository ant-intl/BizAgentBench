from copy import deepcopy


class DepositOperations:
    def deposit_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "tiered_deposit_calendar", "SERVICE_CONTRACT_MISMATCH")
        before = c["cash_minor"]
        fee = 0
        now = self.w["minute"]
        if action == "redeem_early":
            self.args(data, "deposit_id")
            r = self.local(c, data["deposit_id"], "dated_term_deposit")
            self.check(
                r["status"] == "ACTIVE" and now < r["maturity_minute"],
                "DEPOSIT_NOT_EARLY_REDEEMABLE",
            )
            payout = r["principal_minor"] - r["break_charge_minor"]
            c["cash_minor"] += payout
            fee = r["break_charge_minor"]
            c["fees_minor"] += fee
            r.update(status="BROKEN", break_minute=now, payout_minor=payout, payout_minute=now)
        elif action == "pay_obligation":
            self.args(data, "obligation_id", "amount_minor")
            r = self.local(c, data["obligation_id"], "deposit_obligation")
            n = data["amount_minor"]
            self.positive(n)
            self.check(c["cash_minor"] >= n, "INSUFFICIENT_WALLET_CASH")
            c["cash_minor"] -= n
            r["paid_minor"] += n
            r["last_paid_minute"] = now
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="tiered_deposit_calendar",
            arguments=deepcopy(data),
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
            fee_minor=fee,
        )

    def deposit_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        now = self.w["minute"]
        before = c["cash_minor"]
        review = self.byref(c, "REIMBURSEMENT")
        receipt = 0
        if now == review["effective_minute"]:
            receipt = review["_actual_amount_minor"]
            self.check(receipt in review["possible_amounts_minor"], "INVALID_REIMBURSEMENT")
            review.update(actual_amount_minor=receipt, status="FINAL")
            c["cash_minor"] += receipt
        deposits = self.records(c, "dated_term_deposit")
        tier = self.byref(c, "TIER")
        qualifies = (
            sum(
                r["principal_minor"]
                for r in deposits
                if r["tier_group"] == tier["group"] and r["status"] != "BROKEN"
            )
            >= tier["qualifying_original_principal_minor"]
        )
        if now == tier["qualification_through"]:
            tier.update(finalized=True, qualified=qualifies)
        if tier["finalized"]:
            qualifies = tier["qualified"]
        payouts = []
        for r in deposits:
            if r["status"] == "ACTIVE" and r["maturity_minute"] == now:
                interest = r["base_interest_minor"] + (r["tier_bonus_minor"] if qualifies else 0)
                n = r["principal_minor"] + interest
                c["cash_minor"] += n
                r.update(
                    status="MATURED", payout_minor=n, interest_minor=interest, payout_minute=now
                )
                payouts.append(dict(deposit_ref=r["reference"], amount_minor=n))
        auto = self.byref(c, "RENT")
        if now == auto["due_minute"] and c["cash_minor"] >= auto["amount_minor"]:
            c["cash_minor"] -= auto["amount_minor"]
            auto["paid"] = True
        return self.book(
            c,
            "bank_event",
            event_id=event["id"],
            receipt=dict(reimbursement_minor=receipt, payouts=payouts),
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
            fee_minor=0,
        )
