from copy import deepcopy


class CurrencyRefundOperations:
    def currency_refund_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "origin_currency_refunds", "SERVICE_CONTRACT_MISMATCH")
        if action == "refund":
            self.args(data, "purchase_id", "account_id", "currency", "amount_minor")
            p = self.local(c, data["purchase_id"], "refund_purchase")
            a = self.local(c, data["account_id"], "refund_wallet")
            n = data["amount_minor"]
            self.positive(n)
            route = next(
                (
                    r
                    for r in p["refund_routes"]
                    if r["account"] == a["reference"] and r["currency"] == data["currency"]
                ),
                None,
            )
            self.check(route is not None, "UNSUPPORTED_REFUND_ROUTE")
            debit = (n * route["numerator"] + route["denominator"] - 1) // route["denominator"]
            self.check(a["cash_minor"] >= debit, "INSUFFICIENT_CURRENCY_CASH")
            a["cash_minor"] -= debit
            entry = dict(
                currency=data["currency"],
                credited_minor=n,
                account_ref=a["reference"],
                debited_minor=debit,
                minute=self.w["minute"],
            )
            p["new_credits"].append(entry)
            receipt = dict(purchase_ref=p["reference"], credit=deepcopy(entry))
        elif action == "exchange":
            self.args(data, "quote_id", "amount_minor")
            q = self.local(c, data["quote_id"], "refund_fx_quote")
            n = data["amount_minor"]
            self.positive(n)
            a = self.byref(c, q["from_account"])
            b = self.byref(c, q["to_account"])
            self.check(a["cash_minor"] >= n, "INSUFFICIENT_CURRENCY_CASH")
            m = n * q["numerator"] // q["denominator"]
            self.check(m > 0, "ZERO_EXCHANGE_PROCEEDS")
            a["cash_minor"] -= n
            b["cash_minor"] += m
            receipt = dict(quote_ref=q["reference"], debited_minor=n, credited_minor=m)
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="origin_currency_refunds",
            arguments=deepcopy(data),
            receipt=receipt,
            cash_delta_minor=0,
            cash_after_minor=c["cash_minor"],
            fee_minor=0,
        )

    def currency_refund_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        out = []
        for ref in p["transfers"]:
            t = self.byref(c, ref)
            self.check(t["status"] == "IN_FLIGHT", "TRANSFER_ALREADY_FINAL")
            n = t["_actual_credited_minor"]
            self.check(n in t["possible_credited_minor"], "INVALID_CREDIT_OUTCOME")
            used = (n * t["funding_numerator"] + t["funding_denominator"] - 1) // t[
                "funding_denominator"
            ]
            returned = t["funded_minor"] - used
            self.check(returned >= 0, "INVALID_RETURNED_PRINCIPAL")
            t.update(status="FINAL", credited_minor=n, returned_minor=returned)
            self.byref(c, t["funding_account"])["cash_minor"] += returned
            out.append(dict(transfer_ref=ref, credited_minor=n, returned_minor=returned))
        return self.book(
            c,
            "bank_event",
            event_id=event["id"],
            receipt=dict(transfers=out),
            cash_delta_minor=0,
            cash_after_minor=c["cash_minor"],
            fee_minor=0,
        )
