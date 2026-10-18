from copy import deepcopy


class RepresentmentOperations:
    def representment_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "overlapping_remedies", "SERVICE_CONTRACT_MISMATCH")
        before = c["cash_minor"]
        self.args(
            data,
            *(
                ["purchase_id", "amount_minor", "evidence_id"]
                if action == "respond"
                else ["purchase_id", "amount_minor"]
            ),
        )
        p = self.local(c, data["purchase_id"], "disputed_purchase")
        n = data["amount_minor"]
        self.positive(n)
        self.check(n <= p["original_minor"], "AMOUNT_EXCEEDS_PURCHASE")
        if action == "refund":
            self.check(c["cash_minor"] >= n, "INSUFFICIENT_MERCHANT_CASH")
            c["cash_minor"] -= n
            p["new_refund_minor"] += n
            receipt = dict(purchase_ref=p["reference"], refunded_minor=n)
        elif action == "respond":
            memo = self.local(c, data["evidence_id"], "concession_memo")
            self.check(memo["purchase_ref"] == p["reference"], "EVIDENCE_FOR_DIFFERENT_PURCHASE")
            entry = dict(
                amount_minor=n,
                evidence_version=memo["version"],
                evidence_status=memo["status"],
                minute=self.w["minute"],
            )
            p["responses"].append(entry)
            receipt = dict(purchase_ref=p["reference"], accepted=deepcopy(entry))
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="overlapping_remedies",
            arguments=deepcopy(data),
            receipt=receipt,
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
            fee_minor=0,
        )

    def representment_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        before = c["cash_minor"]
        out = []
        for ref in p["refs"]:
            r = self.byref(c, ref)
            self.check(r["status"] == "PENDING", "REMEDY_ALREADY_DECIDED")
            r["status"] = r["_actual_outcome"]
            self.check(r["status"] in r["possible_outcomes"], "INVALID_REMEDY_OUTCOME")
            if r["rail"] == "merchant" and r["status"] == "RETURNED":
                c["cash_minor"] += r["amount_minor"]
            out.append(dict(reference=ref, status=r["status"]))
        memo = None
        if p["memo"]:
            m = self.byref(c, p["memo"])
            v = m["_actual_concession_minor"]
            self.check(v in m["possible_concession_minor"], "INVALID_CONCESSION")
            m.update(concession_minor=v, status="FINAL", version="FINAL2")
            purchase = self.byref(c, m["purchase_ref"])
            purchase.update(concession_minor=v, concession_status="FINAL")
            memo = dict(reference=m["reference"], concession_minor=v, version="FINAL2")
        return self.book(
            c,
            "bank_event",
            event_id=event["id"],
            receipt=dict(remedies=out, memo=memo),
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
            fee_minor=0,
        )
