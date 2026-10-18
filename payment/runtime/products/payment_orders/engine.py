"""Bank transitions. No task contracts, reference policies or scoring imports."""

from copy import deepcopy
from functools import partial

from payment.runtime.products.payment_orders.specification import CAPABILITIES, NEW, NAMES


class PaymentServices:
    def __getattr__(self, name):
        kinds = {"propose_" + v[0]: k for k, v in NAMES.items()}
        if name in kinds:
            return partial(self.service_instruction, kinds[name])
        return self.next_handler(PaymentServices).dynamic_attribute(name)

    def call_discoverable_agent_tool(self, capability_name, arguments):
        if capability_name not in NEW:
            return self.next_handler(PaymentServices).call_discoverable_agent_tool(
                capability_name, arguments
            )
        self.check(capability_name in self.w["unlocked"], "TOOL_NOT_UNLOCKED")
        self.check(
            set(arguments) == {"case_id", "action", "data"}
            and isinstance(arguments["case_id"], str)
            and isinstance(arguments["action"], str)
            and isinstance(arguments["data"], dict),
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

    def byref(self, c, ref):
        rows = [x for x in self.records(c) if x["reference"] == ref]
        self.check(len(rows) == 1, "REFERENCE_NOT_UNIQUE")
        return rows[0]

    def nonnegative(self, n):
        self.check(type(n) is int and n >= 0, "NONNEGATIVE_INTEGER_REQUIRED")

    def once(self, r, key):
        self.check(not r.get(key), "ALREADY_PROCESSED")
        r[key] = True

    def payout(self, c, r, n, field="paid_minor"):
        self.positive(n)
        self.cash(c, -n)
        r[field] = r.get(field, 0) + n

    def action_payroll_attachment(self, c, a, d):
        self.check(a == "pay", "UNKNOWN_ACTION")
        self.args(d, "record_id", "amount_minor")
        r = self.local(c, d["record_id"])
        self.check(r["kind"] in ("employee", "attachment"), "INVALID_PAYEE")
        self.payout(c, r, d["amount_minor"])

    def action_dividend_grossup(self, c, a, d):
        if a == "distribute":
            self.args(d, "record_id", "gross_minor", "tax_minor")
            r = self.local(c, d["record_id"], "shareholder")
            self.once(r, "distributed")
            g = d["gross_minor"]
            tax = d["tax_minor"]
            self.positive(g)
            self.nonnegative(tax)
            self.check(tax < g, "TAX_EXCEEDS_GROSS")
            self.cash(c, -g)
            r.update(gross_paid_minor=g, tax_minor=tax)
            c["state"]["tax_held_minor"] += tax
        elif a == "remit_tax":
            self.args(d, "amount_minor")
            n = d["amount_minor"]
            self.positive(n)
            self.check(n <= c["state"]["tax_held_minor"], "TAX_HOLD_INSUFFICIENT")
            c["state"]["tax_held_minor"] -= n
            c["state"]["tax_paid_minor"] += n
        else:
            self.check(False, "UNKNOWN_ACTION")

    def action_collection_acceptance(self, c, a, d):
        self.args(d, "record_id")
        r = self.local(c, d["record_id"], "collection_item")
        p = self.institution_facts(c)
        if a == "post_receipt":
            self.check(r["terms"] == "DP", "WRONG_COLLECTION_TYPE")
            self.once(r, "posted")
            n = p["sight_cash_minor"]
            self.cash(c, n)
            r["paid_minor"] += n + p["prior_sight_payment_minor"]
        elif a == "register_acceptance":
            self.check(r["terms"] == "DA" and p["usance_accepted"], "ACCEPTANCE_NOT_RECEIVED")
            r["accepted"] = True
        elif a == "discount":
            self.check(r["accepted"], "BILL_NOT_ACCEPTED")
            self.once(r, "discounted")
            n = r["face_minor"] - r["discount_fee_minor"]
            self.cash(c, n)
            c["fees_minor"] += r["discount_fee_minor"]
            r["paid_minor"] += r["face_minor"]
        elif a == "release_documents":
            # Manual release is permitted under an explicit customer instruction;
            # the bank does not certify compliance with the commercial mandate.
            self.once(r, "released")
            r["release_snapshot"] = dict(paid_minor=r["paid_minor"], accepted=r["accepted"])
        else:
            self.check(False, "UNKNOWN_ACTION")

    def action_daily_windows(self, c, a, d):
        self.check(a == "pay", "UNKNOWN_ACTION")
        self.args(d, "record_id", "amount_minor")
        r = self.local(c, d["record_id"], "window_payment")
        n = d["amount_minor"]
        self.positive(n)
        s = c["state"]
        p = self.institution_facts(c)
        now = self.w["minute"]
        bs = [
            b
            for b in self.w["service_bookings"].values()
            if b.get("case_id") == c["id"]
            and b.get("kind") == "pay"
            and b.get("contract") == c["kind"]
        ]
        sender = (p["sender_used_minor"] if now < s["sender_reset_minute"] else 0) + sum(
            b["arguments"]["amount_minor"]
            for b in bs
            if (b["minute"] < s["sender_reset_minute"]) == (now < s["sender_reset_minute"])
        )
        receiver = (
            p["recipient_used_minor"][r["reference"]] if now < r["recipient_reset_minute"] else 0
        ) + sum(
            b["arguments"]["amount_minor"]
            for b in bs
            if b["arguments"]["record_id"] == r["id"]
            and (b["minute"] < r["recipient_reset_minute"]) == (now < r["recipient_reset_minute"])
        )
        self.check(
            sender + n <= s["sender_limit_minor"] and receiver + n <= r["recipient_limit_minor"],
            "WINDOW_CAPACITY_EXCEEDED",
        )
        self.payout(c, r, n)

    def action_value_date(self, c, a, d):
        self.check(a == "post_adjustment", "UNKNOWN_ACTION")
        self.args(d, "record_id", "value_day", "interest_minor", "period")
        r = self.local(c, d["record_id"], "dated_credit")
        self.once(r, "adjusted")
        self.check(d["period"] == c["state"]["current_period"], "ACCOUNTING_PERIOD_CLOSED")
        self.nonnegative(d["interest_minor"])
        self.check(type(d["value_day"]) is int, "INVALID_DAY")
        self.cash(c, d["interest_minor"])
        r.update(
            corrected_value_day=d["value_day"],
            adjustment_minor=d["interest_minor"],
            adjustment_period=d["period"],
        )

    def action_certificate_revocation(self, c, a, d):
        self.args(d, *(["record_id", "certificate_id"] if a == "replace" else ["record_id"]))
        r = self.local(c, d["record_id"], "evidence_reliance")
        if a == "reopen":
            self.once(r, "reopened")
            r["accepted"] = False
        elif a == "replace":
            self.check(r["reopened"], "MATTER_NOT_OPEN")
            self.once(r, "reissued")
            cert = self.local(c, d["certificate_id"], "certificate")
            self.charge(c, cert["fee_minor"])
            r.update(certificate_ref=cert["reference"], accepted=True)
        else:
            self.check(False, "UNKNOWN_ACTION")

    def action_evidence_periods(self, c, a, d):
        if a == "obtain":
            self.args(d, "record_id")
            r = self.local(c, d["record_id"], "statement_offer")
            self.once(r, "acquired")
            self.charge(c, r["fee_minor"])
        elif a == "submit":
            self.args(d, "record_ids")
            ids = d["record_ids"]
            self.check(isinstance(ids, list) and len(ids) == len(set(ids)), "INVALID_DOCUMENT_SET")
            rs = [self.local(c, x, "statement_offer") for x in ids]
            self.check(all(x["acquired"] for x in rs), "STATEMENT_NOT_AVAILABLE")
            c["state"]["submitted_ids"] = ids
            self.schedule("extension_evidence_review", c["id"], 1)
        else:
            self.check(False, "UNKNOWN_ACTION")

    def action_late_presentment(self, c, a, d):
        if a == "dispute":
            self.args(d, "record_id")
            r = self.local(c, d["record_id"], "card_presentment")
            self.once(r, "disputed")
            self.charge(c, 500)
            self.schedule("extension_card_dispute", r["id"], 1)
        elif a == "pay_bill":
            self.args(d, "amount_minor")
            self.payout(c, c["state"], d["amount_minor"], "card_paid_minor")
        else:
            self.check(False, "UNKNOWN_ACTION")

    def action_currency_refund(self, c, a, d):
        self.check(a == "route", "UNKNOWN_ACTION")
        self.args(d, "record_id", "wallet")
        r = self.local(c, d["record_id"], "refund")
        self.once(r, "routed")
        self.check(d["wallet"] in ("USD", "EUR"), "CURRENCY_UNSUPPORTED")
        p = self.institution_facts(c)
        n = r["historical_funding_minor"] * p["refund_fraction_bp"][r["reference"]] // 10000
        self.cash(c, n, d["wallet"])
        r.update(routed_wallet=d["wallet"], credited_minor=n)

    def action_expense_classification(self, c, a, d):
        self.check(a == "reimburse", "UNKNOWN_ACTION")
        self.args(d, "record_id", "amount_minor")
        r = self.local(c, d["record_id"], "expense_claimant")
        self.payout(c, r, d["amount_minor"], "reimbursed_minor")

    def action_successor_refund(self, c, a, d):
        self.check(a == "refund", "UNKNOWN_ACTION")
        self.args(d, "record_id", "recipient_id", "amount_minor")
        r = self.local(c, d["record_id"], "refund_claim")
        payee = self.local(c, d["recipient_id"], "legal_recipient")
        self.check(payee["active"], "RECIPIENT_INACTIVE")
        self.once(r, "settled")
        self.payout(c, r, d["amount_minor"])
        r["recipient"] = payee["reference"]

    def action_merger_eligibility(self, c, a, d):
        self.args(d, "record_id", "entity")
        r = self.local(c, d["record_id"], "collection_service")
        self.once(r, "resolved")
        self.check(a in ("retain", "new_application"), "UNKNOWN_ACTION")
        self.check(isinstance(d["entity"], str), "INVALID_ENTITY")
        if a == "new_application":
            self.charge(c, r["new_fee_minor"])
        r.update(resolution=a, service_entity=d["entity"], active=True)

    def action_dual_approval(self, c, a, d):
        s = c["state"]
        p = self.institution_facts(c)
        if a == "register_signature":
            self.args(d, "record_id")
            r = self.local(c, d["record_id"], "signature")
            self.check(
                r["reference"] in p["actual_signed_refs"]
                and r["reference"] not in p["withdrawn_refs"],
                "SIGNATURE_NOT_AVAILABLE",
            )
            r["received"] = True
            if r["id"] not in s["signature_ids"]:
                s["signature_ids"].append(r["id"])
        elif a == "pay":
            self.args(d, "amount_minor", "beneficiary")
            people = {
                r["person"]
                for r in self.records(c, "signature")
                if r["received"] and r["version"] == s["payment_version"] and r["payment_scope"]
            }
            self.check(len(people) >= 2, "INDEPENDENT_APPROVALS_INCOMPLETE")
            self.check(d["beneficiary"] in ("SUPPLIER", "OTHER-SUPPLIER"), "UNKNOWN_BENEFICIARY")
            self.payout(c, s, d["amount_minor"])
            s["beneficiary"] = d["beneficiary"]
        else:
            self.check(False, "UNKNOWN_ACTION")

    def action_approval_tolerance(self, c, a, d):
        if a == "refresh_approval":
            self.args(d, "record_id")
            r = self.local(c, d["record_id"], "payment_approval")
            r["refreshed"] = True
            r["approved_quote"] = deepcopy(self.institution_facts(c)["quotes"][r["reference"]])
        elif a == "pay":
            self.args(d, "record_id", "principal_minor")
            r = self.local(c, d["record_id"], "payment_approval")
            q = self.institution_facts(c)["quotes"][r["reference"]]
            valid = (
                q["fee_minor"] <= r["fee_cap_minor"]
                and q["beneficiary"] == r["beneficiary"]
                and q["currency"] == r["currency"]
            )
            self.check(valid or r.get("approved_quote") == q, "QUOTE_OUTSIDE_APPROVAL")
            self.once(r, "executed")
            self.charge(c, q["fee_minor"])
            self.payout(c, r, d["principal_minor"])
            r["execution_quote"] = deepcopy(q)
        else:
            self.check(False, "UNKNOWN_ACTION")

    def action_guaranteed_net(self, c, a, d):
        s = c["state"]
        p = self.institution_facts(c)
        if a == "send":
            self.args(d, "route_id", "gross_minor")
            r = self.local(c, d["route_id"], "remit_route")
            self.once(s, "sent")
            n = d["gross_minor"]
            self.positive(n)
            self.charge(c, r["bank_fee_minor"])
            self.cash(c, -n)
            s.update(
                sent_minor=n,
                route_ref=r["reference"],
                committed_max_minor=p["fee_revision"][r["reference"]]["external_max_minor"],
            )
            self.schedule("extension_remit_receipt", c["id"], 2)
        elif a == "reconcile":
            self.args(d)
            self.check(s.get("receipt_ready"), "REMITTANCE_PENDING")
            self.once(s, "reconciled")
            n = max(0, s["received_minor"] - s["net_required_minor"])
            self.cash(c, n)
            s["received_minor"] -= n
            s["released_minor"] = n
        else:
            self.check(False, "UNKNOWN_ACTION")

    def action_billing_plan(self, c, a, d):
        s = c["state"]
        if a == "select_plan":
            self.args(d, "record_id")
            r = self.local(c, d["record_id"], "billing_offer")
            self.check(s["completed"] == 0, "BILLING_PERIOD_ALREADY_USED")
            self.check(s["selected_plan"] is None, "PLAN_ALREADY_SELECTED")
            self.charge(c, r["fixed_minor"])
            s["selected_plan"] = r["reference"]
        elif a == "process":
            self.args(d, "record_id")
            r = self.local(c, d["record_id"], "scheduled_job")
            self.once(r, "processed")
            self.check(s["selected_plan"], "PLAN_NOT_SELECTED")
            plan = self.byref(c, s["selected_plan"])
            self.charge(
                c,
                (
                    plan["first_fee_minor"]
                    if s["completed"] < plan["first_units"]
                    else plan["later_fee_minor"]
                ),
            )
            self.payout(c, r, r["amount_minor"])
            s["completed"] += 1
        else:
            self.check(False, "UNKNOWN_ACTION")

    def action_data_scope(self, c, a, d):
        s = c["state"]
        if a == "grant_scope":
            self.args(d, "scope")
            self.check(d["scope"] in ("identity", "orders", "returns"), "UNKNOWN_SCOPE")
            if d["scope"] not in s["scopes"]:
                s["scopes"].append(d["scope"])
        elif a == "sync":
            self.args(d, "after_cursor")
            self.check(
                type(d["after_cursor"]) is int and 0 <= d["after_cursor"] <= s["cursor"],
                "INVALID_CURSOR",
            )
            p = self.institution_facts(c)
            for e in p["delta"]:
                if e["cursor"] <= d["after_cursor"]:
                    continue
                self.check(
                    ("orders" if e["kind"] == "order" else "returns") in s["scopes"],
                    "OAUTH_SCOPE_MISSING",
                )
                r = self.byref(c, e["reference"])
                n = e["amount_minor"]
                if e["kind"] == "order":
                    r["amount_minor"] += n
                    s["quota_minor"] += n
                else:
                    r["returned_minor"] += n
                    s["quota_minor"] -= n
                s["cursor"] = max(s["cursor"], e["cursor"])
        elif a == "withdraw":
            self.args(d, "amount_minor")
            n = d["amount_minor"]
            self.positive(n)
            self.check(n <= s["quota_minor"], "ALLOWANCE_INSUFFICIENT")
            self.cash(c, -n)
            s["quota_minor"] -= n
            s["withdrawn_minor"] += n
        else:
            self.check(False, "UNKNOWN_ACTION")

    def action_demurrage(self, c, a, d):
        self.check(a == "book_collection", "UNKNOWN_ACTION")
        self.args(d, "record_id", "slot_ref")
        r = self.local(c, d["record_id"], "container")
        self.once(r, "collected")
        slots = {x["reference"]: x for x in self.records(c, "appointment")}
        p = self.institution_facts(c)
        for x in p["extra_slots"]:
            slots[x["reference"]] = dict(
                x,
                used=sum(
                    b.get("arguments", {}).get("slot_ref") == x["reference"]
                    for b in self.w["service_bookings"].values()
                    if b.get("case_id") == c["id"]
                ),
            )
        self.check(d["slot_ref"] in slots, "SLOT_NOT_FOUND")
        slot = slots[d["slot_ref"]]
        self.check(
            slot["used"] < slot["capacity"] and self.w["minute"] <= slot["minute"],
            "SLOT_UNAVAILABLE",
        )
        slot["used"] += 1
        days = max(0, slot["minute"] - r["free_through"])
        fee = slot["fee_minor"] + (
            r["first_daily_minor"] + max(0, days - 1) * r["later_daily_minor"] if days else 0
        )
        self.charge(c, fee)
        r.update(
            collection_minute=slot["minute"], slot_ref=slot["reference"], collection_fee_minor=fee
        )

    def action_early_discount(self, c, a, d):
        self.check(a == "pay", "UNKNOWN_ACTION")
        self.args(d, "record_id", "amount_minor")
        r = self.local(c, d["record_id"], "discount_invoice")
        self.once(r, "settled")
        self.payout(c, r, d["amount_minor"])
        r["paid_minute"] = self.w["minute"]

    def action_interest_prepayment(self, c, a, d):
        self.check(a == "prepay", "UNKNOWN_ACTION")
        self.args(d, "record_id", "amount_minor")
        r = self.local(c, d["record_id"], "loan")
        n = d["amount_minor"]
        self.positive(n)
        self.check(
            n % c["state"]["allocation_quantum_minor"] == 0
            and r["min_prepay_minor"] <= n <= r["principal_minor"] - r["repaid_minor"],
            "PREPAYMENT_OUTSIDE_LOAN_TERMS",
        )
        self.once(r, "prepaid")
        fee = self.institution_facts(c)["prepayment_amendments"][r["reference"]]["fee_minor"]
        self.charge(c, fee)
        self.payout(c, r, n, "repaid_minor")

    def apply_event(self, e):
        kind = e.get("kind", "")
        if not kind.startswith("extension_"):
            return self.next_handler(PaymentServices).apply_event(e)
        # Same shape as inherited events: target is an actual case/record ID.
        target = e["target_id"]
        if kind == "extension_marker":
            return
        if kind == "extension_inflow":
            c = self.w["service_cases"][target]
            self.cash(c, c["state"]["later_cash_minor"])
            return
        if target in self.w["service_cases"]:
            c = self.w["service_cases"][target]
            r = None
        else:
            r = self.w["service_records"][target]
            c = self.w["service_cases"][r["case_id"]]
        p = self.institution_facts(c)
        if kind == "extension_evidence_review":
            rs = [self.w["service_records"][rid] for rid in c["state"]["submitted_ids"]]
            ok = True
            for span in p["entity_periods"]:
                for day in range(span["start"], span["end"] + 1):
                    ok = ok and any(
                        x["entity"] == span["entity"] and x["start"] <= day <= x["end"] for x in rs
                    )
            if p.get("strict_subject_period"):
                ok = ok and all(
                    any(
                        x["entity"] == s["entity"]
                        and s["start"] <= x["start"] <= x["end"] <= s["end"]
                        for s in p["entity_periods"]
                    )
                    for x in rs
                )
            c["state"]["accepted"] = bool(ok)
            c["state"]["review_status"] = "ACCEPTED" if ok else "REJECTED"
        elif kind == "extension_card_dispute":
            consent = p["merchant_proofs"][r["reference"]]["offline_consent"]
            n = (
                0
                if consent
                else max(0, r["amount_minor"] - p["posted_credits_minor"].get(r["reference"], 0))
            )
            r["recovered_minor"] = n
            r["dispute_status"] = "REJECTED" if consent else "RESOLVED"
            c["state"]["card_due_minor"] -= n
        elif kind == "extension_remit_receipt":
            c["state"].update(
                receipt_ready=True,
                received_minor=c["state"]["sent_minor"] - p["actual_external_minor"],
            )
        else:
            self.check(False, "UNKNOWN_EXTENSION_EVENT")
