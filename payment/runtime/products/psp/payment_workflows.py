"""Shared payment allocation, review, submission, recall and reconciliation operations."""

from __future__ import annotations

OPERATIONS = {}
EVENT_PREFIX = "v9"
S = {"type": "string"}
I = {"type": "integer", "minimum": 1}


def op(name, description, parameters, read_only=False):

    def wrap(fn):
        OPERATIONS[name] = dict(
            description=description,
            parameters=dict(
                type="object",
                properties=parameters,
                required=list(parameters),
                additionalProperties=False,
            ),
            read_only=read_only,
            function=fn,
        )
        return fn

    return wrap


def _rows(ctx, resource, **filters):
    return [
        row
        for row in ctx.records(resource)
        if all((row.get(key) == value for (key, value) in filters.items()))
    ]


@op(
    "request_payment_prerequisite_review",
    "Request verification of one current payment prerequisite, such as beneficiary instructions, screening, a government reference, a fee election or an authorization version. The request schedules the recorded review result; requesting it is not approval.",
    {"control_id": S},
)
def request_review(ctx, control_id):
    row = ctx.record("xp_v9_controls", control_id)
    ctx.require(row["status"] in ("UNVERIFIED", "STALE"), "CONTROL_REVIEW_STATE")
    ctx.update(
        "xp_v9_controls", row["id"], status="REVIEW_PENDING", review_requested_minute=ctx.minute
    )
    ctx.schedule("v9_control_complete", ctx.minute + row["review_delay"], row["id"])
    return {"control_id": row["id"], "status": "REVIEW_PENDING"}


@op(
    "allocate_payment_funds",
    "Reserve an amount from one recorded funding source for one payment obligation. Allocation does not move money and does not waive source-purpose, value-date, reserve or safeguarding restrictions.",
    {"obligation_id": S, "source_id": S, "amount_minor": I},
)
def allocate(ctx, obligation_id, source_id, amount_minor):
    obligation = ctx.record("xp_v9_obligations", obligation_id)
    source = ctx.record("xp_v9_sources", source_id)
    ctx.require(obligation["case_id"] == source["case_id"], "SOURCE_CASE_MISMATCH")
    active = sum(
        (
            row["amount_minor"]
            for row in _rows(ctx, "xp_v9_allocations", source_id=source_id)
            if row["status"] == "RESERVED"
        )
    )
    account = ctx.record("xp_accounts", source["account_id"])
    ctx.require(active + amount_minor <= account["balance_minor"], "INSUFFICIENT_AVAILABLE_CASH")
    return ctx.create(
        "xp_v9_allocations",
        case_id=obligation["case_id"],
        obligation_id=obligation_id,
        source_id=source_id,
        amount_minor=amount_minor,
        status="RESERVED",
        allocated_minute=ctx.minute,
    )


@op(
    "cancel_payment_allocation",
    "Release one unsubmitted payment allocation. Submitted or settled payment exposure must be recalled instead and any external fee remains real.",
    {"allocation_id": S},
)
def cancel_allocation(ctx, allocation_id):
    row = ctx.record("xp_v9_allocations", allocation_id)
    ctx.require(row["status"] == "RESERVED", "ALLOCATION_NOT_REVERSIBLE")
    return ctx.update(
        "xp_v9_allocations", row["id"], status="CANCELLED", cancelled_minute=ctx.minute
    )


@op(
    "submit_external_payment",
    "Submit an allocated payment using an explicit beneficiary-instruction version, fee bearer and reference version. The interface accepts recorded versions and sources even when they are commercially stale or restricted; the caller must apply the published payment policy. Submission debits cash and creates external exposure immediately.",
    {"allocation_id": S, "beneficiary_version": S, "charge_bearer": S, "reference_version": S},
)
def submit_payment(ctx, allocation_id, beneficiary_version, charge_bearer, reference_version):
    allocation = ctx.record("xp_v9_allocations", allocation_id)
    ctx.require(allocation["status"] == "RESERVED", "ALLOCATION_ALREADY_USED")
    obligation = ctx.record("xp_v9_obligations", allocation["obligation_id"])
    source = ctx.record("xp_v9_sources", allocation["source_id"])
    ctx.require(charge_bearer in ("OUR", "SHA", "BEN"), "CHARGE_BEARER_UNSUPPORTED")
    fee = obligation["fee_minor"]
    sender_fee = fee if charge_bearer == "OUR" else 0
    receiver_fee = 0 if charge_bearer == "OUR" else fee
    ctx.ledger(
        source["account_id"],
        -(allocation["amount_minor"] + sender_fee),
        source["currency"],
        "v9_payment_submission",
        obligation["id"],
    )
    payment = ctx.create(
        "xp_v9_payments",
        case_id=obligation["case_id"],
        obligation_id=obligation["id"],
        allocation_id=allocation["id"],
        source_id=source["id"],
        account_id=source["account_id"],
        currency=source["currency"],
        amount_minor=allocation["amount_minor"],
        sender_fee_minor=sender_fee,
        receiver_fee_minor=receiver_fee,
        beneficiary_version=beneficiary_version,
        charge_bearer=charge_bearer,
        reference_version=reference_version,
        status="SUBMITTED",
        submitted_minute=ctx.minute,
        recall_fee_minor=obligation.get("recall_fee_minor", 0),
        source_status_at_submission=source.get("status"),
        source_available_minute_at_submission=source.get("available_minute"),
        control_statuses_at_submission={
            control_id: ctx.record("xp_v9_controls", control_id)["status"]
            for control_id in obligation.get("required_control_ids", [])
        },
    )
    ctx.update("xp_v9_allocations", allocation["id"], status="SUBMITTED", payment_id=payment["id"])
    ctx.schedule(
        "v9_payment_outcome",
        ctx.minute + obligation.get("settlement_delay", 1),
        payment["id"],
        result=obligation.get("next_result", "SETTLED"),
        returned_fee_minor=obligation.get("returned_fee_minor", 0),
    )
    return {
        "payment_id": payment["id"],
        "status": "SUBMITTED",
        "cash_debited_minor": allocation["amount_minor"] + sender_fee,
    }


@op(
    "recall_external_payment",
    "Request recall of one submitted, accepted or settled external payment. Recall cannot erase the original exposure or its fees. The recorded recall outcome and net cash return arrive asynchronously.",
    {"payment_id": S},
)
def recall_payment(ctx, payment_id):
    row = ctx.record("xp_v9_payments", payment_id)
    ctx.require(
        row["status"] in ("SUBMITTED", "STATUS_UNKNOWN", "SETTLED", "PARTIALLY_SETTLED"),
        "PAYMENT_NOT_RECALLABLE",
    )
    ctx.require(not row.get("recall_requested"), "RECALL_ALREADY_REQUESTED")
    ctx.update(
        "xp_v9_payments", row["id"], recall_requested=True, recall_requested_minute=ctx.minute
    )
    ctx.schedule("v9_recall_complete", ctx.minute + 1, row["id"])
    return {"payment_id": row["id"], "status": "RECALL_PENDING"}


@op(
    "publish_payment_case_reconciliation",
    "Publish the case reconciliation from actual payment, return, recall and control records. Publication does not repair a wrong beneficiary, duplicate exposure, stale authorization, restricted source use or missed deadline.",
    {"case_id": S},
)
def publish(ctx, case_id):
    case = ctx.record("xp_v9_cases", case_id)
    payments = _rows(ctx, "xp_v9_payments", case_id=case_id)
    return ctx.update(
        "xp_v9_cases",
        case["id"],
        reconciled=True,
        reconciled_minute=ctx.minute,
        payment_ids=[row["id"] for row in payments],
        settled_net_minor=sum(
            (
                row.get("net_minor", 0)
                for row in payments
                if row["status"] in ("SETTLED", "PARTIALLY_SETTLED")
            )
        ),
    )


def _credit(ctx, payment, amount, kind):
    if amount <= 0:
        return
    source = ctx.record("xp_v9_sources", payment["source_id"])
    ctx.ledger(source["account_id"], amount, source["currency"], kind, payment["id"])


def apply_event(ctx, event):
    kind = event["kind"]
    subject_id = event["subject_id"]
    payload = event.get("_payload", {})
    if kind == "v9_control_complete":
        control = ctx.record("xp_v9_controls", subject_id)
        ctx.update(
            "xp_v9_controls",
            subject_id,
            status=control.get("review_result", "VERIFIED"),
            effective_minute=ctx.minute,
        )
    elif kind == "v9_payment_outcome":
        payment = ctx.record("xp_v9_payments", subject_id)
        if payment["status"] == "RECALLED":
            return True
        result = payload.get("result", "SETTLED")
        if result == "SETTLED":
            ctx.update(
                "xp_v9_payments",
                subject_id,
                status="SETTLED",
                net_minor=payment["amount_minor"] - payment["receiver_fee_minor"],
                settled_minute=ctx.minute,
            )
        elif result == "RETURNED":
            fee = payload.get("returned_fee_minor", 0)
            _credit(ctx, payment, payment["amount_minor"] - fee, "v9_payment_return")
            ctx.update(
                "xp_v9_payments",
                subject_id,
                status="RETURNED",
                returned_minor=payment["amount_minor"] - fee,
                return_fee_minor=fee,
                returned_minute=ctx.minute,
            )
        elif result == "STATUS_UNKNOWN":
            ctx.update(
                "xp_v9_payments",
                subject_id,
                status="STATUS_UNKNOWN",
                status_unknown_minute=ctx.minute,
            )
            ctx.schedule("v9_payment_outcome", ctx.minute + 1, subject_id, result="SETTLED")
        elif result == "PARTIAL":
            accepted = payment["amount_minor"] // 2
            _credit(ctx, payment, payment["amount_minor"] - accepted, "v9_payment_partial_return")
            ctx.update(
                "xp_v9_payments",
                subject_id,
                status="PARTIALLY_SETTLED",
                net_minor=accepted - min(payment["receiver_fee_minor"], accepted),
                returned_minor=payment["amount_minor"] - accepted,
                settled_minute=ctx.minute,
            )
        else:
            ctx.require(False, "UNKNOWN_PAYMENT_EVENT_RESULT")
    elif kind == "v9_recall_complete":
        payment = ctx.record("xp_v9_payments", subject_id)
        if payment["status"] == "RECALLED":
            return True
        gross = payment["amount_minor"]
        fee = payment.get("recall_fee_minor", 0)
        _credit(ctx, payment, max(0, gross - fee), "v9_payment_recall")
        ctx.update(
            "xp_v9_payments",
            subject_id,
            status="RECALLED",
            recalled_minor=max(0, gross - fee),
            recall_fee_minor=fee,
            recalled_minute=ctx.minute,
        )
    elif kind == "v9_source_available":
        source = ctx.record("xp_v9_sources", subject_id)
        credit = payload.get("credit_minor", 0)
        if credit:
            ctx.ledger(
                source["account_id"], credit, source["currency"], "v9_source_release", source["id"]
            )
        ctx.update("xp_v9_sources", subject_id, status="AVAILABLE", available_minute=ctx.minute)
    elif kind == "v9_source_reversal":
        source = ctx.record("xp_v9_sources", subject_id)
        ctx.update("xp_v9_sources", subject_id, status="REVERSED", reversed_minute=ctx.minute)
    else:
        return False
    return True
