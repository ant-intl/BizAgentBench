"""Customs, bonded goods and commercial delivery operations."""

from copy import deepcopy


class CustomsOperations:
    def customs_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        kind = c["kind"]
        start = c["cash_minor"]
        fees = c["fees_minor"]
        t = self.byref(c, "TERMS")
        s = c["state"]
        m = self.w["minute"]
        out = {}

        def rec(k):
            return self.local(c, data["record_id"], k)

        def nfield(key):
            n = data[key]
            self.check(type(n) is int and n > 0, "POSITIVE_INTEGER_REQUIRED")
            return n

        if kind == "bonded_drawback":
            if action == "activate_bonded":
                self.args(data, "record_id")
                r = rec("bonded_inventory")
                self.check(
                    m == 0
                    and r["status"] == "AWAITING_ACTIVATION"
                    and not s["activation_requested"],
                    "ACTIVATION_NOT_AVAILABLE",
                )
                self.charge(c, t["activation_fee_minor"])
                s["activation_requested"] = True
            elif action == "convert":
                self.args(data, "record_id", "units")
                r = rec("bonded_inventory")
                n = nfield("units")
                self.check(
                    n % t["lot_increment"] == 0
                    and r["status"] == "BONDED"
                    and n <= r["available_units"] - r["converted_available"],
                    "NO_UNCONVERTED_STOCK",
                )
                tax = n * r["conversion_tax_per_unit_minor"]
                self.cash(c, -tax)
                s["conversion_tax_minor"] += tax
                r["converted_available"] += n
                r["converted_total"] += n
            elif action == "deliver":
                self.args(data, "record_id", "order_id", "units")
                r = rec("bonded_inventory")
                o = self.local(c, data["order_id"], "classified_order")
                n = nfield("units")
                self.check(
                    n % t["lot_increment"] == 0
                    and r["status"] in ("BONDED", "DUTY_PAID")
                    and n <= r["available_units"]
                    and (r["model"] == o["model"] or r["model"] == "dual"),
                    "STOCK_OR_MODEL_UNAVAILABLE",
                )
                if o["destination"] == "domestic":
                    self.check(
                        r["status"] == "DUTY_PAID" or r["converted_available"] >= n,
                        "DOMESTIC_DUTY_MISSING",
                    )
                    if r["status"] == "BONDED":
                        r["converted_available"] -= n
                elif not s["export_fee_paid"]:
                    self.charge(c, t["export_fee_minor"])
                    s["export_fee_paid"] = True
                r["available_units"] -= n
                r["moves"][o["reference"]] = r["moves"].get(o["reference"], 0) + n
                o["delivered_units"] += n
                o["delivery_minute"] = m
            elif action == "certify_drawback":
                self.args(data, "record_id")
                r = rec("bonded_inventory")
                exported = sum(
                    n
                    for ref, n in r["moves"].items()
                    if self.byref(c, ref)["destination"] == "export"
                )
                self.check(
                    m == 1
                    and r["refund_rate_minor"] > 0
                    and exported > 0
                    and not r["certified"]
                    and s["certificate_used"] + r["certificate_slots"] <= t["certificate_capacity"],
                    "DRAWBACK_REVIEW_UNAVAILABLE",
                )
                s["certificate_used"] += r["certificate_slots"]
                r["certified"] = True
            elif action == "drawback":
                self.args(data, "record_id", "amount_minor")
                r = rec("bonded_inventory")
                n = nfield("amount_minor")
                exported = sum(
                    n
                    for ref, n in r["moves"].items()
                    if self.byref(c, ref)["destination"] == "export"
                )
                d = self.byref(c, r["original_duty_ref"])
                self.check(
                    r["certified"]
                    and r["refunded_minor"] + n <= exported * r["refund_rate_minor"]
                    and d["refunded_minor"] + n <= d["paid_minor"],
                    "DRAWBACK_SOURCE_LIMIT",
                )
                r["refunded_minor"] += n
                d["refunded_minor"] += n
                s["drawback_minor"] += n
                self.cash(c, n)
            else:
                self.check(False, "UNKNOWN_ACTION")
            out = dict(reference=r["reference"])
        elif kind == "missing_original_loi":
            if action == "pledge_security":
                self.args(data, "record_id", "security_id")
                r = rec("delivery_indemnity")
                a = self.local(c, data["security_id"], "indemnity_security")
                self.check(
                    not r["issued"]
                    and a["pledged_to"] is None
                    and r["reference"] in a["eligible_indemnities"],
                    "SECURITY_NOT_ELIGIBLE",
                )
                cover = sum(
                    z["value_minor"]
                    for z in self.records(c, "indemnity_security")
                    if z["pledged_to"] == r["reference"]
                )
                self.check(
                    cover + a["value_minor"] <= r["required_cover_minor"], "EXCESS_NONCASH_COVER"
                )
                a.update(pledged_to=r["reference"], returned=False)
            elif action == "pay_supplier":
                self.args(data, "record_id", "amount_minor")
                r = rec("commercial_invoice")
                n = nfield("amount_minor")
                self.cash(c, -n)
                r["paid_minor"] += n
                r["paid_minute"] = m
            else:
                self.args(
                    data,
                    *(
                        ["record_id", "amount_minor"]
                        if action == "issue"
                        else (
                            ["record_id", "units"]
                            if action in ("surrender", "endorse")
                            else ["record_id"]
                        )
                    ),
                )
                r = rec("delivery_indemnity")
                if action == "issue":
                    n = data["amount_minor"]
                    cover = sum(
                        a["value_minor"]
                        for a in self.records(c, "indemnity_security")
                        if a["pledged_to"] == r["reference"]
                    )
                    self.check(
                        type(n) is int
                        and 0 <= n <= r["required_cover_minor"]
                        and not r["issued"]
                        and n + cover >= r["required_cover_minor"],
                        "INSUFFICIENT_OR_INVALID_COVER",
                    )
                    self.cash(c, -n)
                    s["margin_locked_minor"] += n
                    r.update(issued=True, margin_minor=n)
                elif action == "deliver":
                    self.check(
                        r["issued"] and not r["cargo_delivered"], "DELIVERY_INDEMNITY_MISSING"
                    )
                    r.update(cargo_delivered=True, delivery_minute=m)
                elif action == "redeem_bank_original":
                    self.check(r["originals_traced"] and r["bank_held"] == 1, "NO_BANK_ORIGINAL")
                    self.cash(c, -r["lien_release_minor"])
                    r["bank_held"] = 0
                    r["originals_local"] += 1
                elif action in ("surrender", "endorse"):
                    n = nfield("units")
                    self.check(n <= r["originals_local"], "ORIGINAL_NOT_HELD")
                    r["originals_local"] -= n
                    r[
                        "originals_surrendered" if action == "surrender" else "originals_endorsed"
                    ] += n
                elif action == "settle_duplicate_title":
                    self.check(r["originals_endorsed"] > 0, "NO_DUPLICATE_TITLE_CLAIM")
                    self.cash(c, -t["duplicate_cure_minor"])
                    r["originals_surrendered"] += r["originals_endorsed"]
                    r["originals_endorsed"] = 0
                elif action == "lift_bank_hold":
                    self.check(
                        r["originals_traced"] and r["bank_held"] == 0, "BANK_ORIGINAL_OUTSTANDING"
                    )
                    r["bank_hold_lifted"] = True
                elif action == "carrier_release":
                    self.check(
                        s["carrier_desk_open"]
                        and r["cargo_delivered"]
                        and r["originals_surrendered"] == r["originals_required"]
                        and r["bank_hold_lifted"],
                        "CARRIER_EXPOSURE_REMAINS",
                    )
                    r.update(carrier_release=True, liability_minor=0)
                elif action == "close":
                    self.check(r["carrier_release"] and not r["closed"], "NO_CARRIER_DISCHARGE")
                    self.cash(c, r["margin_minor"])
                    s["margin_locked_minor"] -= r["margin_minor"]
                    r.update(closed=True, margin_minor=0)
                    for a in self.records(c, "indemnity_security"):
                        if a["pledged_to"] == r["reference"]:
                            a.update(pledged_to=None, returned=True)
                else:
                    self.check(False, "UNKNOWN_ACTION")
            out = dict(reference=r["reference"])
        elif kind == "incoterm_claim_settlement":
            self.args(
                data, *(["record_id", "amount_minor"] if action == "settle" else ["record_id"])
            )
            r = rec("carriage_order")
            if action == "forward":
                self.check(not r["forwarded"], "ALREADY_FORWARDED")
                self.charge(c, t["carriage_fee_minor"])
                s["carriage_paid_minor"] += t["carriage_fee_minor"]
                r.update(forwarded=True, forwarded_minute=m)
            elif action == "notice":
                self.check(r["forwarded"], "NOT_FORWARDED")
                r["notice"] = True
            elif action == "register_supplier_claim":
                self.check(
                    r["survey_status"] == "FINAL"
                    and r["incident_stage"] == "before_" + r["named_place"]
                    and r["notice"],
                    "NO_SUPPLIER_CLAIM",
                )
                r["supplier_claim_registered"] = True
            elif action == "settle":
                n = nfield("amount_minor")
                self.check(not r["final_settlement"], "SETTLEMENT_ALREADY_FINAL")
                self.cash(c, -n)
                r.update(
                    supplier_paid_minor=n,
                    final_settlement=True,
                    settlement_minute=m,
                    waiver=r["survey_status"] != "FINAL",
                    title_holder="buyer",
                )
            elif action == "assign_claim":
                self.check(
                    m < 3
                    and r["survey_status"] == "FINAL"
                    and r["incident_stage"] == "after_" + r["named_place"]
                    and r["notice"]
                    and (
                        r["title_holder"] == "buyer"
                        or t.get("title_not_required_for_finance", False)
                    )
                    and not r["claim_assigned"],
                    "CLAIM_NOT_FINANCEABLE",
                )
                face = r["damage_minor"]
                fee = r["finance_fee_minor"]
                self.check(
                    s["finance_face_used_minor"] + face <= t["finance_face_limit_minor"],
                    "FINANCE_FACE_LIMIT",
                )
                self.cash(c, face)
                self.charge(c, fee)
                s["finance_face_used_minor"] += face
                s["finance_fees_minor"] += fee
                r.update(claim_assigned=True, factor_advance_minor=face - fee)
            else:
                self.check(False, "UNKNOWN_ACTION")
            out = dict(reference=r["reference"])
        elif kind == "temporary_admission_excise":
            if action == "arrange_processing":
                self.args(data, "record_id", "mode")
                r = rec("processing_report")
                mode = data["mode"]
                self.check(
                    m == 0 and "mode" not in r and mode in ("supervised", "commercial"),
                    "PROCESSING_FIXED",
                )
                r["mode"] = mode
                if mode == "supervised":
                    self.charge(c, t["processing_fee_minor"])
                for ref in r["processing_serial_refs"]:
                    self.byref(c, ref)["processing_mode"] = mode
            elif action == "pay_supplier":
                self.args(data, "record_id", "amount_minor")
                r = rec("commercial_invoice")
                n = nfield("amount_minor")
                self.cash(c, -n)
                r["paid_minor"] += n
                r["paid_minute"] = m
            else:
                self.args(
                    data, *(["record_id", "basis"] if action == "discharge" else ["record_id"])
                )
                r = rec("temporary_serial")
                if action == "convert":
                    self.check(r["regime"] == "TEMPORARY", "ALREADY_CONVERTED")
                    self.cash(c, -t["domestic_tax_minor"])
                    r["tax_paid_minor"] += t["domestic_tax_minor"]
                    s["tax_paid_minor"] += t["domestic_tax_minor"]
                    r["regime"] = "DUTY_PAID"
                elif action == "local_delivery":
                    self.check(
                        r["regime"] == "DUTY_PAID" and r["movement"] is None,
                        "DOMESTIC_ENTRY_MISSING",
                    )
                    r.update(movement="LOCAL", movement_minute=m)
                elif action == "export":
                    self.check(
                        r["movement"] is None and r["actual_condition"] == "INTACT",
                        "NOT_INTACT_AVAILABLE",
                    )
                    r.update(
                        movement="EXPORT",
                        movement_minute=m,
                        exported_components=r["original_components"],
                    )
                elif action == "record_residue":
                    p = self.byref(c, "PROCESSOR")
                    self.check(
                        r["processed"]
                        and r["actual_condition"] == "PARTIAL_LOSS"
                        and not r["residue_recorded"],
                        "NO_FINAL_RESIDUE",
                    )
                    r.update(
                        residue_recorded=True,
                        destroyed_components=p["profiles"][p["profile"]][r["reference"]],
                    )
                elif action == "certify_loss":
                    p = self.byref(c, "PROCESSOR")
                    self.check(
                        r["residue_recorded"]
                        and r["processing_mode"] == "supervised"
                        and not r["loss_certificate"]
                        and p["certified_components"] + r["destroyed_components"]
                        <= t["certification_capacity_components"],
                        "CERTIFICATION_CAPACITY",
                    )
                    p["certified_components"] += r["destroyed_components"]
                    r["loss_certificate"] = True
                elif action == "pay_loss_tax":
                    self.check(
                        r["residue_recorded"]
                        and not r["loss_certificate"]
                        and r["tax_paid_minor"] == 0,
                        "LOSS_TAX_NOT_DUE",
                    )
                    n = r["destroyed_components"] * r["loss_rate_minor"]
                    self.cash(c, -n)
                    r.update(
                        tax_paid_minor=n,
                        tax_paid_minute=m,
                        tax_cleared=bool(t.get("instant_tax_clearance", False)),
                    )
                    s["tax_paid_minor"] += n
                elif action == "export_remainder":
                    key = str(m)
                    count = r["original_components"] - r["destroyed_components"]
                    self.check(
                        r["residue_recorded"]
                        and r["movement"] is None
                        and (r["loss_certificate"] or r["tax_cleared"])
                        and key in t["partial_export_capacity"]
                        and s["partial_export_used"][key] + count
                        <= t["partial_export_capacity"][key],
                        "PARTS_EXIT_WINDOW_OR_CLEARANCE",
                    )
                    s["partial_export_used"][key] += count
                    r.update(
                        movement="PARTIAL_EXPORT", movement_minute=m, exported_components=count
                    )
                elif action == "discharge":
                    basis = data["basis"]
                    self.check(not r["discharged"], "ALREADY_DISCHARGED")
                    valid = (
                        (
                            basis == "domestic_entry"
                            and r["regime"] == "DUTY_PAID"
                            and r["movement"] == "LOCAL"
                        )
                        or (
                            basis == "exit"
                            and r["movement"] == "EXPORT"
                            and r["exported_components"] == r["original_components"]
                        )
                        or (
                            basis == "processing_reconciliation"
                            and r["movement"] == "PARTIAL_EXPORT"
                            and r["exported_components"] + r["destroyed_components"]
                            == r["original_components"]
                            and (r["loss_certificate"] or r["tax_cleared"])
                        )
                    )
                    self.check(valid, "SERIAL_EVIDENCE_MISMATCH")
                    self.cash(c, r["guarantee_minor"])
                    s["collateral_minor"] -= r["guarantee_minor"]
                    r.update(discharged=True, discharge_basis=basis, guarantee_minor=0)
                else:
                    self.check(False, "UNKNOWN_ACTION")
            out = dict(reference=r["reference"])
        else:
            self.check(False, "SERVICE_KIND_MISMATCH")
        return self.book(
            c,
            action,
            contract=kind,
            arguments=deepcopy(data),
            receipt=out,
            cash_delta_minor=c["cash_minor"] - start,
            fee_minor=c["fees_minor"] - fees,
            cash_after_minor=c["cash_minor"],
        )

    def customs_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        k = self.w["_event_payloads"][event["id"]]["event_kind"]
        s = c["state"]
        cash = c["cash_minor"]
        out = {}
        if k == "return_inspection":
            q = self.byref(c, "INSPECTION")
            profile = q["_profile"]
            p = q["profiles"][profile]
            q.update(status="FINAL", profile=profile)
            for ref, key in [
                ("RETURN_MOTOR", "motor_units"),
                ("RETURN_CONTROLLER", "controller_units"),
            ]:
                self.byref(c, ref).update(units=p[key], available_units=p[key], status="DUTY_PAID")
            self.byref(c, "RETURN_MOTOR")["refund_rate_minor"] = p["accepted_original_rate_minor"]
            if s["activation_requested"]:
                r = self.byref(c, "DELAYED_CONTROLLER")
                r.update(status="BONDED", available_units=r["units"])
            out = dict(profile=profile)
        elif k == "original_search":
            q = self.byref(c, "SEARCH")
            complete = q["_complete_set"]
            q.update(status="FINAL", complete_set=complete)
            for r in self.records(c, "delivery_indemnity"):
                r.update(
                    originals_traced=True,
                    originals_local=3 if r["reference"] == complete else 2,
                    bank_held=int(r["reference"] != complete),
                )
            out = dict(complete_set=complete)
        elif k == "carrier_desk":
            s["carrier_desk_open"] = True
        elif k == "damage_survey":
            q = self.byref(c, "SURVEY")
            profile = q["_profile"]
            q.update(status="FINAL", profile=profile)
            for ref, loss in q["profiles"][profile].items():
                self.byref(c, ref).update(**loss, survey_status="FINAL")
            out = dict(profile=profile)
        elif k == "plant_delivery":
            refs = []
            for r in self.records(c, "carriage_order"):
                if r["forwarded"]:
                    r.update(delivered=True, delivered_minute=self.w["minute"])
                    refs.append(r["reference"])
            out = dict(delivered_refs=sorted(refs))
        elif k == "carrier_settlement":
            payments = {}
            for r in self.records(c, "carriage_order"):
                if (
                    r["survey_status"] == "FINAL"
                    and r["notice"]
                    and r["incident_stage"] == "after_" + r["named_place"]
                ):
                    n = r["damage_minor"]
                    r["carrier_paid_minor"] = n
                    if r["claim_assigned"]:
                        r["factor_received_minor"] = n
                        s["finance_face_used_minor"] -= n
                        payments[r["reference"]] = "factor"
                    else:
                        self.cash(c, n)
                        s["claims_received_minor"] += n
                        payments[r["reference"]] = "buyer"
            out = dict(payments=payments)
        elif k == "processing_loss":
            q = self.byref(c, "PROCESSOR")
            q.update(
                status="FINAL",
                profile=q["_profile"],
                buyer_accepted_serial_ref=q["_buyer_serial_ref"],
            )
            for ref in q["processing_serial_refs"]:
                self.byref(c, ref).update(processed=True, actual_condition="PARTIAL_LOSS")
            for ref in q["possible_buyer_serials"]:
                self.byref(c, ref)["contract_destination"] = (
                    "local_customer" if ref == q["_buyer_serial_ref"] else "overseas_owner"
                )
            out = dict(
                profile=q["profile"], buyer_accepted_serial_ref=q["buyer_accepted_serial_ref"]
            )
        elif k == "tax_clearance":
            cleared = []
            for r in self.records(c, "temporary_serial"):
                if r["tax_paid_minute"] is not None and r["tax_paid_minute"] < self.w["minute"]:
                    r["tax_cleared"] = True
                    cleared.append(r["reference"])
            out = dict(cleared=sorted(cleared))
        elif k != "marker":
            self.check(False, "UNKNOWN_EVENT")
        return self.book(
            c,
            "bank_event",
            event_kind=k,
            event_id=event["id"],
            receipt=out,
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
        )


class BondedOperations(CustomsOperations):
    pass


class LOIOperations(CustomsOperations):
    pass


class IncotermOperations(CustomsOperations):
    pass


class TemporaryOperations(CustomsOperations):
    pass
