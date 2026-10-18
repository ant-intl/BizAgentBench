"""Native incoming-payment, payment-recovery and procurement business operations.

All monetary arguments and record values are integer currency minor units.  This
module contains no task identifiers, reference plans, or acceptance predicates.
"""

from copy import deepcopy
from decimal import Decimal

OPERATIONS = {}


def operation(name, description, fields, read_only=False):

    def decorate(fn):
        properties = {}
        required = []
        for key, spec in fields.items():
            kind, text, *default = spec
            prop = {"type": kind, "description": text}
            if kind == "array":
                prop["items"] = {"type": "string"}
            if default:
                prop["default"] = default[0]
            else:
                required.append(key)
            properties[key] = prop
        OPERATIONS[name] = {
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            "read_only": read_only,
            "function": fn,
        }
        return fn

    return decorate


S = "string"
I = "integer"
A = "array"
B = "boolean"


def _rows(ctx, table, **filters):
    return list(ctx.records(table, **filters))


def _new(ctx, table, anchor, **fields):
    return ctx.create(table, customer_id=anchor["customer_id"], **fields)


def _fee(ctx, row, amount, kind):
    if amount:
        ctx.ledger(row["account_id"], -amount, row.get("currency", "USD"), kind, row["id"])


def _evidence(ctx, target_id, purpose):
    """The bank validates the original file binding, not a user-supplied label."""
    files = ctx.world.get("_user_files", [])
    if isinstance(files, dict):
        files = list(files.values())
    genuine = {
        f["id"]: f
        for f in files
        if f.get("customer_id")
        == ctx.record(
            next(
                (
                    table
                    for (table, records) in ctx.world.items()
                    if table.startswith("xp_")
                    and isinstance(records, dict)
                    and (target_id in records)
                )
            ),
            target_id,
        )["customer_id"]
        and f.get("target_id") == target_id
        and (f.get("purpose") == purpose)
    }
    return [
        s
        for s in ctx.uploaded(target_id, purpose)
        if s["file_id"] in genuine and s["content"] == genuine[s["file_id"]]["content"]
    ]


def _payment(ctx, invoice, recipient_id, amount, key, purpose="COMMERCIAL"):
    ctx.require(isinstance(amount, int) and amount > 0, "POSITIVE_MINOR_AMOUNT_REQUIRED")
    recipient = ctx.record("xp_a_recipients", recipient_id)
    ctx.require(recipient.get("bank_enabled", True), "RECIPIENT_BANK_UNAVAILABLE")
    prior = _rows(ctx, "xp_a_payments", account_id=invoice["account_id"], payment_key=key)
    if prior:
        row = prior[0]
        ctx.require(
            row["invoice_id"] == invoice["id"]
            and row["amount_minor"] == amount
            and (row["recipient_id"] == recipient_id),
            "IDEMPOTENCY_PAYLOAD_CONFLICT",
        )
        return row
    from payment.runtime.products.psp.receivables.authority_and_funding import consume_authority

    authority = consume_authority(ctx, invoice, amount, recipient_id)
    row = _new(
        ctx,
        "xp_a_payments",
        invoice,
        invoice_id=invoice["id"],
        account_id=invoice["account_id"],
        recipient_id=recipient_id,
        amount_minor=amount,
        currency=invoice.get("currency", "USD"),
        payment_key=key,
        purpose=purpose,
        status="SETTLED",
        minute=ctx.minute,
    )
    if authority:
        ctx.update("xp_a_payments", row["id"], **authority)
    ctx.ledger(row["account_id"], -amount, row["currency"], "SUPPLIER_PAYMENT", row["id"])
    ctx.update(
        "xp_a_invoices", invoice["id"], bank_paid_minor=invoice.get("bank_paid_minor", 0) + amount
    )
    from payment.runtime.products.psp.receivables.authority_and_funding import hold_source

    hold_source(ctx, invoice)
    ctx.emit(
        "a_economic_payment",
        payment_id=row["id"],
        invoice_id=invoice["id"],
        amount_minor=amount,
        recipient_id=recipient_id,
    )
    return row


@operation(
    "a_pay_business_invoice",
    "Send an irrevocable bank payment against an invoice. The bank checks cash, recipient availability and account-scoped idempotency, not whether the customer's commercial obligation permits another payment.",
    {
        "invoice_id": (S, "Invoice record ID."),
        "recipient_id": (S, "Bank recipient record ID."),
        "amount_minor": (I, "Positive integer amount in the invoice currency's minor units."),
        "payment_key": (S, "Account-scoped immutable business idempotency key."),
        "purpose": (S, "Truthful commercial purpose label.", "COMMERCIAL"),
    },
)
def pay_business_invoice(
    ctx, invoice_id, recipient_id, amount_minor, payment_key, purpose="COMMERCIAL"
):
    return _payment(
        ctx,
        ctx.record("xp_a_invoices", invoice_id),
        recipient_id,
        amount_minor,
        payment_key,
        purpose,
    )


@operation(
    "a_submit_source_chain",
    "Submit actual uploaded shop, PSP and remitter evidence for review. Submission does not release funds; an independent bank result may accept only some orders.",
    {
        "incoming_id": (S, "Pending incoming payment ID."),
        "order_ids": (A, "Sales order IDs covered by this evidence chain."),
    },
)
def submit_source_chain(ctx, incoming_id, order_ids):
    inc = ctx.record("xp_a_incoming", incoming_id)
    ctx.require(_evidence(ctx, incoming_id, "source_chain"), "SOURCE_CHAIN_UPLOAD_REQUIRED")
    ctx.require(order_ids and len(set(order_ids)) == len(order_ids), "DISTINCT_ORDERS_REQUIRED")
    for oid in order_ids:
        order = ctx.record("xp_a_sales", oid)
        ctx.require(
            order["psp_batch"] == inc["psp_batch"]
            and order["remitter_reference"] == inc["bank_reference"],
            "DISCONNECTED_SOURCE_CHAIN",
        )
    return ctx.update(
        "xp_a_incoming", incoming_id, submitted_order_ids=order_ids, evidence_submitted=True
    )


@operation(
    "a_claim_incoming_orders",
    "Credit only bank-accepted and submitted orders from a PSP incoming payment. Each order can be credited once; rejected or unreviewed orders remain quarantined.",
    {
        "incoming_id": (S, "Incoming payment ID."),
        "order_ids": (A, "Distinct accepted sales order IDs to release."),
    },
)
def claim_incoming_orders(ctx, incoming_id, order_ids):
    inc = ctx.record("xp_a_incoming", incoming_id)
    ctx.require(inc["status"] != "RETURNED", "PRINCIPAL_ALREADY_RETURNED")
    ctx.require(inc.get("evidence_submitted"), "SOURCE_EVIDENCE_NOT_SUBMITTED")
    ctx.require(inc.get("review_status") == "COMPLETE", "BANK_REVIEW_PENDING")
    ctx.require(order_ids and len(set(order_ids)) == len(order_ids), "DISTINCT_ORDERS_REQUIRED")
    accepted = set(inc.get("accepted_order_ids", []))
    claimed = set(inc.get("claimed_order_ids", []))
    ctx.require(set(order_ids) <= accepted & set(inc["submitted_order_ids"]), "ORDERS_NOT_ACCEPTED")
    ctx.require(not set(order_ids) & claimed, "ORDER_ALREADY_CLAIMED")
    amount = sum((ctx.record("xp_a_sales", oid)["amount_minor"] for oid in order_ids))
    ctx.ledger(inc["account_id"], amount, inc["currency"], "INCOMING_RELEASE", incoming_id)
    return ctx.update(
        "xp_a_incoming",
        incoming_id,
        claimed_order_ids=sorted(claimed | set(order_ids)),
        released_minor=inc.get("released_minor", 0) + amount,
    )


@operation(
    "a_select_stock_order",
    "Choose a supplier's currently offered stock package. Selection fixes the invoice quantity and price; subsequent payment must match the selected offer.",
    {"invoice_id": (S, "Stock invoice ID."), "offer_id": (S, "Supplier package offer ID.")},
)
def select_stock_order(ctx, invoice_id, offer_id):
    inv = ctx.record("xp_a_invoices", invoice_id)
    offer = ctx.record("xp_a_stock_packages", offer_id)
    ctx.require(offer["invoice_id"] == invoice_id, "OFFER_INVOICE_MISMATCH")
    ctx.require(not _rows(ctx, "xp_a_payments", invoice_id=invoice_id), "PACKAGE_ALREADY_PAID")
    return ctx.update(
        "xp_a_invoices",
        inv["id"],
        selected_offer_id=offer_id,
        amount_minor=offer["amount_minor"],
        quantity=offer["quantity"],
    )


@operation(
    "a_open_bank_trace",
    "Buy a formal bank trace for a missing incoming transfer. Fees are nonrefundable. Sender-bank and receiving-bank traces have different responsibility scopes; the environment publishes the actual finding.",
    {
        "incoming_id": (S, "Missing incoming payment ID."),
        "channel_id": (S, "Trace channel and fee record ID."),
    },
)
def open_bank_trace(ctx, incoming_id, channel_id):
    inc = ctx.record("xp_a_incoming", incoming_id)
    channel = ctx.record("xp_a_trace_channels", channel_id)
    ctx.require(_evidence(ctx, incoming_id, "payment_proof"), "BANK_PAYMENT_PROOF_REQUIRED")
    _fee(ctx, inc, channel["fee_minor"], "TRACE_FEE")
    if channel["responsibility"] == "SENDER":
        for event in ctx.records("xp_events", subject_id=incoming_id, status="SCHEDULED"):
            if event["kind"] == "a_trace_result":
                ctx.update(
                    "xp_events",
                    event["id"],
                    due_minute=min(
                        event["due_minute"], ctx.minute + channel.get("response_delay_minutes", 10)
                    ),
                )
    return _new(
        ctx,
        "xp_a_traces",
        inc,
        incoming_id=incoming_id,
        channel_id=channel_id,
        responsibility=channel["responsibility"],
        status="REQUESTED",
        fee_minor=channel["fee_minor"],
    )


@operation(
    "a_amend_transfer_instruction",
    "Submit the missing address or purpose instruction to the responsible intermediary after a trace locates the payment. The published amendment fee is charged and the original transfer is released once.",
    {
        "incoming_id": (S, "Located incoming transfer ID."),
        "responsible_bank": (S, "Bank named in the trace result."),
        "instruction": (S, "Complete corrected instruction from the customer evidence."),
    },
)
def amend_transfer_instruction(ctx, incoming_id, responsible_bank, instruction):
    inc = ctx.record("xp_a_incoming", incoming_id)
    ctx.require(inc.get("located_bank") == responsible_bank, "TRACE_RESPONSIBILITY_MISMATCH")
    ctx.require(
        any(
            (
                t["responsibility"] == "SENDER"
                for t in _rows(ctx, "xp_a_traces", incoming_id=incoming_id)
            )
        ),
        "SENDER_TRACE_REQUIRED",
    )
    ctx.require(_evidence(ctx, incoming_id, "address_proof"), "ADDRESS_PROOF_REQUIRED")
    ctx.require(instruction == inc.get("required_instruction"), "INCOMPLETE_AMENDMENT")
    ctx.require(inc["status"] == "HELD", "TRANSFER_NOT_HELD")
    _fee(ctx, inc, inc["amendment_fee_minor"], "AMENDMENT_FEE")
    ctx.ledger(
        inc["account_id"], inc["amount_minor"], inc["currency"], "INCOMING_RELEASE", incoming_id
    )
    return ctx.update("xp_a_incoming", incoming_id, status="CREDITED", disposition="AMENDED")


@operation(
    "a_recall_incoming_transfer",
    "Request irrevocable return of a held transfer to its original sender. This is a lawful alternative to correcting the transfer, but it does not credit the recipient.",
    {"incoming_id": (S, "Held incoming transfer ID.")},
)
def recall_incoming_transfer(ctx, incoming_id):
    inc = ctx.record("xp_a_incoming", incoming_id)
    ctx.require(inc["status"] in ("HELD", "QUARANTINED", "PENDING"), "TRANSFER_NOT_RECALLABLE")
    _fee(ctx, inc, inc.get("recall_fee_minor", 0), "RECALL_FEE")
    return ctx.update(
        "xp_a_incoming",
        incoming_id,
        status="RETURNED",
        disposition="VOLUNTARY_RECALL",
        returned_to=inc["origin_recipient_id"],
        returned_minor=inc["amount_minor"],
    )


@operation(
    "a_investigate_incoming_owner",
    "Register an ownership investigation using an original remittance proof. Knowing an amount or sending an email does not establish ownership. The bank result is an independent environment event.",
    {"incoming_id": (S, "Quarantined incoming payment ID.")},
)
def investigate_incoming_owner(ctx, incoming_id):
    inc = ctx.record("xp_a_incoming", incoming_id)
    ctx.require(_evidence(ctx, incoming_id, "origin_proof"), "ORIGINAL_REMITTANCE_PROOF_REQUIRED")
    return ctx.update("xp_a_incoming", incoming_id, ownership_investigation=True)


@operation(
    "a_dispose_incoming_balance",
    "Return quarantined funds along their original bank route or claim funds supported by a published ownership decision. Funds cannot be sent to a claimant's substitute account through this tool.",
    {
        "incoming_id": (S, "Incoming payment ID."),
        "disposition": (S, "RETURN_ORIGIN or CLAIM."),
        "decision_version": (I, "Exact currently published bank decision version."),
    },
)
def dispose_incoming_balance(ctx, incoming_id, disposition, decision_version):
    inc = ctx.record("xp_a_incoming", incoming_id)
    ctx.require(inc.get("decision_version") == decision_version, "STALE_BANK_DECISION")
    ctx.require(
        inc.get("ownership_investigation") or inc.get("claim_submitted"), "INVESTIGATION_REQUIRED"
    )
    ctx.require(inc["status"] not in ("RETURNED", "CREDITED"), "PRINCIPAL_ALREADY_DISPOSED")
    ctx.require(disposition in inc.get("allowed_dispositions", []), "BANK_DISPOSITION_NOT_ALLOWED")
    if disposition == "CLAIM":
        ctx.ledger(
            inc["account_id"], inc["amount_minor"], inc["currency"], "INCOMING_RELEASE", incoming_id
        )
        return ctx.update(
            "xp_a_incoming",
            incoming_id,
            status="CREDITED",
            disposition="CLAIM",
            applied_decision_version=decision_version,
        )
    return ctx.update(
        "xp_a_incoming",
        incoming_id,
        status="RETURNED",
        disposition="RETURN_ORIGIN",
        returned_minor=inc["amount_minor"],
        returned_to=inc["origin_recipient_id"],
        applied_decision_version=decision_version,
    )


@operation(
    "a_submit_receipt_claim",
    "Register a claim while a sender recall is under review. Submission is not an ownership decision and does not cancel the parallel recall.",
    {
        "incoming_id": (S, "Incoming payment involved in a recall."),
        "position": (S, "RETAIN or CONSENT_RETURN."),
    },
)
def submit_receipt_claim(ctx, incoming_id, position):
    inc = ctx.record("xp_a_incoming", incoming_id)
    ctx.require(position in ("RETAIN", "CONSENT_RETURN"), "INVALID_CLAIM_POSITION")
    ctx.require(_evidence(ctx, incoming_id, "contract_proof"), "CONTRACT_PROOF_REQUIRED")
    return ctx.update("xp_a_incoming", incoming_id, claim_submitted=True, claim_position=position)


@operation(
    "a_ingest_credit_notice",
    "Project a credit notice using the immutable bank transaction ID. Replayed deliveries of the same bank transaction do not create a second credit; distinct bank transactions do.",
    {"notice_id": (S, "Notification delivery ID.")},
)
def ingest_credit_notice(ctx, notice_id):
    notice = ctx.record("xp_a_notices", notice_id)
    ctx.require(notice["status"] != "ARCHIVED", "NOTICE_ARCHIVED")
    tx = ctx.record("xp_a_bank_transactions", notice["bank_transaction_id"])
    if not tx.get("projected"):
        ctx.ledger(tx["account_id"], tx["amount_minor"], tx["currency"], "BANK_CREDIT", tx["id"])
        ctx.update("xp_a_bank_transactions", tx["id"], projected=True)
    return ctx.update("xp_a_notices", notice_id, status="PROCESSED", deduplication_key=tx["id"])


@operation(
    "a_archive_credit_notice",
    "Archive a notification without booking it. Archiving is irreversible for this operations window and should only be used when another processed notice represents the same bank transaction.",
    {
        "notice_id": (S, "Notification delivery ID."),
        "reason": (S, "Operator's reason for archiving."),
    },
)
def archive_credit_notice(ctx, notice_id, reason):
    row = ctx.record("xp_a_notices", notice_id)
    ctx.require(row["status"] == "NEW", "NOTICE_NOT_NEW")
    return ctx.update("xp_a_notices", notice_id, status="ARCHIVED", archive_reason=reason)


@operation(
    "a_return_duplicate_transfer",
    "Return a real credited bank transfer to the original sender. Identical notification amounts do not establish that a bank transfer was economically duplicated.",
    {"bank_transaction_id": (S, "Actual bank transaction ID to return.")},
)
def return_duplicate_transfer(ctx, bank_transaction_id):
    tx = ctx.record("xp_a_bank_transactions", bank_transaction_id)
    ctx.require(tx.get("projected") and (not tx.get("returned")), "CREDIT_NOT_AVAILABLE_FOR_RETURN")
    ctx.ledger(tx["account_id"], -tx["amount_minor"], tx["currency"], "BANK_RETURN", tx["id"])
    return ctx.update(
        "xp_a_bank_transactions", tx["id"], returned=True, returned_to=tx["origin_recipient_id"]
    )


@operation(
    "a_assign_virtual_receipt",
    "Assign a late receipt to a shop using an explicit historical lease and timestamp basis. SEND and ARRIVAL are operationally supported bases; the customer's contract determines the applicable basis. Assignment can be corrected until shop payout.",
    {
        "receipt_id": (S, "Late virtual-account receipt ID."),
        "lease_id": (S, "Historical virtual-account lease ID."),
        "timestamp_basis": (S, "SEND or ARRIVAL."),
    },
)
def assign_virtual_receipt(ctx, receipt_id, lease_id, timestamp_basis):
    rec = ctx.record("xp_a_virtual_receipts", receipt_id)
    lease = ctx.record("xp_a_virtual_leases", lease_id)
    ctx.require(not rec.get("paid_out"), "SHOP_PAYOUT_IRREVERSIBLE")
    ctx.require(timestamp_basis in ("SEND", "ARRIVAL"), "UNKNOWN_TIMESTAMP_BASIS")
    stamp = rec["sent_minute" if timestamp_basis == "SEND" else "arrived_minute"]
    ctx.require(
        lease["virtual_account"] == rec["virtual_account"]
        and lease["start_minute"] <= stamp < lease["end_minute"],
        "LEASE_DOES_NOT_COVER_TIMESTAMP",
    )
    return ctx.update(
        "xp_a_virtual_receipts",
        receipt_id,
        lease_id=lease_id,
        shop_id=lease["shop_id"],
        timestamp_basis=timestamp_basis,
        assignment_revision=rec["revision"],
    )


@operation(
    "a_pay_out_shop_receipt",
    "Pay an assigned receipt to the selected shop. Payout is irrevocable; later timestamp corrections cannot move cash already sent to a different shop.",
    {"receipt_id": (S, "Assigned virtual-account receipt ID.")},
)
def pay_out_shop_receipt(ctx, receipt_id):
    rec = ctx.record("xp_a_virtual_receipts", receipt_id)
    ctx.require(rec.get("shop_id") and (not rec.get("paid_out")), "RECEIPT_NOT_PAYABLE")
    shop = ctx.record("xp_a_shops", rec["shop_id"])
    from payment.runtime.products.psp.receivables.authority_and_funding import consume_authority

    authority = consume_authority(ctx, rec, rec["amount_minor"], shop["id"])
    if authority:
        ctx.update("xp_a_virtual_receipts", receipt_id, **authority)
    ctx.ledger(rec["account_id"], -rec["amount_minor"], rec["currency"], "SHOP_PAYOUT", receipt_id)
    return ctx.update(
        "xp_a_virtual_receipts",
        receipt_id,
        paid_out=True,
        payout_shop_id=shop["id"],
        payout_revision=rec["revision"],
    )


@operation(
    "a_resubmit_payment_intent",
    "Resubmit an uncertain payment with a business key. Reusing its original key returns the existing attempt. A fresh key creates a distinct irrevocable payment, even when another key later settles the same invoice.",
    {
        "intent_id": (S, "Uncertain business payment intent ID."),
        "payment_key": (S, "Original or new account-scoped business key."),
    },
)
def resubmit_payment_intent(ctx, intent_id, payment_key):
    intent = ctx.record("xp_a_payment_intents", intent_id)
    if payment_key == intent["original_key"] and intent["bank_status"] in (
        "UNKNOWN",
        "ACCEPTED",
        "SETTLED",
    ):
        return {
            "intent_id": intent_id,
            "bank_status": intent["bank_status"],
            "payment_id": intent.get("payment_id"),
            "deduplicated": True,
        }
    inv = ctx.record("xp_a_invoices", intent["invoice_id"])
    p = _payment(ctx, inv, inv["recipient_id"], inv["amount_minor"], payment_key)
    return ctx.update("xp_a_payment_intents", intent_id, last_retry_payment_id=p["id"])


@operation(
    "a_abandon_timeout_screen",
    "Dismiss a timed-out payment from the user interface. This changes only the screen state; a bank request already sent can still settle.",
    {"intent_id": (S, "Timed-out intent ID.")},
)
def abandon_timeout_screen(ctx, intent_id):
    ctx.record("xp_a_payment_intents", intent_id)
    return ctx.update("xp_a_payment_intents", intent_id, screen_status="DISMISSED")


@operation(
    "a_preview_payment_file",
    "Parse an uploaded payment export with explicit monetary-unit and fee semantics. Returns line-level proposed bank amounts and original obligation references without moving money. MAJOR_NET converts decimal major units to minor units and adds sender fees; MINOR_NET treats values as minor units; MAJOR_GROSS subtracts recipient-borne fees.",
    {
        "file_record_id": (S, "Public export metadata ID with uploaded file contents."),
        "schema": (S, "MAJOR_NET, MINOR_NET, or MAJOR_GROSS."),
    },
    read_only=True,
)
def preview_payment_file(ctx, file_record_id, schema):
    source = ctx.record("xp_a_exports", file_record_id)
    ctx.require(_evidence(ctx, file_record_id, "payment_export"), "ACTUAL_EXPORT_UPLOAD_REQUIRED")
    ctx.require(schema in ("MAJOR_NET", "MINOR_NET", "MAJOR_GROSS"), "UNKNOWN_FILE_SCHEMA")
    rows = []
    for line in source["lines"]:
        scale = 1 if schema == "MINOR_NET" else 100
        amount = int(Decimal(str(line["amount"])) * scale)
        if schema == "MAJOR_GROSS":
            amount -= line["fee_minor"]
        rows.append(
            dict(
                line,
                amount_minor=amount,
                sender_fee_minor=line["fee_minor"] if schema != "MAJOR_GROSS" else 0,
            )
        )
    return {
        "file_record_id": file_record_id,
        "schema": schema,
        "rows": rows,
        "total_debit_minor": sum((r["amount_minor"] + r["sender_fee_minor"] for r in rows)),
    }


@operation(
    "a_execute_payment_file_line",
    "Execute a reviewed payment-file line. The original obligation reference survives beneficiary repair. A rejected line charges its stated rejection fee; a successful line cannot be paid again using the same obligation key. Operator-selected parsing semantics are honored by the bank.",
    {
        "file_record_id": (S, "Uploaded payment export metadata ID."),
        "line_id": (S, "Line ID within the export."),
        "schema": (S, "MAJOR_NET, MINOR_NET, or MAJOR_GROSS."),
        "recipient_id": (S, "Current bank recipient ID."),
        "payment_key": (S, "Idempotency key retaining the original obligation reference."),
    },
)
def execute_payment_file_line(ctx, file_record_id, line_id, schema, recipient_id, payment_key):
    preview = preview_payment_file(ctx, file_record_id, schema)
    line = next((x for x in preview["rows"] if x["id"] == line_id), None)
    ctx.require(line is not None, "FILE_LINE_NOT_FOUND")
    source = ctx.record("xp_a_exports", file_record_id)
    inv = ctx.record("xp_a_invoices", line["invoice_id"])
    recipient = ctx.record("xp_a_recipients", recipient_id)
    if not recipient.get("bank_enabled", True):
        _fee(ctx, inv, source["rejection_fee_minor"], "BATCH_REJECTION_FEE")
        return _new(
            ctx,
            "xp_a_batch_results",
            source,
            file_record_id=file_record_id,
            line_id=line_id,
            invoice_id=inv["id"],
            status="REJECTED",
            recipient_id=recipient_id,
            repair_recipient_id=recipient.get("repair_recipient_id"),
        )
    prior = _rows(ctx, "xp_a_payments", account_id=inv["account_id"], payment_key=payment_key)
    payment = _payment(ctx, inv, recipient_id, line["amount_minor"], payment_key)
    if not prior:
        _fee(ctx, inv, line["sender_fee_minor"], "BATCH_SENDER_FEE")
    return _new(
        ctx,
        "xp_a_batch_results",
        source,
        file_record_id=file_record_id,
        line_id=line_id,
        invoice_id=inv["id"],
        status="SETTLED",
        schema=schema,
        amount_minor=line["amount_minor"],
        payment_id=payment["id"],
        recipient_id=recipient_id,
    )


@operation(
    "a_project_bank_receipts",
    "Recompute payment status from received bank events. CAUSAL selects the greatest bank sequence and respects a RETURNED correction of SETTLED. ARRIVAL selects the most recently delivered message and is available for legacy projections; delivery order is not causal order.",
    {"invoice_id": (S, "Payment obligation ID."), "projection": (S, "CAUSAL or ARRIVAL.")},
)
def project_bank_receipts(ctx, invoice_id, projection):
    inv = ctx.record("xp_a_invoices", invoice_id)
    rows = _rows(ctx, "xp_a_bank_receipts", invoice_id=invoice_id)
    ctx.require(rows and projection in ("CAUSAL", "ARRIVAL"), "RECEIPTS_OR_PROJECTION_INVALID")
    field = "bank_sequence" if projection == "CAUSAL" else "delivery_minute"
    final = max(rows, key=lambda r: (r[field], r["id"]))
    return ctx.update(
        "xp_a_invoices",
        invoice_id,
        projected_status=final["status"],
        projection=projection,
        projected_receipt_id=final["id"],
    )


@operation(
    "a_confirm_recipient_entity",
    "Record entity verification using actual uploaded contractual identity evidence and the bank's published holder/alias result. Verification does not itself pay the recipient.",
    {
        "recipient_id": (S, "Candidate recipient bank account ID."),
        "invoice_id": (S, "Commercial invoice ID."),
    },
)
def confirm_recipient_entity(ctx, recipient_id, invoice_id):
    r = ctx.record("xp_a_recipients", recipient_id)
    inv = ctx.record("xp_a_invoices", invoice_id)
    ctx.require(_evidence(ctx, invoice_id, "entity_proof"), "ENTITY_EVIDENCE_REQUIRED")
    ctx.require(
        r.get("verification_status") in ("EXACT", "ALIAS_CONFIRMED"), "IDENTITY_REVIEW_UNRESOLVED"
    )
    ctx.require(
        r.get("holder_registration") == inv["contract_registration"], "HOLDER_NOT_CONTRACT_ENTITY"
    )
    return _new(
        ctx,
        "xp_a_entity_checks",
        inv,
        invoice_id=invoice_id,
        recipient_id=recipient_id,
        registration=r["holder_registration"],
        verification_status=r["verification_status"],
    )


@operation(
    "a_declare_contract_payment",
    "Declare selected contract components and a purpose to a supported route. The banking route accepts its supported declared purposes; the customer remains responsible for truthful component classification. Published review acceptance can be partial.",
    {
        "contract_id": (S, "Composite commercial contract ID."),
        "component_ids": (A, "Contract component IDs."),
        "route_id": (S, "Purpose-capable payment route ID."),
        "declared_purpose": (S, "GOODS, LICENSE, SERVICE, or MIXED."),
    },
)
def declare_contract_payment(ctx, contract_id, component_ids, route_id, declared_purpose):
    contract = ctx.record("xp_a_contracts", contract_id)
    route = ctx.record("xp_a_purpose_routes", route_id)
    ctx.require(_evidence(ctx, contract_id, "composite_contract"), "CONTRACT_UPLOAD_REQUIRED")
    ctx.require(declared_purpose in route["supported_purposes"], "ROUTE_PURPOSE_UNSUPPORTED")
    ctx.require(
        component_ids and len(set(component_ids)) == len(component_ids),
        "DISTINCT_COMPONENTS_REQUIRED",
    )
    components = [ctx.record("xp_a_components", x) for x in component_ids]
    ctx.require(
        all((x["contract_id"] == contract_id for x in components)), "CONTRACT_COMPONENT_MISMATCH"
    )
    accepted = [
        x["id"]
        for x in components
        if x.get("bank_accepted")
        or (x.get("needs_delivery_proof") and _evidence(ctx, contract_id, "delivery_proof"))
    ]
    return _new(
        ctx,
        "xp_a_declarations",
        contract,
        contract_id=contract_id,
        component_ids=component_ids,
        accepted_component_ids=accepted,
        route_id=route_id,
        declared_purpose=declared_purpose,
        fee_minor=route["fee_minor"],
        status="REVIEWED",
    )


@operation(
    "a_execute_contract_declaration",
    "Pay the accepted components of a reviewed declaration and charge its route fee once. Components not accepted remain unpaid; a supplemental declaration can cover them later.",
    {"declaration_id": (S, "Reviewed declaration ID.")},
)
def execute_contract_declaration(ctx, declaration_id):
    d = ctx.record("xp_a_declarations", declaration_id)
    ctx.require(
        d["status"] == "REVIEWED" and d["accepted_component_ids"], "NO_EXECUTABLE_COMPONENTS"
    )
    contract = ctx.record("xp_a_contracts", d["contract_id"])
    _fee(ctx, contract, d["fee_minor"], "PURPOSE_ROUTE_FEE")
    for component_id in d["accepted_component_ids"]:
        c = ctx.record("xp_a_components", component_id)
        inv = ctx.record("xp_a_invoices", c["invoice_id"])
        p = _payment(
            ctx,
            inv,
            inv["recipient_id"],
            c["amount_minor"],
            declaration_id + ":" + component_id,
            d["declared_purpose"],
        )
        ctx.update(
            "xp_a_payments", p["id"], declaration_id=declaration_id, component_id=component_id
        )
        ctx.update("xp_a_components", component_id, last_payment_id=p["id"])
    return ctx.update("xp_a_declarations", declaration_id, status="EXECUTED")


@operation(
    "a_reserve_equipment_offer",
    "Buy a nonrefundable reservation for one equipment offer. Reservation does not guarantee compatibility with other components. A merchant stock event may invalidate an offer; reservation fees remain charged.",
    {"offer_id": (S, "Equipment supplier offer ID.")},
)
def reserve_equipment_offer(ctx, offer_id):
    offer = ctx.record("xp_a_equipment_offers", offer_id)
    ctx.require(
        offer["status"] == "AVAILABLE" and ctx.minute <= offer["valid_until"], "OFFER_NOT_AVAILABLE"
    )
    _fee(ctx, offer, offer["reservation_fee_minor"], "EQUIPMENT_RESERVATION_FEE")
    return _new(
        ctx,
        "xp_a_reservations",
        offer,
        offer_id=offer_id,
        status="HELD",
        compatibility=offer["compatibility"],
        component=offer["component"],
        amount_minor=offer["amount_minor"],
        fee_minor=offer["reservation_fee_minor"],
    )


@operation(
    "a_release_equipment_reservation",
    "Release an equipment reservation. Its booking fee is nonrefundable; no equipment principal is paid.",
    {"reservation_id": (S, "Held reservation ID.")},
)
def release_equipment_reservation(ctx, reservation_id):
    row = ctx.record("xp_a_reservations", reservation_id)
    ctx.require(row["status"] == "HELD", "RESERVATION_NOT_HELD")
    return ctx.update("xp_a_reservations", reservation_id, status="RELEASED")


@operation(
    "a_commit_equipment_bundle",
    "Atomically buy a complete compatible set of reserved equipment. Every required component, current stock and reservation must be valid before any principal is debited.",
    {
        "reservation_ids": (A, "Distinct held reservation IDs for one complete set."),
        "contract_id": (S, "Equipment purchase contract ID."),
    },
)
def commit_equipment_bundle(ctx, reservation_ids, contract_id):
    contract = ctx.record("xp_a_contracts", contract_id)
    ctx.require(
        reservation_ids and len(set(reservation_ids)) == len(reservation_ids),
        "DISTINCT_RESERVATIONS_REQUIRED",
    )
    rs = [ctx.record("xp_a_reservations", x) for x in reservation_ids]
    offers = [ctx.record("xp_a_equipment_offers", r["offer_id"]) for r in rs]
    ctx.require(all((r["status"] == "HELD" for r in rs)), "RESERVATION_NOT_HELD")
    ctx.require(
        all((o["status"] == "AVAILABLE" and ctx.minute <= o["valid_until"] for o in offers)),
        "BUNDLE_STOCK_UNAVAILABLE",
    )
    ctx.require(
        sorted((r["component"] for r in rs)) == sorted(contract["required_components"]),
        "INCOMPLETE_EQUIPMENT_SET",
    )
    ctx.require(len({r["compatibility"] for r in rs}) == 1, "INCOMPATIBLE_EQUIPMENT_SET")
    total = sum((r["amount_minor"] for r in rs))
    ctx.ledger(
        contract["account_id"], -total, contract["currency"], "EQUIPMENT_ATOMIC_COMMIT", contract_id
    )
    for r in rs:
        ctx.update("xp_a_reservations", r["id"], status="COMMITTED")
    return ctx.update(
        "xp_a_contracts",
        contract_id,
        status="PURCHASED",
        committed_reservation_ids=reservation_ids,
        principal_minor=total,
        compatibility=rs[0]["compatibility"],
    )


@operation(
    "a_buy_equipment_individually",
    "Buy an available equipment component immediately without a complete-set commitment. This legitimate retail purchase is irrevocable and may violate an all-or-none procurement condition.",
    {"offer_id": (S, "Equipment offer ID.")},
)
def buy_equipment_individually(ctx, offer_id):
    offer = ctx.record("xp_a_equipment_offers", offer_id)
    ctx.require(offer["status"] == "AVAILABLE", "OFFER_NOT_AVAILABLE")
    ctx.ledger(
        offer["account_id"],
        -offer["amount_minor"],
        offer["currency"],
        "EQUIPMENT_SINGLE_PURCHASE",
        offer_id,
    )
    return ctx.update("xp_a_equipment_offers", offer_id, status="BOUGHT_INDIVIDUALLY")


@operation(
    "a_register_debit_mandate",
    "Register a replacement debit mandate from an actual debtor-signed upload. A revoked mandate is never reactivated by a balance top-up.",
    {
        "debt_id": (S, "Debt requiring a new mandate."),
        "mandate_reference": (S, "Mandate reference appearing in the debtor-signed evidence."),
    },
)
def register_debit_mandate(ctx, debt_id, mandate_reference):
    debt = ctx.record("xp_a_debts", debt_id)
    ctx.require(_evidence(ctx, debt_id, "new_mandate"), "DEBTOR_SIGNED_MANDATE_REQUIRED")
    ctx.require(
        mandate_reference == debt["replacement_mandate_reference"], "MANDATE_REFERENCE_MISMATCH"
    )
    return ctx.update(
        "xp_a_debts", debt_id, mandate_status="ACTIVE", mandate_reference=mandate_reference
    )


@operation(
    "a_retry_direct_debit",
    "Retry a returned direct debit under the synthetic network rules. NSF has a finite retry count and each attempted retry can consume it; REVOKED requires a replacement mandate; TECHNICAL retains the original retry key. Successful repeated keys deduplicate.",
    {"debt_id": (S, "Returned debt ID."), "retry_key": (S, "Network retry idempotency key.")},
)
def retry_direct_debit(ctx, debt_id, retry_key):
    debt = ctx.record("xp_a_debts", debt_id)
    prior = _rows(
        ctx, "xp_a_debit_attempts", debt_id=debt_id, retry_key=retry_key, status="SETTLED"
    )
    if prior:
        return prior[0]
    ctx.require(debt["mandate_status"] == "ACTIVE", "MANDATE_NOT_ACTIVE")
    if debt["return_code"] == "TECHNICAL":
        ctx.require(retry_key == debt["original_key"], "TECHNICAL_RETRY_KEY_CHANGED")
    ctx.require(debt["attempts_used"] < debt["max_attempts"], "RETRY_ALLOWANCE_EXHAUSTED")
    ctx.update("xp_a_debts", debt_id, attempts_used=debt["attempts_used"] + 1)
    if debt["debtor_available_minor"] < debt["amount_minor"]:
        _fee(ctx, debt, debt["failed_retry_fee_minor"], "DEBIT_RETURN_FEE")
        return _new(
            ctx,
            "xp_a_debit_attempts",
            debt,
            debt_id=debt_id,
            retry_key=retry_key,
            status="NSF",
            amount_minor=debt["amount_minor"],
        )
    ctx.ledger(
        debt["account_id"],
        debt["amount_minor"],
        debt["currency"],
        "DIRECT_DEBIT_COLLECTION",
        debt_id,
    )
    ctx.update(
        "xp_a_debts",
        debt_id,
        debtor_available_minor=debt["debtor_available_minor"] - debt["amount_minor"],
        collected_minor=debt.get("collected_minor", 0) + debt["amount_minor"],
    )
    return _new(
        ctx,
        "xp_a_debit_attempts",
        debt,
        debt_id=debt_id,
        retry_key=retry_key,
        status="SETTLED",
        amount_minor=debt["amount_minor"],
    )


@operation(
    "a_waive_debt",
    "Irrevocably waive an unpaid commercial debt. Waiving is a legitimate customer choice but does not constitute collection.",
    {"debt_id": (S, "Debt ID.")},
)
def waive_debt(ctx, debt_id):
    debt = ctx.record("xp_a_debts", debt_id)
    ctx.require(not debt.get("collected_minor"), "DEBT_ALREADY_COLLECTED")
    return ctx.update("xp_a_debts", debt_id, mandate_status="WAIVED", waived=True)


@operation(
    "a_reserve_cash_pickup",
    "Reserve cash at an agent for a wallet withdrawal or direct remittance. Reservation consumes local cash capacity and charges the nonrefundable agent fee. The beneficiary identity and reservation expiry are binding.",
    {
        "agent_id": (S, "Cash agent ID."),
        "beneficiary_id": (S, "Cash recipient ID."),
        "amount_minor": (I, "Positive cash amount in destination currency minor units."),
        "mode": (S, "WALLET or DIRECT."),
    },
)
def reserve_cash_pickup(ctx, agent_id, beneficiary_id, amount_minor, mode):
    agent = ctx.record("xp_a_cash_agents", agent_id)
    ctx.require(mode in ("WALLET", "DIRECT") and amount_minor > 0, "INVALID_CASH_RESERVATION")
    ctx.require(
        agent["cash_available_minor"] >= amount_minor and agent["open"], "AGENT_CASH_UNAVAILABLE"
    )
    fee = agent["wallet_fee_minor" if mode == "WALLET" else "direct_fee_minor"]
    _fee(ctx, agent, fee, "CASH_RESERVATION_FEE")
    ctx.update(
        "xp_a_cash_agents",
        agent_id,
        cash_available_minor=agent["cash_available_minor"] - amount_minor,
    )
    return _new(
        ctx,
        "xp_a_cash_reservations",
        agent,
        agent_id=agent_id,
        beneficiary_id=beneficiary_id,
        amount_minor=amount_minor,
        mode=mode,
        status="RESERVED",
        fee_minor=fee,
        expires_minute=agent["reservation_expiry"],
    )


@operation(
    "a_fund_mobile_wallet",
    "Transfer money irrevocably into a mobile wallet within its current balance limit. Wallet receipt is digital value and is not evidence that physical cash was delivered.",
    {
        "wallet_id": (S, "Mobile wallet ID."),
        "amount_minor": (I, "Positive amount in wallet-currency minor units."),
    },
)
def fund_mobile_wallet(ctx, wallet_id, amount_minor):
    wallet = ctx.record("xp_a_wallets", wallet_id)
    ctx.require(
        amount_minor > 0
        and wallet["balance_minor"] + amount_minor <= wallet["balance_limit_minor"],
        "WALLET_CAPACITY_EXCEEDED",
    )
    ctx.ledger(
        wallet["account_id"],
        -(amount_minor + wallet["transfer_fee_minor"]),
        wallet["currency"],
        "WALLET_FUNDING",
        wallet_id,
    )
    return ctx.update(
        "xp_a_wallets",
        wallet_id,
        balance_minor=wallet["balance_minor"] + amount_minor,
        funded_minor=wallet.get("funded_minor", 0) + amount_minor,
    )


@operation(
    "a_deliver_reserved_cash",
    "Complete a reserved cash handover and issue its physical-delivery receipt. Wallet mode debits the matching beneficiary's wallet; direct mode debits the remitter's bank account. An expired reservation cannot deliver cash.",
    {
        "reservation_id": (S, "Cash reservation ID."),
        "wallet_id": (S, "Wallet ID for WALLET; empty string for DIRECT."),
    },
)
def deliver_reserved_cash(ctx, reservation_id, wallet_id):
    reservation = ctx.record("xp_a_cash_reservations", reservation_id)
    agent = ctx.record("xp_a_cash_agents", reservation["agent_id"])
    ctx.require(
        reservation["status"] == "RESERVED" and ctx.minute <= reservation["expires_minute"],
        "CASH_RESERVATION_EXPIRED",
    )
    amount = reservation["amount_minor"]
    if reservation["mode"] == "WALLET":
        wallet = ctx.record("xp_a_wallets", wallet_id)
        ctx.require(
            wallet["beneficiary_id"] == reservation["beneficiary_id"]
            and wallet["balance_minor"] >= amount,
            "WALLET_IDENTITY_OR_BALANCE_MISMATCH",
        )
        ctx.update("xp_a_wallets", wallet_id, balance_minor=wallet["balance_minor"] - amount)
    else:
        ctx.ledger(
            agent["account_id"],
            -amount,
            agent["currency"],
            "DIRECT_CASH_REMITTANCE",
            reservation_id,
        )
    return ctx.update(
        "xp_a_cash_reservations",
        reservation_id,
        status="DELIVERED",
        delivered_minor=amount,
        delivery_minute=ctx.minute,
        wallet_id=wallet_id,
    )


@operation(
    "a_snapshot_accepted_orders",
    "Obtain an immutable servicing snapshot of already accepted marketplace orders before changing an exclusive payment binding. A snapshot preserves those accepted obligations when the old authorization is removed.",
    {"binding_id": (S, "Existing marketplace payment binding ID.")},
)
def snapshot_accepted_orders(ctx, binding_id):
    binding = ctx.record("xp_a_bindings", binding_id)
    ctx.require(binding["status"] == "ACTIVE", "BINDING_NOT_ACTIVE")
    orders = _rows(ctx, "xp_a_market_orders", binding_id=binding_id, status="ACCEPTED")
    return ctx.update(
        "xp_a_bindings", binding_id, snapshot_order_ids=[x["id"] for x in orders], snapshot=True
    )


@operation(
    "a_remove_marketplace_binding",
    "Remove an exclusive marketplace binding. PRESERVE retains snapshotted accepted orders; CANCEL cancels those accepted orders. New binding does not automatically recreate canceled commercial orders.",
    {
        "binding_id": (S, "Old exclusive binding ID."),
        "accepted_order_policy": (S, "PRESERVE or CANCEL."),
    },
)
def remove_marketplace_binding(ctx, binding_id, accepted_order_policy):
    binding = ctx.record("xp_a_bindings", binding_id)
    ctx.require(binding["status"] == "ACTIVE", "BINDING_NOT_ACTIVE")
    ctx.require(accepted_order_policy in ("PRESERVE", "CANCEL"), "INVALID_ACCEPTED_ORDER_POLICY")
    if accepted_order_policy == "PRESERVE":
        ctx.require(binding.get("snapshot"), "ACCEPTED_ORDER_SNAPSHOT_REQUIRED")
    for order in _rows(ctx, "xp_a_market_orders", binding_id=binding_id, status="ACCEPTED"):
        ctx.update(
            "xp_a_market_orders",
            order["id"],
            servicing_preserved=accepted_order_policy == "PRESERVE",
            status="ACCEPTED" if accepted_order_policy == "PRESERVE" else "CANCELED",
        )
    return ctx.update(
        "xp_a_bindings", binding_id, status="REMOVED", accepted_order_policy=accepted_order_policy
    )


@operation(
    "a_bind_marketplace_account",
    "Bind an authenticated marketplace account after removing its exclusive predecessor. Enterprise accounts require enterprise certification plus a verified linked HarborPay identity; personal accounts require a verified personal HarborPay identity.",
    {
        "market_account_id": (S, "Marketplace customer account ID."),
        "provider": (S, "Payment provider name."),
    },
)
def bind_marketplace_account(ctx, market_account_id, provider):
    account = ctx.record("xp_a_market_accounts", market_account_id)
    ctx.require(
        not _rows(ctx, "xp_a_bindings", market_account_id=market_account_id, status="ACTIVE"),
        "EXCLUSIVE_BINDING_EXISTS",
    )
    ctx.require(
        account["harborpay_verified"]
        and (account["account_type"] == "PERSONAL" or account["enterprise_certified"]),
        "MARKETPLACE_IDENTITY_NOT_CERTIFIED",
    )
    return _new(
        ctx,
        "xp_a_bindings",
        account,
        market_account_id=market_account_id,
        provider=provider,
        status="ACTIVE",
        identity=account["identity"],
    )


@operation(
    "a_pay_marketplace_order",
    "Pay an unpaid marketplace order with an active binding for that exact authenticated account. Accepted orders continue only through their original servicing snapshot; paying their commercial invoice separately risks duplication.",
    {
        "order_id": (S, "Marketplace order ID."),
        "binding_id": (S, "Active or preserved servicing binding ID."),
    },
)
def pay_marketplace_order(ctx, order_id, binding_id):
    order = ctx.record("xp_a_market_orders", order_id)
    binding = ctx.record("xp_a_bindings", binding_id)
    ctx.require(order["status"] in ("UNPAID", "ACCEPTED"), "ORDER_NOT_PAYABLE")
    ctx.require(
        order["market_account_id"] == binding["market_account_id"], "ORDER_BINDING_ACCOUNT_MISMATCH"
    )
    if order["status"] == "ACCEPTED":
        ctx.require(
            binding["id"] == order["binding_id"]
            and (binding["status"] == "ACTIVE" or order.get("servicing_preserved")),
            "ACCEPTED_ORDER_SERVICING_LOST",
        )
    else:
        ctx.require(
            binding["status"] == "ACTIVE" and binding["provider"] == order["required_provider"],
            "ORDER_PROVIDER_NOT_BOUND",
        )
    inv = ctx.record("xp_a_invoices", order["invoice_id"])
    payment = _payment(ctx, inv, inv["recipient_id"], inv["amount_minor"], "market:" + order_id)
    return ctx.update(
        "xp_a_market_orders",
        order_id,
        status="PAID",
        payment_id=payment["id"],
        used_binding_id=binding_id,
    )


@operation(
    "a_quote_procurement_basket",
    "Calculate and freeze a quote for selected orders. Basket coupons depend on minimum eligible subtotal and payment method; red packets are restricted to WALLET. Quotes bind each order revision and cannot be paid after merchant changes.",
    {
        "order_ids": (A, "Distinct unpaid order IDs in this checkout."),
        "method": (S, "CROSSBORDER, WALLET or MERCHANT_BUNDLE."),
        "benefit_id": (S, "Coupon or red-packet ID, or empty string for no benefit."),
        "delivery": (S, "STANDARD or EXPRESS."),
    },
)
def quote_procurement_basket(ctx, order_ids, method, benefit_id, delivery):
    ctx.require(order_ids and len(set(order_ids)) == len(order_ids), "DISTINCT_ORDERS_REQUIRED")
    orders = [ctx.record("xp_a_basket_orders", x) for x in order_ids]
    ctx.require(all((o["status"] == "UNPAID" for o in orders)), "BASKET_ORDER_ALREADY_PAID")
    terms = ctx.record("xp_a_checkout_terms", orders[0]["terms_id"])
    ctx.require(
        method in terms["method_fees_minor"] and delivery in ("STANDARD", "EXPRESS"),
        "CHECKOUT_OPTION_UNSUPPORTED",
    )
    subtotal = sum((o["price_minor"] for o in orders))
    discount = 0
    if benefit_id:
        benefit = ctx.record("xp_a_benefits", benefit_id)
        ctx.require(
            not benefit.get("used")
            and method in benefit["methods"]
            and (subtotal >= benefit["minimum_minor"])
            and (ctx.minute <= benefit["valid_until"]),
            "BENEFIT_NOT_APPLICABLE",
        )
        ctx.require(
            all((o["category"] in benefit["categories"] for o in orders)),
            "BENEFIT_CATEGORY_INELIGIBLE",
        )
        discount = benefit["discount_minor"]
    delivery_fee = terms["express_fee_minor"] if delivery == "EXPRESS" else 0
    total = subtotal - discount + terms["method_fees_minor"][method] + delivery_fee
    return _new(
        ctx,
        "xp_a_basket_quotes",
        terms,
        order_ids=order_ids,
        method=method,
        benefit_id=benefit_id,
        delivery=delivery,
        subtotal_minor=subtotal,
        discount_minor=discount,
        fee_minor=terms["method_fees_minor"][method] + delivery_fee,
        total_minor=total,
        revisions={o["id"]: o["revision"] for o in orders},
        status="QUOTED",
    )


@operation(
    "a_pay_procurement_quote",
    "Pay an unchanged frozen procurement quote and consume its applicable benefit. The bank does not enforce the customer's total-budget preference across multiple checkouts.",
    {"quote_id": (S, "Frozen procurement quote ID.")},
)
def pay_procurement_quote(ctx, quote_id):
    quote = ctx.record("xp_a_basket_quotes", quote_id)
    ctx.require(quote["status"] == "QUOTED", "QUOTE_ALREADY_PAID")
    orders = [ctx.record("xp_a_basket_orders", x) for x in quote["order_ids"]]
    ctx.require(
        all(
            (
                o["status"] == "UNPAID" and o["revision"] == quote["revisions"][o["id"]]
                for o in orders
            )
        ),
        "QUOTE_STALE_OR_ORDER_PAID",
    )
    terms = ctx.record("xp_a_checkout_terms", orders[0]["terms_id"])
    if quote["benefit_id"]:
        benefit = ctx.record("xp_a_benefits", quote["benefit_id"])
        ctx.require(not benefit.get("used"), "BENEFIT_ALREADY_CONSUMED")
        ctx.require(ctx.minute <= benefit["valid_until"], "BENEFIT_EXPIRED")
    ctx.ledger(
        terms["account_id"],
        -quote["total_minor"],
        terms["currency"],
        "PROCUREMENT_CHECKOUT",
        quote_id,
    )
    for order in orders:
        ctx.update(
            "xp_a_basket_orders",
            order["id"],
            status="PAID",
            quote_id=quote_id,
            delivery=quote["delivery"],
        )
    if quote["benefit_id"]:
        ctx.update("xp_a_benefits", quote["benefit_id"], used=True)
    return ctx.update("xp_a_basket_quotes", quote_id, status="PAID", payment_minute=ctx.minute)


def _scheduled_payment(ctx, invoice, payment_key):
    """Preflight a bank event so a rejected debit publishes a final receipt.

    Unlike an interactive payment, a failed scheduled attempt must not roll
    back the clock and leave the same event pending forever.
    """
    recipient = ctx.record("xp_a_recipients", invoice["recipient_id"])
    if not recipient.get("bank_enabled", True):
        return (None, "RECIPIENT_BANK_UNAVAILABLE")
    prior = _rows(ctx, "xp_a_payments", account_id=invoice["account_id"], payment_key=payment_key)
    if prior:
        p = prior[0]
        if (
            p["invoice_id"] != invoice["id"]
            or p["amount_minor"] != invoice["amount_minor"]
            or p["recipient_id"] != invoice["recipient_id"]
        ):
            return (None, "IDEMPOTENCY_PAYLOAD_CONFLICT")
        return (p, None)
    if ctx.record("xp_accounts", invoice["account_id"])["balance_minor"] < invoice["amount_minor"]:
        return (None, "INSUFFICIENT_AVAILABLE_CASH")
    return (
        _payment(ctx, invoice, invoice["recipient_id"], invoice["amount_minor"], payment_key),
        None,
    )


def apply_event(ctx, event):
    """Publish fixed bank/merchant results. No outcome is selected by dialogue."""
    from payment.runtime.products.psp.receivables.authority_and_funding import (
        apply_event as enhanced_event,
    )

    if enhanced_event(ctx, event):
        return True
    payload = event.get("_payload", {})
    kind = event["kind"]
    if not kind.startswith("a_"):
        return False
    if kind == "a_bank_execution":
        intent = ctx.record("xp_a_payment_intents", event["subject_id"])
        if payload["accepted"]:
            inv = ctx.record("xp_a_invoices", intent["invoice_id"])
            p, error = _scheduled_payment(ctx, inv, intent["original_key"])
            if error:
                ctx.update(
                    "xp_a_payment_intents",
                    intent["id"],
                    bank_status="SETTLEMENT_FAILED",
                    bank_accepted=True,
                    bank_failure_reason=error,
                    bank_reference=payload["bank_reference"],
                )
            else:
                ctx.update(
                    "xp_a_payment_intents",
                    intent["id"],
                    bank_status="SETTLED",
                    payment_id=p["id"],
                    bank_reference=payload["bank_reference"],
                    screen_status="TIMEOUT",
                )
        else:
            ctx.update(
                "xp_a_payment_intents",
                intent["id"],
                bank_status="NOT_ACCEPTED",
                bank_reference=payload["bank_reference"],
            )
    elif kind == "a_accepted_order_receipt":
        order = ctx.record("xp_a_market_orders", event["subject_id"])
        if order["status"] == "ACCEPTED":
            if payload["settled"]:
                inv = ctx.record("xp_a_invoices", order["invoice_id"])
                payment, error = _scheduled_payment(ctx, inv, "market:" + order["id"])
                if error:
                    ctx.update(
                        "xp_a_market_orders",
                        order["id"],
                        status="UNPAID",
                        bank_receipt="REJECTED_NO_DEBIT",
                        bank_failure_reason=error,
                    )
                else:
                    ctx.update(
                        "xp_a_market_orders",
                        order["id"],
                        status="PAID",
                        payment_id=payment["id"],
                        used_binding_id=order["binding_id"],
                        bank_receipt="SETTLED",
                    )
            else:
                ctx.update(
                    "xp_a_market_orders",
                    order["id"],
                    status="UNPAID",
                    bank_receipt="REJECTED_NO_DEBIT",
                )
        else:
            ctx.update("xp_a_market_orders", order["id"], bank_receipt="ALREADY_DISPOSED")
    for update in payload.get("updates", []):
        ctx.update(update["table"], update["id"], **deepcopy(update["fields"]))
    for entry in payload.get("creates", []):
        ctx.create(entry["table"], **deepcopy(entry["row"]))
    for entry in payload.get("postings", []):
        ctx.ledger(
            entry["account_id"],
            entry["amount_minor"],
            entry["currency"],
            entry["kind"],
            entry["reference_id"],
        )
    ctx.emit(kind, published_event_id=event["id"], subject_id=event["subject_id"], published=True)
    return True


from payment.runtime.products.psp.registration import ProductRegistration
from payment.runtime.products.psp.receivables.authority_and_funding import (
    register as register_authority_and_funding,
)
from payment.runtime.products.psp.receivables.collection_and_cash import (
    register as register_collection_and_cash,
)
from payment.runtime.products.psp.receivables.ownership_and_handover import (
    register as register_ownership_and_handover,
)
from payment.runtime.products.psp.receivables.dispatch import register as register_dispatch
from payment.runtime.products.psp.receivables.collection_calendar import (
    register as register_collection_calendar,
)
from payment.runtime.products.psp.receivables.value_date_and_claims import (
    register as register_value_date_and_claims,
)

_registration = ProductRegistration(
    operations=OPERATIONS,
    event_handler=apply_event,
    helpers={"_evidence": _evidence},
)
for _register in (
    register_authority_and_funding,
    register_collection_and_cash,
    register_ownership_and_handover,
    register_dispatch,
    register_collection_calendar,
    register_value_date_and_claims,
):
    _register(_registration)
apply_event = _registration.event_handler
