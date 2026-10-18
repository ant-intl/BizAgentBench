from copy import deepcopy


class ConsolidationOperations:
    def consolidation_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "corporate_consolidation", "SERVICE_CONTRACT_MISMATCH")
        before = c["cash_minor"]
        fees = c["fees_minor"]
        receipt = {}
        if action == "apply_continuity":
            self.args(data, "account_id", "successor_id")
            a = self.local(c, data["account_id"], "corporate_account")
            s = self.local(c, data["successor_id"], "registered_company")
            terms = next(
                (
                    r
                    for r in self.records(c, "continuity_offer")
                    if r["account_ref"] == a["reference"]
                ),
                None,
            )
            self.check(terms is not None, "NO_CONTINUITY_OFFER")
            self.check(
                self.w["minute"] <= terms["submission_through"]
                and a["status"] == "OPEN"
                and a["port_status"] == "NONE",
                "CONTINUITY_OFFER_UNAVAILABLE",
            )
            deeds = [
                r
                for r in self.records(c, "corporate_instrument")
                if r["status"] == "REGISTERED" and r["effective_minute"] <= self.w["minute"]
            ]
            latest = {}
            for d in deeds:
                if d["deal"] not in latest or d["version"] > latest[d["deal"]]["version"]:
                    latest[d["deal"]] = d
            self.check(
                any(
                    d["instrument_type"] == "STATUTORY_MERGER"
                    and d["old_company_ref"] == a["owner_ref"]
                    and d["new_company_ref"] == s["reference"]
                    for d in latest.values()
                ),
                "REGISTERED_SUCCESSION_NOT_ESTABLISHED",
            )
            self.charge(c, terms["fee_minor"])
            a.update(port_status="PENDING", port_target_ref=s["reference"])
            receipt = dict(account_ref=a["reference"], status="PENDING")
        elif action == "nominate":
            self.args(data, "mandate_id", "account_id")
            m = self.local(c, data["mandate_id"], "service_mandate")
            a = self.local(c, data["account_id"], "corporate_account")
            self.check(
                m["status"] == "PENDING"
                and a["status"] == "OPEN"
                and m["capability"] in a["capabilities"],
                "MANDATE_OR_CAPABILITY_UNAVAILABLE",
            )
            m["nominated_account_ref"] = a["reference"]
            receipt = dict(mandate_ref=m["reference"], account_ref=a["reference"])
        elif action == "close":
            self.args(data, "account_id")
            a = self.local(c, data["account_id"], "corporate_account")
            self.check(a["status"] in ("OPEN", "REVIEW_PENDING"), "ACCOUNT_NOT_OPEN")
            a.update(status="CLOSED", closed_minute=self.w["minute"])
            receipt = dict(account_ref=a["reference"], status="CLOSED")
            for beneficiary in self.records(c, "corporate_account"):
                if beneficiary.get("sponsor_account_ref") == a["reference"]:
                    beneficiary["capabilities"] = [
                        cap
                        for cap in beneficiary["capabilities"]
                        if cap not in beneficiary["sponsored_capabilities"]
                    ]
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="corporate_consolidation",
            arguments=deepcopy(data),
            receipt=receipt,
            cash_delta_minor=c["cash_minor"] - before,
            fee_minor=c["fees_minor"] - fees,
            cash_after_minor=c["cash_minor"],
        )

    def consolidation_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        out = {}
        if p["event_kind"] == "bank_reviews":
            ports = []
            for a in self.records(c, "corporate_account"):
                if a["port_status"] == "PENDING":
                    a["port_status"] = "APPROVED" if a["status"] == "OPEN" else "DECLINED"
                    if a["port_status"] == "APPROVED":
                        a["owner_ref"] = a["port_target_ref"]
                    ports.append(dict(account_ref=a["reference"], status=a["port_status"]))
            reviews = []
            for r in self.records(c, "product_review"):
                a = self.byref(c, r["account_ref"])
                r.update(status="FINAL", actual_capabilities=deepcopy(r["_actual_capabilities"]))
                if a["status"] != "CLOSED":
                    a.update(status="OPEN", capabilities=deepcopy(r["actual_capabilities"]))
                reviews.append(
                    dict(
                        account_ref=a["reference"],
                        capabilities=r["actual_capabilities"],
                        account_status=a["status"],
                    )
                )
            refunds = []
            for r in self.records(c, "pending_merchant_refund"):
                paid = self.byref(c, r["account_ref"])["status"] == "OPEN"
                n = r["held_minor"]
                r.update(
                    status="SETTLED" if paid else "FAILED",
                    held_minor=0,
                    settled_minor=n if paid else 0,
                    returned_hold_minor=0 if paid else n,
                )
                refunds.append(
                    dict(
                        refund_ref=r["reference"],
                        status=r["status"],
                        amount_minor=n,
                        currency=r["currency"],
                    )
                )
            out = dict(continuity=ports, products=reviews, refunds=refunds)
        elif p["event_kind"] == "mandate_activation":
            activations = []
            for m in self.records(c, "service_mandate"):
                a = self.byref(c, m["nominated_account_ref"])
                active = a["status"] == "OPEN" and m["capability"] in a["capabilities"]
                m["status"] = "ACTIVE" if active else "FAILED"
                m["activation"] = dict(
                    minute=self.w["minute"],
                    account_ref=a["reference"],
                    owner_ref=a["owner_ref"],
                    capability=m["capability"],
                    successful=active,
                )
                activations.append(dict(mandate_ref=m["reference"], **m["activation"]))
            out = dict(activations=activations)
        else:
            self.check(False, "UNKNOWN_EVENT")
        return self.book(
            c,
            "bank_event",
            event_kind=p["event_kind"],
            event_id=event["id"],
            receipt=out,
            cash_delta_minor=0,
            cash_after_minor=c["cash_minor"],
        )
