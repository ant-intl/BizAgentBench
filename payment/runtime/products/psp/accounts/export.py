"""Shipment-specific reporting certificates competing with one shared export quota."""

from copy import deepcopy

TABLE = "xp_b_reasoning_export_capacity"
BOOK = "xp_b_reasoning_export_certificates"
OPS = {}


def context(ctx, lot=None):
    if not any((x.get("reasoning_version") == "b-reasoning-1" for x in ctx.records("xp_cases"))):
        return None
    return next(
        (
            x
            for x in ctx.records(TABLE)
            if x["customer_id"] == ctx.customer_id and (lot is None or lot in x["lots"])
        ),
        None,
    )


def reserve(ctx, schedule_id, lot_id, certificate_id, amount_minor):
    r = ctx.record(TABLE, schedule_id)
    ctx.require(context(ctx, lot_id) is not None, "EXPORT_CAPACITY_SCOPE")
    cert = r["certificates"][certificate_id]
    ctx.require(lot_id in cert["lot_ids"] and amount_minor > 0, "CERTIFICATE_LOT")
    used = sum(
        (
            x["amount_minor"]
            for x in ctx.records(BOOK, schedule_id=schedule_id)
            if x["certificate_id"] == certificate_id
        )
    )
    ctx.require(used + amount_minor <= cert["capacity_minor"], "CERTIFICATE_CAPACITY")
    ctx.ledger(r["account_id"], -cert["fee_minor"], "USD", "export_certificate_fee", lot_id)
    record = ctx.create(
        BOOK,
        customer_id=r["customer_id"],
        schedule_id=schedule_id,
        lot_id=lot_id,
        certificate_id=certificate_id,
        amount_minor=amount_minor,
        ready_minute=ctx.minute + cert["delay_minutes"],
        booked_minute=ctx.minute,
        fee_minor=cert["fee_minor"],
        ready=cert["delay_minutes"] == 0,
    )
    if cert["delay_minutes"]:
        ctx.schedule(
            "b_reasoning_export_certificate", ctx.minute + cert["delay_minutes"], record["id"]
        )
    return record


OPS["reserve_export_reporting_capacity"] = dict(
    description="Reserve amount_minor of one published certificate for a named export lot. Reservations are irrevocable, consume the certificate’s shared capacity across all its lots and charge its fee. The lot may use only its own matured reserved capacity. A reservation is reporting eligibility, not settlement cash or an issued declaration.",
    parameters=dict(
        type="object",
        properties={
            k: {"type": "integer", "minimum": 1} if k == "amount_minor" else {"type": "string"}
            for k in ["schedule_id", "lot_id", "certificate_id", "amount_minor"]
        },
        required=["schedule_id", "lot_id", "certificate_id", "amount_minor"],
        additionalProperties=False,
    ),
    read_only=False,
    function=reserve,
)


def register(operations, old_event):
    operations.update(OPS)
    old = operations["settle_export_proceeds"]["function"]
    deliver = operations["deliver_declaration_record"]["function"]

    def settle(ctx, lot_id, destination_id, product):
        r = context(ctx, lot_id)
        if not r:
            return old(ctx, lot_id=lot_id, destination_id=destination_id, product=product)
        lot = ctx.record("xp_b_export_lots", lot_id)
        terms = r["lots"][lot_id]
        basis = lot.get("reporting_basis_minor")
        if product == "declaration":
            ctx.require(basis is not None, "CUSTOMS_BASIS_PENDING")
            capacity = sum(
                (
                    x["amount_minor"]
                    for x in ctx.records(BOOK, schedule_id=r["id"])
                    if x["lot_id"] == lot_id and x["ready_minute"] <= ctx.minute
                )
            )
            ctx.require(capacity >= basis, "REPORTING_CERTIFICATE_SHORTFALL")
        out = old(ctx, lot_id=lot_id, destination_id=destination_id, product=product)
        ctx.update(
            "xp_b_export_lots", lot_id, settled_minute=ctx.minute, certificate_basis_minor=basis
        )
        if lot_id == r["first_lot_id"] and (not r.get("allocation_scheduled")):
            ctx.update(TABLE, r["id"], allocation_scheduled=True)
            ctx.schedule(
                "b_reasoning_export_allocation", ctx.minute + r["allocation_delay"], r["id"]
            )
        return out

    def delivered(ctx, lot_id):
        result = deliver(ctx, lot_id=lot_id)
        if context(ctx, lot_id):
            ctx.update("xp_b_export_lots", lot_id, declaration_delivered_minute=ctx.minute)
        return result

    operations["settle_export_proceeds"]["function"] = settle
    operations["deliver_declaration_record"]["function"] = delivered

    def event(ctx, e):
        if e["kind"] == "b_reasoning_export_certificate":
            ctx.update(BOOK, e["subject_id"], ready=True)
            return
        if e["kind"] == "b_reasoning_export_allocation":
            r = ctx.record(TABLE, e["subject_id"])
            ctx.update(
                "xp_b_export_lots",
                r["second_lot_id"],
                reporting_basis_minor=r["_second_basis_minor"],
                allocation_status="final",
            )
            ctx.update(
                TABLE,
                r["id"],
                allocation_received=True,
                second_basis_minor=r["_second_basis_minor"],
            )
            return
        return old_event(ctx, e)

    return event
