class EntitlementOperations:
    def entitlement_instruction(self, case_id, action, data):
        from copy import deepcopy

        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "inherited_entitlements", "SERVICE_CONTRACT_MISMATCH")
        cash = c["cash_minor"]
        fees = c["fees_minor"]
        if action == "open_successor":
            self.args(data, "entity_id", "predecessor_id")
            r = self.local(c, data["entity_id"], "entity")
            p = self.local(c, data["predecessor_id"], "entity")
            terms = self.correspondence(c)
            self.check(
                r["account_status"] == "NONE"
                and r["predecessor"] == p["reference"]
                and p["account_status"] == "ACTIVE"
                and p["account_age_days"] >= terms["minimum_qualifying_age_days"],
                "CONTINUITY_REFERENCE_UNAVAILABLE",
            )
            self.charge(c, r["setup_fee_minor"])
            r["account_status"] = "PENDING"
            r["application_predecessor"] = p["reference"]
            r["application_minute"] = self.w["minute"]
            self.schedule(
                "entitlement_event",
                case_id,
                r["review_delay"],
                event_kind="successor_review",
                reference=r["reference"],
            )
        elif action == "close_account":
            self.args(data, "entity_id")
            r = self.local(c, data["entity_id"], "entity")
            self.check(r["account_status"] == "ACTIVE", "ACCOUNT_NOT_ACTIVE")
            r["account_status"] = "CLOSED"
            r["closed_minute"] = self.w["minute"]
        elif action == "refund":
            self.args(
                data, "record_id", "recipient_id", "holder_id", "amount_minor", "bank_version"
            )
            r = self.local(c, data["record_id"], "refund_claim")
            recipient = self.local(c, data["recipient_id"], "entity")
            holder = self.local(c, data["holder_id"], "entity")
            self.check(
                recipient["verified"]
                and recipient["account_status"] == "ACTIVE"
                and recipient["bank_version"] == data["bank_version"],
                "RECIPIENT_ACCOUNT_UNAVAILABLE",
            )
            self.positive(data["amount_minor"])
            self.cash(c, -data["amount_minor"])
            r["paid_minor"] += data["amount_minor"]
            r["payouts"].append(
                dict(
                    recipient=recipient["reference"],
                    holder=holder["reference"],
                    amount_minor=data["amount_minor"],
                    minute=self.w["minute"],
                    bank_version=data["bank_version"],
                )
            )
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="inherited_entitlements",
            arguments=deepcopy(data),
            cash_delta_minor=c["cash_minor"] - cash,
            fee_minor=c["fees_minor"] - fees,
            cash_after_minor=c["cash_minor"],
        )

    def entitlement_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        out = {}
        if p["event_kind"] == "successor_review":
            r = self.byref(c, p["reference"])
            previous = self.byref(c, r["application_predecessor"])
            passed = previous["account_status"] == "ACTIVE"
            r["account_status"] = "ACTIVE" if passed else "DECLINED"
            r["review_minute"] = self.w["minute"]
            out = dict(entity=r["reference"], status=r["account_status"])
        elif p["event_kind"] != "marker":
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
