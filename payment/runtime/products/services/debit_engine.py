from copy import deepcopy


class DebitOperations:
    def debit_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "reconciled_debits", "SERVICE_CONTRACT_MISMATCH")
        cash = c["cash_minor"]
        if action == "return_debit":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "bank_debit")
            n = data["amount_minor"]
            self.positive(n)
            credits = sum(
                x["amount_minor"]
                for x in self.records(c, "bank_credit")
                if x["debit"] == r["reference"] and x["status"] == "POSTED"
            )
            requested = sum(x["amount_minor"] for x in r["return_requests"])
            self.check(
                n <= r["amount_minor"] - credits - requested, "RETURN_EXCEEDS_AVAILABLE_DEBIT"
            )
            eid = self.schedule(
                "debit_event",
                case_id,
                1,
                event_kind="bank_return",
                reference=r["reference"],
                amount_minor=n,
            )
            r["return_requests"].append(
                dict(
                    event_id=eid["id"] if isinstance(eid, dict) else eid,
                    amount_minor=n,
                    minute=self.w["minute"],
                )
            )
        elif action == "revoke_mandate":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "debit_mandate")
            self.check(r["status"] == "ACTIVE", "MANDATE_NOT_ACTIVE")
            r.update(status="REVOKED", revoked_minute=self.w["minute"])
        elif action == "recall_collection":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "queued_collection")
            self.check(
                r["status"] in ("ACCEPTED", "QUEUED") and self.w["minute"] <= r["recall_until"],
                "COLLECTION_NOT_RECALLABLE",
            )
            r.update(status="RECALLED", recalled_minute=self.w["minute"])
        elif action == "pay_invoice":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "invoice_obligation")
            self.positive(data["amount_minor"])
            self.cash(c, -data["amount_minor"])
            r["manual_paid_minor"] += data["amount_minor"]
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="reconciled_debits",
            arguments=deepcopy(data),
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
        )

    def debit_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        cash = c["cash_minor"]
        kind = p["event_kind"]
        out = {}
        if kind == "mandate_lock":
            for q in self.records(c, "queued_collection"):
                if q["status"] == "QUEUED" and q["lock_minute"] == self.w["minute"]:
                    m = self.byref(c, self.byref(c, q["instruction"])["mandate"])
                    q["status"] = "ACCEPTED" if m["status"] == "ACTIVE" else "REJECTED"
                    q["acceptance_minute"] = self.w["minute"]
            out = dict(window_minute=self.w["minute"])
        elif kind == "collection":
            q = self.byref(c, p["reference"])
            if q["status"] == "ACCEPTED":
                if c["cash_minor"] >= q["amount_minor"]:
                    self.cash(c, -q["amount_minor"])
                    q["status"] = "SETTLED"
                    q["settled_minute"] = self.w["minute"]
                    ref = "SETTLED_" + q["reference"]
                    rid = c["scope"] + "-" + ref
                    self.w["service_records"][rid] = dict(
                        id=rid,
                        reference=ref,
                        name="Settled collection " + q["reference"],
                        kind="bank_debit",
                        case_id=c["id"],
                        scope=c["scope"],
                        customer_access=True,
                        instruction=q["instruction"],
                        amount_minor=q["amount_minor"],
                        posted_minute=self.w["minute"],
                        returned_minor=0,
                        return_requests=[],
                    )
                else:
                    q["status"] = "UNPAID"
            out = dict(reference=q["reference"], status=q["status"])
        elif kind == "merchant_credit":
            r = self.byref(c, p["reference"])
            r.update(
                status="POSTED", amount_minor=r["_actual_minor"], posted_minute=self.w["minute"]
            )
            self.cash(c, r["amount_minor"])
            out = dict(reference=r["reference"], amount_minor=r["amount_minor"])
        elif kind == "bank_return":
            r = self.byref(c, p["reference"])
            r["returned_minor"] += p["amount_minor"]
            self.cash(c, p["amount_minor"])
            out = dict(reference=r["reference"], amount_minor=p["amount_minor"])
        else:
            self.check(False, "UNKNOWN_EVENT")
        return self.book(
            c,
            "bank_event",
            event_kind=kind,
            event_id=event["id"],
            receipt=out,
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
        )
