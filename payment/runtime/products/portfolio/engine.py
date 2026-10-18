"""Deterministic product operations. No acceptance contract or reference imports."""

from copy import deepcopy
from functools import partial

from payment.runtime.products.portfolio.specification import NAMES, NEW, CAPABILITIES


class PortfolioOperations:
    def __getattr__(self, name):
        kinds = {"propose_" + n: k for k, (n, _) in NAMES.items()}
        if name in kinds:
            return partial(self.service_instruction, kinds[name])
        return self.next_handler(PortfolioOperations).dynamic_attribute(name)

    def call_discoverable_agent_tool(self, capability_name, arguments):
        if capability_name not in NEW:
            return self.next_handler(PortfolioOperations).call_discoverable_agent_tool(
                capability_name, arguments
            )
        self.check(capability_name in self.w["unlocked"], "TOOL_NOT_UNLOCKED")
        self.check(
            set(arguments) == {"case_id", "action", "data"}
            and type(arguments["case_id"]) is str
            and type(arguments["action"]) is str
            and type(arguments["data"]) is dict,
            "INVALID_CAPABILITY_ARGUMENTS",
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

    def service_instruction(self, kind, case_id, action, data):
        if kind not in NAMES:
            return self.next_handler(PortfolioOperations).service_instruction(
                kind, case_id, action, data
            )
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == kind, "SERVICE_CONTRACT_MISMATCH")
        cash, fee = c["cash_minor"], c["fees_minor"]
        if action == "pay_bill":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "bill")
            self.positive(data["amount_minor"])
            self.cash(c, -data["amount_minor"])
            r["paid_minor"] += data["amount_minor"]
            out = dict(record_id=r["id"], paid_minor=r["paid_minor"])
        else:
            out = getattr(self, "do_" + kind)(c, action, data)
        return self.book(
            c,
            action,
            contract=kind,
            arguments=deepcopy(data),
            cash_delta_minor=c["cash_minor"] - cash,
            fee_minor=c["fees_minor"] - fee,
            cash_after_minor=c["cash_minor"],
            receipt=out or {},
        )

    def do_diagnostic_debit(self, c, a, d):
        if a == "challenge":
            self.args(d, "record_id", "amount_minor")
            r = self.local(c, d["record_id"], "collection")
            self.positive(d["amount_minor"])
            self.check(
                r["dispute_status"] == "NONE"
                and d["amount_minor"] <= r["amount_minor"] - r["refunded_minor"],
                "CLAIM_NOT_OPEN",
            )
            self.correspondence(c)
            r.update(dispute_status="FILED", claimed_minor=d["amount_minor"])
            self.schedule(
                "diversity_event", c["id"], 2, event_kind="collection_claim", ref=r["reference"]
            )
            return self.public(r)
        if a == "cancel_mandate":
            self.args(d, "record_id")
            self.local(c, d["record_id"], "mandate")["active"] = False
        elif a == "recall":
            self.args(d, "record_id")
            r = self.local(c, d["record_id"], "pending_debit")
            self.check(
                r["status"] == "ACCEPTED" and self.w["minute"] <= r["recall_before_minute"],
                "RECALL_NOT_AVAILABLE",
            )
            r["status"] = "RECALLED"
        else:
            self.check(False, "UNKNOWN_ACTION")

    def do_succession_refund(self, c, a, d):
        if a == "resolve_product":
            self.args(d, "record_id", "mode", "entity_id")
            r = self.local(c, d["record_id"], "product")
            ent = self.local(c, d["entity_id"], "entity")
            self.check(
                d["mode"] in ("KEEP", "NEW") and r["resolution"] is None,
                "PRODUCT_INSTRUCTION_NOT_OPEN",
            )
            if d["mode"] == "NEW":
                p = self.correspondence(c)
                self.charge(c, p["setup_fee_minor"])
                ent["account_ready"] = False
                self.schedule(
                    "diversity_event",
                    c["id"],
                    p["review_delay"],
                    event_kind="account_ready",
                    ref=ent["reference"],
                )
                r["status"] = "PENDING"
            r.update(resolution=d["mode"], resolved_entity=ent["reference"])
            return self.public(r)
        self.check(a == "refund", "UNKNOWN_ACTION")
        self.args(d, "record_id", "recipient_id", "amount_minor")
        r = self.local(c, d["record_id"], "refund_claim")
        ent = self.local(c, d["recipient_id"], "entity")
        self.check(ent["verified"] and ent["account_ready"], "RECIPIENT_ACCOUNT_NOT_READY")
        self.positive(d["amount_minor"])
        self.cash(c, -d["amount_minor"])
        r["paid_minor"] += d["amount_minor"]
        r.setdefault("payouts", []).append(
            dict(entity=ent["reference"], amount_minor=d["amount_minor"], minute=self.w["minute"])
        )

    def collateral_cover(self, c, loan):
        return sum(
            r["face_minor"] * (10000 - r["haircut_bp"]) // 10000
            for r in self.records(c, "asset")
            if r["pledged_to"] == loan
        )

    def do_collateral_portfolio(self, c, a, d):
        if a == "repay":
            self.args(d, "record_id", "amount_minor")
            r = self.local(c, d["record_id"], "loan")
            p = self.correspondence(c)
            n = d["amount_minor"]
            self.positive(n)
            self.check(
                n % p["repayment_increment_minor"] == 0 and n <= r["debt_minor"],
                "REPAYMENT_AMOUNT_INVALID",
            )
            self.cash(c, -n)
            r["debt_minor"] -= n
        elif a == "pledge":
            self.args(d, "record_id", "loan_id")
            r = self.local(c, d["record_id"], "asset")
            loan = self.local(c, d["loan_id"], "loan")
            self.check(
                r["pledged_to"] is None and loan["reference"] in r["eligible_loans"],
                "ASSET_NOT_ELIGIBLE_OR_ALREADY_PLEDGED",
            )
            r["pledged_to"] = loan["reference"]
        elif a == "release":
            self.args(d, "record_id")
            r = self.local(c, d["record_id"], "asset")
            self.check(r["pledged_to"] is not None, "ASSET_NOT_PLEDGED")
            loan = self.byref(c, r["pledged_to"])
            n = r["face_minor"] * (10000 - r["haircut_bp"]) // 10000
            self.check(
                self.collateral_cover(c, loan["reference"]) - n >= loan["debt_minor"],
                "INSUFFICIENT_REMAINING_COLLATERAL",
            )
            r["pledged_to"] = None
        else:
            self.check(False, "UNKNOWN_ACTION")

    def do_layered_recovery(self, c, a, d):
        if a == "sell_salvage":
            self.args(d, "record_id")
            r = self.local(c, d["record_id"], "loss")
            self.check(not r["salvage_sold"] and not r["rights_recorded"], "SALVAGE_NOT_DISPOSABLE")
            r["salvage_sold"] = True
            self.cash(c, r["salvage_minor"])
        elif a == "claim":
            self.args(d, "record_id", "layer", "amount_minor")
            r = self.local(c, d["record_id"], "loss")
            p = self.correspondence(c)
            self.check(
                d["layer"] in ("primary", "excess") and self.w["minute"] <= p["filing_deadline"],
                "FILING_NOT_AVAILABLE",
            )
            key = d["layer"] + "_claim_minor"
            self.check(key not in r, "LAYER_ALREADY_FILED")
            self.positive(d["amount_minor"])
            self.check(d["amount_minor"] <= r[d["layer"] + "_cap_minor"], "LAYER_LIMIT_EXCEEDED")
            r[key] = d["amount_minor"]
            r[d["layer"] + "_filing_snapshot"] = dict(
                merchant_minor=r["merchant_minor"],
                salvage_minor=r["salvage_minor"] if r["salvage_sold"] else 0,
            )
            self.schedule(
                "diversity_event",
                c["id"],
                p["payout_delay"],
                event_kind="insurance_payout",
                ref=r["reference"],
                layer=d["layer"],
                amount_minor=d["amount_minor"],
            )
        elif a == "return_recovery":
            self.args(d, "record_id", "layer", "amount_minor")
            r = self.local(c, d["record_id"], "loss")
            self.check(d["layer"] in ("primary", "excess"), "UNKNOWN_LAYER")
            n = d["amount_minor"]
            self.positive(n)
            self.check(
                n <= r[d["layer"] + "_paid_minor"] - r[d["layer"] + "_returned_minor"],
                "EXCEEDS_RECEIVED_INSURANCE",
            )
            self.cash(c, -n)
            r[d["layer"] + "_returned_minor"] += n
        elif a == "record_rights":
            self.args(d, "record_id", "recoveries_minor")
            r = self.local(c, d["record_id"], "loss")
            self.nonnegative(d["recoveries_minor"])
            r.update(rights_recorded=True, declared_recoveries_minor=d["recoveries_minor"])
        else:
            self.check(False, "UNKNOWN_ACTION")

    def do_ledger_resume(self, c, a, d):
        s = c["state"]
        if a == "grant_scope":
            self.args(d, "scope")
            self.check(d["scope"] in ("orders", "returns"), "UNKNOWN_SCOPE")
            if d["scope"] not in s["scopes"]:
                s["scopes"].append(d["scope"])
        elif a == "import_window":
            self.args(d, "stream", "after_cursor", "through_cursor", "shop")
            self.check(
                d["stream"] in s["scopes"]
                and type(d["after_cursor"]) is int
                and type(d["through_cursor"]) is int
                and 0 <= d["after_cursor"] < d["through_cursor"],
                "INVALID_IMPORT_WINDOW",
            )
            rs = [
                r
                for r in self.records(c, "sales_event")
                if r["stream"] == d["stream"]
                and r["shop"] == d["shop"]
                and d["after_cursor"] < r["sequence"] <= d["through_cursor"]
            ]
            self.check(rs, "EMPTY_IMPORT_WINDOW")
            entries = []
            for r in rs:
                s["quota_minor"] += r["quota_delta_minor"]
                entries.append(
                    self.book(
                        c,
                        "import_entry",
                        event_id=r["id"],
                        quota_delta_minor=r["quota_delta_minor"],
                        reversed=False,
                    )
                )
            s[d["stream"] + "_cursor"] = max(s[d["stream"] + "_cursor"], d["through_cursor"])
            return dict(entries=entries, quota_minor=s["quota_minor"])
        elif a == "reverse_import":
            self.args(d, "entry_id")
            r = self.row("service_bookings", d["entry_id"])
            self.check(
                r["case_id"] == c["id"] and r["kind"] == "import_entry" and not r["reversed"],
                "IMPORT_ENTRY_NOT_REVERSIBLE",
            )
            r["reversed"] = True
            s["quota_minor"] -= r["quota_delta_minor"]
            self.book(
                c, "import_reversal", entry_id=r["id"], quota_delta_minor=-r["quota_delta_minor"]
            )
        elif a == "withdraw":
            self.args(d, "amount_minor", "recipient_id")
            r = self.local(c, d["recipient_id"], "recipient")
            n = d["amount_minor"]
            self.positive(n)
            self.check(
                r["verified"] and s["quota_minor"] >= n and n * 7 > 2000, "WITHDRAWAL_NOT_AVAILABLE"
            )
            self.cash(c, -n)
            s["quota_minor"] -= n
            s["withdrawn_minor"] += n
            s["cny_paid_minor"] += n * 7 - 2000
            s.setdefault("withdrawals", []).append(
                dict(recipient=r["reference"], amount_minor=n, cny_minor=n * 7 - 2000)
            )
        else:
            self.check(False, "UNKNOWN_ACTION")

    def do_cash_calendar(self, c, a, d):
        self.check(a == "redeem", "UNKNOWN_ACTION")
        self.args(d, "record_id", "tax_exempt")
        r = self.local(c, d["record_id"], "deposit")
        self.check(r["status"] == "ACTIVE" and type(d["tax_exempt"]) is bool, "DEPOSIT_NOT_OPEN")
        p = self.correspondence(c)
        matured = self.w["minute"] >= r["maturity_minute"]
        interest = r["interest_minor"] if matured else 0
        tax = 0 if d["tax_exempt"] else interest * p["interest_tax_bp"] // 10000
        fee = 0 if matured else r["early_fee_minor"]
        net = r["principal_minor"] + interest - tax - fee
        self.cash(c, net)
        c["fees_minor"] += fee
        r.update(
            status="REDEEMED",
            redeemed_minute=self.w["minute"],
            declared_exempt=d["tax_exempt"],
            interest_paid_minor=interest,
            tax_minor=tax,
            net_minor=net,
        )

    def do_net_recourse(self, c, a, d):
        if a == "send_guaranteed":
            self.args(d, "record_id", "corridor_id", "gross_minor", "net_minor", "bank_version")
            r = self.local(c, d["record_id"], "net_obligation")
            route = self.local(c, d["corridor_id"], "corridor")
            self.check(
                not r.get("sent") and r["bank_version"] == d["bank_version"],
                "PAYMENT_NOT_OPEN_OR_BANK_CHANGED",
            )
            self.positive(d["gross_minor"])
            self.positive(d["net_minor"])
            self.cash(c, -d["gross_minor"])
            self.charge(c, route["bank_fee_minor"])
            r.update(
                sent=True,
                gross_minor=d["gross_minor"],
                promised_minor=d["net_minor"],
                status="IN_FLIGHT",
            )
            self.schedule(
                "diversity_event",
                c["id"],
                route["receipt_delay"],
                event_kind="net_receipt",
                ref=r["reference"],
                corridor=route["reference"],
            )
            return self.public(r)
        self.check(a in ("direct_pay", "topup"), "UNKNOWN_ACTION")
        self.args(d, "record_id", "amount_minor", "bank_version")
        r = self.local(c, d["record_id"], "net_obligation")
        self.check(r["bank_version"] == d["bank_version"], "BANK_DETAILS_CHANGED")
        self.positive(d["amount_minor"])
        fee = 500 if a == "topup" else 1000
        self.cash(c, -d["amount_minor"])
        self.charge(c, fee)
        r["paid_minor"] += d["amount_minor"]
        r.setdefault("payouts", []).append(
            dict(
                amount_minor=d["amount_minor"],
                minute=self.w["minute"],
                bank_version=d["bank_version"],
            )
        )

    def do_discount_portfolio(self, c, a, d):
        self.check(a == "settle", "UNKNOWN_ACTION")
        self.args(d, "record_id", "amount_minor")
        r = self.local(c, d["record_id"], "discount_invoice")
        self.check(not r.get("settled"), "INVOICE_ALREADY_SETTLED")
        self.positive(d["amount_minor"])
        self.cash(c, -d["amount_minor"])
        r.update(paid_minor=d["amount_minor"], paid_minute=self.w["minute"], settled=True)

    def apply_event(self, e):
        if e["kind"] != "diversity_event":
            return self.next_handler(PortfolioOperations).apply_event(e)
        c = self.w["service_cases"][e["target_id"]]
        p = self.w["_event_payloads"].get(e["id"], {})
        k = p["event_kind"]
        ref = p["ref"]
        cash = c["cash_minor"]
        fee = c["fees_minor"]
        out = {}
        r = next((x for x in self.records(c) if x["reference"] == ref), None)
        if k == "marker":
            pass
        elif k == "cash_receipt":
            self.cash(c, p["amount_minor"])
        elif k == "automatic_debit":
            n = p["amount_minor"]
            ok = c["cash_minor"] >= n
            if ok:
                self.cash(c, -n)
            out = dict(amount_minor=n, paid=ok)
        elif k == "debit":
            if r["status"] == "ACCEPTED":
                ok = c["cash_minor"] >= r["amount_minor"]
                if ok:
                    self.cash(c, -r["amount_minor"])
                r["status"] = "DEBITED" if ok else "RETURNED_UNPAID"
        elif k == "mandate_debit":
            if r["active"]:
                ok = c["cash_minor"] >= p["amount_minor"]
                if ok:
                    self.cash(c, -p["amount_minor"])
                r["renewed_instruction"] = True
        elif k == "collection_claim":
            src = next(x["_response"] for x in self.records(c, "external_correspondence"))
            x = src["postings"][ref]
            group = [
                y
                for y in src["postings"].values()
                if (y["authorization"], y["clearing"], y["invoice_period"])
                == (x["authorization"], x["clearing"], x["invoice_period"])
            ]
            valid = (
                len(group) > 1
                and x["ordinal"] > min(y["ordinal"] for y in group)
                and r["claimed_minor"] == r["amount_minor"] - r["refunded_minor"]
            )
            if valid:
                self.cash(c, r["claimed_minor"])
                r["refunded_minor"] += r["claimed_minor"]
            elif c["cash_minor"] >= 400:
                self.charge(c, 400)
            r["dispute_status"] = "CREDITED" if valid else "REJECTED"
        elif k == "account_ready":
            r["account_ready"] = True
            for prod in self.records(c, "product"):
                if prod.get("resolved_entity") == ref:
                    prod["status"] = "ACTIVE"
        elif k == "merchant_recovery":
            r["merchant_minor"] += p["amount_minor"]
            self.cash(c, p["amount_minor"])
        elif k == "insurance_payout":
            r[p["layer"] + "_paid_minor"] += p["amount_minor"]
            self.cash(c, p["amount_minor"])
        elif k == "transfer_result":
            r["status"] = r["_outcome"]
            if r["status"] == "RETURNED":
                self.cash(c, r["_credit_minor"])
                out = {"returned_minor": r["_credit_minor"]}
            else:
                self.byref(c, r["invoice_ref"])["paid_minor"] += r["amount_minor"]
        elif k == "net_receipt":
            route = self.byref(c, p["corridor"])
            actual = min(
                route["_credited_minor"], max(0, r["gross_minor"] - route["_external_minor"])
            )
            residual = max(0, r["gross_minor"] - route["_external_minor"] - actual)
            r["paid_minor"] += actual
            r.update(
                status="RECEIPT_AVAILABLE",
                actual_external_minor=route["_external_minor"],
                actual_credit_minor=actual,
                return_minor=residual,
                bank_version=route["_new_version"],
                receipt_minute=self.w["minute"],
            )
            self.schedule(
                "diversity_event",
                c["id"],
                route["return_delay"],
                event_kind="net_return",
                ref=ref,
                amount_minor=residual,
            )
            out = self.public(r)
        elif k == "net_return":
            self.cash(c, p["amount_minor"])
            r.update(return_status="CREDITED", return_minute=self.w["minute"])
        else:
            self.check(False, "UNKNOWN_DIVERSITY_EVENT")
        self.book(
            c,
            "bank_event",
            event_kind=k,
            event_ref=ref,
            event_id=e["id"],
            cash_delta_minor=c["cash_minor"] - cash,
            fee_minor=c["fees_minor"] - fee,
            cash_after_minor=c["cash_minor"],
            receipt=out,
        )
