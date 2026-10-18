"""External cash vouchers and serial-rights operations."""

OPERATIONS = {}
S = {"type": "string"}
I = {"type": "integer", "minimum": 0}


def op(name, description, parameters):

    def wrap(fn):
        OPERATIONS[name] = dict(
            description=description,
            parameters=dict(
                type="object",
                properties=parameters,
                required=list(parameters),
                additionalProperties=False,
            ),
            read_only=False,
            function=fn,
        )
        return fn

    return wrap


def charge(ctx, r, amount, kind):
    if amount:
        ctx.ledger(r["account_id"], -amount, r["currency"], kind, r["id"])


@op(
    "request_cash_store_attestation",
    "Ask the external cash-store network for the actual acceptance of an existing barcode. The attestation fee is charged once and a delayed signed result names cash amount, time and code. Revocation or expiry never destroys cash already accepted while valid.",
    {"barcode_id": S},
)
def attest(ctx, barcode_id):
    r = ctx.record("xp_d_cash_barcodes", barcode_id)
    ctx.require(not r.get("attestation_requested"), "ATTESTATION_ALREADY_REQUESTED")
    charge(ctx, r, r["attestation_fee_minor"], "cash_store_attestation")
    ctx.update("xp_d_cash_barcodes", r["id"], attestation_requested=True)
    ctx.schedule("d_cash_attestation", ctx.minute + r["attestation_delay"], r["id"])
    return {"barcode_id": r["id"], "status": "store_response_pending"}


@op(
    "revoke_external_cash_barcode",
    "Request external-network revocation of a code. The old code remains valid until the revocation acknowledgement or its expiry. An offline cash acceptance before either boundary remains payable even after revocation.",
    {"barcode_id": S},
)
def revoke(ctx, barcode_id):
    r = ctx.record("xp_d_cash_barcodes", barcode_id)
    ctx.require(r["status"] in ("active", "expired"), "BARCODE_REVOCATION_STATE")
    ctx.update("xp_d_cash_barcodes", r["id"], status="revocation_pending")
    ctx.schedule("d_cash_revoke", ctx.minute + r["revocation_delay"], r["id"])
    return {"barcode_id": r["id"], "status": "revocation_pending"}


@op(
    "issue_external_cash_barcode",
    "Issue a new fixed-value external-store barcode for an order, network and expiry minute. face_minor is the cash buyer will tender, not PSP net proceeds. Issuing another code does not invalidate earlier codes or prove no earlier cash was collected. The network fee is debited immediately.",
    {"order_id": S, "network_id": S, "face_minor": I, "expiry_minute": I},
)
def issue_code(ctx, order_id, network_id, face_minor, expiry_minute):
    o = ctx.record("xp_d_cash_orders", order_id)
    p = ctx.record("xp_d_cash_networks", network_id)
    ctx.require(
        face_minor > 0
        and expiry_minute > ctx.minute
        and (expiry_minute <= ctx.minute + p["max_validity"]),
        "BARCODE_VALUE_OR_EXPIRY",
    )
    charge(ctx, o, p["issue_fee_minor"], "cash_barcode_issue")
    return ctx.create(
        "xp_d_cash_barcodes",
        customer_id=o["customer_id"],
        account_id=o["account_id"],
        currency=o["currency"],
        order_id=o["id"],
        payee=o["merchant"],
        face_minor=face_minor,
        network_id=p["id"],
        issued_minute=ctx.minute,
        expiry_minute=expiry_minute,
        status="active",
        attestation_fee_minor=0,
        attestation_delay=p["acceptance_delay"],
        revocation_delay=p["revocation_delay"],
        settlement_delay=p["settlement_delay"],
        settlement_fee_minor=p["settlement_fee_minor"],
        _store_accepted=False,
        _accepted_minute=None,
        presented=False,
    )


@op(
    "present_external_cash_barcode",
    "Send an issued code to the genuine buyer for cash payment at the stated network. This schedules an actual cash tender; a second presented code can collect the same commercial order twice. The store tests the code at its actual tender time.",
    {"barcode_id": S},
)
def present(ctx, barcode_id):
    r = ctx.record("xp_d_cash_barcodes", barcode_id)
    ctx.require(not r.get("presented") and r["status"] == "active", "BARCODE_PRESENTATION_STATE")
    ctx.require(
        not any((t["barcode_id"] == r["id"] for t in ctx.records("xp_d_cash_tickets"))),
        "BARCODE_ALREADY_COLLECTED",
    )
    p = ctx.record("xp_d_cash_networks", r["network_id"])
    ctx.update("xp_d_cash_barcodes", r["id"], presented=True)
    ctx.schedule("d_cash_tender", ctx.minute + p["acceptance_delay"], r["id"])
    return {"status": "buyer_cash_tender_pending"}


@op(
    "redeem_accepted_cash_ticket",
    "Submit a signed store ticket for final PSP settlement to its bound merchant. A ticket can be redeemed only once, including one from a revoked old barcode. Store cash accepted and bank proceeds are separate states.",
    {"ticket_id": S},
)
def redeem(ctx, ticket_id):
    t = ctx.record("xp_d_cash_tickets", ticket_id)
    ctx.require(t["status"] == "accepted", "TICKET_ALREADY_REDEEMED")
    b = ctx.record("xp_d_cash_barcodes", t["barcode_id"])
    ctx.update("xp_d_cash_tickets", t["id"], status="settlement_pending")
    ctx.schedule("d_cash_psp_settle", ctx.minute + b["settlement_delay"], t["id"])
    return {"ticket_id": t["id"], "status": "settlement_pending"}


@op(
    "record_cash_order_clearance",
    "Publish one commercial order’s actual store cash and PSP settlement receipts. Positive store acceptance is not final bank settlement. Publication preserves the list of all genuine tickets, including duplicates.",
    {"order_id": S},
)
def close_cash(ctx, order_id):
    o = ctx.record("xp_d_cash_orders", order_id)
    ts = [x for x in ctx.records("xp_d_cash_tickets") if x["order_id"] == order_id]
    return ctx.update(
        "xp_d_cash_orders",
        o["id"],
        clearance_published=True,
        clearance_minute=ctx.minute,
        cash_accepted_minor=sum((x["cash_minor"] for x in ts)),
        psp_net_minor=sum((x.get("net_minor", 0) for x in ts if x["status"] == "settled")),
        ticket_ids=[x["id"] for x in ts],
    )


@op(
    "pay_serial_code_package",
    "Pay the catalog package principal to the listed serialized-code provider. The actual settled package determines quantity, product-family restrictions, serial identity and refund terms. A proposed payment creates no code rights.",
    {"package_id": S},
)
def pay_package(ctx, package_id):
    p = ctx.record("xp_d_serial_packages", package_id)
    ctx.require(p["status"] == "available", "PACKAGE_ALREADY_PAID")
    charge(ctx, p, p["price_minor"], "serial_package_payment")
    ctx.update("xp_d_serial_packages", p["id"], status="settlement_pending")
    ctx.schedule("d_serial_issue", ctx.minute + p["settlement_delay"], p["id"])
    return {"package_id": p["id"], "status": "settlement_pending"}


@op(
    "bind_serial_code_range",
    "Bind units from a settled serial lot to a listed product. The lot’s serial interval is divided without copying identifiers. Family compatibility is a commercial obligation, while the bank allocation interface accepts listed products. A binding can be released only before export.",
    {"lot_id": S, "product_id": S, "units": I},
)
def bind_codes(ctx, lot_id, product_id, units):
    r = ctx.record("xp_d_serial_lots", lot_id)
    p = ctx.record("xp_d_serial_products", product_id)
    ctx.require(units > 0 and units <= r["available_units"], "SERIAL_UNITS_UNAVAILABLE")
    left = units
    used = []
    remaining = []
    for start, end in r["available_ranges"]:
        take = min(left, end - start + 1)
        if take:
            used.append([start, start + take - 1])
            left -= take
        if start + take <= end:
            remaining.append([start + take, end])
    ctx.update(
        "xp_d_serial_lots",
        r["id"],
        available_units=r["available_units"] - units,
        available_ranges=remaining,
    )
    return ctx.create(
        "xp_d_serial_bindings",
        customer_id=r["customer_id"],
        lot_id=r["id"],
        product_id=p["id"],
        product_family=p["family"],
        serial_ranges=used,
        units=units,
        status="bound",
        bound_minute=ctx.minute,
    )


@op(
    "release_unexported_serial_binding",
    "Release an unexported binding and restore those exact serial intervals to the available lot. This never restores exported or activated rights.",
    {"binding_id": S},
)
def release(ctx, binding_id):
    b = ctx.record("xp_d_serial_bindings", binding_id)
    r = ctx.record("xp_d_serial_lots", b["lot_id"])
    ctx.require(b["status"] == "bound", "EXPORTED_RIGHTS_IRREVERSIBLE")
    ctx.update("xp_d_serial_bindings", b["id"], status="released")
    return ctx.update(
        "xp_d_serial_lots",
        r["id"],
        available_units=r["available_units"] + b["units"],
        available_ranges=sorted(r["available_ranges"] + b["serial_ranges"]),
    )


@op(
    "export_serial_code_binding",
    "Export the allocated serial binding to a production batch. This permanently binds product genealogy and batch, removes refund eligibility and cannot be undone by later master-data edits. The supplied batch must exist but the commercial product/batch compatibility remains the caller’s responsibility.",
    {"binding_id": S, "batch_id": S},
)
def export(ctx, binding_id, batch_id):
    b = ctx.record("xp_d_serial_bindings", binding_id)
    batch = ctx.record("xp_d_serial_batches", batch_id)
    ctx.require(b["status"] == "bound", "SERIAL_EXPORT_STATE")
    return ctx.update(
        "xp_d_serial_bindings",
        b["id"],
        status="exported",
        batch_id=batch["id"],
        exported_minute=ctx.minute,
    )


@op(
    "activate_serial_code_binding",
    "Activate all codes in one exported range. Activation is irreversible and does not silently change a product revision, batch or quantity. Only actually activated serials protect the specified production batch.",
    {"binding_id": S},
)
def activate(ctx, binding_id):
    r = ctx.record("xp_d_serial_bindings", binding_id)
    ctx.require(r["status"] == "exported", "SERIAL_ACTIVATION_STATE")
    return ctx.update("xp_d_serial_bindings", r["id"], status="active", activated_minute=ctx.minute)


@op(
    "refund_unexported_serial_units",
    "Surrender unbound available units for the lot’s published per-unit refund. Surrender consumes the serial rights permanently; zero-rate packages return no cash. Bound units must first be released, and exported units cannot be refunded.",
    {"lot_id": S, "units": I},
)
def refund(ctx, lot_id, units):
    r = ctx.record("xp_d_serial_lots", lot_id)
    ctx.require(units > 0 and units <= r["available_units"], "REFUNDABLE_SERIAL_UNITS")
    amount = units * r["refund_per_unit_minor"]
    if amount:
        ctx.ledger(r["account_id"], amount, r["currency"], "serial_rights_refund", r["id"])
    left = units
    used = []
    remaining = []
    for start, end in r["available_ranges"]:
        take = min(left, end - start + 1)
        if take:
            used.append([start, start + take - 1])
            left -= take
        if start + take <= end:
            remaining.append([start + take, end])
    return ctx.update(
        "xp_d_serial_lots",
        r["id"],
        available_units=r["available_units"] - units,
        refunded_units=r["refunded_units"] + units,
        refund_minor=r["refund_minor"] + amount,
        available_ranges=remaining,
        refunded_ranges=r.get("refunded_ranges", []) + used,
    )


@op(
    "publish_serial_rights_statement",
    "Publish paid packages, actual active product/batch rights and surrendered units. It does not alter product lineage or convert an exported code into a refundable one.",
    {"contract_id": S},
)
def serial_statement(ctx, contract_id):
    r = ctx.record("xp_d_serial_contracts", contract_id)
    return ctx.update(
        "xp_d_serial_contracts", r["id"], statement_published=True, statement_minute=ctx.minute
    )


def ticket(ctx, b, minute):
    return ctx.create(
        "xp_d_cash_tickets",
        customer_id=b["customer_id"],
        account_id=b["account_id"],
        currency=b["currency"],
        barcode_id=b["id"],
        order_id=b["order_id"],
        merchant=b["payee"],
        cash_minor=b["face_minor"],
        accepted_minute=minute,
        status="accepted",
    )


def apply_event(ctx, e):
    k = e["kind"]
    sid = e["subject_id"]
    if k == "d_cash_attestation":
        b = ctx.record("xp_d_cash_barcodes", sid)
        accepted = b["_store_accepted"]
        ctx.update(
            "xp_d_cash_barcodes",
            sid,
            attestation_received=True,
            store_cash_accepted=accepted,
            store_accepted_minute=b["_accepted_minute"] if accepted else None,
        )
        if accepted and (
            not any((t["barcode_id"] == sid for t in ctx.records("xp_d_cash_tickets")))
        ):
            ticket(ctx, b, b["_accepted_minute"])
        if not accepted:
            ctx.update("xp_d_cash_barcodes", sid, presented=False)
    elif k == "d_cash_revoke":
        ctx.update("xp_d_cash_barcodes", sid, status="revoked", revoked_minute=ctx.minute)
    elif k == "d_cash_tender":
        b = ctx.record("xp_d_cash_barcodes", sid)
        valid = ctx.minute <= b["expiry_minute"] and b["status"] in ("active", "revocation_pending")
        ctx.update(
            "xp_d_cash_barcodes",
            sid,
            tender_result="accepted" if valid else "rejected_expired_or_revoked",
        )
        if valid:
            ticket(ctx, b, ctx.minute)
    elif k == "d_cash_psp_settle":
        t = ctx.record("xp_d_cash_tickets", sid)
        b = ctx.record("xp_d_cash_barcodes", t["barcode_id"])
        net = t["cash_minor"] - b["settlement_fee_minor"]
        ctx.ledger(t["account_id"], net, t["currency"], "cash_psp_settlement", t["id"])
        ctx.update(
            "xp_d_cash_tickets",
            sid,
            status="settled",
            net_minor=net,
            fee_minor=b["settlement_fee_minor"],
            settled_minute=ctx.minute,
        )
    elif k == "d_serial_issue":
        p = ctx.record("xp_d_serial_packages", sid)
        ctx.update("xp_d_serial_packages", sid, status="settled", settled_minute=ctx.minute)
        for index, (family, units) in enumerate(p["units_by_family"].items()):
            start = p["serial_base"] + index * 10000
            ctx.create(
                "xp_d_serial_lots",
                customer_id=p["customer_id"],
                account_id=p["account_id"],
                currency=p["currency"],
                package_id=p["id"],
                family=family,
                issued_units=units,
                available_units=units,
                refunded_units=0,
                refund_minor=0,
                refund_per_unit_minor=p["refund_per_unit_minor"],
                serial_start=start,
                serial_end=start + units - 1,
                available_ranges=[[start, start + units - 1]],
                refunded_ranges=[],
            )
    elif k == "d_serial_revision":
        r = ctx.record("xp_d_serial_contracts", sid)
        requirements = e["_payload"]["requirements"]
        ctx.update(
            "xp_d_serial_contracts",
            sid,
            requirements=requirements,
            revision_final=True,
            revision_minute=ctx.minute,
        )
    else:
        return False
    return True
