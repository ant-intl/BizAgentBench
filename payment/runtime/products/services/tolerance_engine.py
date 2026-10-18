from copy import deepcopy


class ToleranceOperations:
    def tolerance_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "revised_tolerances", "SERVICE_ACTION_MISMATCH")
        self.check(action == "pay", "UNKNOWN_ACTION")
        self.args(data, "invoice_id", "quote_version", "authority_ids")
        z = self.local(c, data["invoice_id"], "authorized_invoice")
        self.check(z["quote_version"] == data["quote_version"], "QUOTE_VERSION_CHANGED")
        aa = data["authority_ids"]
        self.check(
            type(aa) is list and len(aa) == 2 and len(set(aa)) == 2, "TWO_AUTHORITIES_REQUIRED"
        )
        letters = [self.local(c, i, "corporate_authority") for i in aa]
        self.check(len({a["natural_person"] for a in letters}) == 2, "INDEPENDENT_PEOPLE_REQUIRED")
        gross = z["principal_minor"] + z["fee_minor"]
        for a in letters:
            self.check(
                a["status"] == "SIGNED"
                and z["reference"] in a["invoice_refs"]
                and z["beneficiary_version"] in a["beneficiary_versions"]
                and z["fee_minor"] <= a["fee_ceiling_minor"]
                and a["effective_from"] <= self.w["minute"] <= a["effective_through"]
                and a["used_gross_minor"] + gross <= a["total_gross_limit_minor"],
                "AUTHORITY_SCOPE_OR_CAPACITY",
            )
        cash = c["cash_minor"]
        self.cash(c, -z["principal_minor"])
        self.charge(c, z["fee_minor"])
        z["paid_minor"] += z["principal_minor"]
        for a in letters:
            a["used_gross_minor"] += gross
        return self.book(
            c,
            action,
            contract="revised_tolerances",
            arguments=deepcopy(data),
            receipt=dict(
                invoice_ref=z["reference"],
                principal_minor=z["principal_minor"],
                fee_minor=z["fee_minor"],
                beneficiary_version=z["beneficiary_version"],
                authority_refs=[a["reference"] for a in letters],
            ),
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
            fee_minor=z["fee_minor"],
        )

    def tolerance_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        out = {}
        for z in self.records(c, "authorized_invoice"):
            if z["quote_revision_minute"] == self.w["minute"]:
                z.update(**z["_revision"], quote_version=z["quote_version"] + 1)
                out[z["reference"]] = dict(
                    fee_minor=z["fee_minor"],
                    beneficiary_version=z["beneficiary_version"],
                    quote_version=z["quote_version"],
                )
        return self.book(
            c,
            "bank_event",
            event_id=event["id"],
            event_kind="quote_revision",
            receipt=out,
            cash_delta_minor=0,
            cash_after_minor=c["cash_minor"],
        )
