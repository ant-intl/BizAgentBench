from copy import deepcopy


class AuthorityOperations:
    def authority_commit(self, c, invoice):
        gross = invoice["face_minor"] - invoice["prior_paid_minor"] + invoice["fee_minor"]
        currency = invoice["currency"]
        people = set()
        m = self.w["minute"]
        for ref in invoice["attached_signature_refs"]:
            copy = self.byref(c, ref)
            s = copy.get("body")
            if copy["status"] != "AVAILABLE" or not copy["verified_genuine"] or not s:
                continue
            p = self.byref(c, s["person"])
            if (
                s["payment"] != invoice["reference"]
                or s["currency"] != currency
                or currency not in p["currencies"]
                or s["bank_version"] != invoice["bank_version"]
                or s["capacity"] not in p["capacities"]
            ):
                continue
            if not (
                p["effective_from"] <= s["signed_minute"] <= m < p["effective_until"]
            ) or gross > min(s["ceiling_minor"], p["ceiling_minor"]):
                continue
            people.add(p["reference"])
        for ref in sorted(people):
            p = self.byref(c, ref)
            if invoice["reference"] not in p["committed_invoice_refs"]:
                p["committed_invoice_refs"].append(invoice["reference"])
                p["committed_minor"][currency] += gross
        return sorted(people)

    def authority_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "versioned_authority", "SERVICE_CONTRACT_MISMATCH")
        usd = c["cash_minor"]
        eur = c["state"]["eur_minor"]
        out = {}
        fee = 0
        if action == "attach_signatures":
            self.args(data, "record_id", "signature_ids")
            r = self.local(c, data["record_id"], "controlled_invoice")
            ids = data["signature_ids"]
            self.check(
                type(ids) is list
                and all(type(i) is str for i in ids)
                and len(ids) == len(set(ids)),
                "INVALID_EVIDENCE_LIST",
            )
            copies = [self.local(c, i, "signed_instrument") for i in ids]
            self.check(
                all(s["verified_genuine"] and s["status"] == "AVAILABLE" for s in copies),
                "SIGNED_COPY_UNAVAILABLE",
            )
            r["attached_signature_refs"] = [s["reference"] for s in copies]
        elif action == "pay_invoice":
            self.args(data, "record_id", "amount_minor", "bank_version")
            r = self.local(c, data["record_id"], "controlled_invoice")
            n = data["amount_minor"]
            self.positive(n)
            self.check(r["amendment_status"] != "PENDING", "SUPPLIER_CONFIRMATION_PENDING")
            self.check(
                type(data["bank_version"]) is int
                and data["bank_version"] in r["registered_bank_versions"],
                "BANK_DETAILS_NOT_REGISTERED",
            )
            fee = r["fee_minor"] - r["fee_paid_minor"]
            self.cash(c, -n - fee, r["currency"])
            r["paid_minor"] += n
            r["fee_paid_minor"] += fee
            if r["currency"] == "USD":
                c["fees_minor"] += fee
            else:
                c["state"]["eur_fees_minor"] += fee
            # Commitment follows the actual receiving version, including a
            # customer-mistaken choice of a still registered old destination.
            actual = deepcopy(r)
            actual["bank_version"] = data["bank_version"]
            people = self.authority_commit(c, actual)
            out = dict(
                currency=r["currency"],
                supplier_minor=n,
                fee_minor=fee,
                bank_version=data["bank_version"],
                signature_refs=deepcopy(r["attached_signature_refs"]),
                qualifying_person_refs=people,
            )
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="versioned_authority",
            arguments=deepcopy(data),
            receipt=out,
            usd_delta_minor=c["cash_minor"] - usd,
            eur_delta_minor=c["state"]["eur_minor"] - eur,
            usd_after_minor=c["cash_minor"],
            eur_after_minor=c["state"]["eur_minor"],
        )

    def authority_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        kind = self.w["_event_payloads"][event["id"]]["event_kind"]
        out = {}
        if kind == "authority_amendment":
            r = self.byref(c, "EQUIPMENT")
            r["amendment_status"] = "CONFIRMED"
            amendment = r["_amendment"]
            if amendment == "BANK_DETAILS":
                r["bank_version"] = 2
            else:
                r["remittance_note"] = "Updated supplier reference"
            delivered = []
            for s in self.records(c, "signed_instrument"):
                if s["status"] != "AWAITING_COPY":
                    continue
                body = deepcopy(s["_body"])
                if body["payment"] == "EQUIPMENT":
                    body["bank_version"] = r["bank_version"]
                s.update(status="AVAILABLE", body=body)
                delivered.append(s["reference"])
            out = dict(
                amendment=amendment,
                bank_version=r["bank_version"],
                delivered_signature_refs=sorted(delivered),
            )
        elif kind != "authority_window":
            self.check(False, "UNKNOWN_EVENT")
        return self.book(
            c,
            "bank_event",
            event_kind=kind,
            event_id=event["id"],
            receipt=out,
            usd_delta_minor=0,
            eur_delta_minor=0,
            usd_after_minor=c["cash_minor"],
            eur_after_minor=c["state"]["eur_minor"],
        )
