from copy import deepcopy
from datetime import datetime, timedelta, timezone
from payment.runtime.products.banking.specification import PARAMS as P, CAPABILITIES
from payment.runtime.products.banking.schema import TABLES


class BusinessError(Exception):
    pass


class BankingOperations:

    def check(self, condition, message):
        if not condition:
            raise BusinessError(message)

    def row(self, table, key):
        self.check(key in self.w.get(table, {}), "RECORD_NOT_FOUND")
        r = self.w[table][key]
        self.check(r.get("customer_access", True), "ACCESS_DENIED")
        return r

    def new(self, table, **values):
        n = len(self.w[table]) + 1
        while f"{table.upper()}-{n:04}" in self.w[table]:
            n += 1
        r = dict(id=f"{table.upper()}-{n:04}", **values)
        self.w[table][r["id"]] = r
        return r

    def public(self, r, resource=None):
        hidden = {"scenario_minute"}
        if resource == "incoming":
            hidden.update(("mandate_id", "value_minute", "original_payee_entity"))
        # The initial incoming record does not contain public bank advice.
        # A completed trace is precisely the public evidence that reveals it.
        if resource != "traces" or r.get("status") != "READY":
            hidden.add("intended_account_id")
        return {k: deepcopy(v) for k, v in r.items() if not k.startswith("_") and k not in hidden}

    def search_records(self, resource, filters=None, cursor=0, limit=20):
        self.check(resource in TABLES, "RESOURCE_NOT_PUBLIC")
        self.check(0 < limit <= 100 and cursor >= 0, "INVALID_PAGE")
        filters = filters or {}

        def matches(r):
            for k, v in filters.items():
                self.check(
                    k != "scenario_minute"
                    and not k.startswith("_")
                    and (k != "intended_account_id" or resource == "traces")
                    and not (
                        resource == "incoming"
                        and k in ("mandate_id", "value_minute", "original_payee_entity")
                    ),
                    "FIELD_NOT_PUBLIC",
                )
                if k == "intended_account_id" and r.get("status") != "READY":
                    return False
                value = r.get(k)
                if isinstance(v, dict):
                    for op, a in v.items():
                        self.check(op in ("in", "contains", "gte", "lte"), "UNSUPPORTED_FILTER")
                        if op == "in" and value not in a:
                            return False
                        if op == "contains" and str(a).lower() not in str(value).lower():
                            return False
                        if op == "gte" and (value is None or value < a):
                            return False
                        if op == "lte" and (value is None or value > a):
                            return False
                elif value != v:
                    return False
            return True

        rows = [
            self.public(r, resource)
            for _, r in sorted(self.w[resource].items())
            if r.get("customer_access", True) and matches(r)
        ]
        keys = (
            "id",
            "name",
            "label",
            "legal_name",
            "reference",
            "entity_id",
            "account_id",
            "source_id",
            "status",
            "amount_minor",
            "currency",
            "batch_id",
            "source",
            "remaining_minor",
        )
        return dict(
            records=[{k: r[k] for k in keys if k in r} for r in rows[cursor : cursor + limit]],
            total=len(rows),
            next_cursor=cursor + limit if cursor + limit < len(rows) else None,
        )

    def get_record(self, resource, record_id):
        self.check(resource in TABLES, "RESOURCE_NOT_PUBLIC")
        return self.public(self.row(resource, record_id), resource)

    def get_clock(self):
        return dict(
            minute=self.w["minute"],
            utc=(
                datetime(2026, 9, 3, tzinfo=timezone.utc) + timedelta(minutes=self.w["minute"])
            ).isoformat(),
        )

    def call_discoverable_agent_tool(self, capability_name, arguments):
        self.check(capability_name in self.w["unlocked"], "TOOL_NOT_UNLOCKED")
        s = CAPABILITIES[capability_name]
        required = {k for k, v in s["parameters"].items() if not v.endswith("?")}
        self.check(
            required <= set(arguments) <= set(s["parameters"]), "INVALID_CAPABILITY_ARGUMENTS"
        )
        for k, v in arguments.items():
            typ = s["parameters"][k].rstrip("?")
            self.check(
                type(v) is {"string": str, "integer": int, "object": dict}[typ],
                "INVALID_CAPABILITY_ARGUMENT_TYPE",
            )
            if typ == "integer":
                self.check(v >= 0 if k == "slot_minute" else v > 0, "INVALID_NUMERIC_VALUE")
        if capability_name.startswith("propose_"):
            return self.new(
                "requests",
                operation=capability_name,
                arguments=deepcopy(arguments),
                status="PENDING",
                prepared_minute=self.w["minute"],
            )
        return getattr(self, capability_name)(**arguments)

    def list_my_files(self):
        return [{k: v for k, v in r.items() if k != "contents"} for r in self.w["files"].values()]

    def upload_file(self, file_id, target_id, purpose):
        f = self.row("files", file_id)
        self.check(any(target_id in self.w[t] for t in TABLES), "TARGET_NOT_FOUND")
        return self.new(
            "submissions",
            file_id=file_id,
            target_id=target_id,
            purpose=purpose,
            contents=deepcopy(f["contents"]),
            status="SUBMITTED",
            submitted_minute=self.w["minute"],
        )

    def assess_evidence(self, submission_id):
        s = self.row("submissions", submission_id)
        if s["status"] != "SUBMITTED":
            return self.public(s)
        c = s["contents"]
        reasons = []
        if c["target_id"] != s["target_id"]:
            reasons.append("TARGET_MISMATCH")
        if c["purpose"] != s["purpose"]:
            reasons.append("PURPOSE_MISMATCH")
        if c["valid_until"] < self.w["minute"]:
            reasons.append("EXPIRED")
        if c.get("signed") is False:
            reasons.append("SIGNATURE_MISSING")
        if (
            s["purpose"] in ("registry", "translation")
            and c.get("identity_version") != self.row("entities", s["target_id"])["version"]
        ):
            reasons.append("IDENTITY_VERSION_MISMATCH")
        s.update(
            status="REJECTED" if reasons else "APPROVED",
            reasons=reasons,
            reviewed_minute=self.w["minute"],
        )
        return self.public(s)

    def evidence(self, id, target, purpose):
        s = self.row("submissions", id)
        self.check(
            s["status"] == "APPROVED" and s["target_id"] == target and s["purpose"] == purpose,
            "EVIDENCE_NOT_APPROVED_FOR_TARGET",
        )
        self.check(s["contents"]["valid_until"] >= self.w["minute"], "EVIDENCE_EXPIRED")
        return s["contents"]

    def get_my_requests(self):
        return [
            self.public(r)
            for r in self.w["requests"].values()
            if r["status"] in ("PENDING", "APPROVED")
        ]

    def decide_request(self, request_id, decision):
        r = self.row("requests", request_id)
        self.check(r["status"] in ("PENDING", "APPROVED"), "REQUEST_NOT_OPEN")
        self.check(decision in ("APPROVE", "DECLINE", "REVOKE"), "INVALID_DECISION")
        op = self.row("operators", self.w["active_operator"])
        self.check(op["status"] == "ACTIVE", "OPERATOR_INACTIVE")
        if r["operation"] == "propose_authorized_disbursement":
            self.check("PAY" in op["permissions"], "PAY_PERMISSION_MISSING")
        r.update(
            status={"APPROVE": "APPROVED", "DECLINE": "DECLINED", "REVOKE": "REVOKED"}[decision],
            approved_by=op["id"],
            approved_epoch=op["epoch"],
            approval_seq=len(self.w["audit"]) + 1,
        )
        return self.public(r)

    def accept_operator_invitation(self, operator_id):
        op = self.row("operators", operator_id)
        self.check(op["status"] == "INVITED", "INVITATION_NOT_OPEN")
        op.update(status="ACTIVE", accepted_minute=self.w["minute"])
        return self.public(op)

    def switch_operator(self, operator_id):
        op = self.row("operators", operator_id)
        self.check(op["status"] == "ACTIVE", "OPERATOR_INACTIVE")
        self.w["active_operator"] = operator_id
        return self.public(op)

    def execute_request(self, request_id):
        r = self.row("requests", request_id)
        if r["status"] == "EXECUTED":
            return r["receipt"]
        self.check(r["status"] == "APPROVED", "CUSTOMER_APPROVAL_REQUIRED")
        op = self.row("operators", r["approved_by"])
        self.check(
            op["status"] == "ACTIVE" and op["epoch"] == r["approved_epoch"],
            "APPROVAL_NO_LONGER_VALID",
        )
        if r["operation"] == "propose_authorized_disbursement":
            self.check("PAY" in op["permissions"], "PAY_PERMISSION_MISSING")
        self.w["_executing_approver"] = op["id"]
        receipt = getattr(self, r["operation"])(**r["arguments"])
        self.w.pop("_executing_approver", None)
        r.update(
            status="EXECUTED",
            receipt=deepcopy(receipt),
            executed_minute=self.w["minute"],
            executed_seq=len(self.w["audit"]) + 1,
        )
        if isinstance(receipt, dict) and receipt.get("id") in self.w["payments"]:
            self.w["payments"][receipt["id"]].update(
                approved_by=op["id"], approval_seq=r["approval_seq"], request_id=request_id
            )
        r["receipt"] = deepcopy(receipt)
        return receipt

    def schedule(self, kind, target, delay, **payload):
        ev = self.new(
            "events",
            kind=kind,
            target_id=target,
            due_minute=self.w["minute"] + delay,
            status="SCHEDULED",
        )
        self.w["_event_payloads"][ev["id"]] = payload
        return ev

    def wait_for_update(self):
        evs = [e for e in self.w["events"].values() if e["status"] == "SCHEDULED"]
        if not evs:
            return dict(advanced=False, minute=self.w["minute"], events=[])
        nxt = min(e["due_minute"] for e in evs)
        # Background accounts have future dated events. Customers can inspect
        # the event time before deciding to wait; no real wall time is consumed.
        self.w["minute"] = nxt
        finished = []
        for e in sorted(evs, key=lambda x: (x["due_minute"], x["id"])):
            if e["due_minute"] != nxt:
                continue
            self.apply_event(e)
            e["status"] = "COMPLETED"
            finished.append(self.public(e))
        return dict(advanced=True, minute=nxt, events=finished)

    def _legacy_apply_event(self, ev):
        k = ev["kind"]
        id = ev["target_id"]
        p = self.w["_event_payloads"].get(ev["id"], {})
        if k == "incoming_arrival":
            r = self.row("incoming", id)
            a = self.row("accounts", r["transmitted_account_id"])
            if a["status"] == "OPEN":
                a["balance_minor"] += r["amount_minor"]
                r.update(status="CREDITED", account_id=a["id"], credited_minute=self.w["minute"])
            else:
                r.update(status="RETURNED", reason="TRANSMITTED_ACCOUNT_CLOSED")
        elif k == "activate":
            self.row("accounts", id)["status"] = "OPEN"
        elif k == "cutover":
            self.row("stores", id).update(
                account_id=p["account_id"],
                entity_id=p["entity_id"],
                routing_version=self.row("stores", id)["routing_version"] + 1,
                status="ACTIVE",
            )
        elif k == "lot_hold":
            l = self.row("fund_lots", id)
            l["held_minor"] = min(p["amount_minor"], l["remaining_minor"])
        elif k == "conversion_settle":
            r = self.row("payments", id)
            self.row("accounts", r["account_id"])["cny_minor"] += r["delivered_minor"]
            r["status"] = "SETTLED"
            r["settled_minute"] = self.w["minute"]
        elif k == "payment_settle":
            r = self.row("payments", id)
            r.update(status="SETTLED", settled_minute=self.w["minute"])
            if r.get("obligation_id"):
                self.row("obligations", r["obligation_id"])["paid_minor"] += r["amount_minor"]
        elif k == "payment_return":
            r = self.row("payments", id)
            r.update(status="RETURNED", returned_minute=self.w["minute"])
            self.row("accounts", r["account_id"])["balance_minor"] += r["refund_minor"]
            self.book("bank_return", r["account_id"], r["refund_minor"], reference=id)
        elif k == "trace":
            t = self.row("traces", id)
            r = self.row("incoming", t["incoming_id"])
            t.update(
                status="READY",
                reference=r["reference"],
                intended_account_id=r["intended_account_id"],
                amount_minor=r["amount_minor"],
            )
        elif k == "beneficiary_update":
            self.row("beneficiaries", id).update(
                bank_reference=p["bank_reference"],
                version=self.row("beneficiaries", id)["version"] + 1,
                status="VERIFIED",
            )
        elif k == "operator_role":
            op = self.row("operators", id)
            if op["status"] == "ACTIVE" and "PAY" not in op["permissions"]:
                op["permissions"].append("PAY")
        elif k == "authorization_capture":
            h = self.row("authorizations", id)
            if h["status"] == "HELD":
                self.row("accounts", h["account_id"])["balance_minor"] -= p["amount_minor"]
                h.update(status="SETTLED", captured_minor=p["amount_minor"], held_minor=0)
                self.book("card_capture", h["account_id"], -p["amount_minor"], reference=id)
        elif k == "authorization_void":
            h = self.row("authorizations", id)
            if h["status"] == "HELD" and h["void_supported"]:
                h.update(status="VOIDED", held_minor=0)
        elif k == "bank_sync":
            a = self.row("accounts", id)
            entity = self.row("entities", a["entity_id"])
            a.update(bank_version=entity["version"], bank_legal_name=entity["legal_name"])
        elif k == "platform_review":
            s = self.row("stores", id)
            a = self.row("accounts", s["account_id"])
            entity = self.row("entities", s["entity_id"])
            letter = self.row("letters", p["letter_id"])
            valid = (
                letter["account_id"] == a["id"]
                and letter["bank_version"] == entity["version"]
                and letter["legal_name"] == entity["legal_name"]
                and a["currency"] == s["required_currency"]
            )
            translated = not entity.get("translation_required") or bool(
                p.get("translation_submission_id")
            )
            s.update(
                status="ACTIVE" if valid and translated else "NEEDS_INFORMATION",
                review_reason=(
                    None
                    if valid and translated
                    else (
                        "ACCOUNT_CERTIFICATE_MISMATCH"
                        if not valid
                        else "CERTIFIED_TRANSLATION_REQUIRED"
                    )
                ),
                accepted_letter_id=p["letter_id"] if valid and translated else None,
            )
        elif k == "order_refund":
            o = self.row("orders", id)
            l = self.row("fund_lots", o["lot_id"])
            a = self.row("accounts", o["account_id"])
            q = self.row("quotas", o["quota_id"])
            amount = o["refund_minor"]
            self.check(
                l["remaining_minor"] >= amount and a["balance_minor"] >= amount,
                "REFUND_FUNDS_UNAVAILABLE",
            )
            l["remaining_minor"] -= amount
            a["balance_minor"] -= amount
            q["remaining_minor"] -= amount
            o.update(status="REFUNDED", refunded_minute=self.w["minute"])
            self.book("order_refund", a["id"], -amount, reference=id, quota_id=q["id"])
        elif k == "volume_refund":
            self.row("volume_ledger", id)["status"] = "SETTLED"
        elif k == "invoice_ready":
            pass
        elif k == "plan_activation":
            self.row("accounts", id)["plan"] = p["plan"]
        elif k == "batch_review":
            b = self.row("batches", id)
            amount = sum(self.row("incoming", i)["amount_minor"] for i in b["incoming_ids"])
            a = self.row("accounts", b["account_id"])
            a["balance_minor"] += amount
            l = self.new(
                "fund_lots",
                account_id=a["id"],
                source="EXPORT",
                remaining_minor=amount,
                held_minor=amount - b["first_release_minor"],
                conversion_allowed=True,
                batch_id=id,
            )
            q = self.new(
                "quotas",
                account_id=a["id"],
                remaining_minor=amount,
                expires_minute=200,
                source_id=id,
            )
            b.update(
                status="PARTIALLY_RELEASED",
                lot_id=l["id"],
                quota_id=q["id"],
                released_minute=self.w["minute"],
            )
            for i in b["incoming_ids"]:
                self.row("incoming", i)["status"] = "CREDITED_RESTRICTED"
            self.schedule("batch_release", id, b["remaining_release_delay"])
        elif k == "batch_release":
            b = self.row("batches", id)
            self.row("fund_lots", b["lot_id"])["held_minor"] = 0
            b["status"] = "RELEASED"
            for i in b["incoming_ids"]:
                self.row("incoming", i)["status"] = "CREDITED"
        else:
            raise BusinessError("UNKNOWN_EVENT")

    def book(self, kind, account_id, amount, **extra):
        return self.new(
            "ledger",
            kind=kind,
            account_id=account_id,
            amount_minor=amount,
            minute=self.w["minute"],
            **extra,
        )

    def available(self, account_id):
        a = self.row("accounts", account_id)
        self.check(a["status"] == "OPEN", "ACCOUNT_NOT_OPEN")
        held = sum(
            h["held_minor"]
            for h in self.w["authorizations"].values()
            if h["account_id"] == account_id
        )
        held += sum(
            r["amount_minor"]
            for r in self.w["reservations"].values()
            if r["account_id"] == account_id and r["status"] == "ACTIVE"
        )
        return a["balance_minor"] - held

    def fee(self, account_id, amount, reason):
        a = self.row("accounts", account_id)
        self.check(a["fee_balance_minor"] >= amount, "FEE_BALANCE_INSUFFICIENT")
        a["fee_balance_minor"] -= amount
        self.w["fees_minor"] += amount
        self.book("fee", account_id, -amount, reason=reason)

    def inspect_resource_occupancy(self, account_id):
        return dict(
            available_minor=self.available(account_id),
            holds=[
                self.public(x)
                for x in self.w["authorizations"].values()
                if x["account_id"] == account_id
            ],
            reservations=[
                self.public(x)
                for x in self.w["reservations"].values()
                if x["account_id"] == account_id
            ],
        )

    def propose_account_activation(self, account_id):
        a = self.row("accounts", account_id)
        self.check(a["status"] == "PROVISIONED", "ACCOUNT_NOT_PROVISIONED")
        a["status"] = "ACTIVATING"
        delay = (
            self.w["profiles"][a["profile_id"]]["activation_minutes"]
            if a.get("profile_id")
            else P["activation_delay"]
        )
        self.schedule("activate", account_id, delay)
        return self.public(a)

    def _legacy_cutover(self, store_id, account_id, submission_id):
        s = self.row("stores", store_id)
        a = self.row("accounts", account_id)
        c = self.evidence(submission_id, store_id, "ownership")
        self.check(a["status"] == "OPEN", "ACCOUNT_NOT_OPEN")
        self.check(c.get("new_entity_id") == a["entity_id"], "OWNERSHIP_EVIDENCE_MISMATCH")
        self.check(s["status"] == "ACTIVE", "PLATFORM_CHANGE_IN_PROGRESS")
        s["status"] = "CUTOVER_PENDING"
        self.schedule(
            "cutover", store_id, P["cutover_delay"], account_id=account_id, entity_id=a["entity_id"]
        )
        return self.public(s)

    def propose_account_retirement(self, account_id):
        a = self.row("accounts", account_id)
        self.check(
            a["status"] == "OPEN" and a["balance_minor"] == 0 and a["cny_minor"] == 0,
            "CLOSURE_REQUIRES_OPEN_ZERO_BALANCE",
        )
        a["status"] = "CLOSED"
        a["closed_minute"] = self.w["minute"]
        return self.public(a)

    def _legacy_balance_transfer(self, source_id, destination_id, amount_minor):
        a = self.row("accounts", source_id)
        b = self.row("accounts", destination_id)
        self.check(
            b["status"] == "OPEN" and a["currency"] == b["currency"], "DESTINATION_NOT_COMPATIBLE"
        )
        self.check(self.available(source_id) >= amount_minor, "BALANCE_INSUFFICIENT")
        self.check(
            not any(
                l["account_id"] == source_id and l["remaining_minor"] > 0
                for l in self.w["fund_lots"].values()
            ),
            "LOT_FUNDS_REQUIRE_SOURCE_PRESERVING_PAYMENT",
        )
        a["balance_minor"] -= amount_minor
        b["balance_minor"] += amount_minor
        return self.book("transfer", source_id, -amount_minor, destination_id=destination_id)

    def inspect_conversion(self, lot_id, amount_minor, quota_allocations):
        l = self.row("fund_lots", lot_id)
        self.check(l["conversion_allowed"], "SOURCE_NOT_CONVERTIBLE")
        self.check(
            all(type(v) is int and v > 0 for v in quota_allocations.values())
            and sum(quota_allocations.values()) == amount_minor,
            "QUOTA_ALLOCATION_MISMATCH",
        )
        return self.new(
            "quotes",
            kind="conversion",
            lot_id=lot_id,
            account_id=l["account_id"],
            amount_minor=amount_minor,
            quota_allocations=deepcopy(quota_allocations),
            delivered_minor=amount_minor * P["conversion_rate"],
            fee_minor=P["conversion_fee"],
            expires_minute=self.w["minute"] + P["quote_validity"],
        )

    def quote(self, id, kind):
        q = self.row("quotes", id)
        self.check(q["kind"] == kind, "QUOTE_TYPE_MISMATCH")
        self.check(q["expires_minute"] >= self.w["minute"], "QUOTE_EXPIRED")
        self.check(not q.get("consumed"), "QUOTE_ALREADY_CONSUMED")
        return q

    def consume_lot(self, l, amount):
        reserved = sum(
            r["amount_minor"]
            for r in self.w["reservations"].values()
            if r["source_id"] == l["id"] and r["status"] == "ACTIVE"
        )
        self.check(
            l["remaining_minor"] - l["held_minor"] - reserved >= amount,
            "SOURCE_AVAILABLE_BALANCE_INSUFFICIENT",
        )
        self.check(
            self.available(l["account_id"]) >= amount, "ACCOUNT_AVAILABLE_BALANCE_INSUFFICIENT"
        )
        l["remaining_minor"] -= amount
        self.row("accounts", l["account_id"])["balance_minor"] -= amount

    def _legacy_conversion(self, quote_id):
        q = self.quote(quote_id, "conversion")
        l = self.row("fund_lots", q["lot_id"])
        self.check(l["conversion_allowed"], "SOURCE_NOT_CONVERTIBLE")
        for id, amount in q["quota_allocations"].items():
            quota = self.row("quotas", id)
            reserved = sum(
                r["amount_minor"]
                for r in self.w["reservations"].values()
                if r.get("quota_id") == id and r["status"] == "ACTIVE"
            )
            self.check(
                quota["account_id"] == l["account_id"]
                and quota["expires_minute"] >= self.w["minute"],
                "QUOTA_NOT_CURRENT_FOR_ACCOUNT",
            )
            self.check(
                quota["remaining_minor"] - reserved >= amount, "QUOTA_AVAILABLE_AMOUNT_INSUFFICIENT"
            )
        self.consume_lot(l, q["amount_minor"])
        for id, amount in q["quota_allocations"].items():
            self.w["quotas"][id]["remaining_minor"] -= amount
        self.fee(l["account_id"], q["fee_minor"], "conversion")
        q["consumed"] = True
        r = self.new(
            "payments",
            kind="CONVERSION",
            status="PROCESSING",
            account_id=l["account_id"],
            lot_id=l["id"],
            amount_minor=q["amount_minor"],
            delivered_minor=q["delivered_minor"],
            quota_allocations=deepcopy(q["quota_allocations"]),
            submitted_minute=self.w["minute"],
        )
        self.schedule("conversion_settle", r["id"], P["conversion_delay"])
        self.book("conversion", l["account_id"], -q["amount_minor"], reference=r["id"])
        return self.public(r)

    def propose_cny_withdrawal(self, account_id, beneficiary_id, amount_minor):
        a = self.row("accounts", account_id)
        b = self.row("beneficiaries", beneficiary_id)
        self.check(
            b["status"] == "VERIFIED" and b["currency"] == "CNY" and a["status"] == "OPEN",
            "RECIPIENT_OR_ACCOUNT_NOT_READY",
        )
        self.check(a["cny_minor"] >= amount_minor, "CNY_BALANCE_INSUFFICIENT")
        a["cny_minor"] -= amount_minor
        r = self.new(
            "payments",
            kind="CNY_WITHDRAWAL",
            account_id=account_id,
            beneficiary_id=beneficiary_id,
            amount_minor=amount_minor,
            currency="CNY",
            status="PROCESSING",
            submitted_minute=self.w["minute"],
        )
        self.schedule("payment_settle", r["id"], P["payment_delay"])
        return self.public(r)

    def inspect_lot_payment(self, lot_id, beneficiary_id, amount_minor):
        l = self.row("fund_lots", lot_id)
        b = self.row("beneficiaries", beneficiary_id)
        self.check(b["status"] == "VERIFIED" and b["currency"] == "USD", "RECIPIENT_NOT_READY")
        return self.new(
            "quotes",
            kind="lot_payment",
            lot_id=lot_id,
            account_id=l["account_id"],
            beneficiary_id=beneficiary_id,
            beneficiary_version=b["version"],
            amount_minor=amount_minor,
            fee_minor=(
                P["export_payment_fee"] if l["source"] == "EXPORT" else P["restricted_payment_fee"]
            ),
            expires_minute=self.w["minute"] + P["quote_validity"],
        )

    def propose_lot_payment(self, quote_id):
        q = self.quote(quote_id, "lot_payment")
        l = self.row("fund_lots", q["lot_id"])
        b = self.row("beneficiaries", q["beneficiary_id"])
        self.check(b["version"] == q["beneficiary_version"], "BENEFICIARY_VERSION_CHANGED")
        self.consume_lot(l, q["amount_minor"])
        self.fee(l["account_id"], q["fee_minor"], "lot_payment")
        q["consumed"] = True
        r = self.new(
            "payments",
            kind="LOT_PAYMENT",
            account_id=l["account_id"],
            lot_id=l["id"],
            beneficiary_id=b["id"],
            amount_minor=q["amount_minor"],
            status="PROCESSING",
            submitted_minute=self.w["minute"],
        )
        self.schedule("payment_settle", r["id"], P["payment_delay"])
        self.book("lot_payment", l["account_id"], -q["amount_minor"], reference=r["id"])
        return self.public(r)

    def propose_bank_trace(self, incoming_id):
        i = self.row("incoming", incoming_id)
        self.check("intended_account_id" in i, "TRACE_UNAVAILABLE")
        t = self.new("traces", incoming_id=incoming_id, status="REQUESTED")
        self.schedule("trace", t["id"], P["review_delay"])
        return self.public(t)

    def propose_reservation_cancellation(self, reservation_id):
        r = self.row("reservations", reservation_id)
        self.check(r["status"] == "ACTIVE", "RESERVATION_NOT_ACTIVE")
        r["status"] = "CANCELLED"
        return self.public(r)

    def propose_credit_reversal(self, incoming_id):
        i = self.row("incoming", incoming_id)
        self.check(i["status"] == "CREDITED", "INCOMING_NOT_CREDITED")
        self.check(
            not any(
                r["source_id"] == incoming_id and r["status"] == "ACTIVE"
                for r in self.w["reservations"].values()
            ),
            "CREDIT_OCCUPIED",
        )
        a = self.row("accounts", i["account_id"])
        self.check(self.available(a["id"]) >= i["amount_minor"], "BALANCE_INSUFFICIENT")
        a["balance_minor"] -= i["amount_minor"]
        for q in self.w["quotas"].values():
            if q["source_id"] == incoming_id:
                q["remaining_minor"] -= i["amount_minor"]
        i.update(status="REVERSED", previous_account_id=i["account_id"], account_id=None)
        return self.public(i)

    def propose_credit_assignment(self, incoming_id, account_id):
        i = self.row("incoming", incoming_id)
        a = self.row("accounts", account_id)
        self.check(
            i["status"] == "REVERSED" and a["status"] == "OPEN", "CREDIT_ASSIGNMENT_UNAVAILABLE"
        )
        a["balance_minor"] += i["amount_minor"]
        i.update(status="CREDITED", account_id=account_id)
        for q in self.w["quotas"].values():
            if q["source_id"] == incoming_id:
                q.update(
                    account_id=account_id, remaining_minor=q["remaining_minor"] + i["amount_minor"]
                )
        return self.public(i)

    def propose_beneficiary_update(self, beneficiary_id, submission_id):
        b = self.row("beneficiaries", beneficiary_id)
        c = self.evidence(submission_id, beneficiary_id, "bank_details")
        self.check(b["status"] == "VERIFIED", "BENEFICIARY_CHANGE_PENDING")
        b["status"] = "CHANGE_PENDING"
        self.schedule(
            "beneficiary_update",
            beneficiary_id,
            P["review_delay"],
            bank_reference=c["bank_reference"],
        )
        return self.public(b)

    def inspect_bank_payment(self, account_id, beneficiary_id, obligation_id, amount_minor):
        b = self.row("beneficiaries", beneficiary_id)
        self.row("accounts", account_id)
        self.row("obligations", obligation_id)
        self.check(b["status"] == "VERIFIED", "RECIPIENT_NOT_READY")
        return self.new(
            "quotes",
            kind="bank_payment",
            account_id=account_id,
            beneficiary_id=beneficiary_id,
            beneficiary_version=b["version"],
            obligation_id=obligation_id,
            amount_minor=amount_minor,
            fee_minor=P["bank_payment_fee"],
            expires_minute=self.w["minute"] + P["quote_validity"],
        )

    def _legacy_bank_pay(
        self, account_id, beneficiary_id, obligation_id, amount_minor, fee, method="BANK"
    ):
        a = self.row("accounts", account_id)
        b = self.row("beneficiaries", beneficiary_id)
        o = self.row("obligations", obligation_id)
        self.check(method in o["accepted_payment_methods"], "PAYMENT_METHOD_NOT_ACCEPTED")
        self.check(b["status"] == "VERIFIED" and b["currency"] == "USD", "RECIPIENT_NOT_READY")
        self.check(self.available(account_id) >= amount_minor, "BALANCE_INSUFFICIENT")
        self.check(self.w["minute"] >= o["earliest_minute"], "INVOICE_NOT_YET_PAYABLE")
        a["balance_minor"] -= amount_minor
        self.fee(account_id, fee, "bank_payment")
        r = self.new(
            "payments",
            kind="BANK_PAYMENT",
            account_id=account_id,
            beneficiary_id=beneficiary_id,
            beneficiary_version=b["version"],
            obligation_id=obligation_id,
            amount_minor=amount_minor,
            status="PROCESSING",
            submitted_minute=self.w["minute"],
        )
        self.schedule("payment_settle", r["id"], P["payment_delay"])
        return self.public(r)

    def propose_bank_payment(self, quote_id):
        q = self.quote(quote_id, "bank_payment")
        b = self.row("beneficiaries", q["beneficiary_id"])
        self.check(b["version"] == q["beneficiary_version"], "BENEFICIARY_VERSION_CHANGED")
        r = self.bank_pay(
            q["account_id"],
            q["beneficiary_id"],
            q["obligation_id"],
            q["amount_minor"],
            q["fee_minor"],
        )
        q["consumed"] = True
        return r

    def propose_request_cancellation(self, request_id):
        r = self.row("requests", request_id)
        self.check(r["status"] in ("PENDING", "APPROVED", "REVOKED"), "REQUEST_NOT_CANCELLABLE")
        r["status"] = "CANCELLED"
        return self.public(r)

    def propose_operator_role(self, operator_id):
        op = self.row("operators", operator_id)
        self.check(op["status"] == "ACTIVE", "OPERATOR_INACTIVE")
        self.schedule("operator_role", operator_id, P["review_delay"])
        return self.public(op)

    def propose_operator_revocation(self, operator_id):
        op = self.row("operators", operator_id)
        self.check(op["id"] != "owner" and op["status"] == "ACTIVE", "OPERATOR_NOT_REVOCABLE")
        op.update(status="REVOKED", epoch=op["epoch"] + 1, revoked_minute=self.w["minute"])
        return self.public(op)

    def propose_authorized_disbursement(
        self, account_id, beneficiary_id, obligation_id, amount_minor
    ):
        return self.bank_pay(
            account_id, beneficiary_id, obligation_id, amount_minor, 0, "AUTHORIZED_BANK"
        )

    def inspect_card_authorizations(self, card_id):
        self.row("cards", card_id)
        return [
            self.public(r) for r in self.w["authorizations"].values() if r["card_id"] == card_id
        ]

    def propose_authorization_void(self, authorization_id):
        h = self.row("authorizations", authorization_id)
        self.check(h["status"] == "HELD", "AUTHORIZATION_NOT_OPEN")
        self.schedule("authorization_void", authorization_id, 15)
        return dict(id=h["id"], status="VOID_REQUESTED", hold_released=False)

    def propose_card_funding_source(self, card_id, account_id):
        c = self.row("cards", card_id)
        a = self.row("accounts", account_id)
        self.check(a["status"] == "OPEN", "ACCOUNT_NOT_OPEN")
        c["account_id"] = account_id
        return self.public(c)

    def propose_card_purchase(self, card_id, obligation_id, amount_minor):
        c = self.row("cards", card_id)
        o = self.row("obligations", obligation_id)
        self.check(c["status"] == "ACTIVE", "CARD_NOT_ACTIVE")
        self.check(
            self.available(c["account_id"]) >= amount_minor, "AVAILABLE_BALANCE_INSUFFICIENT"
        )
        self.check("CARD" in o["accepted_payment_methods"], "PAYMENT_METHOD_NOT_ACCEPTED")
        self.check(self.w["minute"] >= o["earliest_minute"], "INVOICE_NOT_YET_PAYABLE")
        self.row("accounts", c["account_id"])["balance_minor"] -= amount_minor
        self.fee(c["account_id"], P["card_payment_fee"], "card_purchase")
        r = self.new(
            "payments",
            kind="CARD_PURCHASE",
            card_id=card_id,
            account_id=c["account_id"],
            obligation_id=o["id"],
            beneficiary_id="ADBOARD",
            amount_minor=amount_minor,
            status="PROCESSING",
            submitted_minute=self.w["minute"],
        )
        self.schedule("payment_settle", r["id"], P["payment_delay"])
        return self.public(r)

    def propose_bank_identity_sync(self, account_id):
        a = self.row("accounts", account_id)
        self.check(a["status"] == "OPEN", "ACCOUNT_NOT_OPEN")
        self.schedule("bank_sync", account_id, P["bank_sync_delay"])
        return self.public(a)

    def issue_bank_letter(self, account_id):
        a = self.row("accounts", account_id)
        self.check(a["status"] == "OPEN", "ACCOUNT_NOT_OPEN")
        return self.new(
            "letters",
            account_id=account_id,
            entity_id=a["entity_id"],
            bank_version=a["bank_version"],
            legal_name=a["bank_legal_name"],
            issued_minute=self.w["minute"],
        )

    def _legacy_reverification(
        self, store_id, letter_id, submission_id, translation_submission_id=""
    ):
        s = self.row("stores", store_id)
        self.row("letters", letter_id)
        self.evidence(submission_id, s["entity_id"], "registry")
        if translation_submission_id:
            self.evidence(translation_submission_id, s["entity_id"], "translation")
        self.check(s["status"] in ("PAUSED", "NEEDS_INFORMATION"), "PLATFORM_REVIEW_NOT_OPEN")
        s["status"] = "UNDER_REVIEW"
        self.schedule(
            "platform_review",
            store_id,
            P["platform_review_delay"],
            letter_id=letter_id,
            translation_submission_id=translation_submission_id,
        )
        return self.public(s)

    def propose_order_refund(self, order_id):
        o = self.row("orders", order_id)
        self.check(o["status"] == "REFUND_REQUESTED", "REFUND_NOT_OPEN")
        self.check(self.available(o["account_id"]) >= o["refund_minor"], "REFUND_FUNDS_UNAVAILABLE")
        o["status"] = "REFUND_PROCESSING"
        self.schedule("order_refund", order_id, 30)
        return self.public(o)

    def _legacy_sales_import(self, account_id, submission_id):
        self.row("accounts", account_id)
        c = self.evidence(submission_id, account_id, "sales")
        created = []
        existing = {q["source_id"] for q in self.w["quotas"].values()}
        for order in c["sales"]:
            if (
                order["eligible"]
                and order["expires_minute"] >= self.w["minute"]
                and order["order_reference"] not in existing
            ):
                q = self.new(
                    "quotas",
                    account_id=account_id,
                    remaining_minor=order["amount_minor"],
                    expires_minute=order["expires_minute"],
                    source_id=order["order_reference"],
                )
                created.append(self.public(q))
                existing.add(order["order_reference"])
        return dict(created_quotas=created, cash_created_minor=0)

    def effective_plan(self, account_id):
        a = self.row("accounts", account_id)
        plan = a.get("plan", "STANDARD")
        volume = sum(
            r["amount_minor"]
            for r in self.w["volume_ledger"].values()
            if r["account_id"] == a.get("linked_account_id")
            and r["status"] == "SETTLED"
            and r["age_days"] <= 90
            and r["kind"] in ("EXTERNAL_SETTLED", "REFUND")
        )
        return "STANDARD" if plan == "SHARED" and volume < P["shared_r90_minimum"] else plan

    def price(self, account_id, amount):
        plan = self.effective_plan(account_id)
        p = P["plans"][plan]
        return max(p["minimum_minor"], (amount * p["rate_bp"] + 9999) // 10000)

    def inspect_plan_charge(self, account_id, obligation_id):
        o = self.row("obligations", obligation_id)
        return dict(
            plan=self.effective_plan(account_id),
            amount_minor=o["amount_minor"],
            fee_minor=self.price(account_id, o["amount_minor"]),
            as_of_minute=self.w["minute"],
        )

    def propose_plan_switch(self, account_id, plan):
        a = self.row("accounts", account_id)
        self.check(plan in P["plans"], "UNKNOWN_PLAN")
        p = P["plans"][plan]
        self.fee(account_id, p["activation_fee"], "plan_activation")
        if p["delay"]:
            self.schedule("plan_activation", account_id, p["delay"], plan=plan)
        else:
            a["plan"] = plan
        return dict(
            account_id=account_id,
            requested_plan=plan,
            activation_minute=self.w["minute"] + p["delay"],
        )

    def propose_priced_payment(self, account_id, obligation_id, amount_minor):
        a = self.row("accounts", account_id)
        o = self.row("obligations", obligation_id)
        self.check(o["account_id"] == account_id, "INVOICE_ACCOUNT_MISMATCH")
        self.check(self.w["minute"] >= o["earliest_minute"], "INVOICE_NOT_YET_PAYABLE")
        self.check(self.available(account_id) >= amount_minor, "BALANCE_INSUFFICIENT")
        self.check("PLAN_BANK" in o["accepted_payment_methods"], "PAYMENT_METHOD_NOT_ACCEPTED")
        self.fee(account_id, self.price(account_id, amount_minor), "priced_payment")
        a["balance_minor"] -= amount_minor
        r = self.new(
            "payments",
            kind="PRICED_PAYMENT",
            account_id=account_id,
            obligation_id=obligation_id,
            beneficiary_id=o["beneficiary_id"],
            amount_minor=amount_minor,
            status="PROCESSING",
            submitted_minute=self.w["minute"],
        )
        self.schedule("payment_settle", r["id"], P["payment_delay"])
        return self.public(r)

    def propose_batch_review(self, batch_id, binding_submission_id, remittance_submission_id):
        b = self.row("batches", batch_id)
        c = self.evidence(binding_submission_id, batch_id, "binding")
        d = self.evidence(remittance_submission_id, batch_id, "remittance")
        self.check(
            c["sample_incoming_id"] == d["sample_incoming_id"] == b["sample_incoming_id"],
            "SAMPLE_LINK_MISMATCH",
        )
        self.check(b["status"] == "AWAITING_EVIDENCE", "BATCH_REVIEW_NOT_OPEN")
        b["status"] = "UNDER_REVIEW"
        self.schedule("batch_review", batch_id, P["review_delay"])
        return self.public(b)

    def record_case_outcome(self, outcome, summary):
        self.check(
            outcome in ("COMPLETED", "PENDING_EXTERNAL_ACTION", "CUSTOMER_DECLINED", "ESCALATED"),
            "INVALID_OUTCOME",
        )
        self.w["_closure_state"] = deepcopy(
            {k: v for k, v in self.w.items() if k not in ("audit", "_closure_state")}
        )
        self.w["case_outcome"] = dict(
            outcome=outcome, summary=summary, minute=self.w["minute"], seq=len(self.w["audit"]) + 1
        )
        return dict(recorded=True, outcome=outcome)

    def propose_collection_application(self, entity_id, profile_id):
        e = self.row("entities", entity_id)
        p = self.row("profiles", profile_id)
        self.check(e["region"] == p["region"], "PROFILE_REGISTRATION_REGION_MISMATCH")
        return self.new(
            "accounts",
            label=p["product_name"],
            entity_id=entity_id,
            status="PROVISIONED",
            currency=p["currency"],
            balance_minor=0,
            cny_minor=0,
            fee_balance_minor=0,
            bank_version=e["version"],
            bank_legal_name=e["legal_name"],
            product="COLLECTION_PROFILE",
            profile_id=profile_id,
            customer_access=True,
        )

    def inspect_profile_pricing(self, account_id, amount_minor):
        a = self.row("accounts", account_id)
        self.check(a.get("profile_id") in self.w["profiles"], "ACCOUNT_NOT_COLLECTION_PROFILE")
        p = self.row("profiles", a["profile_id"])
        return dict(
            profile_id=p["id"],
            currency=p["currency"],
            fee_minor=max(p["minimum_fee_minor"], (amount_minor * p["fee_bp"] + 9999) // 10000),
            amount_minor=amount_minor,
            minimum_payment_minor=p["minimum_payment_minor"],
            daily_limit_minor=p["daily_limit_minor"],
        )

    def propose_profile_payout(self, account_id, beneficiary_id, amount_minor):
        a = self.row("accounts", account_id)
        q = self.inspect_profile_pricing(account_id, amount_minor)
        p = self.row("profiles", q["profile_id"])
        b = self.row("beneficiaries", beneficiary_id)
        self.check(
            a["status"] == "OPEN" and b["status"] == "VERIFIED" and b["currency"] == a["currency"],
            "PROFILE_RECIPIENT_OR_ACCOUNT_NOT_READY",
        )
        self.check(amount_minor >= p["minimum_payment_minor"], "PROFILE_MINIMUM_PAYMENT")
        used = sum(
            r["amount_minor"]
            for r in self.w["payments"].values()
            if r["account_id"] == account_id
            and r.get("kind") == "PROFILE_PAYMENT"
            and r["submitted_minute"] // 1440 == self.w["minute"] // 1440
        )
        self.check(used + amount_minor <= p["daily_limit_minor"], "PROFILE_DAILY_CAP")
        self.check(self.available(account_id) >= amount_minor, "BALANCE_INSUFFICIENT")
        a["balance_minor"] -= amount_minor
        self.fee(account_id, q["fee_minor"], "profile_payout")
        r = self.new(
            "payments",
            kind="PROFILE_PAYMENT",
            account_id=account_id,
            beneficiary_id=beneficiary_id,
            amount_minor=amount_minor,
            currency=a["currency"],
            status="PROCESSING",
            submitted_minute=self.w["minute"],
        )
        self.schedule("payment_settle", r["id"], p["settlement_minutes"])
        return self.public(r)

    def frontier(self):
        return P["frontier"]

    def check_covenants(self, account_id):
        for c in self.w["covenants"].values():
            if (
                c["account_id"] == account_id
                and c["status"] == "ACTIVE"
                and self.w["minute"] < c["until_minute"]
                and self.row("accounts", account_id)["balance_minor"] < c["minimum_minor"]
            ):
                c.update(status="BREACHED", breached_minute=self.w["minute"])
                self.fee(
                    account_id,
                    self.frontier()["covenant_breach_fee_minor"],
                    "reserve_covenant_breach",
                )

    def propose_balance_transfer(self, source_id, destination_id, amount_minor):
        r = self._legacy_balance_transfer(source_id, destination_id, amount_minor)
        self.check_covenants(source_id)
        return r

    def propose_platform_cutover(self, store_id, account_id, submission_id):
        a = self.row("accounts", account_id)
        self.check(
            a["balance_minor"] >= a.get("cutover_minimum_minor", 0), "ROUTING_DEPOSIT_INSUFFICIENT"
        )
        r = self._legacy_cutover(store_id, account_id, submission_id)
        for c in self.w["covenants"].values():
            if c["account_id"] == account_id and c["status"] == "DORMANT":
                c["status"] = "ACTIVE"
        return r

    def propose_receivable_advance(self, incoming_id, destination_id, amount_minor):
        i = self.row("incoming", incoming_id)
        a = self.row("accounts", destination_id)
        self.check(i["status"] == "IN_TRANSIT" and a["status"] == "OPEN", "ADVANCE_NOT_AVAILABLE")
        used = sum(
            r["amount_minor"]
            for r in self.w["advances"].values()
            if r["incoming_id"] == incoming_id
        )
        self.check(used + amount_minor <= i.get("advance_limit_minor", 0), "ADVANCE_LIMIT_EXCEEDED")
        self.fee(destination_id, self.frontier()["advance_fee_minor"], "receivable_advance")
        a["balance_minor"] += amount_minor
        return self.new(
            "advances",
            incoming_id=incoming_id,
            account_id=destination_id,
            amount_minor=amount_minor,
            status="OUTSTANDING",
            submitted_minute=self.w["minute"],
        )

    def propose_conversion(self, quote_id):
        q = self.row("quotes", quote_id)
        for id in q.get("quota_allocations", {}):
            self.check(
                self.row("quotas", id).get("valid_from_minute", -(10**9)) <= self.w["minute"],
                "QUOTA_NOT_YET_EFFECTIVE",
            )
        r = self._legacy_conversion(quote_id)
        for l in self.w["fund_lots"].values():
            if l["account_id"] == r["account_id"] and l.pop("review_on_first_conversion", False):
                self.schedule("lot_hold", l["id"], 10, amount_minor=100000)
                l["next_update_minute"] = self.w["minute"] + 10
        return r

    def source_policy(self, lot):
        return self.frontier()["source_policies"].get(
            lot["source"],
            dict(
                convertible=lot["conversion_allowed"],
                beneficiary_segments=["GENERAL", "LICENSED"],
                fee_minor=(
                    P["export_payment_fee"]
                    if lot["source"] == "EXPORT"
                    else P["restricted_payment_fee"]
                ),
            ),
        )

    def inspect_source_payment(self, beneficiary_id, allocations):
        b = self.row("beneficiaries", beneficiary_id)
        self.check(b["status"] == "VERIFIED" and b["currency"] == "USD", "RECIPIENT_NOT_READY")
        self.check(
            bool(allocations) and all(type(v) is int and v > 0 for v in allocations.values()),
            "ALLOCATION_INVALID",
        )
        lots = [self.row("fund_lots", id) for id in allocations]
        accounts = {l["account_id"] for l in lots}
        self.check(len(accounts) == 1, "ALLOCATION_ACCOUNT_MISMATCH")
        for l in lots:
            self.check(
                b.get("segment", "GENERAL") in self.source_policy(l)["beneficiary_segments"],
                "SOURCE_CHANNEL_NOT_PERMITTED",
            )
        return self.new(
            "quotes",
            kind="source_payment",
            account_id=lots[0]["account_id"],
            beneficiary_id=beneficiary_id,
            beneficiary_version=b["version"],
            allocations=deepcopy(allocations),
            amount_minor=sum(allocations.values()),
            fee_minor=sum(self.source_policy(l)["fee_minor"] for l in lots),
            expires_minute=self.w["minute"] + 20,
        )

    def propose_source_payment(self, quote_id):
        q = self.quote(quote_id, "source_payment")
        b = self.row("beneficiaries", q["beneficiary_id"])
        self.check(b["version"] == q["beneficiary_version"], "BENEFICIARY_VERSION_CHANGED")
        for id, amount in q["allocations"].items():
            l = self.row("fund_lots", id)
            self.check(
                b.get("segment", "GENERAL") in self.source_policy(l)["beneficiary_segments"],
                "SOURCE_CHANNEL_NOT_PERMITTED",
            )
            self.consume_lot(l, amount)
        self.fee(q["account_id"], q["fee_minor"], "source_payment")
        q["consumed"] = True
        r = self.new(
            "payments",
            kind="SOURCE_PAYMENT",
            account_id=q["account_id"],
            beneficiary_id=b["id"],
            amount_minor=q["amount_minor"],
            allocations=deepcopy(q["allocations"]),
            status="PROCESSING",
            submitted_minute=self.w["minute"],
        )
        self.schedule("payment_settle", r["id"], 10)
        return self.public(r)

    def inspect_lot_payment(self, lot_id, beneficiary_id, amount_minor):
        q = self.inspect_source_payment(beneficiary_id, {lot_id: amount_minor})
        q["kind"] = "lot_payment"
        q["lot_id"] = lot_id
        return q

    def bank_pay(self, account_id, beneficiary_id, obligation_id, amount_minor, fee, method="BANK"):
        self.check(
            not any(
                l["account_id"] == account_id and l["remaining_minor"] > 0
                for l in self.w["fund_lots"].values()
            ),
            "SOURCE_ALLOCATION_REQUIRED",
        )
        o = self.row("obligations", obligation_id)
        if method == "AUTHORIZED_BANK":
            # The executing request carries the actual approver, not whichever
            # identity happens to be selected in the user portal now.
            op = self.row("operators", self.w["_executing_approver"])
            policy = op.get("limit_policy")
            if policy:
                self.check(
                    o.get("approver_scope") in op.get("beneficiary_scopes", []),
                    "DELEGATION_SCOPE_MISMATCH",
                )
                used = sum(
                    p["amount_minor"]
                    for p in self.w["payments"].values()
                    if p.get("approved_by") == op["id"] and p.get("submitted_minute", -1) >= 0
                )
                self.check(
                    used + amount_minor <= self.frontier()["operator_limits"][policy],
                    "DELEGATED_DAILY_LIMIT_EXCEEDED",
                )
            fee = 100
        return self._legacy_bank_pay(
            account_id, beneficiary_id, obligation_id, amount_minor, fee, method
        )

    def propose_credit_note_allocation(self, note_id, obligation_id, amount_minor):
        n = self.row("credit_notes", note_id)
        o = self.row("obligations", obligation_id)
        self.check(
            obligation_id in n["eligible_obligations"]
            and o.get("credit_eligible")
            and n["remaining_minor"] >= amount_minor,
            "CREDIT_NOT_AVAILABLE_FOR_INVOICE",
        )
        if o.get("cash_discount_claimed"):
            o["amount_minor"] += self.frontier()["cash_discount_minor"]
            o["cash_discount_claimed"] = False
        n["remaining_minor"] -= amount_minor
        o["credit_minor"] = o.get("credit_minor", 0) + amount_minor
        return self.book(
            "commercial_credit",
            o["account_id"],
            amount_minor,
            note_id=note_id,
            obligation_id=obligation_id,
        )

    def propose_invoice_cash_discount(self, obligation_id):
        o = self.row("obligations", obligation_id)
        self.check(
            o.get("cash_discount_eligible")
            and not o.get("cash_discount_claimed")
            and not o.get("credit_minor"),
            "CASH_CONCESSION_NOT_AVAILABLE",
        )
        self.check(
            self.w["minute"] <= self.frontier()["cash_discount_deadline"] - P["payment_delay"],
            "CASH_CONCESSION_WINDOW_CLOSED",
        )
        o["amount_minor"] -= self.frontier()["cash_discount_minor"]
        o["cash_discount_claimed"] = True
        return self.public(o)

    def propose_capture_refund(self, authorization_id):
        h = self.row("authorizations", authorization_id)
        self.check(
            h["status"] == "SETTLED"
            and h.get("refundable_minor", 0) > 0
            and not h.get("refund_requested"),
            "REFUND_NOT_AVAILABLE",
        )
        h["refund_requested"] = True
        self.schedule("capture_refund", authorization_id, 25)
        return dict(
            authorization_id=authorization_id,
            status="REFUND_REQUESTED",
            amount_minor=h["refundable_minor"],
        )

    def propose_platform_intake(self, store_id, submission_id):
        s = self.row("stores", store_id)
        self.evidence(submission_id, s["entity_id"], "registry")
        self.check(
            self.w["minute"] <= s["intake_closes_minute"] and "intake_minute" not in s,
            "INTAKE_NOT_OPEN",
        )
        s.update(
            intake_minute=self.w["minute"],
            intake_identity_version=self.row("entities", s["entity_id"])["version"],
        )
        return self.public(s)

    def issue_attested_bank_letter(self, account_id, addressee):
        r = self.issue_bank_letter(account_id)
        r["addressee"] = addressee
        return r

    def propose_platform_reverification(
        self, store_id, letter_id, submission_id, translation_submission_id=""
    ):
        s = self.row("stores", store_id)
        if "intake_closes_minute" in s:
            self.check("intake_minute" in s, "REVIEW_CASE_NOT_REGISTERED")
        return self._legacy_reverification(
            store_id, letter_id, submission_id, translation_submission_id
        )

    def propose_conversion_allocation_amendment(self, payment_id, quota_allocations):
        p = self.row("payments", payment_id)
        self.check(
            p["kind"] == "CONVERSION" and p["status"] == "SETTLED", "SETTLED_CONVERSION_REQUIRED"
        )
        self.check(
            bool(quota_allocations)
            and all(type(v) is int and v > 0 for v in quota_allocations.values())
            and sum(quota_allocations.values()) == p["amount_minor"],
            "QUOTA_ALLOCATION_MISMATCH",
        )
        previous = p["quota_allocations"]
        for id, amount in quota_allocations.items():
            q = self.row("quotas", id)
            occupied = sum(
                r["amount_minor"]
                for r in self.w["reservations"].values()
                if r.get("quota_id") == id and r["status"] == "ACTIVE"
            )
            self.check(
                q["account_id"] == p["account_id"]
                and q.get("valid_from_minute", -(10**9))
                <= p["submitted_minute"]
                <= q["expires_minute"]
                and self.w["minute"] <= q["expires_minute"],
                "QUOTA_NOT_VALID_FOR_HISTORICAL_LEG",
            )
            self.check(
                q["remaining_minor"] + previous.get(id, 0) - occupied >= amount,
                "AMENDMENT_CAPACITY_INSUFFICIENT",
            )
        for id, amount in previous.items():
            self.w["quotas"][id]["remaining_minor"] += amount
        for id, amount in quota_allocations.items():
            self.w["quotas"][id]["remaining_minor"] -= amount
        p["quota_allocations"] = deepcopy(quota_allocations)
        p["allocation_amended_minute"] = self.w["minute"]
        self.fee(
            p["account_id"], self.frontier()["allocation_amendment_fee_minor"], "quota_amendment"
        )
        return self.public(p)

    def propose_sales_import(self, account_id, submission_id):
        r = self._legacy_sales_import(account_id, submission_id)
        sales = self.row("submissions", submission_id)["contents"]["sales"]
        for q in r["created_quotas"]:
            source = next(s for s in sales if s["order_reference"] == q["source_id"])
            self.w["quotas"][q["id"]]["valid_from_minute"] = source.get(
                "valid_from_minute", self.w["minute"]
            )
            q["valid_from_minute"] = self.w["quotas"][q["id"]]["valid_from_minute"]
        return r

    def propose_tariff_enrolment(self, account_id, plan, slot_minute):
        self.row("accounts", account_id)
        self.check(
            plan in self.frontier()["tariffs"]
            and slot_minute in self.frontier()["tariff_slots"]
            and slot_minute >= self.w["minute"],
            "TARIFF_ENROLMENT_NOT_AVAILABLE",
        )
        for old in self.w["tariff_instructions"].values():
            if (
                old["account_id"] == account_id
                and old["slot_minute"] == slot_minute
                and old["status"] == "SCHEDULED"
            ):
                old["status"] = "SUPERSEDED"
        r = self.new(
            "tariff_instructions",
            account_id=account_id,
            plan=plan,
            slot_minute=slot_minute,
            status="SCHEDULED",
        )
        if slot_minute == self.w["minute"]:
            self.activate_tariff(r)
        else:
            self.schedule("tariff_activation", r["id"], slot_minute - self.w["minute"])
        return self.public(r)

    def activate_tariff(self, r):
        if r["status"] != "SCHEDULED":
            return
        a = self.row("accounts", r["account_id"])
        if a["tariff"] != r["plan"]:
            self.fee(
                a["id"],
                self.frontier()["tariffs"][r["plan"]]["activation_minor"],
                "tariff_activation",
            )
        a["tariff"] = r["plan"]
        r["status"] = "EFFECTIVE"

    def tariff_price(self, account_id, amount_minor):
        a = self.row("accounts", account_id)
        plan = a["tariff"]
        if plan == "PARTNER":
            volume = sum(
                v["amount_minor"]
                for v in self.w["volume_ledger"].values()
                if v["account_id"] == a["linked_account_id"]
                and v["status"] == "SETTLED"
                and v["kind"] in ("EXTERNAL_SETTLED", "REFUND")
                and v["age_days"] <= 90
            )
            if volume < P["shared_r90_minimum"]:
                plan = "BASIC"
        t = self.frontier()["tariffs"][plan]
        lower = (
            min(amount_minor, max(0, t["threshold_minor"] - a["tariff_period_volume_minor"]))
            if t["threshold_minor"]
            else amount_minor
        )
        fee = max(
            t["minimum_minor"],
            (lower * t["bp"] + (amount_minor - lower) * t["upper_bp"] + 9999) // 10000,
        )
        return fee, plan

    def inspect_tariff_quote(self, account_id, obligation_id):
        o = self.row("obligations", obligation_id)
        self.check(o["account_id"] == account_id, "INVOICE_ACCOUNT_MISMATCH")
        fee, plan = self.tariff_price(account_id, o["amount_minor"])
        return dict(
            amount_minor=o["amount_minor"],
            fee_minor=fee,
            effective_plan=plan,
            as_of_minute=self.w["minute"],
        )

    def propose_tariff_payment(self, account_id, obligation_id):
        a = self.row("accounts", account_id)
        o = self.row("obligations", obligation_id)
        self.check(
            o["account_id"] == account_id and "TARIFF_BANK" in o["accepted_payment_methods"],
            "TARIFF_INVOICE_REQUIRED",
        )
        self.check(
            o["paid_minor"] == 0
            and not any(
                p.get("obligation_id") == obligation_id and p["status"] == "PROCESSING"
                for p in self.w["payments"].values()
            ),
            "WHOLE_INVOICE_ALREADY_SUBMITTED",
        )
        self.check(
            self.w["minute"] >= o["earliest_minute"]
            and self.available(account_id) >= o["amount_minor"],
            "INVOICE_NOT_PAYABLE",
        )
        fee, plan = self.tariff_price(account_id, o["amount_minor"])
        self.fee(account_id, fee, "tariff_payment")
        a["balance_minor"] -= o["amount_minor"]
        a["tariff_period_volume_minor"] += o["amount_minor"]
        r = self.new(
            "payments",
            kind="TARIFF_PAYMENT",
            account_id=account_id,
            obligation_id=obligation_id,
            beneficiary_id=o["beneficiary_id"],
            amount_minor=o["amount_minor"],
            tariff=plan,
            fee_minor=fee,
            status="PROCESSING",
            submitted_minute=self.w["minute"],
        )
        self.schedule("payment_settle", r["id"], 10)
        return self.public(r)

    def propose_tariff_rebate(self, account_id):
        a = self.row("accounts", account_id)
        self.check(
            not any(r["account_id"] == account_id for r in self.w["rebates"].values()),
            "REBATE_ALREADY_CLAIMED",
        )
        qualifying = sum(
            p["amount_minor"]
            for p in self.w["payments"].values()
            if p.get("account_id") == account_id
            and p.get("tariff") == "COMMIT"
            and p["status"] == "SETTLED"
            and self.row("obligations", p["obligation_id"]).get("qualifying_rebate")
        )
        self.check(
            qualifying >= self.frontier()["commit_qualifying_minimum_minor"],
            "QUALIFYING_VOLUME_BELOW_THRESHOLD",
        )
        amount = self.frontier()["commit_rebate_minor"]
        a["fee_balance_minor"] += amount
        self.w["fees_minor"] -= amount
        self.book("tariff_rebate", account_id, amount)
        return self.new(
            "rebates",
            account_id=account_id,
            amount_minor=amount,
            qualifying_minor=qualifying,
            status="POSTED",
        )

    def propose_source_reclassification(self, lot_id, amount_minor, submission_id):
        l = self.row("fund_lots", lot_id)
        b = self.row("batches", l["batch_id"])
        e = self.evidence(submission_id, b["id"], "source_goods")
        self.check(
            e["reference"] == b["reference"] and e["sample_incoming_id"] == b["sample_incoming_id"],
            "CLASSIFICATION_EVIDENCE_MISMATCH",
        )
        file_id = self.row("submissions", submission_id)["file_id"]
        used = sum(
            p.get("goods_amount_minor", 0)
            for p in self.w["fund_lots"].values()
            if p.get("classification_submission_id")
            and self.row("submissions", p["classification_submission_id"])["file_id"] == file_id
        )
        self.check(
            l["source"] == "SERVICE"
            and not l.get("classification_pending")
            and amount_minor <= e["goods_amount_minor"] - used
            and l["remaining_minor"] - l["held_minor"] >= amount_minor,
            "RECLASSIFICATION_AMOUNT_NOT_SUPPORTED",
        )
        l["classification_pending"] = True
        self.fee(
            l["account_id"], self.frontier()["source_review_fee_minor"], "source_reclassification"
        )
        self.schedule(
            "source_reclassification",
            lot_id,
            self.frontier()["source_review_delay"],
            amount_minor=amount_minor,
            submission_id=submission_id,
        )
        return dict(lot_id=lot_id, status="UNDER_REVIEW", amount_minor=amount_minor)

    def apply_event(self, ev):
        k = ev["kind"]
        id = ev["target_id"]
        p = self.w["_event_payloads"].get(ev["id"], {})
        if k == "covenant_expiry":
            c = self.row("covenants", id)
            if c["status"] in ("ACTIVE", "DORMANT"):
                c["status"] = "RELEASED"
        elif k == "trace" and self.row("incoming", self.row("traces", id)["incoming_id"]).get(
            "mandate_id"
        ):
            t = self.row("traces", id)
            i = self.row("incoming", t["incoming_id"])
            t.update(
                status="READY",
                reference=i["reference"],
                mandate_id=i["mandate_id"],
                value_minute=i["value_minute"],
                original_payee_entity=i["original_payee_entity"],
                amount_minor=i["amount_minor"],
            )
        elif k == "authorization_void" and self.row("authorizations", id).get(
            "_partial_capture_on_void_minor"
        ):
            h = self.row("authorizations", id)
            if h["status"] == "HELD":
                amount = h["_partial_capture_on_void_minor"]
                self.row("accounts", h["account_id"])["balance_minor"] -= amount
                h.update(
                    status="SETTLED",
                    held_minor=0,
                    captured_minor=amount,
                    refundable_minor=amount,
                    merchant_advice="DUPLICATE_CAPTURE_CONFIRMED",
                )
                self.book("card_capture", h["account_id"], -amount, reference=id)
        elif k == "capture_refund":
            h = self.row("authorizations", id)
            self.row("accounts", h["account_id"])["balance_minor"] += h["refundable_minor"]
            h.update(
                refunded_minor=h["refundable_minor"], refundable_minor=0, refund_status="SETTLED"
            )
            self.book("card_refund", h["account_id"], h["refunded_minor"], reference=id)
        elif k == "registry_revision":
            self.row("entities", id).update(version=p["version"], legal_name=p["legal_name"])
        elif k == "tariff_activation":
            self.activate_tariff(self.row("tariff_instructions", id))
        elif k == "source_reclassification":
            l = self.row("fund_lots", id)
            l["remaining_minor"] -= p["amount_minor"]
            l["classification_pending"] = False
            self.new(
                "fund_lots",
                account_id=l["account_id"],
                source="MARKET_B",
                remaining_minor=p["amount_minor"],
                held_minor=0,
                conversion_allowed=True,
                batch_id=l["batch_id"],
                classification_submission_id=p["submission_id"],
                goods_amount_minor=p["amount_minor"],
            )
        elif k == "batch_release" and self.row("batches", id).get("lot_ids"):
            b = self.row("batches", id)
            for lid in b["lot_ids"]:
                self.row("fund_lots", lid)["held_minor"] = 0
            b["status"] = "RELEASED"
            for iid in b["incoming_ids"]:
                self.row("incoming", iid)["status"] = "CREDITED"
        else:
            self._legacy_apply_event(ev)
            if k == "incoming_arrival" and self.row("incoming", id)["status"] == "CREDITED":
                i = self.row("incoming", id)
                a = self.row("accounts", i["account_id"])
                for loan in self.w["advances"].values():
                    if loan["incoming_id"] == id and loan["status"] == "OUTSTANDING":
                        a["balance_minor"] -= loan["amount_minor"]
                        loan.update(status="REPAID", repaid_minute=self.w["minute"])
            if k == "platform_review":
                s = self.row("stores", id)
                letter = self.row("letters", p["letter_id"])
                if s.get("required_addressee") and (
                    letter.get("addressee") != s["required_addressee"]
                    or self.w["minute"] - letter["issued_minute"] > s["letter_max_age_minutes"]
                ):
                    s.update(
                        status="NEEDS_INFORMATION",
                        review_reason="CERTIFICATE_SCOPE_OR_AGE",
                        accepted_letter_id=None,
                    )
            if k == "batch_review" and self.row("batches", id).get("classification_review"):
                b = self.row("batches", id)
                l = self.row("fund_lots", b["lot_id"])
                l.update(source="MARKET_B", remaining_minor=250000, held_minor=0)
                service = self.new(
                    "fund_lots",
                    account_id=b["account_id"],
                    source="SERVICE",
                    remaining_minor=150000,
                    held_minor=0,
                    conversion_allowed=False,
                    batch_id=id,
                    source_review_available=True,
                )
                late = self.new(
                    "fund_lots",
                    account_id=b["account_id"],
                    source="MARKET_B",
                    remaining_minor=300000,
                    held_minor=300000,
                    conversion_allowed=True,
                    batch_id=id,
                )
                b["lot_ids"] = [l["id"], service["id"], late["id"]]
