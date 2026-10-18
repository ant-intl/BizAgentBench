from copy import deepcopy


class CertificateOperations:
    def certificate_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == "temporal_certificates", "SERVICE_CONTRACT_MISMATCH")
        cash = c["cash_minor"]
        fee = c["fees_minor"]
        if action == "order_certificate":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "evidence_certificate")
            self.check(r["status"] == "AVAILABLE", "CERTIFICATE_NOT_ORDERABLE")
            self.charge(c, r["fee_minor"])
            r["status"] = "PENDING"
            self.schedule(
                "certificate_event",
                case_id,
                r["delivery_delay"],
                event_kind="certificate_delivery",
                reference=r["reference"],
            )
        elif action == "reopen_reliance":
            self.args(data, "record_id")
            r = self.local(c, data["record_id"], "evidence_reliance")
            self.check(r["status"] == "COMPLETE", "FILE_NOT_COMPLETE")
            r.update(
                status="OPEN",
                reopened=True,
                version=r["version"] + 1,
                accepted_day=None,
                completed_minute=None,
            )
        elif action == "submit_evidence":
            self.args(data, "record_id", "certificate_id")
            r = self.local(c, data["record_id"], "evidence_reliance")
            cert = self.local(c, data["certificate_id"], "evidence_certificate")
            self.check(
                r["status"] == "OPEN" and cert["status"] == "ISSUED",
                "FILE_OR_CERTIFICATE_UNAVAILABLE",
            )
            r.update(status="PENDING", certificate=cert["reference"])
            self.schedule(
                "certificate_event",
                case_id,
                1,
                event_kind="reliance_acceptance",
                reference=r["reference"],
            )
        elif action == "rebind_authority":
            self.args(data, "record_id", "versions")
            r = self.local(c, data["record_id"], "dependent_authority")
            self.check(type(data["versions"]) is dict, "INVALID_VERSION_MAP")
            expected = {
                self.byref(c, ref)["id"]: self.byref(c, ref)["version"] for ref in r["requires"]
            }
            self.check(
                data["versions"] == expected
                and all(self.byref(c, ref)["status"] == "COMPLETE" for ref in r["requires"]),
                "SOURCE_ACCEPTANCE_OR_VERSION_UNAVAILABLE",
            )
            r.update(
                bound_versions={ref: self.byref(c, ref)["version"] for ref in r["requires"]},
                rebound=True,
                rebound_minute=self.w["minute"],
            )
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract="temporal_certificates",
            arguments=deepcopy(data),
            cash_delta_minor=c["cash_minor"] - cash,
            fee_minor=c["fees_minor"] - fee,
            cash_after_minor=c["cash_minor"],
        )

    def certificate_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        kind = p["event_kind"]
        out = {}
        if kind == "issuer_scope":
            r = self.byref(c, p["reference"])
            r.update(
                status="PUBLISHED", mode="ANNULMENT", invalid_issue_interval=r["_actual_interval"]
            )
            out = dict(
                reference=r["reference"],
                mode=r["mode"],
                invalid_issue_interval=r["invalid_issue_interval"],
            )
        elif kind == "certificate_delivery":
            r = self.byref(c, p["reference"])
            r.update(status="ISSUED", issued_day=c["bank_day"], delivered_minute=self.w["minute"])
            out = dict(reference=r["reference"], status=r["status"], issued_day=r["issued_day"])
        elif kind == "reliance_acceptance":
            r = self.byref(c, p["reference"])
            r.update(
                status="COMPLETE", accepted_day=c["bank_day"], completed_minute=self.w["minute"]
            )
            out = dict(reference=r["reference"], status=r["status"], version=r["version"])
        elif kind != "marker":
            self.check(False, "UNKNOWN_EVENT")
        return self.book(
            c,
            "bank_event",
            event_kind=kind,
            event_id=event["id"],
            receipt=out,
            cash_delta_minor=0,
            cash_after_minor=c["cash_minor"],
        )
