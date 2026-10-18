"""Opt-in value dates, invoice application, equipment calibration and legacy claims.
Only the four current-customer commercial contracts below activate these hooks.
"""

from copy import deepcopy
from payment.runtime.products.psp.receivables.dispatch import (
    rows,
    row,
    update,
    profile,
    need,
    ledger,
)

OPS = {}
S = "string"
I = "integer"
A = "array"


def op(name, description, **fields):

    def dec(fn):
        props = {
            k: dict(type=t, description=d, **{"items": {"type": "string"}} if t == "array" else {})
            for (k, (t, d)) in fields.items()
        }
        OPS[name] = dict(
            description=description,
            parameters=dict(
                type="object", properties=props, required=list(props), additionalProperties=False
            ),
            read_only=False,
            function=fn,
        )
        return fn

    return dec


def release_value(ctx, p, b):
    if b.get("released"):
        return b
    amount = b["amount_minor"]
    ctx.ledger(p["value_account_id"], -amount, "USD", "CREDIT_VALUE_RELEASE_OUT", b["id"])
    ledger(ctx, p, amount, "CREDIT_VALUE_AVAILABLE", b["id"])
    return update(ctx, "credit_value_batches", b["id"], released=True, released_minute=ctx.minute)


@op(
    "a_accelerate_transfer_value",
    "Bring forward one bank-confirmed credited transfer value using a published acceleration service. This schedules or immediately moves its existing held cash once and pays the service charge; it does not create a new transfer or change who intended the original payment.",
    batch_id=(S, "Published held transfer-value batch."),
    service_id=(S, "Transfer-value acceleration service."),
)
def accelerate(ctx, batch_id, service_id):
    b = row(ctx, "credit_value_batches", batch_id)
    p = need(ctx, b["contract_id"], -23)
    s = row(ctx, "credit_value_services", service_id)
    ctx.require(
        b.get("value_date_confirmed")
        and (not b.get("released"))
        and (not b.get("acceleration_requested"))
        and (s["contract_id"] == p["id"]),
        "TRANSFER_VALUE_STATE",
    )
    ledger(ctx, p, -s["fee_minor"], "TRANSFER_VALUE_FEE", s["id"])
    update(
        ctx,
        "credit_value_batches",
        b["id"],
        acceleration_requested=True,
        acceleration_service_id=s["id"],
    )
    if s["release_delay_minutes"]:
        ctx.schedule(
            "a_reasoning_credit_value_date", ctx.minute + s["release_delay_minutes"], b["id"]
        )
        return update(
            ctx,
            "credit_value_batches",
            b["id"],
            accelerated_due_minute=ctx.minute + s["release_delay_minutes"],
        )
    return release_value(ctx, p, b)


@op(
    "a_choose_invoice_collection_service",
    "Buy the merchant application service for a bank-valid legal account and this original invoice. An unused selection may be replaced, retaining all previous fees. The service controls commercial invoice application after bank payment, not legal ownership verification.",
    contract_id=(S, "Original invoice collection contract."),
    service_id=(S, "Merchant collection service ID."),
)
def choose_collection(ctx, contract_id, service_id):
    p = need(ctx, contract_id, -27)
    s = row(ctx, "invoice_collection_services", service_id)
    ctx.require(
        s["contract_id"] == p["id"] and (not p.get("bank_payment_id")), "INVOICE_SERVICE_STATE"
    )
    ledger(ctx, p, -s["fee_minor"], "INVOICE_COLLECTION_FEE", s["id"])
    return update(
        ctx,
        "reasoning_contracts",
        p["id"],
        selected_collection_service=s["id"],
        selected_recipient_id=s["recipient_id"],
    )


@op(
    "a_reserve_equipment_test_slot",
    "Reserve a commissioning bench for the original compatible equipment set. A nonrefundable reservation price and a later execution fee are separate. The slot does not buy merchandise or certify components that have not arrived.",
    contract_id=(S, "Original equipment commissioning contract."),
    slot_id=(S, "Published test slot."),
)
def reserve_test(ctx, contract_id, slot_id):
    p = need(ctx, contract_id, -29)
    s = row(ctx, "equipment_test_slots", slot_id)
    ctx.require(
        s["contract_id"] == p["id"]
        and ctx.minute <= s["book_until"]
        and (s["remaining_capacity"] > 0),
        "TEST_SLOT_UNAVAILABLE",
    )
    ledger(ctx, p, -s["booking_fee_minor"], "EQUIPMENT_TEST_BOOKING", s["id"])
    update(ctx, "equipment_test_slots", s["id"], remaining_capacity=s["remaining_capacity"] - 1)
    return ctx.create(
        "xp_a_equipment_test_bookings",
        customer_id=p["customer_id"],
        contract_id=p["id"],
        slot_id=s["id"],
        status="RESERVED",
        booked_minute=ctx.minute,
    )


@op(
    "a_cancel_equipment_test_slot",
    "Cancel an unexecuted equipment test reservation. Capacity returns to that bench while its nonrefundable reservation price remains spent.",
    booking_id=(S, "Unexecuted equipment test booking."),
)
def cancel_test(ctx, booking_id):
    b = row(ctx, "equipment_test_bookings", booking_id)
    ctx.require(b["status"] == "RESERVED", "TEST_BOOKING_EXECUTED")
    s = row(ctx, "equipment_test_slots", b["slot_id"])
    update(ctx, "equipment_test_slots", s["id"], remaining_capacity=s["remaining_capacity"] + 1)
    return update(ctx, "equipment_test_bookings", b["id"], status="CANCELLED")


@op(
    "a_commission_replacement_equipment",
    "Test the actual original equipment set with its arrived replacement components. Execution charges the booked bench fee and publishes the actual commissioning result at the slot completion time. A tested set cannot include a future or incompatible replacement.",
    contract_id=(S, "Original equipment commissioning contract."),
    booking_id=(S, "Reserved test slot."),
    replacement_order_ids=(A, "Actual replacement orders used in this equipment set."),
)
def commission(ctx, contract_id, booking_id, replacement_order_ids):
    p = need(ctx, contract_id, -29)
    b = row(ctx, "equipment_test_bookings", booking_id)
    s = row(ctx, "equipment_test_slots", b["slot_id"])
    delivery = row(ctx, "deliveries", p["delivery_id"])
    orders = [row(ctx, "replacement_orders", i) for i in replacement_order_ids]
    source = row(ctx, "contracts", p["subject_ids"][0])
    ctx.require(
        b["contract_id"] == p["id"]
        and b["status"] == "RESERVED"
        and (ctx.minute <= s["start_minute"]),
        "TEST_SLOT_MISSED",
    )
    ctx.require(
        delivery["status"] == "RECEIVED"
        and orders
        and all(
            (
                o.get("physically_ready") and o["compatibility"] == source["compatibility"]
                for o in orders
            )
        ),
        "TEST_COMPONENTS_NOT_READY",
    )
    ledger(ctx, p, -s["execution_fee_minor"], "EQUIPMENT_COMMISSIONING_FEE", b["id"])
    update(
        ctx,
        "equipment_test_bookings",
        b["id"],
        status="TESTING",
        replacement_order_ids=replacement_order_ids,
        started_minute=ctx.minute,
    )
    ctx.schedule(
        "a_reasoning_equipment_commissioned", s["start_minute"] + s["duration_minutes"], b["id"]
    )
    return {"status": "TESTING", "complete_minute": s["start_minute"] + s["duration_minutes"]}


@op(
    "a_request_legacy_order_claim",
    "Submit the existing old-order quality claim to the original marketplace provider. Review costs are paid now. The provider retains servicing for this registered claim when the payment binding is later removed, and publishes its actual allowance after the old order result.",
    contract_id=(S, "Existing old-order claim agreement."),
)
def claim_review(ctx, contract_id):
    p = need(ctx, contract_id, -32)
    binding = row(ctx, "bindings", p["binding_id"])
    ctx.require(
        not p.get("claim_requested") and binding["status"] == "ACTIVE",
        "LEGACY_CLAIM_REGISTRATION_STATE",
    )
    ledger(ctx, p, -p["review_fee_minor"], "LEGACY_CLAIM_REVIEW", p["id"])
    update(ctx, "reasoning_contracts", p["id"], claim_requested=True, servicing_retained=True)
    ctx.schedule("a_reasoning_market_claim_notice", max(ctx.minute, p["notice_minute"]), p["id"])
    return {"status": "UNDER_REVIEW"}


@op(
    "a_dispose_legacy_order_claim",
    "Use a confirmed old-order allowance as a credit on the designated unpaid new order, or request a standard/express cash refund. Applying it reduces actual invoice principal; a cash refund arrives only on its value-date event. Future-order credit is also supported but gives neither current cash nor current purchase credit.",
    contract_id=(S, "Confirmed old-order claim."),
    order_id=(S, "Marketplace order associated with the allowance."),
    disposition=(S, "APPLY, CASH_STANDARD, CASH_EXPRESS, CASH_PRIORITY or FUTURE."),
)
def dispose_claim(ctx, contract_id, order_id, disposition):
    p = need(ctx, contract_id, -32)
    order = row(ctx, "market_orders", order_id)
    inv = row(ctx, "invoices", order["invoice_id"])
    ctx.require(
        p.get("claim_confirmed")
        and (not p.get("claim_disposed"))
        and (disposition in ("APPLY", "CASH_STANDARD", "CASH_EXPRESS", "CASH_PRIORITY", "FUTURE")),
        "LEGACY_CLAIM_DISPOSITION",
    )
    amount = p["allowance_minor"]
    if disposition == "APPLY":
        ctx.require(
            order_id == p["new_order_id"]
            and order["status"] != "PAID"
            and (inv["amount_minor"] >= amount),
            "CLAIM_ORDER_NOT_OPEN",
        )
        update(
            ctx,
            "invoices",
            inv["id"],
            amount_minor=inv["amount_minor"] - amount,
            legacy_claim_credit_minor=amount,
        )
        update(
            ctx, "reasoning_contracts", p["id"], realized_minor=amount, realized_minute=ctx.minute
        )
    elif disposition.startswith("CASH"):
        terms = p["cash_services"][disposition]
        ledger(ctx, p, -terms["fee_minor"], "LEGACY_CLAIM_CASH_FEE", p["id"])
        ctx.schedule("a_reasoning_market_claim_cash", ctx.minute + terms["delay_minutes"], p["id"])
    return update(
        ctx,
        "reasoning_contracts",
        p["id"],
        claim_disposed=True,
        claim_disposition=disposition,
        disposed_order_id=order_id,
    )


def register(api):
    api.operations.update(OPS)
    old_event = api.event_handler

    def wrap(name, fn):
        original = api.operations[name]["function"]
        api.operations[name]["function"] = lambda ctx, _original=original, _fn=fn, **args: _fn(
            ctx, _original, args
        )

    def ingest(ctx, original, args):
        notice = row(ctx, "notices", args["notice_id"])
        tx = (
            row(ctx, "bank_transactions", notice["bank_transaction_id"])
            if notice.get("bank_transaction_id")
            else None
        )
        p = profile(ctx, -23, tx["id"]) if tx else None
        was = tx.get("projected") if tx else None
        result = original(ctx, **args)
        if p and (not was):
            value = p["_value_dates"][tx["id"]]
            if value > ctx.minute:
                batch = ctx.create(
                    "xp_a_credit_value_batches",
                    customer_id=p["customer_id"],
                    contract_id=p["id"],
                    transaction_id=tx["id"],
                    amount_minor=tx["amount_minor"],
                    released=False,
                    value_date_confirmed="sender_confirmed_duplicate" in tx,
                    estimated_value_minute=5,
                    **{"value_minute": value} if "sender_confirmed_duplicate" in tx else {},
                )
                ledger(ctx, p, -tx["amount_minor"], "CREDIT_VALUE_HOLD", batch["id"])
                ctx.ledger(
                    p["value_account_id"],
                    tx["amount_minor"],
                    "USD",
                    "CREDIT_VALUE_CUSTODY",
                    batch["id"],
                )
                ctx.schedule("a_reasoning_credit_value_date", value, batch["id"])
        return result

    wrap("a_ingest_credit_notice", ingest)

    def returned(ctx, original, args):
        result = original(ctx, **args)
        p = profile(ctx, -23, args["bank_transaction_id"])
        if p:
            update(
                ctx, "bank_transactions", args["bank_transaction_id"], returned_minute=ctx.minute
            )
        return result

    wrap("a_return_duplicate_transfer", returned)

    def invoice_payment(ctx, original, args):
        p = profile(ctx, -27, args["invoice_id"])
        if p:
            ctx.require(p.get("selected_collection_service"), "INVOICE_COLLECTION_SERVICE_REQUIRED")
            s = row(ctx, "invoice_collection_services", p["selected_collection_service"])
            ctx.require(
                s["recipient_id"] == args["recipient_id"], "INVOICE_SERVICE_ACCOUNT_MISMATCH"
            )
        result = original(ctx, **args)
        if p:
            inv = row(ctx, "invoices", args["invoice_id"])
            payments = [
                x
                for x in rows(ctx, "payments")
                if x["invoice_id"] == inv["id"]
                and x["recipient_id"] == s["recipient_id"]
                and (x["status"] == "SETTLED")
            ]
            total = sum((x["amount_minor"] for x in payments))
            update(
                ctx,
                "reasoning_contracts",
                p["id"],
                bank_payment_id=p.get("bank_payment_id", result["id"]),
                bank_paid_minor=total,
            )
            if total >= inv["amount_minor"] and (not p.get("application_scheduled")):
                update(
                    ctx,
                    "reasoning_contracts",
                    p["id"],
                    application_scheduled=True,
                    bank_paid_minute=ctx.minute,
                    application_due_minute=ctx.minute + s["delay_minutes"],
                )
                ctx.schedule(
                    "a_reasoning_merchant_invoice_applied", ctx.minute + s["delay_minutes"], p["id"]
                )
        p55 = profile(ctx, -23)
        if p55 and args["invoice_id"] == p55["supplier_invoice_id"]:
            update(ctx, "reasoning_contracts", p55["id"], supplier_paid_minute=ctx.minute)
        return result

    wrap("a_pay_business_invoice", invoice_payment)

    def replace(ctx, original, args):
        result = original(ctx, **args)
        p = profile(ctx, -29)
        if p and args["delivery_id"] == p["delivery_id"]:
            offer = row(ctx, "replacement_offers", args["offer_id"])
            due = ctx.minute + offer.get("delivery_minutes", 0)
            update(
                ctx,
                "replacement_orders",
                result["id"],
                physically_ready=due == ctx.minute,
                physical_ready_minute=due,
                calibrated=offer.get("calibrated", False),
            )
            if due > ctx.minute:
                ctx.schedule("a_reasoning_replacement_arrival", due, result["id"])
        return result

    wrap("a_order_replacement_units", replace)

    def accept(ctx, original, args):
        p = profile(ctx, -29)
        if p and args["delivery_id"] == p["delivery_id"]:
            selected = [row(ctx, "replacement_orders", i) for i in args["replacement_order_ids"]]
            ctx.require(
                all((o.get("physically_ready") and o.get("calibrated") for o in selected)),
                "REPLACEMENT_NOT_COMMISSIONED",
            )
        result = original(ctx, **args)
        if p and args["delivery_id"] == p["delivery_id"]:
            update(
                ctx,
                "reasoning_contracts",
                p["id"],
                usable_minute=ctx.minute,
                accepted_replacement_ids=args["replacement_order_ids"],
            )
        return result

    wrap("a_accept_supplier_delivery", accept)

    def pay_market(ctx, original, args):
        result = original(ctx, **args)
        p = profile(ctx, -32)
        if p and args["order_id"] == p["new_order_id"]:
            update(ctx, "reasoning_contracts", p["id"], new_order_paid_minute=ctx.minute)
        return result

    wrap("a_pay_marketplace_order", pay_market)

    def event(ctx, e):
        kind = e["kind"]
        if kind == "a_reasoning_credit_value_date":
            b = row(ctx, "credit_value_batches", e["subject_id"])
            p = need(ctx, b["contract_id"], -23)
            release_value(ctx, p, b)
            return True
        if kind == "a_reasoning_merchant_invoice_applied":
            update(
                ctx,
                "reasoning_contracts",
                e["subject_id"],
                commercially_applied=True,
                commercial_application_minute=ctx.minute,
            )
            return True
        if kind == "a_reasoning_replacement_arrival":
            update(ctx, "replacement_orders", e["subject_id"], physically_ready=True)
            return True
        if kind == "a_reasoning_equipment_commissioned":
            b = row(ctx, "equipment_test_bookings", e["subject_id"])
            update(
                ctx,
                "equipment_test_bookings",
                b["id"],
                status="COMPLETED",
                completed_minute=ctx.minute,
            )
            for oid in b["replacement_order_ids"]:
                update(
                    ctx,
                    "replacement_orders",
                    oid,
                    calibrated=True,
                    commissioning_booking_id=b["id"],
                )
            return True
        if kind == "a_reasoning_market_claim_notice":
            p = need(ctx, e["subject_id"], -32)
            update(
                ctx,
                "reasoning_contracts",
                p["id"],
                claim_confirmed=True,
                allowance_minor=p["_allowance_minor"],
                cash_services=deepcopy(p["_cash_services"]),
            )
            return True
        if kind == "a_reasoning_market_claim_cash":
            p = need(ctx, e["subject_id"], -32)
            ledger(ctx, p, p["allowance_minor"], "LEGACY_CLAIM_CASH", p["id"])
            update(
                ctx,
                "reasoning_contracts",
                p["id"],
                realized_minor=p["allowance_minor"],
                realized_minute=ctx.minute,
            )
            return True
        result = old_event(ctx, e)
        if kind == "a_bank_intent_confirmation":
            p = profile(ctx, -23)
            if p:
                for b in rows(ctx, "credit_value_batches"):
                    if b["contract_id"] == p["id"]:
                        update(
                            ctx,
                            "credit_value_batches",
                            b["id"],
                            value_date_confirmed=True,
                            value_minute=p["_value_dates"][b["transaction_id"]],
                        )
        if kind == "a_alias_verification":
            p = profile(ctx, -27)
            if p:
                for sid, fields in p["_collection_response"].items():
                    update(ctx, "invoice_collection_services", sid, **fields)
                update(ctx, "reasoning_contracts", p["id"], collection_terms_final=True)
        if kind == "a_supplier_dispatch_result":
            p = profile(ctx, -29)
            if p and p["delivery_id"] == e["subject_id"]:
                for offer in rows(ctx, "replacement_offers"):
                    if offer["delivery_id"] == p["delivery_id"] and offer.get(
                        "certificate_pending"
                    ):
                        update(
                            ctx,
                            "replacement_offers",
                            offer["id"],
                            calibrated=p["_certificate_acceptance"],
                            certificate_pending=False,
                        )
        return result

    api.event_handler = event
