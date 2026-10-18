"""Local bank transitions; never reads task acceptance or a reference strategy."""

from copy import deepcopy
from functools import partial

from payment.runtime.products.trade.records import TABLES
from payment.runtime.products.trade.specification import NEW, CAPABILITIES, NAMES


class TradeServices:
    def __getattr__(self, name):
        kinds = {"propose_" + n: k for k, (n, _) in NAMES.items()}
        if name in kinds:
            return partial(self.service_instruction, kinds[name])
        raise AttributeError(name)

    def search_records(self, resource, filters=None, cursor=0, limit=20):
        if resource not in TABLES:
            return self.next_handler(TradeServices).search_records(resource, filters, cursor, limit)
        self.check(0 < limit <= 100 and cursor >= 0, "INVALID_PAGE")
        self.check(not any(k.startswith("_") for k in (filters or {})), "FIELD_NOT_PUBLIC")
        rows = [
            self.public(r, resource)
            for r in self.w[resource].values()
            if r.get("scope") == self.w["_session_scope"]
        ]
        for k, v in (filters or {}).items():
            if isinstance(v, dict):
                for op, x in v.items():
                    self.check(op in ("in", "contains", "gte", "lte"), "UNSUPPORTED_FILTER")
                    rows = (
                        [r for r in rows if r.get(k) in x]
                        if op == "in"
                        else (
                            [r for r in rows if str(x).lower() in str(r.get(k, "")).lower()]
                            if op == "contains"
                            else [
                                r
                                for r in rows
                                if r.get(k) is not None
                                and (r[k] >= x if op == "gte" else r[k] <= x)
                            ]
                        )
                    )
            else:
                rows = [r for r in rows if r.get(k) == v]
        rows.sort(key=lambda r: r["id"])
        keys = (
            "id",
            "name",
            "kind",
            "reference",
            "case_id",
            "status",
            "receipt_status",
            "currency",
        )
        return dict(
            records=[{k: r[k] for k in keys if k in r} for r in rows[cursor : cursor + limit]],
            total=len(rows),
            next_cursor=cursor + limit if cursor + limit < len(rows) else None,
        )

    def get_record(self, resource, record_id):
        if resource in TABLES:
            return self.public(self.row(resource, record_id), resource)
        return self.next_handler(TradeServices).get_record(resource, record_id)

    def call_discoverable_agent_tool(self, capability_name, arguments):
        if capability_name not in NEW:
            return self.next_handler(TradeServices).call_discoverable_agent_tool(
                capability_name, arguments
            )
        self.check(capability_name in self.w["unlocked"], "TOOL_NOT_UNLOCKED")
        s = NEW[capability_name]
        self.check(set(arguments) == set(s["parameters"]), "INVALID_CAPABILITY_ARGUMENTS")
        for k, t in s["parameters"].items():
            self.check(
                type(arguments[k]) is {"string": str, "object": dict, "integer": int}[t],
                "INVALID_CAPABILITY_ARGUMENT_TYPE",
            )
        self.row("service_cases", arguments["case_id"])
        return self.public(
            self.new(
                "requests",
                operation=capability_name,
                arguments=deepcopy(arguments),
                status="PENDING",
                prepared_minute=self.w["minute"],
            )
        )

    def records(self, c, kind=None):
        return [
            r
            for r in self.w["service_records"].values()
            if r["case_id"] == c["id"] and (kind is None or r["kind"] == kind)
        ]

    def local(self, c, rid, kind=None):
        r = self.row("service_records", rid)
        self.check(
            r["case_id"] == c["id"] and (kind is None or r["kind"] == kind), "WRONG_SERVICE_RECORD"
        )
        return r

    def correspondence(self, c):
        rows = self.records(c, "external_correspondence")
        self.check(
            all(r.get("receipt_status") == "READY" for r in rows), "SOURCE_RECEIPT_NOT_READY"
        )
        return rows[0]["receipt"]

    def institution_facts(self, c):
        # An institution's scheduled decision uses its own source evidence.
        # Delivery of a support-requested copy is an observation, not a gate
        # that may block earlier events and prevent that copy from arriving.
        rows = self.records(c, "external_correspondence")
        self.check(len(rows) == 1, "AMBIGUOUS_INSTITUTION_SOURCE")
        return deepcopy(rows[0]["_response"])

    def cash(self, c, delta, currency="USD"):
        target = c if currency == "USD" else c["state"]
        key = "cash_minor" if currency == "USD" else "eur_minor"
        self.check(
            type(delta) is int and target.get(key, 0) + delta >= 0, "SERVICE_WALLET_INSUFFICIENT"
        )
        target[key] = target.get(key, 0) + delta

    def book(self, c, kind, **data):
        return self.public(
            self.new(
                "service_bookings", case_id=c["id"], kind=kind, minute=self.w["minute"], **data
            )
        )

    def charge(self, c, n):
        self.cash(c, -n)
        c["fees_minor"] += n

    def propose_service_inquiry(self, case_id, record_id):
        c = self.row("service_cases", case_id)
        r = self.local(c, record_id, "external_correspondence")
        if r.get("receipt_status") in ("PENDING", "READY"):
            return self.public(r)
        r["receipt_status"] = "PENDING"
        self.schedule("trade_source_receipt", r["id"], r["delay"])
        return self.public(r)

    def propose_service_payment(self, case_id, record_id, amount_minor):
        c = self.row("service_cases", case_id)
        r = self.local(c, record_id, "obligation")
        self.check(type(amount_minor) is int and amount_minor > 0, "POSITIVE_MINOR_AMOUNT_REQUIRED")
        currency = r.get("currency", "USD")
        if r.get("payer"):
            p = next(x for x in self.records(c, "participant") if x["reference"] == r["payer"])
            self.check(p["cash_minor"] >= amount_minor, "PAYER_CASH_INSUFFICIENT")
            p["cash_minor"] -= amount_minor
        else:
            self.cash(c, -amount_minor, currency)
        r["paid_minor"] += amount_minor
        return self.book(
            c, "payment", record_id=record_id, amount_minor=amount_minor, currency=currency
        )

    def args(self, data, *keys):
        self.check(set(data) == set(keys), "INVALID_LOCAL_ACTION_FIELDS")

    def positive(self, n):
        self.check(type(n) is int and n > 0, "POSITIVE_INTEGER_REQUIRED")

    def service_instruction(self, kind, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == kind, "SERVICE_CONTRACT_MISMATCH")
        before = dict(cash=c["cash_minor"], fees=c["fees_minor"])
        out = getattr(self, "action_" + kind)(c, action, data)
        return self.book(
            c,
            action,
            contract=kind,
            arguments=deepcopy(data),
            cash_delta_minor=c["cash_minor"] - before["cash"],
            fee_minor=c["fees_minor"] - before["fees"],
            receipt=out or {},
        )

    def action_escrow(self, c, a, d):
        if a == "record_acceptance":
            self.args(d, "units")
            self.check(isinstance(d["units"], dict), "UNIT_MAPPING_REQUIRED")
            self.correspondence(c)
            for k, n in d["units"].items():
                r = next((r for r in self.records(c, "delivery") if r["shipment_ref"] == k), None)
                self.check(
                    r and type(n) is int and 0 <= n <= r["ordered_units"],
                    "INVALID_DECLARED_ACCEPTANCE",
                )
            c["state"]["declared_units"] = deepcopy(d["units"])
        elif a == "distribute":
            self.args(d, "beneficiary", "amount_minor")
            self.check(d["beneficiary"] in ("supplier", "buyer"), "UNKNOWN_PARTY")
            self.positive(d["amount_minor"])
            self.cash(c, -d["amount_minor"])
        else:
            self.check(False, "UNKNOWN_ESCROW_ACTION")

    def action_factoring(self, c, a, d):
        s = c["state"]
        if a == "repay_senior":
            self.args(d, "amount_minor")
            self.positive(d["amount_minor"])
            self.check(d["amount_minor"] <= s["senior_debt_minor"], "DEBT_OVERPAYMENT")
            self.cash(c, -d["amount_minor"])
            s["senior_debt_minor"] -= d["amount_minor"]
            if s["senior_debt_minor"] == 0:
                for r in self.records(c, "receivable"):
                    r["pledged"] = False
        elif a == "assign":
            self.args(d, "record_id", "offer_id", "face_minor")
            r = self.local(c, d["record_id"], "receivable")
            o = self.local(c, d["offer_id"], "factor_offer")
            self.check(not r["pledged"] and not r["assigned"], "RECEIVABLE_ENCUMBERED")
            self.positive(d["face_minor"])
            self.check(d["face_minor"] <= r["face_minor"], "DECLARED_FACE_EXCEEDS_NOTE")
            gross = d["face_minor"] * o["advance_bp"] // 10000
            fee = (d["face_minor"] * o["fee_bp"] + 9999) // 10000
            self.cash(c, gross - fee)
            c["fees_minor"] += fee
            r.update(
                assigned=True,
                assigned_face_minor=d["face_minor"],
                advance_minor=gross,
                offer_id=o["id"],
            )
            self.schedule("trade_factor_notice", r["id"], o["notification_delay"])
        else:
            self.check(False, "UNKNOWN_FACTORING_ACTION")

    def action_credit_letter(self, c, a, d):
        s = c["state"]
        if a == "amend":
            self.args(d, "field", "day")
            self.check(
                d["field"] in ("latest_shipment_day", "expiry_day") and type(d["day"]) is int,
                "AMENDMENT_FIELD_INVALID",
            )
            self.charge(c, 1000)
            s[d["field"]] = d["day"]
        elif a == "present":
            self.args(d, "route_id", "amount_minor", "certificate_version")
            o = self.local(c, d["route_id"], "presentation_route")
            self.positive(d["amount_minor"])
            self.check(
                d["amount_minor"] <= s["face_minor"] and not s.get("presentation"),
                "PRESENTATION_NOT_OPEN",
            )
            self.charge(c, o["fee_minor"])
            s["presentation"] = deepcopy(d)
            s["presentation_status"] = "UNDER_REVIEW"
            self.schedule("trade_credit_decision", c["id"], o["delay"])
        else:
            self.check(False, "UNKNOWN_CREDIT_ACTION")

    def action_guarantee(self, c, a, d):
        s = c["state"]
        if a == "issue":
            self.args(d, "product_id", "amount_minor")
            o = self.local(c, d["product_id"], "guarantee_product")
            self.positive(d["amount_minor"])
            self.check(not s["new_active"], "GUARANTEE_ALREADY_ISSUED")
            need = (
                max(0, d["amount_minor"] - s["collateral_minor"])
                if o["collateral_mode"] == "maximum_of_old_and_new"
                else d["amount_minor"]
            )
            self.charge(c, o["fee_minor"])
            self.cash(c, -need)
            s.update(
                collateral_minor=s["collateral_minor"] + need,
                new_active=True,
                new_amount_minor=d["amount_minor"],
                product_id=o["id"],
            )
        elif a == "return_old":
            self.args(d)
            self.correspondence(c)
            self.check(s["old_active"], "OLD_GUARANTEE_ALREADY_RETURNED")
            s["old_active"] = False
            required = s["new_amount_minor"] if s["new_active"] else 0
            release = max(0, s["collateral_minor"] - required)
            self.cash(c, release)
            s["collateral_minor"] -= release
        else:
            self.check(False, "UNKNOWN_GUARANTEE_ACTION")

    def action_forward(self, c, a, d):
        self.args(d)
        s = c["state"]
        self.check(s["forward_status"] == "OPEN", "FORWARD_NOT_OPEN")
        if a == "deliver":
            self.check(self.w["minute"] <= 5, "DELIVERY_WINDOW_CLOSED")
            self.cash(c, -s["deliver_minor"])
            self.cash(c, s["receive_minor"], "EUR")
            self.cash(c, s["margin_minor"])
            s.update(margin_minor=0, forward_status="DELIVERED")
        elif a == "closeout":
            self.charge(c, 15000)
            self.cash(c, s["margin_minor"])
            s.update(margin_minor=0, forward_status="CLOSED_OUT")
        else:
            self.check(False, "UNKNOWN_FORWARD_ACTION")

    def action_debit(self, c, a, d):
        if a == "refund":
            self.args(d, "record_id", "amount_minor")
            r = self.local(c, d["record_id"], "collection")
            self.positive(d["amount_minor"])
            self.check(
                r["refunded_minor"] + d["amount_minor"] <= r["amount_minor"],
                "REFUND_EXCEEDS_COLLECTION",
            )
            self.cash(c, -d["amount_minor"])
            r["refunded_minor"] += d["amount_minor"]
        elif a == "cancel_mandate":
            self.args(d, "record_id")
            self.local(c, d["record_id"], "mandate")["active"] = False
        elif a == "recall":
            self.args(d, "record_id")
            r = self.local(c, d["record_id"], "debit_instruction")
            self.check(
                r["state"] == "ACCEPTED" and self.w["minute"] <= r["recall_before_minute"],
                "RECALL_WINDOW_CLOSED",
            )
            r["state"] = "RECALLED"
        else:
            self.check(False, "UNKNOWN_DEBIT_ACTION")

    def action_representment(self, c, a, d):
        s = c["state"]
        if a == "concede":
            self.args(d, "amount_minor")
            self.positive(d["amount_minor"])
            self.check(s["held_minor"] >= d["amount_minor"], "HELD_AMOUNT_INSUFFICIENT")
            s["held_minor"] -= d["amount_minor"]
            s["conceded_minor"] += d["amount_minor"]
        elif a == "defend":
            self.args(d, "amount_minor", "delivery_reference", "refund_reference")
            self.positive(d["amount_minor"])
            self.check(
                not s.get("defense_status")
                and d["amount_minor"] <= s["held_minor"]
                and self.w["minute"] <= 5,
                "DEFENSE_NOT_AVAILABLE",
            )
            self.charge(c, 1000)
            s.update(defense=deepcopy(d), defense_status="SUBMITTED")
            self.schedule("trade_defense_decision", c["id"], 2)
        else:
            self.check(False, "UNKNOWN_REPRESENTMENT_ACTION")

    def action_freight(self, c, a, d):
        self.check("record_id" in d, "RECORD_REQUIRED")
        r = self.local(c, d["record_id"], "cargo")
        if a == "pay_lien":
            self.args(d, "record_id", "amount_minor")
            self.positive(d["amount_minor"])
            self.cash(c, -d["amount_minor"])
            r["freight_paid_minor"] += d["amount_minor"]
        elif a == "endorse":
            self.args(d, "record_id", "consignee", "bill_version")
            self.check(d["bill_version"] == r["bill_version"], "BILL_VERSION_NOT_CURRENT")
            self.check(isinstance(d["consignee"], str) and d["consignee"], "CONSIGNEE_REQUIRED")
            r["endorsed_to"] = d["consignee"]
        elif a == "release":
            self.args(d, "record_id", "cartons")
            self.positive(d["cartons"])
            self.check(
                r["endorsed_to"] and r["freight_paid_minor"] >= r["freight_minor"],
                "LIEN_OR_TITLE_NOT_RELEASED",
            )
            self.check(
                r["released_cartons"] + d["cartons"] <= r["cartons"], "CARGO_QUANTITY_EXCEEDED"
            )
            if self.w["minute"] > 5:
                self.charge(c, 3000)
            r["released_cartons"] += d["cartons"]
        else:
            self.check(False, "UNKNOWN_CARGO_ACTION")

    def action_rolling_reserve(self, c, a, d):
        self.args(d, "record_id", "amount_minor")
        r = self.local(c, d["record_id"], "reserve_cohort")
        self.positive(d["amount_minor"])
        self.check(d["amount_minor"] <= r["held_minor"], "COHORT_BALANCE_INSUFFICIENT")
        if a == "refund":
            r["held_minor"] -= d["amount_minor"]
            r["refunds_minor"] += d["amount_minor"]
        elif a == "withdraw":
            fee = 2000 if self.w["minute"] < r["matures_minute"] else 0
            self.check(d["amount_minor"] > fee, "ADVANCE_BELOW_FEE")
            r["held_minor"] -= d["amount_minor"]
            r["withdrawn_minor"] += d["amount_minor"]
            self.cash(c, d["amount_minor"] - fee)
            c["fees_minor"] += fee
        else:
            self.check(False, "UNKNOWN_COHORT_ACTION")

    def action_netting(self, c, a, d):
        s = c["state"]
        if a == "novate":
            self.args(d, "invoice_ids")
            self.check(
                isinstance(d["invoice_ids"], list)
                and len(set(d["invoice_ids"])) == len(d["invoice_ids"])
                and not s.get("novation"),
                "INVALID_NOVATION_SET",
            )
            balances = {r["reference"]: 0 for r in self.records(c, "participant")}
            for rid in d["invoice_ids"]:
                r = self.local(c, rid, "intercompany_invoice")
                self.check(not r["settled"], "INVOICE_ALREADY_NOVATED")
                balances[r["debtor"]] -= r["amount_minor"]
                balances[r["creditor"]] += r["amount_minor"]
                r["settled"] = True
            s["novation"] = balances
        elif a == "fund":
            self.args(d, "amount_minor")
            self.positive(d["amount_minor"])
            self.check(bool(s.get("novation")), "NOVATION_NOT_REGISTERED")
            self.cash(c, -d["amount_minor"])
            s["clearing_minor"] += d["amount_minor"]
        elif a == "distribute":
            self.args(d, "record_id", "amount_minor")
            p = self.local(c, d["record_id"], "participant")
            self.positive(d["amount_minor"])
            self.check(s["clearing_minor"] >= d["amount_minor"], "CLEARING_CASH_INSUFFICIENT")
            s["clearing_minor"] -= d["amount_minor"]
            p["cash_minor"] += d["amount_minor"]
        else:
            self.check(False, "UNKNOWN_NETTING_ACTION")

    def collateral_value(self, c):
        return sum(
            r["market_minor"] * (10000 - r["haircut_bp"]) // 10000
            for r in self.records(c, "collateral_asset")
            if r["pledged"]
        )

    def action_collateral(self, c, a, d):
        s = c["state"]
        if a == "repay":
            self.args(d, "amount_minor")
            self.positive(d["amount_minor"])
            self.check(d["amount_minor"] <= s["principal_minor"], "REPAYMENT_EXCEEDS_DEBT")
            self.cash(c, -d["amount_minor"])
            s["principal_minor"] -= d["amount_minor"]
        elif a == "pledge":
            self.args(d, "record_id")
            r = self.local(c, d["record_id"], "collateral_asset")
            self.check(not r["pledged"] and not r["released"], "ASSET_UNAVAILABLE")
            r["pledged"] = True
        elif a == "release":
            self.args(d, "record_id")
            r = self.local(c, d["record_id"], "collateral_asset")
            self.check(r["pledged"], "ASSET_NOT_PLEDGED")
            r["pledged"] = False
            self.check(
                self.collateral_value(c) >= (s["principal_minor"] * s["margin_bp"] + 9999) // 10000,
                "COLLATERAL_RELEASE_MARGIN_INSUFFICIENT",
            )
            r["released"] = True
        else:
            self.check(False, "UNKNOWN_COLLATERAL_ACTION")

    def action_insurance(self, c, a, d):
        s = c["state"]
        if a == "sell_salvage":
            self.args(d)
            receipt = self.correspondence(c)
            self.check(
                not s["salvage_sold"] and not s["rights_assigned"], "SALVAGE_RIGHT_UNAVAILABLE"
            )
            self.cash(c, receipt["salvage_offer_minor"])
            s["salvage_sold"] = True
        elif a == "claim":
            self.args(d, "record_id", "amount_minor", "declared_net_loss_minor")
            r = self.local(c, d["record_id"], "policy")
            self.positive(d["amount_minor"])
            self.check(
                d["amount_minor"] <= r["limit_minor"] and not r.get("claim_status"),
                "POLICY_CLAIM_UNAVAILABLE",
            )
            r.update(
                claim_status="PENDING",
                claimed_minor=d["amount_minor"],
                declared_net_loss_minor=d["declared_net_loss_minor"],
            )
            self.schedule("trade_insurance_payment", r["id"], 2)
        elif a == "assign_rights":
            self.args(d, "declared_recoveries_minor")
            self.check(not s["rights_assigned"], "RIGHTS_ALREADY_ASSIGNED")
            s.update(rights_assigned=True, declared_recoveries_minor=d["declared_recoveries_minor"])
        else:
            self.check(False, "UNKNOWN_INSURANCE_ACTION")

    def action_term_deposit(self, c, a, d):
        if a == "register_tax":
            self.args(d)
            p = self.correspondence(c)
            c["state"].update(tax_registered=True, tax_effective_minute=p["effective_minute"])
        elif a == "redeem":
            self.args(d, "record_id")
            r = self.local(c, d["record_id"], "term_deposit")
            self.check(not r["redeemed"], "DEPOSIT_ALREADY_REDEEMED")
            early = self.w["minute"] < r["matures_minute"]
            interest = 0 if early else r["interest_minor"]
            fee = r["break_fee_minor"] if early else 0
            exempt = (
                c["state"]["tax_registered"]
                and c["state"]["tax_effective_minute"] <= r["matures_minute"]
            )
            tax = 0 if exempt else interest * 2000 // 10000
            self.cash(c, r["principal_minor"] + interest - fee - tax)
            c["fees_minor"] += fee
            r.update(
                redeemed=True,
                interest_paid_minor=interest,
                tax_minor=tax,
                break_fee_paid_minor=fee,
                redeemed_minute=self.w["minute"],
            )
        else:
            self.check(False, "UNKNOWN_DEPOSIT_ACTION")

    def action_export_license(self, c, a, d):
        if a == "register_return":
            self.args(d, "record_id")
            r = self.local(c, d["record_id"], "export_license")
            p = self.correspondence(c)
            self.check(not c["state"].get("return_registered"), "RETURN_ALREADY_REGISTERED")
            self.check(r["reference"] == p["original_license"], "ORIGINATING_LICENSE_MISMATCH")
            r["used_kg"] -= p["returned_cartons"] * p["kg_per_carton"]
            self.cash(c, p["refundable_bond_minor"])
            c["state"]["return_registered"] = True
        elif a == "declare":
            self.args(d, "record_id", "license_id", "commodity", "kilograms")
            r = self.local(c, d["record_id"], "export_load")
            lic = self.local(c, d["license_id"], "export_license")
            self.positive(d["kilograms"])
            self.check(
                d["commodity"] in lic["commodities"] and self.w["minute"] <= lic["expires_minute"],
                "DECLARED_GOODS_OR_LICENSE_WINDOW_INVALID",
            )
            self.check(
                lic["used_kg"] + d["kilograms"] <= lic["capacity_kg"],
                "LICENSE_QUANTITY_INSUFFICIENT",
            )
            self.cash(c, -10 * d["kilograms"])
            lic["used_kg"] += d["kilograms"]
            r["declared_kg"] += d["kilograms"]
        else:
            self.check(False, "UNKNOWN_LICENSE_ACTION")

    def apply_event(self, e):
        k = e["kind"]
        rid = e["target_id"]
        if not k.startswith("trade_"):
            return self.next_handler(TradeServices).apply_event(e)
        if k == "trade_source_receipt":
            r = self.row("service_records", rid)
            r.update(receipt_status="READY", receipt=deepcopy(r["_response"]))
            c = self.row("service_cases", r["case_id"])
            if c["kind"] == "forward":
                self.cash(c, r["receipt"]["credited_minor"])
            if c["kind"] == "collateral":
                asset = next(
                    a
                    for a in self.records(c, "collateral_asset")
                    if a["reference"] == r["receipt"]["valuation_reference"]
                )
                asset["market_minor"] = r["receipt"]["market_minor"]
        elif k == "trade_factor_notice":
            self.row("service_records", rid)["notified"] = True
        elif k == "trade_credit_decision":
            c = self.row("service_cases", rid)
            s = c["state"]
            p = s["presentation"]
            raw = self.institution_facts(c)
            reasons = []
            if raw["shipment_day"] > s["latest_shipment_day"]:
                reasons.append("SHIPMENT_AFTER_CREDIT_DATE")
            if raw["shipment_day"] > s["expiry_day"]:
                reasons.append("CREDIT_EXPIRED")
            if (
                p["certificate_version"] != raw["certificate_version"]
                or raw["insured_minor"] * 10000 < raw["invoice_minor"] * 11000
            ):
                reasons.append("INSURANCE_DOCUMENT_DISCREPANCY")
            s["presentation_status"] = "DISCREPANT" if reasons else "PAID"
            s["discrepancies"] = reasons
            if not reasons:
                self.cash(c, p["amount_minor"])
        elif k == "trade_defense_decision":
            c = self.row("service_cases", rid)
            s = c["state"]
            p = self.institution_facts(c)
            d = s["defense"]
            good = (
                d["delivery_reference"] == p["delivery_reference"]
                and d["refund_reference"] == p["refund_reference"]
            )
            s["defense_status"] = "ACCEPTED" if good else "EVIDENCE_REJECTED"
            if good:
                self.cash(c, d["amount_minor"])
                s["held_minor"] -= d["amount_minor"]
                s["defended_minor"] += d["amount_minor"]
        elif k == "trade_insurance_payment":
            r = self.row("service_records", rid)
            c = self.row("service_cases", r["case_id"])
            self.cash(c, r["claimed_minor"])
            r.update(claim_status="PAID", paid_minor=r["claimed_minor"])
        elif k == "trade_debit_settle":
            r = self.row("service_records", rid)
            if r["state"] == "ACCEPTED":
                r["state"] = "SETTLED"
                self.cash(self.row("service_cases", r["case_id"]), r["amount_minor"])
        elif k == "trade_maturity":
            pass
        else:
            self.check(False, "UNKNOWN_TRADE_EVENT")
