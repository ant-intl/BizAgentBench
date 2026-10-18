"""Platform contract operations."""

from copy import deepcopy


class PlatformOperations:
    def platform_instruction(self, case_id, action, data):
        c = self.row("service_cases", case_id)
        before = c["cash_minor"]
        fees = c["fees_minor"]
        m = self.w["minute"]
        k = c["kind"]
        out = {}
        if k == "platform_campaign_cofunding":
            if action == "elect_campaign":
                self.args(data, "record_id", "mode")
                r = self.local(c, data["record_id"], "campaign_merchant")
                mode = data["mode"]
                self.check(
                    m == 0
                    and r["election"] is None
                    and mode in ("DIRECT_FINAL", "ADJUSTABLE_AGENCY"),
                    "CAMPAIGN_ELECTION_UNAVAILABLE",
                )
                r.update(election=mode, elected_minute=m)
                if mode == "ADJUSTABLE_AGENCY":
                    self.cash(c, r["agency_advance_minor"])
                    self.charge(c, r["agency_fee_minor"])
                    r["advance_received_minor"] = r["agency_advance_minor"]
                out = dict(
                    merchant_ref=r["reference"],
                    mode=mode,
                    advance_received_minor=r["advance_received_minor"],
                )
            elif action == "adjust_agency":
                self.args(data, "record_id")
                r = self.local(c, data["record_id"], "campaign_merchant")
                self.check(
                    r["election"] == "ADJUSTABLE_AGENCY"
                    and r["award_minor"] > 0
                    and not r["agency_adjusted"],
                    "AGENCY_ADJUSTMENT_UNAVAILABLE",
                )
                n = r["award_minor"] - r["advance_received_minor"]
                self.cash(c, n)
                r.update(agency_adjusted=True, agency_adjustment_minor=n)
                out = dict(merchant_ref=r["reference"], signed_sponsor_receipt_minor=n)
            elif action == "remit_sponsor":
                self.args(data, "record_id", "amount_minor")
                r = self.local(c, data["record_id"], "campaign_merchant")
                n = data["amount_minor"]
                self.positive(n)
                self.check(
                    r["agency_adjusted"] and n + r["sponsor_paid_minor"] <= r["award_minor"],
                    "SPONSOR_REMITTANCE_UNAVAILABLE",
                )
                self.cash(c, -n)
                r["sponsor_paid_minor"] += n
                r["last_paid_minute"] = m
                out = dict(
                    merchant_ref=r["reference"], amount_minor=n, funding_source="SPONSOR_AGENCY"
                )
            elif action == "pay_creative":
                self.args(data, "amount_minor")
                n = data["amount_minor"]
                self.positive(n)
                r = self.byref(c, "CREATIVE")
                self.cash(c, -n)
                r["paid_minor"] += n
                r["last_paid_minute"] = m
                out = dict(amount_minor=n)
            elif action in ("claim_sponsor", "pay_cofunding"):
                self.args(data, "record_id", "amount_minor")
                r = self.local(c, data["record_id"], "campaign_merchant")
                n = data["amount_minor"]
                self.positive(n)
                if action == "claim_sponsor":
                    p = self.byref(c, "SPONSOR")
                    self.check(
                        r["election"] is not None
                        and r["final_eligible_minor"] is not None
                        and not r["agency_adjusted"]
                        and m <= p["claim_deadline"]
                        and r["award_minor"] + n
                        <= min(r["individual_claim_limit_minor"], r["final_eligible_minor"])
                        and p["claimed_minor"] + n <= p["cap_minor"],
                        "SPONSOR_CLAIM_UNAVAILABLE",
                    )
                    r["award_minor"] += n
                    p["claimed_minor"] += n
                    if r["election"] == "DIRECT_FINAL":
                        r["sponsor_paid_minor"] += n
                else:
                    self.cash(c, -n)
                    r["owner_paid_minor"] += n
                r["last_paid_minute"] = m
                out = dict(
                    merchant_ref=r["reference"],
                    amount_minor=n,
                    funding_source="SPONSOR" if action == "claim_sponsor" else "CAMPAIGN_OWNER",
                )
            else:
                self.check(False, "UNKNOWN_ACTION")
        elif k == "platform_seller_transfer":
            o = self.byref(c, "ORDER")
            terms = self.byref(c, "TERMS")
            if action == "select_supply":
                self.args(data, "route")
                route = data["route"]
                self.check(
                    m == 0 and o["route"] is None and route in terms["route_fees_minor"],
                    "SUPPLY_SELECTION_UNAVAILABLE",
                )
                self.charge(c, terms["route_fees_minor"][route])
                o.update(route=route, route_minute=m)
                out = dict(route=route)
            elif action == "transfer_stock":
                self.args(data, "record_id", "units")
                x = self.local(c, data["record_id"], "transfer_stock")
                n = data["units"]
                self.positive(n)
                self.check(
                    self.byref(c, "REPORT")["status"] == "FINAL"
                    and x["legal_owner"] == "OLD"
                    and x["old_units"] >= n,
                    "TITLE_TRANSFER_UNAVAILABLE",
                )
                x["old_units"] -= n
                x["new_units"] += n
                out = dict(stock_ref=x["reference"], units=n, from_owner="OLD", to_owner="NEW")
            elif action == "novate_credit":
                self.args(data)
                self.check(
                    self.byref(c, "REPORT")["status"] == "FINAL"
                    and o["credit_owner"] == "OLD"
                    and o["credit_minor"] > 0
                    and o["replacement_delivered_units"] == 0,
                    "CREDIT_NOVATION_UNAVAILABLE",
                )
                o["credit_owner"] = "NEW"
                o["credit_transferred_minor"] = o["credit_minor"]
                out = dict(credit_minor=o["credit_minor"], obligation_owner="NEW")
            elif action == "fulfill_credit":
                self.args(data, "record_id", "units")
                x = self.local(c, data["record_id"], "transfer_stock")
                n = data["units"]
                self.positive(n)
                field = "new_units" if o["credit_owner"] == "NEW" else "old_units"
                self.check(
                    x["legal_owner"] == "OLD"
                    and x["product_model"] == o["remaining_model"]
                    and o["remaining_model"]
                    in terms["replacement_authorizations"][o["credit_owner"]]
                    and x[field] >= n
                    and n * o["unit_price_minor"] <= o["credit_minor"],
                    "CREDIT_FULFILLMENT_UNAVAILABLE",
                )
                x[field] -= n
                x["replacement_consumed_units"] += n
                o["replacement_delivered_units"] += n
                o["credit_minor"] -= n * o["unit_price_minor"]
                o["last_replacement_minute"] = m
                out = dict(stock_ref=x["reference"], units=n, obligation_owner=o["credit_owner"])
            elif action == "settle_payee":
                self.args(data, "record_id", "amount_minor")
                x = self.local(c, data["record_id"], "transfer_payee")
                n = data["amount_minor"]
                self.positive(n)
                self.cash(c, -n)
                x["paid_minor"] += n
                x["last_paid_minute"] = m
                out = dict(payee_ref=x["reference"], amount_minor=n)
            else:
                self.check(False, "UNKNOWN_ACTION")
        elif k == "platform_tradein_replacement":
            terms = self.byref(c, "TERMS")
            if action == "book_collection":
                self.args(data)
                x = self.byref(c, "COLLECTION")
                self.check(m <= 1 and not x["booked"], "COLLECTION_UNAVAILABLE")
                self.charge(c, x["fee_minor"])
                x["booked"] = True
                out = dict(collected_intact=True)
            elif action in ("retire_serial", "return_serial", "release_replacement", "pay_benefit"):
                self.args(data, "record_id")
                x = self.local(c, data["record_id"], "tradein_serial")
                if action == "retire_serial":
                    self.check(
                        self.byref(c, "COLLECTION")["booked"]
                        and not x["retired"]
                        and not x["returned_intact"],
                        "SERIAL_UNAVAILABLE",
                    )
                    x.update(retired=True, retired_minute=m, certificate_id=x["id"] + "-RETIREMENT")
                    out = dict(serial_ref=x["reference"], certificate_id=x["certificate_id"])
                elif action == "return_serial":
                    self.check(
                        self.byref(c, "REPORT")["status"] == "FINAL"
                        and not x["retired"]
                        and not x["returned_intact"],
                        "INTACT_RETURN_UNAVAILABLE",
                    )
                    x["returned_intact"] = True
                    out = dict(serial_ref=x["reference"], returned_intact=True)
                elif action == "release_replacement":
                    self.check(
                        x["retired"]
                        and x["eligible"]
                        and x["offer_minute"] is not None
                        and m >= x["offer_minute"]
                        and not x["supplier_released"],
                        "CERTIFICATE_STOCK_UNAVAILABLE",
                    )
                    self.charge(c, terms["supplier_release_fee_minor"])
                    stock = self.byref(c, "STOCK")
                    stock[x["model"].lower() + "_units"] += 1
                    x["supplier_released"] = True
                    out = dict(
                        serial_ref=x["reference"],
                        certificate_id=x["certificate_id"],
                        model=x["model"],
                        units=1,
                    )
                else:
                    order = self.byref(c, x["order_ref"])
                    self.check(
                        x["retired"]
                        and x["eligible"]
                        and order["delivered"]
                        and x["benefit_paid_minor"] == 0,
                        "BENEFIT_CLAIM_UNAVAILABLE",
                    )
                    x["benefit_paid_minor"] = terms["benefit_minor"]
                    out = dict(
                        serial_ref=x["reference"],
                        household=x["household"],
                        order_ref=x["order_ref"],
                        amount_minor=x["benefit_paid_minor"],
                        certificate_id=x["certificate_id"],
                    )
            elif action == "fulfill_order":
                self.args(data, "record_id", "model", "source")
                x = self.local(c, data["record_id"], "replacement_order")
                model = data["model"]
                source = data["source"]
                self.check(
                    model in x["accepted_models"]
                    and source in ("WAREHOUSE", "STOCK")
                    and not x["delivered"],
                    "ORDER_FULFILLMENT_UNAVAILABLE",
                )
                stock = self.byref(c, source)
                field = model.lower() + "_units"
                self.check(
                    stock[field] > 0 and stock["condition"] in x["accepted_conditions"][model],
                    "MODEL_STOCK_UNAVAILABLE",
                )
                stock[field] -= 1
                x.update(
                    delivered=True,
                    delivered_minute=m,
                    model=model,
                    source=source,
                    condition=stock["condition"],
                )
                out = dict(
                    order_ref=x["reference"],
                    model=model,
                    source=source,
                    condition=stock["condition"],
                    units=1,
                )
            else:
                self.check(False, "UNKNOWN_ACTION")
        else:
            self.check(False, "SERVICE_CONTRACT_MISMATCH")
        return self.book(
            c,
            action,
            contract=k,
            arguments=deepcopy(data),
            receipt=out,
            fee_minor=c["fees_minor"] - fees,
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
        )

    def platform_event(self, event):
        c = self.w["service_cases"][event["target_id"]]
        ek = self.w["_event_payloads"][event["id"]]["event_kind"]
        k = c["kind"]
        m = self.w["minute"]
        out = {}
        before = c["cash_minor"]
        if k == "platform_campaign_cofunding":
            if ek == "campaign_returns":
                p = self.byref(c, "REPORT")
                profile = p["_profile"]
                costs = {
                    "ALDER": 7000 if profile == "ALDER_HIGH" else 3000,
                    "BIRCH": 4000 if profile == "ALDER_HIGH" else 6000,
                    "CEDAR": 1000 if profile == "ALDER_HIGH" else 3000,
                }
                p.update(status="FINAL", profile=profile, final_costs_minor=costs)
                for ref, n in costs.items():
                    self.byref(c, ref)["final_eligible_minor"] = n
                owner = self.byref(c, "OWNER_CONTRIBUTION")
                self.cash(c, owner["amount_minor"])
                owner["received_minor"] = owner["amount_minor"]
                out = dict(
                    profile=profile,
                    final_eligible_minor=costs,
                    owner_received_minor=owner["amount_minor"],
                )
            else:
                self.check(ek in ("creative_deadline", "campaign_deadline"), "UNKNOWN_EVENT")
        elif k == "platform_seller_transfer":
            o = self.byref(c, "ORDER")
            if ek == "priority_delivery":
                dispatched = o["route"] == "PRIORITY_CONSIGN"
                if dispatched:
                    x = self.byref(c, "CONSIGN")
                    self.check(x["old_units"] >= o["ordered_units"], "DISPATCH_STOCK_UNAVAILABLE")
                    x["old_units"] -= o["ordered_units"]
                    x["in_transit_units"] += o["ordered_units"]
                    o.update(dispatched_units=o["ordered_units"], dispatched_minute=m)
                out = dict(
                    priority_dispatch_confirmed=dispatched, dispatched_units=o["dispatched_units"]
                )
            elif ek == "transfer_completion":
                p = self.byref(c, "REPORT")
                profile = p["_profile"]
                delivery = {
                    "STANDARD_ONLY": {"STANDARD": 2, "POWERED": 0},
                    "POWERED_ONLY": {"STANDARD": 0, "POWERED": 2},
                    "FULL": {"STANDARD": 2, "POWERED": 2},
                }[profile]
                n = sum(delivery.values())
                self.check(o["route"] is not None, "SUPPLY_NOT_SELECTED")
                if o["route"] == "OWNED":
                    for ref in ("A", "B"):
                        x = self.byref(c, ref)
                        used = delivery[x["product_model"]]
                        self.check(x["old_units"] >= used, "SHIPMENT_STOCK_UNAVAILABLE")
                        x["old_units"] -= used
                        x["legacy_consumed_units"] += used
                    o.update(dispatched_units=n, dispatched_minute=m)
                    owner = "OLD"
                else:
                    x = self.byref(c, "CONSIGN")
                    self.check(
                        x["old_units"] + x["in_transit_units"] >= n, "SHIPMENT_STOCK_UNAVAILABLE"
                    )
                    if o["route"] == "PRIORITY_CONSIGN":
                        x["old_units"] += x["in_transit_units"] - n
                        x["in_transit_units"] = 0
                    else:
                        x["old_units"] -= n
                        o.update(dispatched_units=n, dispatched_minute=m)
                    x["legacy_consumed_units"] += n
                    owner = "SUPPLIER"
                missing = next(
                    (
                        model
                        for model, count in o["ordered_by_model"].items()
                        if delivery[model] < count
                    ),
                    None,
                )
                p.update(
                    status="FINAL", profile=profile, delivered_by_model=delivery, delivered_units=n
                )
                o.update(
                    legacy_delivered_units=n,
                    remaining_model=missing,
                    credit_minor=(o["ordered_units"] - n) * o["unit_price_minor"],
                )
                out = dict(
                    delivered_units=n,
                    delivered_by_model=delivery,
                    remaining_model=missing,
                    credit_minor=o["credit_minor"],
                    stock_owner=owner,
                )
            else:
                self.check(ek == "transfer_deadline", "UNKNOWN_EVENT")
        elif k == "platform_tradein_replacement":
            if ek == "tradein_inspection":
                p = self.byref(c, "REPORT")
                profile = p["_profile"]
                self.check(self.byref(c, "COLLECTION")["booked"], "COLLECTION_NOT_BOOKED")
                p.update(status="FINAL", profile=profile)
                offers = {}
                for ref in ("S1", "S2", "S3"):
                    x = self.byref(c, ref)
                    eligible = ref != "S1" or profile == "STANDARD"
                    offer = (
                        None
                        if ref == "S3"
                        else (2 if (ref == "S1") == (profile == "STANDARD") else 4)
                    )
                    offer = x.get("offer_override", offer)
                    x.update(eligible=eligible, offer_minute=offer)
                    offers[ref] = dict(eligible=eligible, offer_minute=offer)
                out = dict(profile=profile, serial_results=offers)
            else:
                self.check(
                    ek in ("urgent_deadline", "tradein_deadline", "late_stock"), "UNKNOWN_EVENT"
                )
        else:
            self.check(False, "SERVICE_CONTRACT_MISMATCH")
        return self.book(
            c,
            "bank_event",
            event_id=event["id"],
            event_kind=ek,
            receipt=out,
            cash_delta_minor=c["cash_minor"] - before,
            cash_after_minor=c["cash_minor"],
        )
