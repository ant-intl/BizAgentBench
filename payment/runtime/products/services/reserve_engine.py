from copy import deepcopy


class ReserveOperations:
    def reserve_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "maturing_refund_reserves", "SERVICE_CONTRACT_MISMATCH")
        before = c["cash_minor"]
        fee = 0
        now = self.w["minute"]
        if action in ("pay_refund", "reserve_refund"):
            self.args(data, "source_id", "refund_id", "amount_minor")
            source = self.local(c, data["source_id"])
            refund = self.local(c, data["refund_id"], "merchant_refund_obligation")
            n = data["amount_minor"]
            self.positive(n)
            self.check(
                refund["status"] == "FINAL"
                and refund["paid_minor"] + refund["scheduled_minor"] + n <= refund["amount_minor"],
                "REFUND_NOT_FINAL_OR_EXCEEDS_REMAINDER",
            )
            if source["kind"] == "merchant_refund_wallet":
                self.check(
                    action == "pay_refund"
                    and now >= refund["earliest_minute"]
                    and refund["wallet_allowed"],
                    "REFUND_NOT_PAYABLE_FROM_WALLET",
                )
                self.check(c["cash_minor"] >= n, "INSUFFICIENT_WALLET_CASH")
                c["cash_minor"] -= n
            else:
                self.check(
                    source["kind"] == "merchant_reserve_tranche"
                    and source["status"] == "HELD"
                    and refund["reference"] in source["eligible_refunds"]
                    and source["balance_minor"] >= n,
                    "RESERVE_UNAVAILABLE_OR_OUTSIDE_SCOPE",
                )
                self.check(
                    (
                        (now < refund["earliest_minute"])
                        if action == "reserve_refund"
                        else (now >= refund["earliest_minute"])
                    ),
                    "INCORRECT_REFUND_TIMING",
                )
                source["balance_minor"] -= n
                if action == "reserve_refund":
                    source["held"][refund["reference"]] = (
                        source["held"].get(refund["reference"], 0) + n
                    )
                else:
                    source["refund_paid_minor"] += n
            if action == "reserve_refund":
                refund["scheduled_minor"] += n
            else:
                refund["paid_minor"] += n
                refund["last_paid_minute"] = now
                refund["payment_sources"][source["reference"]] = (
                    refund["payment_sources"].get(source["reference"], 0) + n
                )
        elif action == "accelerate_tranche":
            self.args(data, "tranche_id")
            r = self.local(c, data["tranche_id"], "merchant_reserve_tranche")
            self.check(r["status"] == "HELD" and not r["refund_only"], "TRANCHE_NOT_ACCELERABLE")
            fee = r["balance_minor"] * r["acceleration_fee_bp"] // 10000
            net = r["balance_minor"] - fee
            c["cash_minor"] += net
            c["fees_minor"] += fee
            r.update(
                balance_minor=0,
                status="ACCELERATED",
                released_minor=r["released_minor"] + net,
                fee_minor=r["fee_minor"] + fee,
            )
        elif action == "pay_logistics":
            self.args(data, "invoice_id", "amount_minor")
            r = self.local(c, data["invoice_id"], "merchant_service_invoice")
            n = data["amount_minor"]
            self.positive(n)
            self.check(c["cash_minor"] >= n, "INSUFFICIENT_WALLET_CASH")
            c["cash_minor"] -= n
            r["paid_minor"] += n
            r["last_paid_minute"] = now
        elif action == "pay_out":
            self.args(data, "bank_id", "amount_minor")
            r = self.local(c, data["bank_id"], "merchant_payout_account")
            n = data["amount_minor"]
            self.positive(n)
            self.check(c["cash_minor"] >= n, "INSUFFICIENT_WALLET_CASH")
            c["cash_minor"] -= n
            r["received_minor"] += n
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="maturing_refund_reserves",
            arguments=deepcopy(data),
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
            fee_minor=fee,
        )

    def reserve_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        before = c["cash_minor"]
        now = self.w["minute"]
        review = self.byref(c, "REVIEW")
        actual = None
        payments = []
        maturities = []
        if now == review["effective_minute"]:
            actual = deepcopy(review["_actual"])
            self.check(actual in review["possible_outcomes"], "INVALID_REVIEW")
            review.update(status="FINAL", actual=actual)
            for ref, n in actual.items():
                self.byref(c, ref).update(status="FINAL", amount_minor=n)
        for source in self.records(c, "merchant_reserve_tranche"):
            for ref, n in list(source["held"].items()):
                refund = self.byref(c, ref)
                if now == refund["earliest_minute"]:
                    refund["scheduled_minor"] -= n
                    refund["paid_minor"] += n
                    refund["last_paid_minute"] = now
                    refund["payment_sources"][source["reference"]] = (
                        refund["payment_sources"].get(source["reference"], 0) + n
                    )
                    source["refund_paid_minor"] += n
                    del source["held"][ref]
                    payments.append(
                        dict(source_ref=source["reference"], refund_ref=ref, amount_minor=n)
                    )
            if now == source["maturity_minute"] and source["status"] == "HELD":
                n = source["balance_minor"]
                source["balance_minor"] = 0
                source["status"] = "MATURED"
                if source["refund_only"]:
                    source["returned_minor"] += n
                else:
                    source["released_minor"] += n
                    c["cash_minor"] += n
                maturities.append(
                    dict(
                        source_ref=source["reference"],
                        amount_minor=n,
                        disposition=(
                            "RETURN_TO_PLATFORM" if source["refund_only"] else "MERCHANT_WALLET"
                        ),
                    )
                )
        return self.book(
            c,
            "bank_event",
            event_id=event["id"],
            receipt=dict(adjustments=actual, refunds=payments, maturities=maturities),
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
            fee_minor=0,
        )
