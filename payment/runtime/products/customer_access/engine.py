"""Atomic Portal transitions layered over the preserved CORE business engine."""

from copy import deepcopy
from decimal import Decimal, ROUND_HALF_EVEN
from payment.runtime.products.banking.engine import BusinessError
from payment.runtime.products.banking.schema import TABLES as CORE_TABLES
from payment.runtime.products.customer_access.specification import (
    TABLES,
    NEW_CAPABILITIES,
    CAPABILITIES,
    P,
)


class CustomerPortal:
    def schedule(self, kind, target, delay, **payload):
        ev = self.next_handler(CustomerPortal).schedule(kind, target, delay, **payload)
        if kind == "portal_card_capture":
            # Confirmed merchant advice is the sole source of the future debit.
            # It is queryable before the event; no parallel hidden amount exists.
            ev.update(
                amount_minor=self.w["_event_payloads"][ev["id"]].pop("amount_minor"),
                currency="USD",
                advice_status="CONFIRMED",
                advice_version=1,
            )
        return ev

    def search_records(self, resource, filters=None, cursor=0, limit=20):
        if resource not in TABLES:
            return self.next_handler(CustomerPortal).search_records(
                resource, filters, cursor, limit
            )
        self.check(0 < limit <= 100 and cursor >= 0, "INVALID_PAGE")
        filters = filters or {}

        def match(r):
            for k, v in filters.items():
                self.check(not k.startswith("_"), "FIELD_NOT_PUBLIC")
                x = r.get(k)
                if isinstance(v, dict):
                    for op, a in v.items():
                        self.check(op in ("in", "contains", "gte", "lte"), "UNSUPPORTED_FILTER")
                        if op == "in" and x not in a:
                            return False
                        if op == "contains" and str(a).lower() not in str(x).lower():
                            return False
                        if op == "gte" and (x is None or x < a):
                            return False
                        if op == "lte" and (x is None or x > a):
                            return False
                elif x != v:
                    return False
            return True

        rows = [
            self.public(r)
            for _, r in sorted(self.w[resource].items())
            if r.get("customer_access", True) and match(r)
        ]
        keys = (
            "id",
            "name",
            "label",
            "reference",
            "status",
            "account_id",
            "entity_id",
            "source_id",
            "amount_minor",
            "currency",
            "group_id",
        )
        return dict(
            records=[{k: r[k] for k in keys if k in r} for r in rows[cursor : cursor + limit]],
            total=len(rows),
            next_cursor=cursor + limit if cursor + limit < len(rows) else None,
        )

    def get_record(self, resource, record_id):
        if resource not in TABLES:
            return self.next_handler(CustomerPortal).get_record(resource, record_id)
        return self.public(self.row(resource, record_id))

    def call_discoverable_agent_tool(self, capability_name, arguments):
        if capability_name not in NEW_CAPABILITIES:
            return self.next_handler(CustomerPortal).call_discoverable_agent_tool(
                capability_name, arguments
            )
        self.check(capability_name in self.w["unlocked"], "TOOL_NOT_UNLOCKED")
        s = NEW_CAPABILITIES[capability_name]
        self.check(set(arguments) == set(s["parameters"]), "INVALID_CAPABILITY_ARGUMENTS")
        for k, typ in s["parameters"].items():
            self.check(
                type(arguments[k]) is {"string": str, "integer": int}[typ],
                "INVALID_CAPABILITY_ARGUMENT_TYPE",
            )
            if typ == "integer":
                self.check(arguments[k] > 0, "INVALID_NUMERIC_VALUE")
        if capability_name.startswith("propose_"):
            return self.new(
                "requests",
                operation=capability_name,
                arguments=deepcopy(arguments),
                status="PENDING",
                prepared_minute=self.w["minute"],
            )
        return getattr(self, capability_name)(**arguments)

    def upload_file(self, file_id, target_id, purpose):
        if not any(target_id in self.w[t] for t in TABLES):
            return self.next_handler(CustomerPortal).upload_file(file_id, target_id, purpose)
        f = self.row("files", file_id)
        return self.new(
            "submissions",
            file_id=file_id,
            target_id=target_id,
            purpose=purpose,
            contents=deepcopy(f["contents"]),
            status="SUBMITTED",
            submitted_minute=self.w["minute"],
        )

    def available_portal(self, id):
        a = self.row("portal_accounts", id)
        self.check(a["status"] == "OPEN", "ACCOUNT_NOT_OPEN")
        return a["balance_minor"] - sum(
            h["held_minor"] for h in self.w["portal_holds"].values() if h["account_id"] == id
        )

    def pfee(self, account_id, amount, reason):
        a = self.row("portal_accounts", account_id)
        self.check(a["fee_balance_minor"] >= amount, "FEE_BALANCE_INSUFFICIENT")
        a["fee_balance_minor"] -= amount
        self.w["fees_minor"] += amount
        self.book("portal_fee", account_id, -amount, reason=reason)

    def propose_portal_authorization(self, store_id, account_id, action):
        s = self.row("portal_stores", store_id)
        a = self.row("portal_accounts", account_id)
        self.check(
            a["entity_id"] == s["entity_id"] and a["status"] == "OPEN",
            "STORE_ENTITY_OR_ACCOUNT_MISMATCH",
        )
        self.check(action in ("AUTHORIZE", "CANCEL"), "UNKNOWN_AUTHORIZATION_ACTION")
        if action == "AUTHORIZE":
            self.check(
                s.get("seller_binding") in (None, account_id)
                and s.get("authorized_account") in (None, account_id),
                "SELLER_ASSOCIATION_HELD",
            )
            s.update(seller_binding=account_id, authorized_account=account_id, status="ACTIVE")
            self.schedule(
                "portal_sales_backfill",
                store_id,
                P["authorization_backfill"],
                account_id=account_id,
            )
        else:
            self.check(
                s.get("authorized_account") == account_id, "NO_ACTIVE_AUTHORIZATION_FOR_ACCOUNT"
            )
            s.update(seller_binding=None, authorized_account=None, status="UNAUTHORIZED")
        return self.public(s)

    def credit_orders(self, store_id, account_id, orders, automatic):
        a = self.row("portal_accounts", account_id)
        added = 0
        items = []
        for record in orders:
            oid = record["id"]
            existing = self.w["portal_orders"].get(oid)
            age = self.w["minute"] // 1440 - record["sale_day"]
            valid = (
                record["store_id"] == store_id
                and record["status"] == "Delivered"
                and type(record["amount_minor"]) is int
                and record["amount_minor"] > 0
                and 0 <= age <= (180 if automatic else 365)
            )
            duplicate = bool(existing and existing.get("credited"))
            amount = record["amount_minor"] if valid and not duplicate else 0
            if amount:
                self.w["portal_orders"][oid] = {
                    **deepcopy(record),
                    "credited": True,
                    "credited_account": account_id,
                    "credited_minute": self.w["minute"],
                }
                a["quota_minor"] += amount
                added += amount
            items.append(
                dict(
                    order_id=oid,
                    added_minor=amount,
                    reason="DUPLICATE" if duplicate else "ELIGIBLE" if valid else "NOT_ELIGIBLE",
                )
            )
        return dict(added_quota_minor=added, items=items, account_id=account_id)

    def propose_portal_sales_import(self, store_id, submission_id):
        s = self.row("portal_stores", store_id)
        self.check(s["status"] == "ACTIVE", "AUTHORIZATION_REQUIRED")
        c = self.evidence(submission_id, store_id, "sales")
        return self.credit_orders(store_id, s["authorized_account"], c["orders"], False)

    def inspect_portal_payment(self, account_id, recipient_id, amount_minor, currency, reference):
        a = self.row("portal_accounts", account_id)
        b = self.row("portal_recipients", recipient_id)
        self.check(currency in ("USD", "CNY") and b["currency"] == currency, "CURRENCY_MISMATCH")
        fee = (
            P["cny_fee_minor"]
            if currency == "CNY"
            else max(
                a["fee_floor_minor"],
                int(
                    (Decimal(amount_minor) * a["rate_bp"] / 10000).quantize(
                        Decimal(1), rounding=ROUND_HALF_EVEN
                    )
                ),
            )
        )
        self.check(
            (
                5000 <= amount_minor <= 9000000000
                if currency == "USD"
                else 1500 <= amount_minor * P["cny_rate"] - fee <= 500000000
            ),
            "PORTAL_PAYMENT_AMOUNT_OUT_OF_RANGE",
        )
        q = self.new(
            "quotes",
            kind="portal_payment",
            account_id=account_id,
            recipient_id=recipient_id,
            amount_minor=amount_minor,
            principal_currency="USD",
            currency=currency,
            fee_currency=currency,
            reference=reference,
            fee_minor=fee,
            delivered_minor=(
                amount_minor * P["cny_rate"] - fee if currency == "CNY" else amount_minor
            ),
            recipient_version=b["version"],
            security_epoch=self.w["portal_security"].get(account_id, {}).get("epoch", 0),
            tariff_version=a["tariff_version"],
            expires_minute=self.w["minute"] + P["quote_valid"],
        )
        return self.public(q)

    def propose_portal_payment(self, quote_id):
        q = self.row("quotes", quote_id)
        self.check(q["kind"] == "portal_payment", "WRONG_QUOTE_KIND")
        a = self.row("portal_accounts", q["account_id"])
        b = self.row("portal_recipients", q["recipient_id"])
        self.check(q["expires_minute"] >= self.w["minute"], "QUOTE_EXPIRED")
        self.check(
            q["tariff_version"] == a["tariff_version"] and q["recipient_version"] == b["version"],
            "TERMS_VERSION_CHANGED",
        )
        self.check(b["status"] == "VERIFIED", "RECIPIENT_NOT_VERIFIED")
        sec = self.w["portal_security"].get(a["id"])
        if sec and sec["required"]:
            self.check(q["security_epoch"] == sec["epoch"], "SECURITY_VERSION_CHANGED")
            self.check(
                sec.get("verified_epoch") == sec["epoch"]
                and sec.get("verified_channel") == "SMS"
                and sec.get("verified_until", -1) >= self.w["minute"],
                "CURRENT_SMS_VERIFICATION_REQUIRED",
            )
        self.check(self.available_portal(a["id"]) >= q["amount_minor"], "BALANCE_INSUFFICIENT")
        if q["currency"] == "CNY":
            self.check(a["quota_minor"] >= q["amount_minor"], "QUOTA_INSUFFICIENT")
            self.check(
                a["region"] != "HK" or a.get("rmb_status") == "ACTIVE",
                "RMB_REGISTRATION_NOT_ACTIVE",
            )
            a["quota_minor"] -= q["amount_minor"]
        else:
            self.pfee(a["id"], q["fee_minor"], "bank_payment")
        a["balance_minor"] -= q["amount_minor"]
        r = self.new(
            "portal_payments",
            **{k: v for k, v in q.items() if k not in ("id", "kind")},
            status="PROCESSING",
            submitted_minute=self.w["minute"],
            reporting_party=a.get("reporting_party"),
        )
        self.schedule("portal_payment_settle", r["id"], P["payment_delay"])
        self.book("portal_payment", a["id"], -q["amount_minor"], reference=r["id"])
        return self.public(r)

    def propose_portal_contact_change(self, account_id, submission_id):
        c = self.evidence(submission_id, account_id, "phone_recovery")
        sec = self.row("portal_security", account_id)
        self.check(sec["email_reachable"], "REGISTERED_EMAIL_UNAVAILABLE")
        self.check(not sec.get("pending"), "RECOVERY_IN_PROGRESS")
        r = self.new(
            "portal_applications",
            kind="phone",
            account_id=account_id,
            status="UNDER_REVIEW",
            submission_id=submission_id,
        )
        sec["pending"] = r["id"]
        self.schedule("portal_phone_review", r["id"], P["contact_review"])
        return self.public(r)

    def propose_portal_verification(self, account_id, channel):
        sec = self.row("portal_security", account_id)
        self.check(channel in ("SMS", "EMAIL"), "UNKNOWN_CHANNEL")
        return self.new(
            "portal_challenges",
            account_id=account_id,
            channel=channel,
            epoch=sec["epoch"],
            status="PENDING",
            expires_minute=self.w["minute"] + P["verification_valid"],
        )

    def complete_portal_verification(self, challenge_id):
        c = self.row("portal_challenges", challenge_id)
        s = self.row("portal_security", c["account_id"])
        self.check(
            c["status"] == "PENDING"
            and c["expires_minute"] >= self.w["minute"]
            and c["epoch"] == s["epoch"],
            "CHALLENGE_NOT_CURRENT",
        )
        self.check(
            s["phone_reachable"] if c["channel"] == "SMS" else s["email_reachable"],
            "CHANNEL_UNREACHABLE",
        )
        c["status"] = "VERIFIED"
        s.update(
            verified_epoch=s["epoch"],
            verified_channel=c["channel"],
            verified_until=c["expires_minute"],
        )
        return self.public(c)

    def propose_rmb_registration(self, account_id, reporting_party, submission_id):
        a = self.row("portal_accounts", account_id)
        self.check(a["region"] == "HK" and a["holder_type"] == "COMPANY", "HK_COMPANY_REQUIRED")
        self.check(
            reporting_party in ("RECIPIENT", "ASSOCIATED_EXPORTER"), "UNKNOWN_REPORTING_PARTY"
        )
        self.check(
            a.get("rmb_status") not in ("ACTIVE", "UNDER_REVIEW"), "RMB_REGISTRATION_ALREADY_OPEN"
        )
        if reporting_party == "RECIPIENT":
            a.update(
                rmb_status="ACTIVE", reporting_party=reporting_party, reporting_entity="RECIPIENT"
            )
            return self.public(a)
        c = self.evidence(submission_id, account_id, "exporter_registration")
        self.check(
            c.get("associated")
            and c.get("authorized")
            and c.get("legal_representative")
            and c.get("offline_signed"),
            "ASSOCIATED_EXPORTER_AUTHORIZATION_REQUIRED",
        )
        a["rmb_status"] = "UNDER_REVIEW"
        self.schedule(
            "portal_rmb_activation",
            account_id,
            P["registration_review"],
            entity=c["exporter_entity"],
        )
        return self.public(a)

    def propose_portal_recipient_verification(self, recipient_id, submission_id):
        b = self.row("portal_recipients", recipient_id)
        c = self.evidence(submission_id, recipient_id, "recipient")
        self.check(
            c.get("holder") == b["holder"] and c.get("tax_id") and c.get("bank_reference"),
            "RECIPIENT_IDENTITY_INCOMPLETE",
        )
        self.check(b["status"] != "UNDER_REVIEW", "RECIPIENT_REVIEW_OPEN")
        b["status"] = "UNDER_REVIEW"
        self.schedule(
            "portal_recipient_review",
            recipient_id,
            P["recipient_review"],
            bank_reference=c["bank_reference"],
            tax_id=c["tax_id"],
        )
        return self.public(b)

    def net_volume(self, id):
        return sum(
            v["amount_minor"]
            for v in self.w["portal_volume"].values()
            if v["group_id"] == id
            and v["status"] == "SETTLED"
            and v["external"]
            and 0 <= self.w["minute"] - v["value_minute"] <= 90 * 1440
        )

    def propose_group_invitation(self, group_id, child_id):
        g = self.row("portal_groups", group_id)
        c = self.row("portal_children", child_id)
        self.check(c["level"] in ("L1", "L3"), "CHILD_LEVEL_INELIGIBLE")
        active = [
            m
            for m in self.w["portal_members"].values()
            if m["group_id"] == group_id and m["status"] in ("PENDING", "ACTIVE", "UNBINDING")
        ]
        self.check(len(active) < g["capacity"], "GROUP_CAPACITY_FULL")
        self.check(not any(m["child_id"] == child_id for m in active), "CHILD_ALREADY_LINKED")
        return self.new("portal_members", group_id=group_id, child_id=child_id, status="PENDING")

    def respond_group_invitation(self, member_id, decision):
        m = self.row("portal_members", member_id)
        c = self.row("portal_children", m["child_id"])
        self.check(c["controlled"], "CHILD_NOT_CONTROLLED")
        self.check(
            m["status"] == "PENDING" and decision in ("ACCEPT", "DECLINE"), "INVITATION_NOT_OPEN"
        )
        m["status"] = "ACTIVE" if decision == "ACCEPT" else "CANCELLED"
        return self.public(m)

    def propose_group_unbinding(self, member_id):
        m = self.row("portal_members", member_id)
        self.check(m["status"] in ("PENDING", "ACTIVE"), "LINK_NOT_OPEN")
        if m["status"] == "PENDING":
            m["status"] = "CANCELLED"
        else:
            m["status"] = "UNBINDING"
            self.schedule("portal_group_unbind", member_id, P["unbind_delay"])
        return self.public(m)

    def propose_group_expansion(self, group_id):
        g = self.row("portal_groups", group_id)
        self.check(g["product"] == "EC" and g["level"] == "L3", "GROUP_PRODUCT_INELIGIBLE")
        self.check(
            not any(
                a.get("group_id") == group_id and a["status"] == "UNDER_REVIEW"
                for a in self.w["portal_applications"].values()
            ),
            "GROUP_REVIEW_OPEN",
        )
        r = self.new(
            "portal_applications",
            kind="group",
            group_id=group_id,
            status="UNDER_REVIEW",
            submitted_minute=self.w["minute"],
        )
        self.schedule("portal_group_review", r["id"], P["group_review"])
        return self.public(r)

    def propose_portal_bank_trace(self, incoming_id):
        self.row("portal_incoming", incoming_id)
        r = self.new("portal_traces", incoming_id=incoming_id, status="PENDING")
        self.schedule("portal_trace", r["id"], P["bank_trace_delay"])
        return self.public(r)

    def propose_portal_return(self, incoming_id, amount_minor, route, recipient_id, submission_id):
        i = self.row("portal_incoming", incoming_id)
        self.check(i["status"] in ("HELD", "PARTIALLY_RETURNED"), "RECEIPT_NOT_RETURNABLE")
        self.check(100 <= amount_minor <= i["remaining_minor"], "RETURN_AMOUNT_INVALID")
        trace = next(
            (
                t
                for t in self.w["portal_traces"].values()
                if t["incoming_id"] == incoming_id and t["status"] == "READY"
            ),
            None,
        )
        self.check(trace, "ORIGIN_ADVICE_REQUIRED")
        self.check(route in ("ORIGINAL", "ALTERNATIVE"), "UNKNOWN_RETURN_ROUTE")
        fee = 0
        if route == "ORIGINAL":
            self.check(i["original_return_supported"], "ORIGINAL_ROUTE_UNAVAILABLE")
            holder = trace["sender"]
            bank = trace["sender_bank"]
        else:
            b = self.row("portal_recipients", recipient_id)
            c = self.evidence(submission_id, incoming_id, "return_instruction")
            self.check(
                b["status"] == "VERIFIED"
                and b["holder"] == trace["sender"]
                and b["currency"] == i["currency"],
                "RETURN_DESTINATION_MISMATCH",
            )
            self.check(
                c["bank_reference"] == b["bank_reference"]
                and c["sender"] == trace["sender"]
                and c["maximum_minor"] >= amount_minor,
                "RETURN_INSTRUCTION_MISMATCH",
            )
            holder = b["holder"]
            bank = b["bank_reference"]
            fee = P["return_fee_minor"]
        self.check(amount_minor > fee, "RETURN_NET_NONPOSITIVE")
        i["remaining_minor"] -= amount_minor
        i["status"] = "RETURNING"
        r = self.new(
            "portal_refunds",
            incoming_id=incoming_id,
            amount_minor=amount_minor,
            delivered_minor=amount_minor - fee,
            fee_minor=fee,
            currency=i["currency"],
            route=route,
            holder=holder,
            bank_reference=bank,
            status="PROCESSING",
            submitted_minute=self.w["minute"],
        )
        self.schedule("portal_return_settle", r["id"], P["return_delay"])
        return self.public(r)

    def propose_portal_card_void(self, hold_id):
        h = self.row("portal_holds", hold_id)
        self.check(h["held_minor"] > 0, "NO_OPEN_HOLD")
        h["void_requested"] = True
        self.schedule("portal_card_void", hold_id, P["void_delay"])
        return self.public(h)

    def propose_portal_card_funding(self, card_id, account_id):
        card = self.row("portal_cards", card_id)
        a = self.row("portal_accounts", account_id)
        self.check(
            account_id in card["eligible_accounts"] and a["status"] == "OPEN",
            "CARD_FUNDING_INELIGIBLE",
        )
        card["funding_account"] = account_id
        return self.public(card)

    def propose_portal_card_purchase(self, card_id, invoice_id, amount_minor):
        c = self.row("portal_cards", card_id)
        o = self.row("portal_invoices", invoice_id)
        a = self.row("portal_accounts", c["funding_account"])
        self.check("CARD" in o["accepted_methods"], "CARD_NOT_ACCEPTED")
        self.check(
            self.available_portal(a["id"]) >= amount_minor, "CARD_AVAILABLE_BALANCE_INSUFFICIENT"
        )
        self.check(amount_minor <= c["per_transaction_minor"], "CARD_LIMIT_EXCEEDED")
        a["balance_minor"] -= amount_minor
        r = self.new(
            "portal_payments",
            account_id=a["id"],
            card_id=card_id,
            invoice_id=invoice_id,
            reference=o["reference"],
            amount_minor=amount_minor,
            delivered_minor=amount_minor,
            fee_minor=0,
            currency="USD",
            status="PROCESSING",
            submitted_minute=self.w["minute"],
        )
        self.schedule("portal_payment_settle", r["id"], P["card_settle_delay"])
        return self.public(r)

    def propose_portal_fee_share(self, account_id, agreement_id, submission_id):
        a = self.row("portal_accounts", account_id)
        g = self.row("portal_agreements", agreement_id)
        self.evidence(submission_id, account_id, "fee_relationship")
        self.check(
            not any(
                r.get("account_id") == account_id
                and r["kind"] == "fee"
                and r["status"] == "UNDER_REVIEW"
                for r in self.w["portal_applications"].values()
            ),
            "FEE_REVIEW_OPEN",
        )
        self.pfee(account_id, P["fee_application_minor"], "fee_share_application")
        r = self.new(
            "portal_applications",
            kind="fee",
            account_id=account_id,
            agreement_id=agreement_id,
            submission_id=submission_id,
            status="UNDER_REVIEW",
        )
        self.schedule("portal_fee_review", r["id"], P["fee_share_review"])
        return self.public(r)

    def apply_event(self, ev):
        k = ev["kind"]
        id = ev["target_id"]
        p = self.w["_event_payloads"].get(ev["id"], {})
        if not k.startswith("portal_"):
            return self.next_handler(CustomerPortal).apply_event(ev)
        if k == "portal_sales_backfill":
            s = self.row("portal_stores", id)
            if s["status"] == "ACTIVE" and s["authorized_account"] == p["account_id"]:
                orders = [r for r in self.w["portal_orders"].values() if r["store_id"] == id]
                s["last_backfill"] = self.credit_orders(id, p["account_id"], orders, True)
        elif k == "portal_payment_settle":
            self.row("portal_payments", id).update(
                status="SETTLED", settled_minute=self.w["minute"]
            )
        elif k == "portal_phone_review":
            a = self.row("portal_applications", id)
            s = self.row("portal_security", a["account_id"])
            sub = self.row("submissions", a["submission_id"])
            c = sub["contents"]
            valid = (
                c.get("photo_day") == sub["submitted_minute"] // 1440
                and c.get("handwritten")
                and c.get("original_company_photo")
                and c.get("binding_screenshot")
                and c.get("latest_remittance")
                and c.get("new_phone")
            )
            a.update(
                status="APPROVED" if valid else "REJECTED",
                reviewed_minute=self.w["minute"],
                reason=None if valid else "RECOVERY_DOCUMENT_REQUIREMENTS_NOT_MET",
            )
            s["pending"] = None
            if valid:
                s.update(
                    phone=c["new_phone"],
                    phone_reachable=True,
                    epoch=s["epoch"] + 1,
                    verified_epoch=None,
                )
        elif k == "portal_rmb_activation":
            self.row("portal_accounts", id).update(
                rmb_status="ACTIVE",
                reporting_party="ASSOCIATED_EXPORTER",
                reporting_entity=p["entity"],
            )
        elif k == "portal_recipient_review":
            b = self.row("portal_recipients", id)
            b.update(
                status="VERIFIED",
                version=b["version"] + 1,
                bank_reference=p["bank_reference"],
                tax_id=p["tax_id"],
            )
        elif k == "portal_volume_settle":
            self.row("portal_volume", id).update(status="SETTLED", value_minute=self.w["minute"])
        elif k == "portal_group_review":
            r = self.row("portal_applications", id)
            g = self.row("portal_groups", r["group_id"])
            active = sum(
                m["group_id"] == g["id"] and m["status"] == "ACTIVE"
                for m in self.w["portal_members"].values()
            )
            net = self.net_volume(g["id"])
            good = active >= 10 and net > 15000000
            r.update(
                status="APPROVED" if good else "REJECTED",
                active_children=active,
                net_r90_minor=net,
                reviewed_minute=self.w["minute"],
            )
            if good:
                g["capacity"] += 5
        elif k == "portal_group_unbind":
            self.row("portal_members", id)["status"] = "UNBOUND"
        elif k == "portal_trace":
            t = self.row("portal_traces", id)
            i = self.row("portal_incoming", t["incoming_id"])
            t.update(
                status="READY",
                reference=i["reference"],
                sender=i["_sender"],
                sender_bank=i["_sender_bank"],
                value_minute=i["_value_minute"],
            )
        elif k == "portal_return_settle":
            r = self.row("portal_refunds", id)
            r.update(status="SETTLED", settled_minute=self.w["minute"])
            i = self.row("portal_incoming", r["incoming_id"])
            i["status"] = "RETURNED" if i["remaining_minor"] == 0 else "PARTIALLY_RETURNED"
        elif k == "portal_card_void":
            h = self.row("portal_holds", id)
            if h["void_supported"]:
                h.update(held_minor=0, status="VOIDED")
        elif k == "portal_card_capture":
            h = self.row("portal_holds", id)
            amount = ev["amount_minor"]
            self.check(
                ev.get("advice_status") == "CONFIRMED"
                and type(amount) is int
                and 0 < amount <= h["held_minor"],
                "INVALID_CAPTURE_ADVICE",
            )
            if h["status"] != "VOIDED":
                self.row("portal_accounts", h["account_id"])["balance_minor"] -= amount
                h.update(
                    captured_minor=h["captured_minor"] + amount,
                    held_minor=h["held_minor"] - amount,
                    status="PARTIALLY_CAPTURED",
                )
        elif k == "portal_card_release":
            self.row("portal_holds", id).update(held_minor=0, status="CLOSED")
        elif k == "portal_fee_review":
            r = self.row("portal_applications", id)
            a = self.row("portal_accounts", r["account_id"])
            g = self.row("portal_agreements", r["agreement_id"])
            c = self.row("submissions", r["submission_id"])["contents"]
            good = (
                c.get("relationship")
                in ("ASSOCIATED_COMPANY", "EMPLOYMENT", "SPOUSE", "CLOSE_FAMILY")
                and c.get("source_holder") == g["holder"]
                and c.get("target_holder") == a["holder"]
            )
            r.update(
                status="APPROVED" if good else "REJECTED",
                reviewed_minute=self.w["minute"],
                reason=None if good else "RELATIONSHIP_NOT_ELIGIBLE",
            )
            if good:
                a.update(
                    rate_bp=max(g["rate_bp"], a["corridor_floor_bp"]),
                    tariff_version=a["tariff_version"] + 1,
                    shared_agreement=g["id"],
                )
        else:
            raise BusinessError("UNKNOWN_PORTAL_EVENT")
