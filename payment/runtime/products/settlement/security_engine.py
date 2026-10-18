"""Local product transitions. No reference or verifier code is imported."""

from copy import deepcopy


def start(e, cid, kind):
    c = e.row("service_cases", cid)
    e.check(c["kind"] == kind, "SERVICE_CONTRACT_MISMATCH")
    return c, (c["cash_minor"], c["fees_minor"])


def booking(e, c, a, d, before, receipt=None):
    return e.book(
        c,
        a,
        contract=c["kind"],
        arguments=deepcopy(d),
        receipt=receipt or {},
        cash_delta_minor=c["cash_minor"] - before[0],
        cash_after_minor=c["cash_minor"],
        fee_minor=c["fees_minor"] - before[1],
    )


def eventbook(e, c, event, before, out):
    return e.book(
        c,
        "bank_event",
        event_id=event["id"],
        event_kind=e.w["_event_payloads"][event["id"]]["event_kind"],
        receipt=out,
        cash_delta_minor=c["cash_minor"] - before,
        cash_after_minor=c["cash_minor"],
    )


def pay(e, c, r, n):
    e.positive(n)
    e.cash(c, -n)
    r["paid_minor"] += n
    r["payments"].append([e.w["minute"], n])


class CredentialOperations:
    def credential_instruction(self, case_id, action, data):
        c, before = start(self, case_id, "credential_ack_cutover")
        old = self.byref(c, "OLD")
        new = self.byref(c, "NEW")
        tenant = self.byref(c, "TENANT")
        pending = self.byref(c, "PENDING")
        if action == "fence_sender":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "sender_credential")
            r["send_enabled"] = False
        elif action == "block_tenant":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "tenant_state")
            self.check(self.w["minute"] == 0, "BLOCK_WINDOW_CLOSED")
            r["blocked"] = self.w["minute"] < self.byref(c, "TERMS")["tenant_unlock_minute"]
            old["send_enabled"] = False
        elif action == "activate":
            self.args(data, "record_id", "identity_mode")
            r = self.local(c, data["record_id"], "sender_credential")
            self.check(
                not r["compromised"] and not r["active"] and not old["send_enabled"],
                "UNSAFE_ACTIVATION",
            )
            slots = int(old["send_enabled"]) + int(old["receipt_enabled"]) + int(new["active"])
            self.check(
                slots < self.byref(c, "TERMS")["slot_capacity"]
                and data["identity_mode"] in ("inherited", "isolated"),
                "ROTATION_CAPACITY_OR_MODE",
            )
            r.update(active=True, send_enabled=True, identity_mode=data["identity_mode"])
        elif action == "retire_receipt":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "sender_credential")
            self.check(pending["status"] != "PENDING", "ACKNOWLEDGEMENT_NOT_FINAL")
            r["receipt_enabled"] = False
        elif action == "production_cutover":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "sender_credential")
            self.check(
                r["active"]
                and not r["compromised"]
                and not old["send_enabled"]
                and not old["receipt_enabled"]
                and pending["status"] != "PENDING"
                and not tenant["blocked"],
                "PRODUCTION_PREREQUISITES",
            )
            r["production"] = True
        elif action in ("retry_original", "pay"):
            self.args(
                data,
                *(
                    ["record_id", "credential_id"]
                    if action == "retry_original"
                    else ["record_id", "credential_id", "amount_minor", "rail"]
                ),
            )
            sender = self.local(c, data["credential_id"], "sender_credential")
            self.check(
                sender["active"]
                and sender["send_enabled"]
                and not sender["compromised"]
                and not tenant["blocked"],
                "SENDER_UNAVAILABLE",
            )
            if action == "pay":
                r = self.local(c, data["record_id"], "security_bill")
                n = data["amount_minor"]
                self.positive(n)
                window = self.byref(c, "CLEARING")
                self.check(data["rail"] in ("standard", "priority"), "INVALID_CLEARING_RAIL")
                if self.w["minute"] >= window["available_minute"]:
                    if data["rail"] == "standard":
                        self.check(
                            window["standard_used_minor"] + n <= window["standard_limit_minor"],
                            "STANDARD_BATCH_CAPACITY",
                        )
                        window["standard_used_minor"] += n
                    else:
                        self.charge(c, window["priority_fee_minor"])
                else:
                    self.check(data["rail"] == "standard", "PRIORITY_WINDOW_NOT_OPEN")
                    self.check(
                        window["early_standard_used_minor"] + n
                        <= window["early_standard_limit_minor"],
                        "EARLY_STANDARD_BATCH_CAPACITY",
                    )
                    window["early_standard_used_minor"] += n
                pay(self, c, r, n)
            else:
                r = self.local(c, data["record_id"], "ambiguous_payment")
                self.check(
                    sender["identity_mode"] == "inherited"
                    and r["status"] != "PENDING"
                    and (r["status"] == "POSTED" or old["receipt_enabled"]),
                    "IDENTITY_OR_ACK_UNAVAILABLE",
                )
                if r["status"] == "POSTED":
                    r["duplicate_attempts"] += 1
                else:
                    window = self.byref(c, "CLEARING")
                    self.check(
                        window["standard_used_minor"] + r["amount_minor"]
                        <= window["standard_limit_minor"],
                        "STANDARD_BATCH_CAPACITY",
                    )
                    window["standard_used_minor"] += r["amount_minor"]
                    pay(self, c, self.byref(c, r["invoice_ref"]), r["amount_minor"])
                    r.update(status="POSTED", settled_minute=self.w["minute"])
        else:
            self.check(False, "UNKNOWN_ACTION")
        return booking(self, c, action, data, before)

    def credential_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        before = c["cash_minor"]
        a = self.w["_event_payloads"][event["id"]]["event_kind"]
        out = {}
        if a == "payment_ack":
            p = self.byref(c, "PENDING")
            outcome = p["_outcome"]
            self.check(outcome in p["possible_outcomes"], "BAD_BANK_OUTCOME")
            r = self.byref(c, p["invoice_ref"])
            if outcome == "POSTED":
                r["paid_minor"] += p["amount_minor"]
                r["payments"].append([self.w["minute"], p["amount_minor"]])
                p["settled_minute"] = self.w["minute"]
            else:
                self.cash(c, p["amount_minor"] + p["rejection_service_credit_minor"])
            if self.byref(c, "OLD")["receipt_enabled"]:
                p["status"] = outcome
            out = dict(
                outcome=outcome,
                amount_minor=p["amount_minor"],
                service_credit_minor=(
                    p["rejection_service_credit_minor"] if outcome == "REJECTED" else 0
                ),
            )
        elif a == "tenant_unlock":
            self.byref(c, "TENANT")["blocked"] = False
        return eventbook(self, c, event, before, out)


class FraudOperations:
    def fraud_instruction(self, case_id, action, data):
        c, before = start(self, case_id, "traced_fraud_restitution")
        f = self.byref(c, "FREEZE")
        claim = self.byref(c, "CLAIM")
        if action == "freeze":
            self.args(data, "record_id", "mode")
            r = self.local(c, data["record_id"], "freeze_election")
            self.check(
                r["mode"] is None and data["mode"] in ("fraud_only", "whole_account"),
                "FREEZE_ELECTION_UNAVAILABLE",
            )
            r["mode"] = data["mode"]
            if r["mode"] == "whole_account":
                self.cash(c, -r["legal_balance_minor"])
                r["legal_locked_minor"] = r["legal_balance_minor"]
        elif action == "return_edge":
            self.args(data, "record_id", "amount_minor", "rail")
            r = self.local(c, data["record_id"], "recovery_node")
            n = data["amount_minor"]
            self.positive(n)
            self.check(
                not claim["settled"]
                and f["mode"] is not None
                and self.byref(c, "TRACE")["status"] == "FINAL",
                "RECALL_NOT_AVAILABLE",
            )
            self.check(
                r["pending_minor"] == 0
                and n <= r["balance_minor"]
                and n <= r["original_edge_minor"] - r["returned_minor"]
                and data["rail"] in ("instant", "deferred"),
                "RECALL_AMOUNT_OR_RAIL",
            )
            r["balance_minor"] -= n
            r["returned_minor"] += n
            if data["rail"] == "instant":
                self.charge(c, r["fee_minor"])
                if r["parent_ref"]:
                    self.byref(c, r["parent_ref"])["balance_minor"] += n
                else:
                    self.cash(c, n)
                    claim["recovered_minor"] += n
            else:
                self.check(r["deferred_eligible"], "NODE_HAS_NO_DEFERRED_RAIL")
                self.check(
                    any(
                        e["status"] == "SCHEDULED" and e["due_minute"] > self.w["minute"]
                        for e in self.w["events"].values()
                    ),
                    "NO_NEXT_SETTLEMENT_EVENT",
                )
                r["pending_minor"] = n
        elif action == "accept_settlement":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "fraud_claim")
            self.check(
                not r["settled"]
                and r["recovered_minor"] == 0
                and all(not x["pending_minor"] for x in self.records(c, "recovery_node")),
                "SETTLEMENT_UNAVAILABLE",
            )
            r.update(settled=True, recovered_minor=r["settlement_minor"])
            self.cash(c, r["settlement_minor"])
            for x in self.records(c, "recovery_node"):
                x["balance_minor"] = 0
        elif action == "pay":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "security_bill")
            pay(self, c, r, data["amount_minor"])
        else:
            self.check(False, "UNKNOWN_ACTION")
        return booking(self, c, action, data, before)

    def fraud_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        before = c["cash_minor"]
        a = self.w["_event_payloads"][event["id"]]["event_kind"]
        claim = self.byref(c, "CLAIM")
        out = {"returns": []}
        for r in self.records(c, "recovery_node"):
            n = r["pending_minor"]
            if n:
                if r["parent_ref"]:
                    self.byref(c, r["parent_ref"])["balance_minor"] += n
                else:
                    self.cash(c, n)
                    claim["recovered_minor"] += n
                r["pending_minor"] = 0
                out["returns"].append([r["reference"], n])
        out["returns"].sort()
        if a == "trace_report":
            t = self.byref(c, "TRACE")
            terminal = t["_terminal"]
            self.check(terminal in t["possible_terminals"], "BAD_BANK_OUTCOME")
            t.update(status="FINAL", terminal_ref=terminal)
            balances = {
                "A": 200000,
                "B": 100000 if terminal == "C" else 200000,
                "C": 200000 if terminal == "C" else 0,
                "D": 100000 if terminal == "D" else 0,
            }
            for ref, n in balances.items():
                self.byref(c, ref)["balance_minor"] = 0 if claim["settled"] else n
            out.update(terminal_ref=terminal, balances=balances)
        elif a == "legal_release":
            r = self.byref(c, "FREEZE")
            self.cash(c, r["legal_locked_minor"])
            out["released_minor"] = r["legal_locked_minor"]
            r["legal_locked_minor"] = 0
        return eventbook(self, c, event, before, out)


def claim_fields(e, c):
    issue = e.byref(c, "REQUEST")["issue"]
    return [
        r
        for r in e.records(c, "personal_field")
        if issue and r["evidence_purpose"] in ("order_reference", issue)
    ]


class RetentionOperations:
    def retention_instruction(self, case_id, action, data):
        c, before = start(self, case_id, "claim_field_retention")
        claim = self.byref(c, "CLAIM")
        exports = self.byref(c, "EXPORTS")
        loan = self.byref(c, "LOAN")
        if action == "delete":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "personal_field")
            self.check(r["original_present"], "ORIGINAL_ALREADY_ABSENT")
            r.update(original_present=False, deleted_minute=self.w["minute"], content=None)
        elif action in ("borrow", "substitute_collateral"):
            self.args(data, "record_id", "collateral_id")
            self.local(c, data["record_id"], "evidence_secured_advance")
            r = self.local(c, data["collateral_id"], "personal_field")
            self.check(r["collateral_eligible"] and r["original_present"], "COLLATERAL_UNAVAILABLE")
            if action == "borrow":
                self.check(
                    not loan["drawn"] and self.w["minute"] <= loan["draw_deadline"],
                    "ADVANCE_UNAVAILABLE",
                )
                loan.update(
                    drawn=True, balance_minor=loan["principal_minor"], collateral_ref=r["reference"]
                )
                self.cash(c, loan["principal_minor"])
                self.charge(c, loan["fee_minor"])
            else:
                self.check(
                    loan["balance_minor"] > 0 and loan["collateral_ref"] != r["reference"],
                    "SUBSTITUTION_UNAVAILABLE",
                )
                self.charge(c, loan["substitution_fee_minor"])
                loan["collateral_ref"] = r["reference"]
        elif action == "repay":
            self.args(data, "record_id", "amount_minor")
            self.local(c, data["record_id"], "evidence_secured_advance")
            n = data["amount_minor"]
            self.positive(n)
            self.check(n <= loan["balance_minor"], "REPAYMENT_EXCEEDS_PRINCIPAL")
            self.cash(c, -n)
            loan["balance_minor"] -= n
            if not loan["balance_minor"]:
                loan["repaid_minute"] = self.w["minute"]
        elif action == "accept_settlement":
            self.args(data, "record_id")
            self.local(c, data["record_id"], "cargo_claim")
            self.check(
                not claim["submitted"] and not claim["settled"] and claim["status"] == "OPEN",
                "CLAIM_SETTLEMENT_UNAVAILABLE",
            )
            claim.update(settled=True, award_minor=claim["early_settlement_minor"], status="FINAL")
            self.cash(c, claim["early_settlement_minor"])
        elif action in ("export", "submit_claim"):
            self.args(data, "record_ids")
            ids = data["record_ids"]
            self.check(
                type(ids) is list and ids and len(ids) == len(set(ids)), "INVALID_FIELD_LIST"
            )
            rs = [self.local(c, x, "personal_field") for x in ids]
            if action == "export":
                self.check(all(r["original_present"] for r in rs), "ORIGINAL_NOT_AVAILABLE")
                exports["items"].append(
                    dict(minute=self.w["minute"], fields={r["reference"]: r["content"] for r in rs})
                )
            else:
                self.check(
                    not claim["submitted"] and not claim["settled"] and self.w["minute"] < 2,
                    "CLAIM_ALREADY_FINAL",
                )
                exported = {ref for x in exports["items"] for ref in x["fields"]}
                self.check(all(r["reference"] in exported for r in rs), "FIELD_NOT_EXPORTED")
                claim.update(submitted=True, export_refs=[r["reference"] for r in rs])
        elif action == "pay":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "security_bill")
            pay(self, c, r, data["amount_minor"])
        else:
            self.check(False, "UNKNOWN_ACTION")
        return booking(self, c, action, data, before)

    def retention_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        before = c["cash_minor"]
        a = self.w["_event_payloads"][event["id"]]["event_kind"]
        request = self.byref(c, "REQUEST")
        claim = self.byref(c, "CLAIM")
        out = {}
        if a == "evidence_request":
            field = request["_field"]
            issue = self.byref(c, field)["evidence_purpose"]
            self.check(issue in request["possible_issues"], "BAD_BANK_OUTCOME")
            request.update(status="FINAL", issue=issue)
            out = dict(issue=issue)
        elif a == "claim_award":
            required = claim_fields(self, c)
            original = all(r["original_present"] for r in required)
            exports = self.byref(c, "EXPORTS")["items"]
            exported = {ref for x in exports if x["minute"] >= 1 for ref in x["fields"]}
            valid = (
                bool(required)
                and {r["reference"] for r in required} <= set(claim["export_refs"])
                and {r["reference"] for r in required} <= exported
                and (original or not self.byref(c, "TERMS")["original_required"])
            )
            amount = (
                0
                if claim["settled"]
                else (
                    (claim["full_minor"] if valid else claim["low_evidence_minor"])
                    if claim["submitted"]
                    else 0
                )
            )
            if not claim["settled"]:
                claim.update(status="FINAL", award_minor=amount)
                self.cash(c, amount)
            chosen = next((r for r in required if r["evidence_purpose"] != "order_reference"), None)
            if chosen and (
                chosen["original_present"] or self.byref(c, "TERMS")["original_required"]
            ):
                self.byref(c, "AUDIT_BASIS")["field_refs"].append(chosen["reference"])
            for basis in self.records(c, "retention_basis"):
                if basis["end_event"] == "claim_award":
                    basis.update(active=False, released_minute=self.w["minute"])
            out = dict(
                award_minor=amount, audit_evidence_ref=chosen["reference"] if chosen else None
            )
        elif a == "audit_release":
            for basis in self.records(c, "retention_basis"):
                if basis["end_event"] == "audit_release":
                    basis.update(active=False, released_minute=self.w["minute"])
            out = dict(released_hold="audit")
        return eventbook(self, c, event, before, out)
