"""Distinct recursive business extensions. No task id or acceptance data enters tools."""

from copy import deepcopy
from payment.runtime.products.customer_access.engine import BusinessError
from payment.runtime.products.credit.specification import (
    TABLES,
    EXTRA_TABLES,
    CAPABILITIES,
    NEW_CAPABILITIES,
)


class CreditAndAuthority:

    def search_records(self, resource, filters=None, cursor=0, limit=20):
        if resource not in EXTRA_TABLES:
            return self.next_handler(CreditAndAuthority).search_records(
                resource, filters, cursor, limit
            )
        self.check(0 < limit <= 100 and cursor >= 0, "INVALID_PAGE")
        rows = []
        for row in self.w[resource].values():
            matched = True
            for k, v in (filters or {}).items():
                self.check(not k.startswith("_"), "FIELD_NOT_PUBLIC")
                x = row.get(k)
                if isinstance(v, dict):
                    for op, a in v.items():
                        self.check(op in ("in", "contains", "gte", "lte"), "UNSUPPORTED_FILTER")
                        matched &= (
                            x in a
                            if op == "in"
                            else (
                                str(a).lower() in str(x).lower()
                                if op == "contains"
                                else x is not None and (x >= a if op == "gte" else x <= a)
                            )
                        )
                else:
                    matched &= x == v
            if matched:
                rows.append(self.public(row))
        rows.sort(key=lambda r: r["id"])
        keys = ("id", "reference", "status", "account_id", "group_id", "incoming_id", "name")
        return dict(
            records=[{k: r[k] for k in keys if k in r} for r in rows[cursor : cursor + limit]],
            total=len(rows),
            next_cursor=cursor + limit if cursor + limit < len(rows) else None,
        )

    def get_record(self, resource, record_id):
        if resource in EXTRA_TABLES:
            return self.public(self.row(resource, record_id))
        return self.next_handler(CreditAndAuthority).get_record(resource, record_id)

    def call_discoverable_agent_tool(self, capability_name, arguments):
        if capability_name not in NEW_CAPABILITIES:
            return self.next_handler(CreditAndAuthority).call_discoverable_agent_tool(
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
        return self.new(
            "requests",
            operation=capability_name,
            arguments=deepcopy(arguments),
            status="PENDING",
            prepared_minute=self.w["minute"],
        )

    def propose_bridge_draw(self, facility_id, account_id, amount_minor):
        f = self.row("bridge_facilities", facility_id)
        a = self.row("accounts", account_id)
        self.check(
            not f.get("outstanding_minor")
            and account_id in f["eligible_accounts"]
            and amount_minor <= f["limit_minor"],
            "FACILITY_LIMIT_OR_ELIGIBILITY",
        )
        a["balance_minor"] += amount_minor
        f.update(
            outstanding_minor=amount_minor,
            account_id=account_id,
            status="DRAWN",
            drawn_minute=self.w["minute"],
        )
        if f.get("collateral_source"):
            f["collateral_encumbered"] = True
        return self.public(f)

    def propose_bridge_repayment(self, facility_id):
        f = self.row("bridge_facilities", facility_id)
        a = self.row("accounts", f["account_id"])
        amount = f.get("outstanding_minor", 0)
        self.check(
            amount > 0 and self.inspect_resource_occupancy(a["id"])["available_minor"] >= amount,
            "REPAYMENT_NOT_FUNDED",
        )
        a["balance_minor"] -= amount
        f.update(outstanding_minor=0, status="REPAID", repaid_minute=self.w["minute"])
        return self.public(f)

    def propose_credit_redemption(self, note_id, method):
        r = self.row("credit_redemptions", note_id)
        n = self.row("credit_notes", note_id)
        self.check(
            r["status"] == "UNREDEEMED" and method in ("EARLY", "SETTLEMENT"), "REDEMPTION_NOT_OPEN"
        )
        if method == "SETTLEMENT":
            self.check(
                self.row("obligations", r["trigger_obligation"])["paid_minor"]
                >= r["trigger_paid_minor"],
                "SETTLEMENT_NOT_RECORDED",
            )
        loss = r["early_haircut_minor"] if method == "EARLY" else 0
        n["remaining_minor"] -= loss
        r.update(
            status="REDEEMED", method=method, haircut_minor=loss, redeemed_minute=self.w["minute"]
        )
        return self.public(r)

    def propose_credit_note_allocation(self, note_id, obligation_id, amount_minor):
        if note_id in self.w["credit_redemptions"]:
            self.check(
                self.w["credit_redemptions"][note_id]["status"] == "REDEEMED", "CREDIT_NOT_REDEEMED"
            )
        return self.next_handler(CreditAndAuthority).propose_credit_note_allocation(
            note_id, obligation_id, amount_minor
        )

    def propose_legalization_booking(self, slot_id, mode):
        s = self.row("legalization_slots", slot_id)
        self.check(
            s["status"] == "OPEN"
            and self.w["minute"] <= s["book_by_minute"]
            and mode in ("CURRENT", "FORWARD"),
            "SLOT_NOT_BOOKABLE",
        )
        a = self.row("accounts", s["account_id"])
        ent = self.row("entities", a["entity_id"])
        version = a["bank_version"] if mode == "CURRENT" else ent["next_identity_version"]
        s.update(
            status="BOOKED", mode=mode, identity_version=version, booked_minute=self.w["minute"]
        )
        return self.public(s)

    def propose_letter_legalization(self, slot_id, letter_id):
        s = self.row("legalization_slots", slot_id)
        l = self.row("letters", letter_id)
        self.check(
            s["status"] == "BOOKED"
            and l["account_id"] == s["account_id"]
            and l["bank_version"] == s["identity_version"],
            "LETTER_BOOKING_VERSION_MISMATCH",
        )
        s.update(status="PROCESSING", letter_id=letter_id)
        self.schedule("recursive_legalization", slot_id, s["processing_minutes"])
        return self.public(s)

    def propose_marketplace_adjustment(self, adjustment_id, classification):
        r = self.row("marketplace_adjustments", adjustment_id)
        self.check(
            r["status"] == "NOTIFIED" and classification in ("SALES_RETURN", "SERVICE_FEE"),
            "ADJUSTMENT_NOT_OPEN",
        )
        if classification == "SALES_RETURN":
            a = self.row("portal_accounts", r["account_id"])
            self.check(a["quota_minor"] >= r["amount_minor"], "QUOTA_DEBIT_NOT_AVAILABLE")
            a["quota_minor"] -= r["amount_minor"]
        r.update(status="BOOKED", classification=classification, booked_minute=self.w["minute"])
        return self.public(r)

    def propose_delegate_adoption(self, delegate_id):
        d = self.row("portal_delegates", delegate_id)
        sec = self.row("portal_security", d["account_id"])
        self.check(
            d["status"] != "REVOKED"
            and sec.get("verified_epoch") == sec["epoch"]
            and sec.get("verified_channel") == "SMS"
            and sec.get("verified_until", -1) >= self.w["minute"],
            "CURRENT_SMS_REQUIRED",
        )
        d.update(status="ACTIVE", adopted_epoch=sec["epoch"])
        sec["recovery_owner"] = delegate_id
        return self.public(d)

    def propose_delegate_revocation(self, delegate_id):
        d = self.row("portal_delegates", delegate_id)
        sec = self.row("portal_security", d["account_id"])
        self.check(d["status"] == "ACTIVE", "DELEGATE_NOT_ACTIVE")
        d["status"] = "REVOKED"
        sec["epoch"] += 1
        sec["verified_epoch"] = None
        if sec.get("recovery_owner") == delegate_id:
            sec["recovery_suspended"] = True
        return self.public(d)

    def propose_portal_contact_change(self, account_id, submission_id):
        self.check(
            not self.row("portal_security", account_id).get("recovery_suspended"),
            "RECOVERY_OWNER_REVOKED",
        )
        return self.next_handler(CreditAndAuthority).propose_portal_contact_change(
            account_id, submission_id
        )

    def propose_export_allocation(self, declaration_id, account_id, amount_minor):
        d = self.row("export_declarations", declaration_id)
        a = self.row("portal_accounts", account_id)
        used = sum(
            r["amount_minor"]
            for r in self.w["export_allocations"].values()
            if r["declaration_id"] == declaration_id
            and r["status"] in ("RESERVED", "CONSUMED")
            and r["declaration_version"] == d["version"]
        )
        self.check(
            d["status"] == "RELEASED"
            and amount_minor <= d["capacity_minor"] - used
            and account_id in d["permitted_accounts"],
            "DECLARATION_CAPACITY_OR_SCOPE",
        )
        return self.new(
            "export_allocations",
            account_id=account_id,
            declaration_id=declaration_id,
            declaration_version=d["version"],
            exporter=d["exporter"],
            amount_minor=amount_minor,
            status="RESERVED",
        )

    def propose_export_release(self, allocation_id):
        r = self.row("export_allocations", allocation_id)
        self.check(r["status"] in ("RESERVED", "STALE"), "ALLOCATION_NOT_RELEASABLE")
        r["status"] = "RELEASED"
        return self.public(r)

    def propose_volume_certificate(self, group_id, basis):
        self.check(basis in ("CURRENT_ROLLING", "LOCKED_PERIOD"), "UNKNOWN_CERTIFICATION_BASIS")
        g = self.row("portal_groups", group_id)
        sources = [
            self.public(v)
            for v in self.w["portal_volume"].values()
            if v["group_id"] == group_id
            and v["status"] == "SETTLED"
            and v["external"]
            and 0 <= self.w["minute"] - v["value_minute"] <= 90 * 1440
        ]
        pending = [
            self.public(v)
            for v in self.w["portal_volume"].values()
            if basis == "LOCKED_PERIOD"
            and v["group_id"] == group_id
            and v["status"] == "PENDING"
            and v["external"]
            and v["amount_minor"] < 0
            and any(
                e["target_id"] == v["id"]
                and e["status"] == "SCHEDULED"
                and e["due_minute"] <= self.w["minute"] + g["certificate_valid_minutes"]
                for e in self.w["events"].values()
            )
        ]
        return self.new(
            "volume_certificates",
            pending_debits=pending,
            group_id=group_id,
            basis=basis,
            issued_minute=self.w["minute"],
            expires_minute=self.w["minute"] + g["certificate_valid_minutes"],
            sources=sources,
            net_minor=sum(v["amount_minor"] for v in sources + pending),
            status="ISSUED",
        )

    def propose_return_routing(self, incoming_id, route_id):
        i = self.row("portal_incoming", incoming_id)
        r = self.row("return_routes", route_id)
        self.check(
            r["kind"] == "ROUTE" and i["currency"] == r["currency"] and i["status"] == "HELD",
            "RETURN_ROUTE_SCOPE",
        )
        i["dispatch_route"] = route_id
        return self.public(i)

    def propose_return_allocation(self, refund_id, instruction_id):
        r = self.row("portal_refunds", refund_id)
        i = self.row("return_routes", instruction_id)
        self.check(
            r["status"] == "SETTLED"
            and r.get("buyer_allocation_status") == "PENDING"
            and i.get("bank_reference") == r["bank_reference"]
            and i["kind"] == "INSTRUCTION"
            and i["valid_from"] <= r["settled_minute"] <= i["valid_to"],
            "ALLOCATION_INSTRUCTION_NOT_CURRENT",
        )
        self.check(
            not any(a["refund_id"] == refund_id for a in self.w["return_allocations"].values()),
            "ALREADY_ALLOCATED",
        )
        r["buyer_allocation_status"] = "CREDITED"
        return self.new(
            "return_allocations",
            allocated_minute=self.w["minute"],
            refund_id=refund_id,
            instruction_id=instruction_id,
            owner=i["owner"],
            bank_reference=i["bank_reference"],
            delivered_minor=r["delivered_minor"],
            status="CREDITED",
        )

    def propose_card_limit_transfer(self, source_id, destination_id, amount_minor):
        a = self.row("card_limits", source_id)
        b = self.row("card_limits", destination_id)
        self.check(
            a["pool"] == b["pool"]
            and a["daily_limit_minor"] >= amount_minor
            and source_id != destination_id,
            "LIMIT_TRANSFER_SCOPE",
        )
        a["daily_limit_minor"] -= amount_minor
        b["daily_limit_minor"] += amount_minor
        return dict(source=self.public(a), destination=self.public(b))

    def propose_portal_card_purchase(self, card_id, invoice_id, amount_minor):
        if card_id in self.w["card_limits"]:
            l = self.w["card_limits"][card_id]
            used = sum(
                h["held_minor"] + h["captured_minor"]
                for h in self.w["portal_holds"].values()
                if h["account_id"] in self.w["portal_cards"][card_id]["eligible_accounts"]
            )
            used += sum(
                p["amount_minor"]
                for p in self.w["portal_payments"].values()
                if p.get("card_id") == card_id
            )
            self.check(
                amount_minor + used <= l["daily_limit_minor"], "DAILY_CARD_CAPACITY_EXCEEDED"
            )
        return self.next_handler(CreditAndAuthority).propose_portal_card_purchase(
            card_id, invoice_id, amount_minor
        )

    def propose_fee_guarantee(self, account_id, coverage, amount_minor):
        a = self.row("portal_accounts", account_id)
        self.check(
            coverage in ("CASH", "MARGIN") and a["fee_balance_minor"] >= amount_minor,
            "GUARANTEE_COVERAGE_OR_BALANCE",
        )
        a["fee_balance_minor"] -= amount_minor
        return self.new(
            "fee_guarantees",
            account_id=account_id,
            coverage=coverage,
            deposit_minor=amount_minor,
            status="HELD",
            coverage_minor=amount_minor * 1000,
            created_minute=self.w["minute"],
        )

    def propose_fee_guarantee_release(self, guarantee_id):
        g = self.row("fee_guarantees", guarantee_id)
        self.check(
            g["status"] == "HELD"
            and (
                not g.get("payment_id")
                or self.row("portal_payments", g["payment_id"])["status"] == "SETTLED"
            ),
            "GUARANTEE_STILL_REQUIRED",
        )
        self.row("portal_accounts", g["account_id"])["fee_balance_minor"] += g["deposit_minor"]
        g["status"] = "RELEASED"
        return self.public(g)

    def propose_portal_payment(self, quote_id):
        q = self.row("quotes", quote_id)
        a = self.row("portal_accounts", q["account_id"])
        if a.get("declaration_required"):
            rows = [
                r
                for r in self.w["export_allocations"].values()
                if r["account_id"] == a["id"]
                and r["status"] == "RESERVED"
                and r["declaration_version"]
                == self.row("export_declarations", r["declaration_id"])["version"]
            ]
            self.check(
                sum(r["amount_minor"] for r in rows) == q["amount_minor"],
                "CURRENT_DECLARATION_COVERAGE_MISMATCH",
            )
        g = None
        if a.get("tariff_continuity_balance") and a.get("shared_agreement"):
            g = next(
                (
                    g
                    for g in self.w["fee_guarantees"].values()
                    if g["account_id"] == a["id"]
                    and g["status"] == "HELD"
                    and g["coverage"] == "MARGIN"
                    and g["coverage_minor"] >= q["amount_minor"]
                ),
                None,
            )
        p = self.next_handler(CreditAndAuthority).propose_portal_payment(quote_id)
        if a.get("declaration_required"):
            for r in rows:
                r.update(status="CONSUMED", payment_id=p["id"])
        if a.get("tariff_continuity_balance") and a.get("shared_agreement"):
            deposit = g or next(
                (
                    x
                    for x in self.w["fee_guarantees"].values()
                    if x["account_id"] == a["id"]
                    and x["status"] == "HELD"
                    and not x.get("payment_id")
                ),
                None,
            )
            if deposit:
                deposit["payment_id"] = p["id"]
            if not g and a["balance_minor"] < a["tariff_continuity_balance"]:
                self.pfee(
                    a["id"], a["continuity_surcharge_minor"], "relationship_continuity_adjustment"
                )
                self.w["portal_payments"][p["id"]]["continuity_surcharge_minor"] = a[
                    "continuity_surcharge_minor"
                ]
        return self.public(self.w["portal_payments"][p["id"]])

    def apply_event(self, ev):
        k = ev["kind"]
        id = ev["target_id"]
        if k == "recursive_legalization":
            s = self.row("legalization_slots", id)
            s.update(status="SEALED", sealed_minute=self.w["minute"])
            return
        if k == "recursive_card_commitment":
            l = self.row("card_limits", id)
            l["commitment_status"] = (
                "HONORED" if l["daily_limit_minor"] >= l["committed_minor"] else "DECLINED"
            )
            return
        if k == "portal_group_review":
            r = self.row("portal_applications", id)
            g = self.row("portal_groups", r["group_id"])
            certs = [
                x
                for x in self.w["volume_certificates"].values()
                if x["group_id"] == g["id"]
                and x["basis"] == "LOCKED_PERIOD"
                and x["expires_minute"] >= self.w["minute"]
            ]
            if certs:
                cert = max(certs, key=lambda x: x["issued_minute"])
                active = sum(
                    m["group_id"] == g["id"] and m["status"] == "ACTIVE"
                    for m in self.w["portal_members"].values()
                )
                good = active >= 10 and cert["net_minor"] > 15000000
                r.update(
                    status="APPROVED" if good else "REJECTED",
                    active_children=active,
                    net_r90_minor=cert["net_minor"],
                    certificate_id=cert["id"],
                    reviewed_minute=self.w["minute"],
                )
                if good:
                    g["capacity"] += 5
                return
        self.next_handler(CreditAndAuthority).apply_event(ev)
        if k == "portal_rmb_activation":
            for d in self.w["export_declarations"].values():
                if d.get("amend_on_activation") == id and d["version"] == 1:
                    d.update(capacity_minor=d["amended_capacity_minor"], version=2)
                    for r in self.w["export_allocations"].values():
                        if r["declaration_id"] == d["id"] and r["status"] == "RESERVED":
                            r["status"] = "STALE"
        if k == "portal_phone_review":
            app = self.row("portal_applications", id)
            sec = self.row("portal_security", app["account_id"])
            if sec.get("recovery_suspended"):
                app["status"] = "REJECTED"
                sec["phone_reachable"] = False

    def propose_portal_return(self, incoming_id, amount_minor, route, recipient_id, submission_id):
        incoming = self.row("portal_incoming", incoming_id)
        instruction = incoming.get("dispatch_route") or incoming.get("default_dispatch_route")
        routing = self.row("return_routes", instruction) if instruction else None
        if routing and route == "ALTERNATIVE":
            self.check(
                routing["bank_reference"]
                == self.row("portal_recipients", recipient_id)["bank_reference"],
                "ROUTE_BANK_NOT_AUTHORIZED_BY_RECIPIENT",
            )
        r = self.next_handler(CreditAndAuthority).propose_portal_return(
            incoming_id, amount_minor, route, recipient_id, submission_id
        )
        if routing:
            r = self.w["portal_refunds"][r["id"]]
            r.update(
                bank_reference=routing["bank_reference"],
                dispatch_route=instruction,
                buyer_allocation_status=(
                    "PENDING" if routing["mode"] == "AGGREGATE" else "DIRECT_DISPATCH"
                ),
            )
        return self.public(r)
