from copy import deepcopy
from payment.runtime.products.services.netting_format import fingerprint


class NettingOperations:
    def netting_balance(self, c, inv):
        notes = {}
        for n in self.records(c, "clearing_credit_note"):
            if (
                n["invoice_ref"] == inv["reference"]
                and n["status"] in ("ISSUED", "FINAL", "CANCELLED")
                and n["effective_minute"] <= self.w["minute"]
            ):
                if n["series"] not in notes or n["version"] > notes[n["series"]]["version"]:
                    notes[n["series"]] = n
        credit = sum(n["amount_minor"] for n in notes.values() if n["status"] != "CANCELLED")
        paid = sum(
            p["amount_minor"]
            for p in self.records(c, "intercompany_cash_posting")
            if p["invoice_ref"] == inv["reference"] and p["status"] == "SETTLED"
        )
        return inv["gross_minor"] - credit - paid - inv["netting_settled_minor"]

    def netting_internal(self, c, invoice_refs):
        basis = []
        net = {}
        participants = set()
        for ref in sorted(invoice_refs):
            inv = self.byref(c, ref)
            self.check(inv["kind"] == "intercompany_invoice", "WRONG_SERVICE_RECORD")
            n = self.netting_balance(c, inv)
            basis.append(dict(invoice_ref=ref, amount_minor=n))
            d = inv["debtor_ref"]
            z = inv["creditor_ref"]
            participants.update([d, z])
            net[d] = net.get(d, 0) - n
            net[z] = net.get(z, 0) + n
        payload = dict(type="INTERNAL", currency="USD", basis=basis)
        return dict(
            payload=payload,
            instruction_digest=fingerprint(payload),
            net_minor=dict(sorted(net.items())),
            participant_refs=sorted(participants),
        )

    def netting_new_cycle(self, c, values):
        number = len(self.records(c, "clearing_cycle")) + 1
        return self.new(
            "service_records",
            case_id=c["id"],
            reference="CYCLE-" + str(number),
            name="Prepared clearing instruction " + str(number),
            kind="clearing_cycle",
            customer_access=True,
            status="PREPARED",
            revision=1,
            prepared_minute=self.w["minute"],
            executed_minute=None,
            signature_refs=[],
            **values,
        )

    def netting_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "revised_netting", "SERVICE_CONTRACT_MISMATCH")
        before = c["cash_minor"]
        oldfees = c["fees_minor"]
        receipt = {}
        if action == "prepare_cycle":
            self.args(data, "invoice_ids")
            ids = data["invoice_ids"]
            self.check(
                type(ids) is list
                and ids
                and all(type(x) is str for x in ids)
                and len(ids) == len(set(ids)),
                "DISTINCT_INVOICE_LIST_REQUIRED",
            )
            invoices = [self.local(c, rid, "intercompany_invoice") for rid in ids]
            cycle = self.netting_new_cycle(
                c, self.netting_internal(c, [i["reference"] for i in invoices])
            )
            receipt = dict(cycle=deepcopy(cycle))
        elif action == "prepare_external":
            self.args(data, "obligation_id", "payer_account_id", "recipient_id", "amount_minor")
            o = self.local(c, data["obligation_id"], "clearing_external_obligation")
            payer = self.local(c, data["payer_account_id"], "clearing_account")
            recipient = self.local(c, data["recipient_id"], "clearing_recipient")
            n = data["amount_minor"]
            self.positive(n)
            self.check(
                recipient["status"] == "VERIFIED"
                and recipient["currency"] == payer["currency"] == "USD",
                "RECIPIENT_UNAVAILABLE",
            )
            payload = dict(
                type="EXTERNAL",
                currency="USD",
                obligation_ref=o["reference"],
                payer_ref=payer["reference"],
                recipient_ref=recipient["reference"],
                amount_minor=n,
            )
            cycle = self.netting_new_cycle(
                c,
                dict(
                    payload=payload,
                    instruction_digest=fingerprint(payload),
                    net_minor={payer["reference"]: -n},
                    participant_refs=[payer["reference"]],
                ),
            )
            receipt = dict(cycle=deepcopy(cycle))
        elif action == "refresh_cycle":
            self.args(data, "cycle_id")
            cycle = self.local(c, data["cycle_id"], "clearing_cycle")
            self.check(
                cycle["status"] == "PREPARED" and cycle["payload"]["type"] == "INTERNAL",
                "CYCLE_NOT_REFRESHABLE",
            )
            cycle.update(
                self.netting_internal(c, [x["invoice_ref"] for x in cycle["payload"]["basis"]])
            )
            cycle["revision"] += 1
            receipt = dict(cycle=deepcopy(cycle))
        elif action == "collect_signature":
            self.args(data, "cycle_id", "login_id")
            cycle = self.local(c, data["cycle_id"], "clearing_cycle")
            login = self.local(c, data["login_id"], "clearing_staff_login")
            person = self.byref(c, login["person_ref"])
            self.check(cycle["status"] == "PREPARED", "CYCLE_ALREADY_EXECUTED")
            self.check(
                login["status"] == "ACTIVE"
                and person["effective_from"] <= self.w["minute"] < person["effective_until"],
                "STAFF_SIGNER_UNAVAILABLE",
            )
            self.check(
                login["capacity"] in person["capacities"]
                and person["currency"] == cycle["payload"]["currency"],
                "STAFF_CAPACITY_UNAVAILABLE",
            )
            companies = sorted(set(cycle["participant_refs"]) & set(person["company_refs"]))
            fee = (
                self.byref(c, "TERMS")["fee_per_net_debtor_minor"]
                if cycle["payload"]["type"] == "INTERNAL"
                else 0
            )
            self.check(
                companies
                and all(
                    max(0, -cycle["net_minor"][ref]) + (fee if cycle["net_minor"][ref] < 0 else 0)
                    <= person["debit_ceiling_minor"]
                    for ref in companies
                ),
                "STAFF_SIGNING_SCOPE_EXCEEDED",
            )
            number = len(self.records(c, "clearing_signature")) + 1
            signature = self.new(
                "service_records",
                case_id=c["id"],
                reference="SIGNATURE-" + str(number),
                name="Registered staff signature " + str(number),
                kind="clearing_signature",
                customer_access=True,
                cycle_ref=cycle["reference"],
                person_ref=person["reference"],
                login_ref=login["reference"],
                capacity=login["capacity"],
                company_refs=companies,
                instruction_digest=cycle["instruction_digest"],
                signed_minute=self.w["minute"],
                verified_genuine=True,
                source="registered_staff_signature_service",
            )
            cycle["signature_refs"].append(signature["reference"])
            receipt = dict(signature=deepcopy(signature), cycle_ref=cycle["reference"])
        elif action == "execute":
            self.args(data, "cycle_id")
            cycle = self.local(c, data["cycle_id"], "clearing_cycle")
            self.check(cycle["status"] == "PREPARED", "CYCLE_ALREADY_EXECUTED")
            payload = cycle["payload"]
            rate = (
                self.byref(c, "TERMS")["fee_per_net_debtor_minor"]
                if payload["type"] == "INTERNAL"
                else 0
            )
            charges = {ref: rate if n < 0 else 0 for ref, n in cycle["net_minor"].items()}
            for ref, n in cycle["net_minor"].items():
                account = self.byref(c, ref)
                self.check(
                    account["kind"] == "clearing_account"
                    and account["status"] == "OPEN"
                    and account["currency"] == payload["currency"],
                    "PARTICIPANT_ACCOUNT_UNAVAILABLE",
                )
                self.check(
                    account["cash_minor"] + n - charges[ref] >= 0, "PARTICIPANT_CASH_INSUFFICIENT"
                )
            for ref, n in cycle["net_minor"].items():
                self.byref(c, ref)["cash_minor"] += n - charges[ref]
            c["fees_minor"] += sum(charges.values())
            if payload["type"] == "INTERNAL":
                for item in payload["basis"]:
                    inv = self.byref(c, item["invoice_ref"])
                    inv["netting_settled_minor"] += item["amount_minor"]
                    inv["settlements"].append(
                        dict(
                            cycle_ref=cycle["reference"],
                            amount_minor=item["amount_minor"],
                            minute=self.w["minute"],
                        )
                    )
            else:
                recipient = self.byref(c, payload["recipient_ref"])
                self.check(recipient["status"] == "VERIFIED", "RECIPIENT_UNAVAILABLE")
                o = self.byref(c, payload["obligation_ref"])
                o["paid_minor"] += payload["amount_minor"]
                o["payments"].append(
                    dict(
                        payer_ref=payload["payer_ref"],
                        recipient_ref=payload["recipient_ref"],
                        amount_minor=payload["amount_minor"],
                        minute=self.w["minute"],
                        cycle_ref=cycle["reference"],
                    )
                )
            cycle.update(status="EXECUTED", executed_minute=self.w["minute"])
            receipt = dict(
                cycle_ref=cycle["reference"],
                instruction_digest=cycle["instruction_digest"],
                net_minor=deepcopy(cycle["net_minor"]),
                charges_minor=charges,
                signature_refs=deepcopy(cycle["signature_refs"]),
            )
        else:
            self.check(False, "UNKNOWN_ACTION")
        c["cash_minor"] = sum(a["cash_minor"] for a in self.records(c, "clearing_account"))
        return self.book(
            c,
            action,
            contract="revised_netting",
            arguments=deepcopy(data),
            receipt=receipt,
            cash_delta_minor=c["cash_minor"] - before,
            fee_minor=c["fees_minor"] - oldfees,
            cash_after_minor=c["cash_minor"],
        )

    def netting_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        kind = self.w["_event_payloads"][event["id"]]["event_kind"]
        self.check(kind == "credit_confirmation", "UNKNOWN_EVENT")
        changes = []
        for note in self.records(c, "clearing_credit_note"):
            if note["status"] == "PENDING":
                note.update(status="FINAL", amount_minor=note["_actual_amount_minor"])
                changes.append(dict(note_ref=note["reference"], amount_minor=note["amount_minor"]))
        return self.book(
            c,
            "bank_event",
            event_kind=kind,
            event_id=event["id"],
            receipt=dict(credits=changes),
            cash_delta_minor=0,
            fee_minor=0,
            cash_after_minor=c["cash_minor"],
        )
