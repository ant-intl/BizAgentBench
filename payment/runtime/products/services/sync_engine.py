from copy import deepcopy


class SyncOperations:
    def sync_allowance(self, c):
        p = self.byref(c, "SOURCE").get("receipt") or self.byref(c, "SOURCE")["_response"]
        c["state"]["available_quota_minor"] = (
            sum(
                (
                    max(0, r["face_minor"] - r["existing_encumbrance_minor"])
                    if not r["assigned"]
                    else 0
                )
                for r in self.records(c, "sales_receivable")
                if r["shop"] == p["target_shop"]
            )
            - c["state"]["consumed_quota_minor"]
        )

    def sync_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "reconciled_sync", "SERVICE_CONTRACT_MISMATCH")
        cash = c["cash_minor"]
        out = {}
        if action == "grant_stream":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "sync_stream")
            r["granted"] = True
        elif action == "import_window":
            self.args(data, "record_id", "after_cursor", "through_cursor")
            r = self.local(c, data["record_id"], "sync_stream")
            a = data["after_cursor"]
            b = data["through_cursor"]
            self.check(type(a) is int and type(b) is int and 0 <= a <= b, "INVALID_CURSOR")
            self.check(r["granted"], "STREAM_PERMISSION_REQUIRED")
            events = sorted(
                (
                    v
                    for v in self.records(c, "source_event")
                    if v["shop"] == r["shop"]
                    and v["stream"] == r["stream"]
                    and a < v["sequence"] <= b
                ),
                key=lambda v: v["sequence"],
            )
            applied = []
            for e in events:
                key = e["native_event"] + ":" + str(e["revision"])
                if key in r["posted_keys"]:
                    continue
                inv = self.byref(c, e["receivable"])
                inv["face_minor"] += e["delta_minor"]
                r["posted_keys"].append(key)
                applied.append(key)
                if inv["assigned"]:
                    advance = max(0, inv["face_minor"]) * inv["advance_bp"] // 10000
                    self.cash(c, advance - inv["advanced_minor"])
                    inv["advanced_minor"] = advance
            r.update(cursor=max(r["cursor"], b), last_attempt_status="COMPLETED")
            out = dict(applied_keys=applied, cursor=r["cursor"])
        elif action in ("finance", "unwind"):
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "sales_receivable")
            if action == "finance":
                self.check(
                    not r["assigned"]
                    and r["existing_encumbrance_minor"] == 0
                    and r["face_minor"] > 0
                    and r["advance_bp"] > 0,
                    "RECEIVABLE_UNAVAILABLE",
                )
                n = r["face_minor"] * r["advance_bp"] // 10000
                r.update(assigned=True, advanced_minor=n)
                self.cash(c, n)
            else:
                self.check(r["assigned"], "NO_ACTIVE_ASSIGNMENT")
                self.cash(c, -r["advanced_minor"])
                r.update(assigned=False, advanced_minor=0)
        elif action == "withdraw":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "sync_recipient")
            n = data["amount_minor"]
            self.positive(n)
            self.check(r["status"] == "VERIFIED", "RECIPIENT_NOT_VERIFIED")
            self.sync_allowance(c)
            self.check(
                c["state"]["available_quota_minor"] >= n, "INSUFFICIENT_SETTLEMENT_ALLOWANCE"
            )
            p = self.correspondence(c)
            net = n * p["rate"] - p["cny_fee_minor"]
            self.check(net > 0, "PROCEEDS_NOT_POSITIVE")
            self.cash(c, -n)
            r["paid_minor"] += net
            c["state"]["consumed_quota_minor"] += n
            c["state"]["withdrawals"].append(
                dict(
                    recipient=r["reference"],
                    amount_minor=n,
                    net_cny_minor=net,
                    minute=self.w["minute"],
                )
            )
            out = dict(net_cny_minor=net)
        elif action == "pay_supplier":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "sync_invoice")
            self.positive(data["amount_minor"])
            self.cash(c, -data["amount_minor"])
            r["paid_minor"] += data["amount_minor"]
        else:
            self.check(False, "UNKNOWN_ACTION")
        self.sync_allowance(c)
        return self.book(
            c,
            action,
            contract="reconciled_sync",
            arguments=deepcopy(data),
            receipt=out,
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
        )

    def sync_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        kind = self.w["_event_payloads"][event["id"]]["event_kind"]
        cash = c["cash_minor"]
        out = {}
        if kind == "supplier_result":
            r = self.byref(c, "PENDING")
            r["status"] = r["_result"]
            if r["status"] == "RETURNED":
                r["actual_return_minor"] = r["amount_minor"] - r["_return_fee_minor"]
                self.cash(c, r["actual_return_minor"])
            else:
                self.byref(c, r["invoice"])["paid_minor"] += r["amount_minor"]
            out = dict(
                status=r["status"],
                actual_return_minor=r["actual_return_minor"],
                amount_minor=r["amount_minor"],
            )
        elif kind != "sync_window":
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
