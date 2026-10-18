"""Scoped beneficiary application windows and file-line bank availability."""

from copy import deepcopy
from payment.runtime.products.psp.receivables.dispatch import (
    rows,
    row,
    update,
    profile,
    need,
    ledger,
)
from payment.runtime.products.psp.receivables.custody_and_credit import op, OPS

LOCAL = {}


def registration(name, description, **fields):

    def dec(fn):
        props = {k: dict(type=t, description=d) for (k, (t, d)) in fields.items()}
        LOCAL[name] = dict(
            description=description,
            parameters=dict(
                type="object", properties=props, required=list(props), additionalProperties=False
            ),
            read_only=False,
            function=fn,
        )
        return fn

    return dec


S = "string"


@registration(
    "a_reserve_receivable_application_window",
    "Reserve capacity in a published beneficiary application service for one original receivable. The booking fee is nonrefundable. Unexecuted reservations may be replaced; the consumed capacity is returned but its fee is not.",
    contract_id=(S, "Commercial application window contract."),
    receivable_id=(S, "Original receivable ID."),
    service_id=(S, "Bank processing service ID."),
)
def reserve_application(ctx, contract_id, receivable_id, service_id):
    return reserve(ctx, contract_id, receivable_id, service_id, 52)


@registration(
    "a_reserve_payment_line_service",
    "Reserve processing capacity for one original file obligation. The booking fee is paid from the same operating account as sender and rejection fees. A rejected beneficiary can be repaired using this same booking.",
    contract_id=(S, "Original payment file processing contract."),
    invoice_id=(S, "Original invoice or payroll obligation ID."),
    service_id=(S, "Bank processing service ID."),
)
def reserve_file(ctx, contract_id, invoice_id, service_id):
    return reserve(ctx, contract_id, invoice_id, service_id, 58)


def reserve(ctx, contract_id, subject_id, service_id, n):
    p = need(ctx, contract_id, n)
    s = row(ctx, "processing_services", service_id)
    ctx.require(subject_id in p["subject_ids"] and s["contract_id"] == p["id"], "PROCESSING_SCOPE")
    prior = next(
        (
            b
            for b in rows(ctx, "processing_bookings")
            if b["subject_id"] == subject_id
            and b["contract_id"] == p["id"]
            and (b["status"] == "RESERVED")
        ),
        None,
    )
    if prior:
        old = row(ctx, "processing_services", prior["service_id"])
        update(ctx, "processing_services", old["id"], remaining_slots=old["remaining_slots"] + 1)
        update(ctx, "processing_bookings", prior["id"], status="REPLACED")
    ctx.require(s["remaining_slots"] > 0 and ctx.minute <= s["book_until"], "PROCESSING_CAPACITY")
    ledger(
        ctx,
        p,
        -s["fee_minor"],
        "APPLICATION_WINDOW_FEE" if n == -22 else "FILE_PROCESSING_FEE",
        s["id"],
    )
    update(ctx, "processing_services", s["id"], remaining_slots=s["remaining_slots"] - 1)
    return ctx.create(
        "xp_a_processing_bookings",
        customer_id=p["customer_id"],
        contract_id=p["id"],
        subject_id=subject_id,
        service_id=service_id,
        status="RESERVED",
        booked_minute=ctx.minute,
    )


@registration(
    "a_request_file_processing_terms",
    "Buy the current bank processing-window review for the original file. The fixed review charge is nonrefundable; the bank returns actual availability delays for the listed services.",
    contract_id=(S, "Original file processing contract."),
)
def file_review(ctx, contract_id):
    p = need(ctx, contract_id, -26)
    ctx.require(not p.get("review_requested"), "FILE_REVIEW_EXISTS")
    ledger(ctx, p, -p["review_fee_minor"], "FILE_PROCESSING_REVIEW", p["id"])
    update(ctx, "reasoning_contracts", p["id"], review_requested=True)
    ctx.schedule("a_reasoning_processing_terms", ctx.minute + 2, p["id"])
    return {"status": "PENDING"}


def register(api):
    api.operations.update(LOCAL)
    old_apply = api.operations["a_apply_beneficiary_invoice"]["function"]
    old_file = api.operations["a_execute_payment_file_line"]["function"]
    old_event = api.event_handler

    def app(ctx, **args):
        p = profile(ctx, -22, args["receivable_id"])
        if not p:
            return old_apply(ctx, **args)
        b = next(
            (
                b
                for b in rows(ctx, "processing_bookings")
                if b["contract_id"] == p["id"]
                and b["subject_id"] == args["receivable_id"]
                and (b["status"] == "RESERVED")
            ),
            None,
        )
        ctx.require(b is not None, "APPLICATION_WINDOW_REQUIRED")
        case = row(ctx, "settlement_cases", args["settlement_id"])
        alloc = deepcopy(case.get("allocations", {}))
        ctx.require(
            alloc.get(args["commercial_account"], 0) >= args["amount_minor"],
            "BENEFICIARY_ALLOCATED_VALUE_INSUFFICIENT",
        )
        alloc[args["commercial_account"]] -= args["amount_minor"]
        update(ctx, "settlement_cases", case["id"], allocations=alloc)
        service = row(ctx, "processing_services", b["service_id"])
        due = ctx.minute + service["delay_minutes"]
        update(
            ctx,
            "processing_bookings",
            b["id"],
            status="PROCESSING",
            submitted_minute=ctx.minute,
            due_minute=due,
            amount_minor=args["amount_minor"],
        )
        ctx.schedule("a_reasoning_receivable_application", due, b["id"], arguments=deepcopy(args))
        return {"status": "PROCESSING", "due_minute": due}

    def file(ctx, **args):
        export = row(ctx, "exports", args["file_record_id"])
        line = next((x for x in export["lines"] if x["id"] == args["line_id"]))
        p = profile(ctx, -26, line["invoice_id"])
        if not p:
            return old_file(ctx, **args)
        ctx.require(p.get("review_complete"), "PROCESSING_TERMS_PENDING")
        previous = next(
            (x for x in rows(ctx, "payments") if x["payment_key"] == args["payment_key"]), None
        )
        b = next(
            (
                b
                for b in rows(ctx, "processing_bookings")
                if b["contract_id"] == p["id"]
                and b["subject_id"] == line["invoice_id"]
                and (b["status"] == "RESERVED")
            ),
            None,
        )
        ctx.require(b is not None or previous, "FILE_PROCESSING_BOOKING_REQUIRED")
        result = old_file(ctx, **args)
        if result.get("status") == "SETTLED" and (not previous):
            s = row(ctx, "processing_services", b["service_id"])
            due = ctx.minute + s["delay_minutes"]
            update(
                ctx,
                "processing_bookings",
                b["id"],
                status="PROCESSING",
                submitted_minute=ctx.minute,
                due_minute=due,
                payment_id=result["payment_id"],
            )
            update(
                ctx,
                "payments",
                result["payment_id"],
                beneficiary_available=False,
                availability_due_minute=due,
            )
            ctx.schedule("a_reasoning_file_available", due, b["id"])
        return result

    def event(ctx, e):
        if e["kind"] == "a_reasoning_processing_terms":
            p = need(ctx, e["subject_id"], -26)
            for sid, changes in p["_processing_response"].items():
                update(ctx, "processing_services", sid, **changes)
            update(ctx, "reasoning_contracts", p["id"], review_complete=True)
            return True
        if e["kind"] == "a_reasoning_receivable_application":
            b = row(ctx, "processing_bookings", e["subject_id"])
            args = e["_payload"]["arguments"]
            case = row(ctx, "settlement_cases", args["settlement_id"])
            alloc = deepcopy(case["allocations"])
            alloc[args["commercial_account"]] = (
                alloc.get(args["commercial_account"], 0) + args["amount_minor"]
            )
            update(ctx, "settlement_cases", case["id"], allocations=alloc)
            result = old_apply(ctx, **args)
            update(
                ctx,
                "processing_bookings",
                b["id"],
                status="APPLIED",
                completed_minute=ctx.minute,
                application_id=result["id"],
            )
            return True
        if e["kind"] == "a_reasoning_file_available":
            b = row(ctx, "processing_bookings", e["subject_id"])
            update(
                ctx,
                "payments",
                b["payment_id"],
                beneficiary_available=True,
                beneficiary_available_minute=ctx.minute,
            )
            update(
                ctx, "processing_bookings", b["id"], status="APPLIED", completed_minute=ctx.minute
            )
            return True
        result = old_event(ctx, e)
        if e["kind"] == "a_beneficiary_allocation_result":
            p = profile(ctx, -22)
            if p and row(ctx, "settlement_cases", e["subject_id"])["source_id"] in [
                x["source_id"] for x in rows(ctx, "settlement_cases") if x["id"] == e["subject_id"]
            ]:
                for sid, changes in p["_processing_response"].items():
                    update(ctx, "processing_services", sid, **changes)
        return result

    api.operations["a_apply_beneficiary_invoice"]["function"] = app
    api.operations["a_execute_payment_file_line"]["function"] = file
    api.event_handler = event
