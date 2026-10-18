"""Operations Desk transitions. No task IDs, acceptance targets or witnesses."""

from copy import deepcopy
from fractions import Fraction

from payment.runtime.products.banking.engine import BusinessError
from payment.runtime.products.payments.specification import (
    EXTRA_TABLES,
    NEW_CAPABILITIES,
    CAPABILITIES,
    TABLES,
)


class DeskPayments:

    def search_records(self, resource, filters=None, cursor=0, limit=20):
        if resource not in EXTRA_TABLES:
            return self.next_handler(DeskPayments).search_records(resource, filters, cursor, limit)
        self.check(0 < limit <= 100 and cursor >= 0, "INVALID_PAGE")
        rows = []
        for r in self.w[resource].values():
            if not r.get("customer_access", True):
                continue
            public = self.public(r)
            matched = True
            for key, wanted in (filters or {}).items():
                self.check(not key.startswith("_"), "FIELD_NOT_PUBLIC")
                value = public.get(key)
                if isinstance(wanted, dict):
                    for op, v in wanted.items():
                        self.check(op in ("in", "contains", "gte", "lte"), "UNSUPPORTED_FILTER")
                        matched &= (
                            value in v
                            if op == "in"
                            else (
                                str(v).lower() in str(value).lower()
                                if op == "contains"
                                else value is not None
                                and (value >= v if op == "gte" else value <= v)
                            )
                        )
                else:
                    matched &= value == wanted
            if matched:
                rows.append(public)
        rows.sort(key=lambda r: r["id"])
        keys = (
            "id",
            "name",
            "scope",
            "business_area",
            "reference",
            "status",
            "entity_id",
            "account_id",
            "portfolio_id",
            "currency",
            "amount_minor",
            "case_id",
        )
        return dict(
            records=[{k: r[k] for k in keys if k in r} for r in rows[cursor : cursor + limit]],
            total=len(rows),
            next_cursor=cursor + limit if cursor + limit < len(rows) else None,
        )

    def get_record(self, resource, record_id):
        if resource in EXTRA_TABLES:
            return self.public(self.row(resource, record_id))
        return self.next_handler(DeskPayments).get_record(resource, record_id)

    def call_discoverable_agent_tool(self, capability_name, arguments):
        if capability_name not in NEW_CAPABILITIES:
            return self.next_handler(DeskPayments).call_discoverable_agent_tool(
                capability_name, arguments
            )
        self.check(capability_name in self.w["unlocked"], "TOOL_NOT_UNLOCKED")
        s = NEW_CAPABILITIES[capability_name]
        self.check(set(arguments) == set(s["parameters"]), "INVALID_CAPABILITY_ARGUMENTS")
        for key, typ in s["parameters"].items():
            self.check(
                type(arguments[key]) is {"string": str, "integer": int, "object": dict}[typ],
                "INVALID_CAPABILITY_ARGUMENT_TYPE",
            )
        if capability_name.startswith("propose_"):
            req = self.new(
                "requests",
                operation=capability_name,
                arguments=deepcopy(arguments),
                status="PENDING",
                prepared_minute=self.w["minute"],
            )
            if capability_name == "propose_treasury_payment":
                req["institution_version"] = self.row("treasury_mandates", arguments["mandate_id"])[
                    "version"
                ]
            return self.public(req)
        return getattr(self, capability_name)(**arguments)

    def execute_request(self, request_id):
        r = self.row("requests", request_id)
        if r["status"] != "EXECUTED" and r["operation"] == "propose_treasury_payment":
            m = self.row("treasury_mandates", r["arguments"]["mandate_id"])
            self.check(
                r["institution_version"] == m["version"], "INSTITUTION_MANDATE_VERSION_CHANGED"
            )
        return self.next_handler(DeskPayments).execute_request(request_id)

    def list_my_files(self):
        return [
            {k: deepcopy(v) for k, v in r.items() if k != "contents" and not k.startswith("_")}
            for r in self.w["files"].values()
            if r.get("available_from", 0) <= self.w["minute"]
        ]

    def upload_file(self, file_id, target_id, purpose):
        f = self.row("files", file_id)
        self.check(f.get("available_from", 0) <= self.w["minute"], "FILE_NOT_AVAILABLE")
        if any(target_id in self.w.get(t, {}) for t in EXTRA_TABLES):
            return self.new(
                "submissions",
                file_id=file_id,
                target_id=target_id,
                purpose=purpose,
                contents=deepcopy(f["contents"]),
                status="SUBMITTED",
                submitted_minute=self.w["minute"],
            )
        return self.next_handler(DeskPayments).upload_file(file_id, target_id, purpose)

    def amounts(self, values):
        self.check(
            bool(values) and all(type(v) is int and v > 0 for v in values.values()),
            "POSITIVE_INTEGER_AMOUNTS_REQUIRED",
        )

    def desk_debit(self, account_id, currency, amount, reason, reference):
        a = self.row("desk_accounts", account_id)
        self.check(
            a["status"] == "OPEN" and amount >= 0 and a["wallet"].get(currency, 0) >= amount,
            "DESK_BALANCE_OR_ACCOUNT_NOT_READY",
        )
        a["wallet"][currency] -= amount
        self.new(
            "desk_journal",
            account_id=account_id,
            currency=currency,
            delta_minor=-amount,
            reason=reason,
            reference=reference,
            minute=self.w["minute"],
        )

    def desk_credit(self, account_id, currency, amount, reason, reference):
        a = self.row("desk_accounts", account_id)
        self.check(a["status"] == "OPEN" and amount >= 0, "DESK_ACCOUNT_NOT_OPEN")
        a["wallet"][currency] = a["wallet"].get(currency, 0) + amount
        self.new(
            "desk_journal",
            account_id=account_id,
            currency=currency,
            delta_minor=amount,
            reason=reason,
            reference=reference,
            minute=self.w["minute"],
        )

    def propose_remittance_advice(self, remittance_id):
        r = self.row("remittances", remittance_id)
        t = self.new(
            "remittance_advice",
            remittance_id=remittance_id,
            scope=r["scope"],
            status="PROCESSING",
            revision=r.get("pending_revision", r["revision"]),
        )
        self.schedule("desk_advice", t["id"], r["trace_delay"])
        return self.public(t)

    def propose_invoice_allocation(self, remittance_id, advice_id, allocations):
        r = self.row("remittances", remittance_id)
        d = self.row("remittance_advice", advice_id)
        self.check(
            self.row("desk_portfolios", r["portfolio_id"])["status"] == "OPEN", "PORTFOLIO_FROZEN"
        )
        self.check(d["remittance_id"] == r["id"] and d["status"] == "READY", "ADVICE_NOT_READY")
        self.amounts(allocations)
        self.check(sum(allocations.values()) <= r["gross_minor"], "ALLOCATION_EXCEEDS_GROSS")
        for iid in allocations:
            self.check(
                self.row("trade_invoices", iid)["portfolio_id"] == r["portfolio_id"],
                "INVOICE_PORTFOLIO_MISMATCH",
            )
        for x in self.w["invoice_allocations"].values():
            if x["remittance_id"] == r["id"] and x["status"] == "CURRENT":
                x["status"] = "SUPERSEDED"
        return self.new(
            "invoice_allocations",
            remittance_id=r["id"],
            scope=r["scope"],
            advice_id=advice_id,
            advice_revision=d["revision"],
            allocations=deepcopy(allocations),
            status="CURRENT",
            minute=self.w["minute"],
        )

    def propose_receivable_close(self, portfolio_id):
        p = self.row("desk_portfolios", portfolio_id)
        self.check(p["status"] == "OPEN", "PORTFOLIO_NOT_OPEN")
        p.update(status="CLOSED", closed_minute=self.w["minute"])
        return self.public(p)

    def propose_attempt_trace(self, attempt_id):
        a = self.row("submission_attempts", attempt_id)
        r = self.new("bank_receipts", attempt_id=a["id"], scope=a["scope"], status="PROCESSING")
        self.schedule("desk_attempt_receipt", r["id"], a["trace_delay"])
        return self.public(r)

    def propose_batch_recipient_update(self, item_id, submission_id):
        item = self.row("payment_batch_items", item_id)
        evidence = self.evidence(submission_id, item_id, "bank_details")
        item.update(
            bank_reference=evidence["bank_reference"],
            bank_version=evidence["bank_version"],
            recipient_verified=True,
        )
        return self.public(item)

    def propose_batch_item_payment(self, item_id, bank_version, submission_key):
        i = self.row("payment_batch_items", item_id)
        old = next(
            (
                r
                for r in self.w["submission_attempts"].values()
                if r["item_id"] == item_id and r["submission_key"] == submission_key
            ),
            None,
        )
        if old:
            self.check(old["bank_version"] == bank_version, "IDEMPOTENCY_PAYLOAD_CONFLICT")
            return self.public(old)
        self.check(
            bank_version == i["bank_version"] and i["recipient_verified"],
            "RECIPIENT_VERSION_NOT_CURRENT",
        )
        a = self.new(
            "submission_attempts",
            item_id=item_id,
            scope=i["scope"],
            submission_key=submission_key,
            bank_version=bank_version,
            bank_reference=i["bank_reference"],
            amount_minor=i["amount_minor"],
            status="SUBMITTED",
            trace_delay=3,
            _bank_received=True,
            submitted_minute=self.w["minute"],
        )
        self.desk_debit(i["account_id"], i["currency"], i["amount_minor"], "batch_payment", a["id"])
        self.schedule("desk_batch_settlement", a["id"], i["settlement_delay"])
        return self.public(a)

    def propose_bank_message_amendment(self, transfer_id, tax_period, tax_reference):
        t = self.row("tax_transfers", transfer_id)
        p = self.row("tax_periods", tax_period)
        self.check(
            self.w["minute"] <= t["amend_until_minute"] and t["status"] == "SENT",
            "BANK_AMENDMENT_WINDOW_CLOSED",
        )
        self.check(
            p["taxpayer_id"] == t["taxpayer_id"] and p["authority_id"] == t["authority_id"],
            "TAXPAYER_OR_AUTHORITY_MISMATCH",
        )
        a = self.new(
            "message_amendments",
            transfer_id=transfer_id,
            scope=t["scope"],
            tax_period=tax_period,
            tax_reference=tax_reference,
            status="REQUESTED",
        )
        t["status"] = "AMENDMENT_PENDING"
        self.schedule("desk_message_ack", a["id"], t["amendment_delay"])
        return self.public(a)

    def propose_tax_allocation(self, transfer_id, allocations, submission_id):
        t = self.row("tax_transfers", transfer_id)
        evidence = self.evidence(submission_id, transfer_id, "tax_certificate")
        self.check(
            evidence["certificate_version"] == t["certificate_version"], "TAX_CERTIFICATE_OUTDATED"
        )
        self.check(
            t["status"] not in ("AMENDMENT_PENDING", "ALLOCATION_PENDING"),
            "TAX_APPLICATION_IN_PROGRESS",
        )
        self.amounts(allocations)
        self.check(sum(allocations.values()) <= t["amount_minor"], "ALLOCATION_EXCEEDS_TRANSFER")
        for pid in allocations:
            p = self.row("tax_periods", pid)
            self.check(
                p["taxpayer_id"] == t["taxpayer_id"] and p["authority_id"] == t["authority_id"],
                "TAXPAYER_OR_AUTHORITY_MISMATCH",
            )
        r = self.new(
            "tax_allocations",
            transfer_id=transfer_id,
            scope=t["scope"],
            allocations=deepcopy(allocations),
            certificate_version=evidence["certificate_version"],
            status="PROCESSING",
        )
        t["status"] = "ALLOCATION_PENDING"
        self.schedule("desk_tax_post", r["id"], t["allocation_delay"])
        return self.public(r)

    def institution_subjects(self, application):
        target = application["entity_id"]
        result = set(application["direct_controllers"])

        def share(person, company, seen):
            self.check(company not in seen, "CYCLIC_OWNERSHIP_GRAPH")
            total = Fraction(0)
            for e in self.w["ownership_edges"].values():
                if e["owned_id"] != company or e["scope"] != application["scope"]:
                    continue
                part = Fraction(e["ownership_bp"], 10000)
                total += (
                    part
                    if e["owner_id"] == person
                    else part * share(person, e["owner_id"], seen | {company})
                )
            return total

        for person in self.w["company_profiles"].values():
            if (
                person["scope"] == application["scope"]
                and person["kind"] == "PERSON"
                and share(person["id"], target, set())
                >= Fraction(application["ownership_threshold_bp"], 10000)
            ):
                result.add(person["id"])
        return result

    def propose_onboarding_bundle(self, application_id, submission_ids):
        a = self.row("onboarding_applications", application_id)
        self.check(
            bool(submission_ids) and all(v is True for v in submission_ids.values()),
            "SUBMISSION_ID_SET_REQUIRED",
        )
        for sid in submission_ids:
            self.evidence(sid, application_id, "corporate_evidence")
        return self.new(
            "evidence_bundles",
            application_id=application_id,
            scope=a["scope"],
            submission_ids=list(submission_ids),
            status="ASSEMBLED",
        )

    def propose_onboarding_submission(self, bundle_id):
        b = self.row("evidence_bundles", bundle_id)
        a = self.row("onboarding_applications", b["application_id"])
        self.check(
            b["status"] == "ASSEMBLED" and a["status"] != "UNDER_REVIEW",
            "APPLICATION_NOT_SUBMITTABLE",
        )
        b.update(status="SUBMITTED", submitted_minute=self.w["minute"])
        a.update(status="UNDER_REVIEW", bundle_id=bundle_id)
        self.schedule("desk_onboarding_review", a["id"], a["review_delay"])
        return self.public(a)

    def propose_desk_account_opening(self, profile_id, entity_id):
        p = self.row("rail_profiles", profile_id)
        e = self.row("company_profiles", entity_id)
        self.check(
            p["new_accounts"] and e["region"] in p["entity_regions"],
            "PRODUCT_APPLICATION_INELIGIBLE",
        )
        self.check(e["openings_remaining"] > 0, "ACCOUNT_ISSUANCE_LIMIT_REACHED")
        e["openings_remaining"] -= 1
        return self.new(
            "desk_accounts",
            scope=e["scope"],
            entity_id=entity_id,
            profile_id=profile_id,
            currency=p["currency"],
            wallet={p["currency"]: 0},
            status="OPEN",
            holder_name=e["legal_name"],
        )

    def propose_rail_binding(self, site_id, account_id, rail):
        s = self.row("rail_sites", site_id)
        a = self.row("desk_accounts", account_id)
        self.check(
            a["status"] == "OPEN" and rail in ("ACH", "WIRE", "LOCAL"), "ACCOUNT_OR_RAIL_INVALID"
        )
        b = self.new(
            "rail_bindings",
            scope=s["scope"],
            site_id=site_id,
            account_id=account_id,
            rail=rail,
            status="PROCESSING",
        )
        self.schedule("desk_rail_validation", b["id"], s["validation_delay"])
        return self.public(b)

    def rail_valid(self, binding):
        s = self.row("rail_sites", binding["site_id"])
        a = self.row("desk_accounts", binding["account_id"])
        p = self.row("rail_profiles", a["profile_id"])
        return (
            a["status"] == "OPEN"
            and a["entity_id"] == s["entity_id"]
            and p["currency"] == s["currency"]
            and binding["rail"] in p["rails"]
            and binding["rail"] == s["sender_rail"]
            and s["category"] in p["categories"]
            and s["market"] in p["markets"]
            and a["holder_name"] == self.row("company_profiles", s["entity_id"])["legal_name"]
        )

    def propose_payout_validation(self, binding_id):
        b = self.row("rail_bindings", binding_id)
        self.check(b["status"] == "ACCEPTED", "BINDING_NOT_ACCEPTED")
        b["status"] = "TEST_PENDING"
        self.schedule(
            "desk_rail_test", binding_id, self.row("rail_sites", b["site_id"])["test_delay"]
        )
        return self.public(b)

    def propose_treasury_mandate(self, mandate_id, submission_id):
        m = self.row("treasury_mandates", mandate_id)
        e = self.evidence(submission_id, mandate_id, "mandate")
        self.check(e["mandate_version"] > m["version"], "MANDATE_REVISION_NOT_NEW")
        self.check(m["status"] != "UNDER_REVIEW", "MANDATE_REVIEW_PENDING")
        m["status"] = "UNDER_REVIEW"
        self.schedule("desk_mandate_effective", mandate_id, m["review_delay"], evidence=deepcopy(e))
        return self.public(m)

    def propose_treasury_payment(
        self, obligation_id, account_id, mandate_id, executor_id, amount_minor
    ):
        o = self.row("treasury_obligations", obligation_id)
        m = self.row("treasury_mandates", mandate_id)
        a = self.row("desk_accounts", account_id)
        self.check(type(amount_minor) is int and amount_minor > 0, "INVALID_AMOUNT")
        used = sum(
            x["amount_minor"]
            for x in self.w["desk_payments"].values()
            if x.get("mandate_id") == mandate_id and x.get("mandate_version") == m["version"]
        )
        self.check(
            m["status"] == "EFFECTIVE" and m["valid_from"] <= self.w["minute"] <= m["valid_until"],
            "MANDATE_NOT_EFFECTIVE",
        )
        self.check(
            m["executor_id"] == executor_id
            and a["entity_id"] == m["payer_entity"]
            and a["entity_id"] in o["institutional_payers"]
            and o["beneficiary_id"] in m["beneficiaries"]
            and o["purpose"] in m["purposes"],
            "INSTITUTION_MANDATE_SCOPE_MISMATCH",
        )
        self.check(used + amount_minor <= m["limit_minor"], "INSTITUTION_MANDATE_LIMIT")
        p = self.new(
            "desk_payments",
            scope=o["scope"],
            kind="TREASURY",
            obligation_id=o["id"],
            account_id=account_id,
            mandate_id=mandate_id,
            mandate_version=m["version"],
            executor_id=executor_id,
            amount_minor=amount_minor,
            currency=o["currency"],
            status="SETTLED",
            minute=self.w["minute"],
        )
        self.desk_debit(account_id, o["currency"], amount_minor, "treasury_payment", p["id"])
        return self.public(p)

    def check_claims(self, case_id, claims):
        self.amounts(claims)
        for cid, amount in claims.items():
            c = self.row("loss_components", cid)
            self.check(
                c["case_id"] == case_id and amount <= c["cleared_minor"],
                "CLAIM_COMPONENT_OR_CLEARING_LIMIT",
            )

    def propose_card_dispute(self, case_id, claims, submission_id):
        c = self.row("card_loss_cases", case_id)
        self.evidence(submission_id, case_id, "delivery")
        self.check_claims(case_id, claims)
        return self.new(
            "card_disputes",
            case_id=case_id,
            scope=c["scope"],
            claims=deepcopy(claims),
            submission_id=submission_id,
            status="OPEN",
            opened_minute=self.w["minute"],
        )

    def propose_dispute_adjustment(self, dispute_id, claims):
        d = self.row("card_disputes", dispute_id)
        self.check(d["status"] == "OPEN", "DISPUTE_NOT_MUTABLE")
        if claims:
            self.check_claims(d["case_id"], claims)
        d["claims"] = deepcopy(claims)
        return self.public(d)

    def propose_dispute_finalization(self, dispute_id):
        d = self.row("card_disputes", dispute_id)
        c = self.row("card_loss_cases", d["case_id"])
        self.check(
            d["status"] == "OPEN" and self.w["minute"] >= c["review_minute"],
            "DISPUTE_REVIEW_NOT_OPEN",
        )
        d.update(status="FINALIZED", finalized_minute=self.w["minute"])
        self.schedule("desk_dispute_settle", dispute_id, c["settlement_delay"])
        return self.public(d)

    def propose_restriction_review(self, restriction_id, submission_id):
        r = self.row("fund_restrictions", restriction_id)
        e = self.evidence(submission_id, restriction_id, "restriction")
        self.check(e["restriction_version"] == r["version"], "RESTRICTION_EVIDENCE_VERSION")
        self.check(set(e["released_lot_ids"]) <= set(r["lot_ids"]), "RELEASE_SCOPE_INVALID")
        self.schedule(
            "desk_restriction_release",
            restriction_id,
            r["review_delay"],
            lot_ids=e["released_lot_ids"],
        )
        return dict(restriction_id=restriction_id, status="UNDER_REVIEW")

    def propose_receivable_release(self, right_id, submission_id):
        r = self.row("receivable_rights", right_id)
        e = self.evidence(submission_id, right_id, "rights_release")
        self.check(
            e["right_version"] == r["version"] and set(e["released_lot_ids"]) <= set(r["lot_ids"]),
            "RIGHTS_EVIDENCE_SCOPE_OR_VERSION",
        )
        required = r.get("release_requires", {})
        if required:
            paid = sum(
                p["amount_minor"]
                for p in self.w["desk_payments"].values()
                if p.get("instruction_id") == required["instruction_id"]
                and p["status"] == "SETTLED"
            )
            self.check(paid >= required["paid_minor"], "RECEIVABLE_RELEASE_CONDITION_UNMET")
        self.schedule(
            "desk_right_release", right_id, r["review_delay"], lot_ids=e["released_lot_ids"]
        )
        return dict(right_id=right_id, status="UNDER_REVIEW")

    def lot_available(self, lot_id):
        for table in ("fund_restrictions", "receivable_rights"):
            for r in self.w[table].values():
                if lot_id in r["lot_ids"] and lot_id not in r["released_lot_ids"]:
                    return False
        return True

    def propose_purpose_payment(self, instruction_id, allocations):
        i = self.row("purpose_instructions", instruction_id)
        self.amounts(allocations)
        for lid, n in allocations.items():
            l = self.row("restricted_lots", lid)
            self.check(
                l["scope"] == i["scope"]
                and i["purpose"] in l["purposes"]
                and self.lot_available(lid),
                "SOURCE_PURPOSE_OR_RESTRICTION",
            )
            self.check(n <= l["remaining_minor"], "SOURCE_BALANCE_INSUFFICIENT")
            l["remaining_minor"] -= n
        return self.new(
            "desk_payments",
            kind="PURPOSE",
            scope=i["scope"],
            instruction_id=instruction_id,
            allocations=deepcopy(allocations),
            amount_minor=sum(allocations.values()),
            currency=i["currency"],
            status="SETTLED",
            minute=self.w["minute"],
        )

    def propose_service_termination(self, service_id):
        s = self.row("desk_services", service_id)
        self.check(s["status"] == "ACTIVE", "SERVICE_NOT_ACTIVE")
        s["status"] = "CANCEL_PENDING"
        self.schedule("desk_service_stop", service_id, s["notice_delay"])
        return self.public(s)

    def propose_billing_migration(self, service_id, account_id):
        s = self.row("desk_services", service_id)
        a = self.row("desk_accounts", account_id)
        self.check(
            a["entity_id"] == s["entity_id"]
            and a["currency"] == s["currency"]
            and a["status"] == "OPEN",
            "BILLING_ACCOUNT_INELIGIBLE",
        )
        self.schedule("desk_billing_move", service_id, s["migration_delay"], account_id=account_id)
        return dict(service_id=service_id, status="MIGRATION_PENDING", account_id=account_id)

    def propose_account_sweep(self, source_account_id, destination_account_id, amount_minor):
        s = self.row("desk_accounts", source_account_id)
        d = self.row("desk_accounts", destination_account_id)
        self.check(
            amount_minor > 0
            and s["entity_id"] == d["entity_id"]
            and s["currency"] == d["currency"]
            and s["id"] != d["id"],
            "SWEEP_SCOPE_INVALID",
        )
        self.check(d["status"] == "OPEN", "DESTINATION_NOT_OPEN")
        self.desk_debit(s["id"], s["currency"], amount_minor, "sweep", d["id"])
        self.desk_credit(d["id"], d["currency"], amount_minor, "sweep", s["id"])
        return dict(
            source_account_id=s["id"], destination_account_id=d["id"], amount_minor=amount_minor
        )

    def propose_desk_account_close(self, account_id):
        a = self.row("desk_accounts", account_id)
        self.check(
            a["status"] == "OPEN" and all(v == 0 for v in a["wallet"].values()),
            "ACCOUNT_NOT_EMPTY_OR_OPEN",
        )
        a.update(status="CLOSED", closed_minute=self.w["minute"])
        return self.public(a)

    def inspect_multicurrency_quote(self, account_id, route_id, source_minor):
        a = self.row("desk_accounts", account_id)
        r = self.row("fx_routes", route_id)
        self.check(
            a["status"] == "OPEN"
            and source_minor >= r["minimum_minor"]
            and r["scope"] == a["scope"],
            "FX_MINIMUM_OR_ACCOUNT",
        )
        value = source_minor
        legs = []
        for leg in r["segments"]:
            value = value * leg["numerator"] // leg["denominator"]
            legs.append(value)
        return self.new(
            "fx_quotes",
            scope=r["scope"],
            account_id=account_id,
            route_id=route_id,
            route_version=r["version"],
            source_currency=r["source_currency"],
            target_currency=r["target_currency"],
            source_minor=source_minor,
            target_minor=value,
            leg_outputs=legs,
            segments=deepcopy(r["segments"]),
            fee_currency=r["fee_currency"],
            fee_minor=r["fee_minor"],
            expires_minute=self.w["minute"] + r["quote_lifetime"],
            settlement_delay=r["settlement_delay"],
            status="QUOTED",
        )

    def propose_fx_execution(self, quote_id):
        q = self.row("fx_quotes", quote_id)
        r = self.row("fx_routes", q["route_id"])
        self.check(
            q["status"] == "QUOTED"
            and self.w["minute"] <= q["expires_minute"]
            and q["route_version"] == r["version"],
            "FX_QUOTE_EXPIRED_OR_VERSION",
        )
        self.desk_debit(
            q["account_id"], q["source_currency"], q["source_minor"], "fx_principal", quote_id
        )
        self.desk_debit(q["account_id"], q["fee_currency"], q["fee_minor"], "fx_fee", quote_id)
        q["status"] = "EXECUTED"
        trade = self.new(
            "fx_trades",
            scope=q["scope"],
            quote_id=quote_id,
            status="PROCESSING",
            submitted_minute=self.w["minute"],
        )
        self.schedule("desk_fx_settle", trade["id"], q["settlement_delay"])
        return self.public(trade)

    def propose_currency_payment(self, obligation_id, account_id, net_minor):
        o = self.row("currency_obligations", obligation_id)
        a = self.row("desk_accounts", account_id)
        self.check(net_minor > 0 and a["scope"] == o["scope"], "CURRENCY_PAYMENT_SCOPE")
        p = self.new(
            "desk_payments",
            kind="CURRENCY",
            scope=o["scope"],
            obligation_id=obligation_id,
            account_id=account_id,
            amount_minor=net_minor,
            currency=o["currency"],
            fee_minor=o["delivery_fee_minor"],
            status="SETTLED",
            minute=self.w["minute"],
        )
        self.desk_debit(
            account_id,
            o["currency"],
            net_minor + o["delivery_fee_minor"],
            "currency_delivery",
            p["id"],
        )
        return self.public(p)

    def apply_event(self, event):
        k = event["kind"]
        target = event["target_id"]
        payload = self.w["_event_payloads"].get(event["id"], {})
        if not k.startswith("desk_"):
            return self.next_handler(DeskPayments).apply_event(event)
        if k == "desk_advice":
            a = self.row("remittance_advice", target)
            r = self.row("remittances", a["remittance_id"])
            lines = r.get("_next_lines", r["allocation_lines"])
            a.update(
                status="READY", allocation_lines=deepcopy(lines), received_minute=self.w["minute"]
            )
            r.update(
                revision=a["revision"], allocation_lines=deepcopy(lines), current_advice_id=a["id"]
            )
            r.pop("pending_revision", None)
            r.pop("_next_lines", None)
        elif k == "desk_attempt_receipt":
            r = self.row("bank_receipts", target)
            a = self.row("submission_attempts", r["attempt_id"])
            status = "SETTLED" if a["_bank_received"] else "NOT_RECEIVED"
            a.update(status=status, confirmed_minute=self.w["minute"])
            r.update(
                status=status,
                item_id=a["item_id"],
                amount_minor=a["amount_minor"],
                bank_reference=a["bank_reference"],
                received_minute=self.w["minute"],
            )
        elif k == "desk_batch_settlement":
            a = self.row("submission_attempts", target)
            a.update(status="SETTLED", settled_minute=self.w["minute"])
            self.new(
                "bank_receipts",
                scope=a["scope"],
                attempt_id=a["id"],
                item_id=a["item_id"],
                amount_minor=a["amount_minor"],
                bank_reference=a["bank_reference"],
                status="SETTLED",
                received_minute=self.w["minute"],
            )
        elif k == "desk_message_ack":
            a = self.row("message_amendments", target)
            t = self.row("tax_transfers", a["transfer_id"])
            a["status"] = "BANK_ACCEPTED"
            t.update(
                status="AMENDED", tax_reference=a["tax_reference"], declared_period=a["tax_period"]
            )
            allocation = self.new(
                "tax_allocations",
                scope=t["scope"],
                transfer_id=t["id"],
                allocations={a["tax_period"]: t["amount_minor"]},
                tax_reference=a["tax_reference"],
                status="PROCESSING",
            )
            self.schedule("desk_tax_post", allocation["id"], t["allocation_delay"])
        elif k == "desk_tax_post":
            r = self.row("tax_allocations", target)
            t = self.row("tax_transfers", r["transfer_id"])
            for old in self.w["tax_allocations"].values():
                if old["transfer_id"] == t["id"] and old["status"] == "POSTED":
                    old["status"] = "SUPERSEDED"
            r.update(status="POSTED", posted_minute=self.w["minute"])
            t["status"] = "ALLOCATED"
        elif k == "desk_profile_revision":
            self.row("company_profiles", target).update(payload["changes"])
        elif k == "desk_document_available":
            pass
        elif k == "desk_onboarding_review":
            a = self.row("onboarding_applications", target)
            b = self.row("evidence_bundles", a["bundle_id"])
            required = {
                a["entity_id"]: "registry",
                **{p: "identity" for p in self.institution_subjects(a)},
            }
            proved = set()
            for sid in b["submission_ids"]:
                s = self.row("submissions", sid)
                c = s["contents"]
                if s["status"] != "APPROVED" or c["valid_until"] < self.w["minute"]:
                    continue
                for subject, claim in c["claims"].items():
                    p = self.row("company_profiles", subject)
                    if claim["version"] == p["version"] and claim["kind"] == required.get(subject):
                        proved.add(subject)
            missing = sorted(set(required) - proved)
            a.update(
                status="NEEDS_EVIDENCE" if missing else "APPROVED",
                missing_subjects=missing,
                reviewed_minute=self.w["minute"],
            )
            b["status"] = a["status"]
        elif k == "desk_sender_revision":
            self.row("rail_sites", target).update(payload["changes"])
        elif k == "desk_rail_validation":
            b = self.row("rail_bindings", target)
            b.update(
                status="ACCEPTED" if self.rail_valid(b) else "REJECTED",
                reviewed_minute=self.w["minute"],
            )
            if b["status"] == "REJECTED":
                b["reason"] = (
                    "Platform attributes or rail do not match the submitted collection arrangement."
                )
        elif k == "desk_rail_test":
            b = self.row("rail_bindings", target)
            s = self.row("rail_sites", b["site_id"])
            b.update(
                status="READY" if self.rail_valid(b) else "TEST_FAILED",
                tested_minute=self.w["minute"],
            )
            if b["status"] == "READY":
                s.update(active_binding=b["id"], status="COLLECTING")
            else:
                b["reason"] = "Actual sender configuration does not accept this collection route."
        elif k == "desk_mandate_effective":
            m = self.row("treasury_mandates", target)
            e = payload["evidence"]
            m.update(e["mandate_fields"])
            m.update(
                version=e["mandate_version"], status="EFFECTIVE", effective_minute=self.w["minute"]
            )
        elif k == "desk_mandate_revision":
            self.row("treasury_mandates", target).update(payload["changes"])
        elif k == "desk_refund_receipt":
            self.row("compensation_entries", target).update(
                status=payload["status"], received_minute=self.w["minute"]
            )
        elif k == "desk_dispute_window":
            pass
        elif k == "desk_dispute_settle":
            d = self.row("card_disputes", target)
            d.update(status="SETTLED", settled_minute=self.w["minute"])
            for cid, amount in d["claims"].items():
                self.new(
                    "compensation_entries",
                    scope=d["scope"],
                    case_id=d["case_id"],
                    component_id=cid,
                    amount_minor=amount,
                    kind="DISPUTE",
                    dispute_id=d["id"],
                    status="SETTLED",
                    received_minute=self.w["minute"],
                )
        elif k in ("desk_restriction_release", "desk_right_release"):
            r = self.row(
                "fund_restrictions" if k == "desk_restriction_release" else "receivable_rights",
                target,
            )
            r["released_lot_ids"] = sorted(set(r["released_lot_ids"]) | set(payload["lot_ids"]))
            r["status"] = (
                "RELEASED" if set(r["released_lot_ids"]) == set(r["lot_ids"]) else "PARTIAL"
            )
        elif k == "desk_service_stop":
            self.row("desk_services", target).update(
                status="TERMINATED", terminated_minute=self.w["minute"]
            )
        elif k == "desk_billing_move":
            self.row("desk_services", target).update(billing_account=payload["account_id"])
        elif k == "desk_bill_accept":
            s = self.row("desk_services", target)
            if s["status"] != "TERMINATED":
                b = self.new(
                    "billing_instances",
                    scope=s["scope"],
                    service_id=s["id"],
                    account_id=s["billing_account"],
                    currency=s["currency"],
                    amount_minor=payload["amount_minor"],
                    status="ACCEPTED",
                    accepted_minute=self.w["minute"],
                )
                self.schedule("desk_bill_debit", b["id"], payload["debit_delay"])
        elif k == "desk_bill_debit":
            b = self.row("billing_instances", target)
            try:
                self.desk_debit(
                    b["account_id"], b["currency"], b["amount_minor"], "service_bill", b["id"]
                )
                b["status"] = "PAID"
            except BusinessError:
                b["status"] = "FAILED"
            b["processed_minute"] = self.w["minute"]
        elif k == "desk_account_return":
            b = self.row("billing_instances", target)
            a = self.row("desk_accounts", b["account_id"])
            if a["status"] == "OPEN":
                self.desk_credit(
                    a["id"], b["currency"], b["amount_minor"], "service_return", b["id"]
                )
                b["status"] = "REFUNDED"
            else:
                b["status"] = "RETURNED"
        elif k == "desk_fx_revision":
            self.row("fx_routes", target).update(payload["changes"])
        elif k == "desk_fx_settle":
            t = self.row("fx_trades", target)
            q = self.row("fx_quotes", t["quote_id"])
            self.desk_credit(
                q["account_id"], q["target_currency"], q["target_minor"], "fx_settlement", q["id"]
            )
            t.update(status="SETTLED", settled_minute=self.w["minute"])
        else:
            raise BusinessError("UNKNOWN_DESK_EVENT")


# Shared customer authorization and event scheduling are identical in both conditions.


class CustomerScope:
    def row(self, table, key):
        r = self.next_handler(CustomerScope).row(table, key)
        self.check(
            getattr(self, "_system_event", False)
            or r.get("scope", self.w["_session_scope"]) in (self.w["_session_scope"], "ARCHIVE"),
            "ACCESS_DENIED",
        )
        return r

    def new(self, table, **values):
        values.setdefault("scope", getattr(self, "_event_scope", None) or self.w["_session_scope"])
        return self.next_handler(CustomerScope).new(table, **values)

    def search_records(self, resource, filters=None, cursor=0, limit=20):
        # User permission follows the signed-in customer, never acceptance target IDs.
        from payment.runtime.core.scoping import project

        scoped = project(self.w, self.w["_session_scope"])
        scoped["_session_scope"] = self.w["_session_scope"]
        return self.view_from(DeskPayments, scoped).search_records(resource, filters, cursor, limit)

    def list_my_files(self):
        return [
            x
            for x in self.next_handler(CustomerScope).list_my_files()
            if x.get("scope") == self.w["_session_scope"]
        ]

    def wait_for_update(self):
        evs = [e for e in self.w["events"].values() if e["status"] == "SCHEDULED"]
        own = [e for e in evs if e.get("scope") == self.w["_session_scope"]]
        if not own:
            return dict(advanced=False, minute=self.w["minute"], events=[])
        nxt = min(e["due_minute"] for e in own)
        finished = []
        # Apply all earlier events, including newly scheduled intermediate events.
        while True:
            due = [
                e
                for e in self.w["events"].values()
                if e["status"] == "SCHEDULED" and e["due_minute"] <= nxt
            ]
            if not due:
                break
            ev = min(due, key=lambda e: (e["due_minute"], e["kind"] == "desk_tax_cutoff", e["id"]))
            self.w["minute"] = ev["due_minute"]
            self._system_event = True
            self._event_scope = ev.get("scope")
            try:
                self.apply_event(ev)
                ev["status"] = "COMPLETED"
            finally:
                self._system_event = False
                self._event_scope = None
            if ev.get("scope") == self.w["_session_scope"]:
                finished.append(self.public(ev))
        self.w["minute"] = nxt
        return dict(advanced=True, minute=nxt, events=finished)

    def propose_onboarding_submission(self, bundle_id):
        b = self.row("evidence_bundles", bundle_id)
        a = self.row("onboarding_applications", b["application_id"])
        fee = a.get("submission_fee_minor", 0)
        if fee:
            self.desk_debit(a["billing_account_id"], "USD", fee, "institution_review", bundle_id)
        result = self.next_handler(CustomerScope).propose_onboarding_submission(bundle_id)
        b.update(charged_fee_minor=fee, billing_account_id=a.get("billing_account_id"))
        return result


# Shared mechanisms parameterized by public record fields, never task IDs.


class ResidualPayments:
    def propose_batch_item_payment(self, item_id, bank_version, submission_key):
        return self.propose_batch_residual_payment(
            item_id,
            bank_version,
            submission_key,
            self.row("payment_batch_items", item_id)["amount_minor"],
        )

    def propose_batch_residual_payment(self, item_id, bank_version, submission_key, amount_minor):
        i = self.row("payment_batch_items", item_id)
        self.check(
            type(amount_minor) is int and 0 < amount_minor <= i["amount_minor"],
            "INVALID_ITEM_PAYMENT_AMOUNT",
        )
        old = next(
            (
                a
                for a in self.w["submission_attempts"].values()
                if a["item_id"] == item_id and a["submission_key"] == submission_key
            ),
            None,
        )
        if old:
            self.check(
                old["bank_version"] == bank_version and old["amount_minor"] == amount_minor,
                "IDEMPOTENCY_PAYLOAD_CONFLICT",
            )
            return self.public(old)
        self.check(
            bank_version == i["bank_version"] and i["recipient_verified"],
            "RECIPIENT_VERSION_NOT_CURRENT",
        )
        a = self.new(
            "submission_attempts",
            item_id=item_id,
            scope=i["scope"],
            submission_key=submission_key,
            bank_version=bank_version,
            bank_reference=i["bank_reference"],
            amount_minor=amount_minor,
            status="SUBMITTED",
            trace_delay=3,
            _bank_received=True,
            submitted_minute=self.w["minute"],
        )
        self.desk_debit(i["account_id"], i["currency"], amount_minor, "batch_payment", a["id"])
        self.schedule("desk_batch_settlement", a["id"], i["settlement_delay"])
        return self.public(a)

    def propose_tax_allocation(self, transfer_id, allocations, submission_id):
        self.check(
            not any(
                x["transfer_id"] == transfer_id and x["status"] == "PROCESSING"
                for x in self.w["tax_allocations"].values()
            ),
            "TAX_APPLICATION_IN_PROGRESS",
        )
        return self.next_handler(ResidualPayments).propose_tax_allocation(
            transfer_id, allocations, submission_id
        )

    def propose_desk_account_opening(self, profile_id, entity_id):
        p = self.row("rail_profiles", profile_id)
        fee = p.get("issuance_fee_minor", 0)
        if fee:
            self.desk_debit(p["billing_account_id"], "USD", fee, "account_issuance", profile_id)
        result = self.next_handler(ResidualPayments).propose_desk_account_opening(
            profile_id, entity_id
        )
        self.row("desk_accounts", result["id"]).update(
            charged_issuance_fee_minor=fee, opened_minute=self.w["minute"]
        )
        return self.public(self.row("desk_accounts", result["id"]))

    def propose_card_dispute(self, case_id, claims, submission_id):
        c = self.row("card_loss_cases", case_id)
        self.check(
            self.w["minute"] <= c.get("filing_deadline_minute", 10**9),
            "NETWORK_FILING_WINDOW_CLOSED",
        )
        fee = c.get("filing_fee_minor", 0)
        if fee:
            self.desk_debit(c["billing_account_id"], "USD", fee, "network_filing", case_id)
        out = self.next_handler(ResidualPayments).propose_card_dispute(
            case_id, claims, submission_id
        )
        d = self.row("card_disputes", out["id"])
        d.update(registered_components=sorted(claims), charged_filing_fee_minor=fee)
        return self.public(d)

    def propose_dispute_adjustment(self, dispute_id, claims):
        d = self.row("card_disputes", dispute_id)
        self.check(
            set(claims) <= set(d.get("registered_components", d["claims"])),
            "COMPONENT_NOT_REGISTERED",
        )
        return self.next_handler(ResidualPayments).propose_dispute_adjustment(dispute_id, claims)

    def apply_event(self, event):
        k = event["kind"]
        target = event["target_id"]
        if k == "desk_attempt_receipt":
            r = self.row("bank_receipts", target)
            a = self.row("submission_attempts", r["attempt_id"])
            n = a.get("_bank_amount_minor", a["amount_minor"]) if a["_bank_received"] else 0
            status = "SETTLED" if n else "NOT_RECEIVED"
            a.update(status=status, confirmed_minute=self.w["minute"], settled_minor=n)
            r.update(
                status=status,
                item_id=a["item_id"],
                amount_minor=n,
                instruction_minor=a["amount_minor"],
                bank_reference=a["bank_reference"],
                received_minute=self.w["minute"],
            )
            return
        if k == "desk_tax_cutoff":
            p = self.row("tax_periods", target)
            paid = sum(
                a["allocations"].get(target, 0)
                for a in self.w["tax_allocations"].values()
                if a["status"] == "POSTED"
            )
            p.update(
                cutoff_paid_minor=paid,
                cutoff_checked_minute=self.w["minute"],
                filing_status="DISCHARGED" if paid >= p["amount_minor"] else "OVERDUE",
            )
            if paid < p["amount_minor"]:
                self.desk_debit(
                    p["fee_account_id"], "USD", p["late_fee_minor"], "authority_late_fee", p["id"]
                )
            return
        self.next_handler(ResidualPayments).apply_event(event)
        if k == "desk_message_ack":
            self.row("tax_transfers", self.row("message_amendments", target)["transfer_id"])[
                "status"
            ] = "ALLOCATION_PENDING"


class LinkedSettlement:
    def desk_debit(self, account_id, currency, amount, reason, reference):
        a = self.row("desk_accounts", account_id)
        self.check(
            a.get("cash_control") != "RESERVE" or getattr(self, "_reserve_release", False),
            "FUNDS_ARE_RESTRICTED_COLLATERAL",
        )
        return self.next_handler(LinkedSettlement).desk_debit(
            account_id, currency, amount, reason, reference
        )

    def reserve_ready(self, conditions):
        for x in conditions:
            if x["kind"] == "field":
                if self.row(x["table"], x["id"]).get(x["field"]) != x["value"]:
                    return False
            elif x["kind"] == "sum":
                rows = [
                    r
                    for r in self.w[x["table"]].values()
                    if r.get("scope") == self.w["_session_scope"]
                    and all(r.get(k) == v for k, v in x["filters"].items())
                ]
                if x.get("dedup_by"):
                    rows = list({r[x["dedup_by"]]: r for r in rows}.values())
                if sum(r.get(x["field"], 0) for r in rows) < x["minimum"]:
                    return False
            else:
                raise BusinessError("UNSUPPORTED_RESERVE_CONDITION")
        return True

    def propose_reserve_disbursement(self, route_id):
        r = self.row("reserve_routes", route_id)
        self.check(
            not any(x["route_id"] == route_id for x in self.w["reserve_disbursements"].values()),
            "RESERVE_OPTION_ALREADY_USED",
        )
        self.check(self.reserve_ready(r["conditions"]), "RESERVE_RELEASE_CONDITIONS_NOT_MET")
        self._reserve_release = True
        try:
            self.desk_debit(
                r["reserve_account_id"], "USD", r["debit_minor"], "reserve_disbursement", route_id
            )
        finally:
            self._reserve_release = False
        self.desk_credit(
            r["destination_account_id"], "USD", r["net_minor"], "reserve_disbursement", route_id
        )
        return self.public(
            self.new(
                "reserve_disbursements",
                route_id=route_id,
                reserve_account_id=r["reserve_account_id"],
                destination_account_id=r["destination_account_id"],
                debit_minor=r["debit_minor"],
                net_minor=r["net_minor"],
                fee_minor=r["debit_minor"] - r["net_minor"],
                minute=self.w["minute"],
                status="SETTLED",
            )
        )

    def propose_bank_message_amendment(self, transfer_id, tax_period, tax_reference):
        t = self.row("tax_transfers", transfer_id)
        fee = t.get("amendment_fee_minor", 0)
        if fee:
            self.desk_debit(
                t["amendment_fee_account_id"], "USD", fee, "bank_amendment_fee", transfer_id
            )
        return self.next_handler(LinkedSettlement).propose_bank_message_amendment(
            transfer_id, tax_period, tax_reference
        )

    def new(self, table, **values):
        if "business_area" not in values:
            if values.get("operation") in CAPABILITIES:
                values["business_area"] = CAPABILITIES[values["operation"]]["rule_id"].removeprefix(
                    "DESK-"
                )
            else:
                for key, val in values.items():
                    if key.endswith("_id") and isinstance(val, str):
                        row = next(
                            (
                                self.w[t][val]
                                for t in EXTRA_TABLES + ["requests", "submissions"]
                                if val in self.w.get(t, {})
                            ),
                            {},
                        )
                        if row.get("business_area"):
                            values["business_area"] = row["business_area"]
                            break
        return self.next_handler(LinkedSettlement).new(table, **values)
