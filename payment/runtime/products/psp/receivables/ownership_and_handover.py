"""Receipt ownership, successor rights and payment authority handover."""

from copy import deepcopy

OPERATIONS = {}
BASE = {}
S = "string"
I = "integer"
L = "array"


def op(name, description, **fields):

    def register(fn):
        props = {
            k: dict(type=t, description=d, **{"items": {"type": "string"}} if t == L else {})
            for (k, (t, d)) in fields.items()
        }
        OPERATIONS[name] = dict(
            description=description,
            parameters=dict(
                type="object", properties=props, required=list(props), additionalProperties=False
            ),
            read_only=False,
            function=fn,
        )
        return fn

    return register


def row(ctx, t, i):
    return ctx.record("xp_a_" + t, i)


def rows(ctx, t):
    return ctx.records("xp_a_" + t)


def upd(ctx, t, i, **kw):
    return ctx.update("xp_a_" + t, i, **kw)


def linked(ctx, t, source):
    result = next((r for r in rows(ctx, t) if r["source_id"] == source), None)
    ctx.require(result is not None, "BUSINESS_RECORD_NOT_ASSOCIATED")
    return result


def post(ctx, r, amount, kind):
    if amount:
        ctx.ledger(r["account_id"], amount, "USD", kind, r["id"])


@op(
    "a_obtain_psp_net_statement",
    "Obtain the PSP settlement statement for this incoming remittance; publishes gross-sale references and provider deductions, not bank acceptance.",
    incoming_id=(S, "Incoming remittance ID."),
)
def psp_statement(ctx, incoming_id):
    r = linked(ctx, "psp_net_batches", incoming_id)
    if not r.get("statement_received"):
        for line in r["_statement"]:
            ctx.create(
                "xp_a_psp_adjustments",
                customer_id=r["customer_id"],
                batch_id=r["id"],
                **deepcopy(line),
            )
        upd(ctx, "psp_net_batches", r["id"], statement_received=True)
    return dict(
        batch_id=r["id"],
        adjustments=[x for x in rows(ctx, "psp_adjustments") if x["batch_id"] == r["id"]],
    )


@op(
    "a_allocate_psp_deduction",
    "Allocate a provider deduction to one sale, or correct its allocation before that sale is released. A supported statement line can belong to another shop or batch.",
    adjustment_id=(S, "Published statement deduction ID."),
    sale_id=(S, "Sale receiving this deduction."),
)
def allocate_psp(ctx, adjustment_id, sale_id):
    d = row(ctx, "psp_adjustments", adjustment_id)
    s = row(ctx, "sales", sale_id)
    r = row(ctx, "psp_net_batches", d["batch_id"])
    inc = row(ctx, "incoming", r["source_id"])
    ctx.require(not inc.get("claimed_order_ids"), "NETTING_ALREADY_RELEASED")
    ctx.require(s["amount_minor"] >= d["amount_minor"], "DEDUCTION_EXCEEDS_SALE")
    return upd(ctx, "psp_adjustments", d["id"], allocated_sale_id=sale_id)


@op(
    "a_fix_psp_net_release",
    "Fix the included deduction IDs before claiming accepted orders. This changes each original sale release amount to gross less its allocated deductions. Omitted deductions remain unapplied; repeated fixing is allowed before release.",
    batch_id=(S, "PSP net batch ID."),
    adjustment_ids=(L, "Distinct allocated statement line IDs to include."),
)
def fix_psp(ctx, batch_id, adjustment_ids):
    r = row(ctx, "psp_net_batches", batch_id)
    inc = row(ctx, "incoming", r["source_id"])
    ctx.require(
        r.get("statement_received") and (not inc.get("claimed_order_ids")), "NET_RELEASE_STATE"
    )
    ctx.require(len(adjustment_ids) == len(set(adjustment_ids)), "DUPLICATE_DEDUCTION")
    deductions = [row(ctx, "psp_adjustments", i) for i in adjustment_ids]
    ctx.require(
        all((d["batch_id"] == batch_id and d.get("allocated_sale_id") for d in deductions)),
        "UNALLOCATED_DEDUCTION",
    )
    for sid, gross in r["gross_sales"].items():
        amount = gross - sum(
            (d["amount_minor"] for d in deductions if d["allocated_sale_id"] == sid)
        )
        ctx.require(amount >= 0, "NEGATIVE_NET_SALE")
        upd(ctx, "sales", sid, amount_minor=amount, gross_minor=gross)
    return upd(
        ctx, "psp_net_batches", batch_id, included_adjustment_ids=adjustment_ids, net_fixed=True
    )


@op(
    "a_submit_quarantine_escrow_direction",
    "Submit the existing two-party dispute direction. The bank publishes the disputed amount and named escrow trustee; it neither credits operating cash nor pays a substitute claimant.",
    incoming_id=(S, "Quarantined original incoming ID."),
)
def escrow_direction(ctx, incoming_id):
    r = linked(ctx, "quarantine_disputes", incoming_id)
    ctx.require(
        BASE["_evidence"](ctx, incoming_id, "escrow_direction"),
        "MATCHING_ESCROW_DIRECTION_REQUIRED",
    )
    ctx.require(not r.get("review_requested"), "ESCROW_REVIEW_EXISTS")
    upd(ctx, "quarantine_disputes", r["id"], review_requested=True)
    ctx.schedule(
        "a_diversity_escrow",
        ctx.minute + 2,
        r["id"],
        table="quarantine_disputes",
        fields=deepcopy(r["_bank"]),
    )
    return {"status": "PENDING", "dispute_id": r["id"]}


@op(
    "a_partition_quarantined_funds",
    "Dispose the whole segregated principal into original-route return, trustee escrow and operating claim amounts. Every amount is nonnegative and their sum must equal principal. These bank-supported dispositions do not certify the customer is entitled to claim or waive disputed security.",
    incoming_id=(S, "Original incoming ID."),
    return_minor=(I, "Amount returned to original sender, USD minor units."),
    escrow_minor=(I, "Amount transferred to trustee account, USD minor units."),
    claim_minor=(I, "Amount credited to operating cash, USD minor units."),
    trustee_id=(S, "Directory trustee account ID."),
)
def partition(ctx, incoming_id, return_minor, escrow_minor, claim_minor, trustee_id):
    r = linked(ctx, "quarantine_disputes", incoming_id)
    inc = row(ctx, "incoming", incoming_id)
    ctx.require(
        r.get("review_complete")
        and inc.get("ownership_investigation")
        and (inc["decision_version"] > 0),
        "QUARANTINE_REVIEW_PENDING",
    )
    ctx.require(
        not r.get("disposed")
        and min(return_minor, escrow_minor, claim_minor) >= 0
        and (return_minor + escrow_minor + claim_minor == inc["amount_minor"]),
        "QUARANTINE_PARTITION",
    )
    ctx.require(trustee_id in r["trustees"], "TRUSTEE_NOT_LISTED")
    ctx.ledger(
        r["quarantine_account_id"], -inc["amount_minor"], "USD", "QUARANTINE_DISPOSAL", r["id"]
    )
    if escrow_minor:
        ctx.ledger(trustee_id, escrow_minor, "USD", "DISPUTE_ESCROW_DEPOSIT", r["id"])
    if claim_minor:
        ctx.ledger(inc["account_id"], claim_minor, "USD", "QUARANTINE_CLAIM", r["id"])
    upd(
        ctx,
        "incoming",
        incoming_id,
        status="DISPOSED",
        disposition="PARTITIONED",
        returned_to=inc["origin_recipient_id"],
    )
    return upd(
        ctx,
        "quarantine_disputes",
        r["id"],
        disposed=True,
        returned_minor=return_minor,
        escrowed_minor=escrow_minor,
        claimed_minor=claim_minor,
        paid_trustee_id=trustee_id,
    )


@op(
    "a_request_notice_alias_register",
    "Retrieve the bank alias register covering delivered notification epochs. Provider delivery references rotate independently of stable economic transactions.",
    register_id=(S, "Public alias register ID."),
)
def alias_register(ctx, register_id):
    r = row(ctx, "notice_registers", register_id)
    if not r.get("published"):
        for a in r["_aliases"]:
            ctx.create(
                "xp_a_notice_aliases",
                customer_id=r["customer_id"],
                register_id=r["id"],
                **deepcopy(a),
            )
    return upd(ctx, "notice_registers", register_id, published=True)


@op(
    "a_bind_credit_notice_alias",
    "Bind a delivered notice to a published alias record with equal currency and amount. Epoch, provider reference and remitter scope determine economic identity; equal money alone is insufficient. Binding may be corrected before ingestion.",
    notice_id=(S, "Delivered notice ID."),
    alias_id=(S, "Published alias record ID."),
)
def bind_alias(ctx, notice_id, alias_id):
    n = row(ctx, "notices", notice_id)
    a = row(ctx, "notice_aliases", alias_id)
    tx = row(ctx, "bank_transactions", a["bank_transaction_id"])
    ctx.require(
        n["status"] == "NEW" and n["amount_minor"] == tx["amount_minor"], "NOTICE_BINDING_STATE"
    )
    return upd(
        ctx,
        "notices",
        notice_id,
        bank_transaction_id=tx["id"],
        alias_id=a["id"],
        identity_bound=True,
    )


@op(
    "a_obtain_receivable_succession",
    "Obtain the registry response for an existing shop succession deed. It publishes effective scope and the legal payee account; historical shop attribution remains a separate question.",
    receipt_id=(S, "Delayed virtual receipt ID."),
    channel=(S, "registry_copy or registered_agent. Both publish the same legal record."),
)
def succession(ctx, receipt_id, channel):
    r = linked(ctx, "receivable_successions", receipt_id)
    ctx.require(channel in r["channels"], "SUCCESSION_CHANNEL")
    ctx.require(BASE["_evidence"](ctx, receipt_id, "succession_deed"), "SUCCESSION_DEED_REQUIRED")
    post(ctx, r, -r["channels"][channel], "SUCCESSION_RECORD_FEE")
    return upd(
        ctx,
        "receivable_successions",
        r["id"],
        review_complete=True,
        review_channel=channel,
        **deepcopy(r["_bank"]),
    )


@op(
    "a_bind_successor_receivable_payee",
    "Choose a bank-valid legal payee for the attributed historical receipt. The deed may transfer only certain historical claims. Binding is reversible before payout; current shop ownership alone does not transfer every older receivable.",
    receipt_id=(S, "Receipt ID."),
    payee_id=(S, "Bank payee directory key."),
)
def bind_successor(ctx, receipt_id, payee_id):
    r = linked(ctx, "receivable_successions", receipt_id)
    rec = row(ctx, "virtual_receipts", receipt_id)
    ctx.require(
        r.get("review_complete") and payee_id in r["payees"] and (not rec.get("paid_out")),
        "SUCCESSOR_BINDING_STATE",
    )
    return upd(ctx, "receivable_successions", r["id"], bound_payee_id=payee_id)


@op(
    "a_request_payment_execution_handover",
    "Transfer operational responsibility for a timed-out instruction. DRAIN leaves accepted old execution intact; CANCEL asks the bank to stop the old request. Only the bank acknowledgement determines whether cancellation succeeded.",
    intent_id=(S, "Original timed-out intent ID."),
    mode=(S, "DRAIN or CANCEL."),
)
def handover(ctx, intent_id, mode):
    r = linked(ctx, "execution_handovers", intent_id)
    ctx.require(mode in ("DRAIN", "CANCEL") and (not r.get("requested")), "HANDOVER_REQUEST_STATE")
    upd(ctx, "execution_handovers", r["id"], requested=True, mode=mode)
    ctx.schedule("a_diversity_handover", ctx.minute + 3, r["id"], mode=mode)
    return {"status": "PENDING", "handover_id": r["id"]}


@op(
    "a_adopt_payment_execution",
    "Adopt the acknowledged bank execution responsibility for a listed successor executor. This enables the selected successor; old authority requires separate revocation and already settled money is never recalled. Use the original bank key when invoking a safe resubmission.",
    handover_id=(S, "Bank handover record ID."),
    executor_id=(S, "Listed successor executor ID."),
)
def adopt(ctx, handover_id, executor_id):
    r = row(ctx, "execution_handovers", handover_id)
    ctx.require(r.get("acknowledged") and executor_id in r["executors"], "EXECUTOR_ADOPTION_STATE")
    return upd(
        ctx, "execution_handovers", r["id"], adopted_executor=executor_id, successor_active=True
    )


@op(
    "a_revoke_handover_executor",
    "Revoke future authority of a listed handover executor. Revoking the retired workstation suppresses its queued fallback, but does not cancel an already accepted old bank attempt. Revoking the successor can prevent necessary replacement execution.",
    handover_id=(S, "Handover ID."),
    executor_id=(S, "Old executor or adopted successor ID."),
)
def revoke_executor(ctx, handover_id, executor_id):
    r = row(ctx, "execution_handovers", handover_id)
    ctx.require(
        executor_id == r["old_executor_id"] or executor_id in r["executors"], "EXECUTOR_NOT_LISTED"
    )
    return upd(
        ctx,
        "execution_handovers",
        r["id"],
        **(
            {"old_executor_active": False}
            if executor_id == r["old_executor_id"]
            else {"successor_active": False}
        ),
    )


@op(
    "a_execute_transferred_payment",
    "Submit the original invoice under a listed adopted executor. The bank retains account-key idempotency; an unrelated fresh key remains a separate payment even if old execution settled.",
    intent_id=(S, "Original intent ID."),
    executor_id=(S, "Adopted executor ID."),
    payment_key=(S, "Original bank key or another proposed economic key."),
)
def execute_transferred(ctx, intent_id, executor_id, payment_key):
    r = linked(ctx, "execution_handovers", intent_id)
    ctx.require(
        executor_id == r.get("adopted_executor") and r.get("successor_active"),
        "EXECUTOR_NOT_ADOPTED",
    )
    return BASE["resubmit"](ctx, intent_id=intent_id, payment_key=payment_key)


@op(
    "a_compile_batch_line",
    "Compile one untouched source row into canonical major-unit net payment. MAJOR or MINOR selects numeric scale; NET adds sender fee, GROSS includes beneficiary fee. Each row has independent source semantics. Recompilation is allowed before successful execution.",
    file_record_id=(S, "Original uploaded export ID."),
    line_id=(S, "Source line ID."),
    units=(S, "MAJOR or MINOR."),
    fee_mode=(S, "NET or GROSS."),
    invoice_id=(S, "Original commercial obligation ID mapped to this row."),
)
def compile_line(ctx, file_record_id, line_id, units, fee_mode, invoice_id):
    from decimal import Decimal

    f = row(ctx, "exports", file_record_id)
    r = linked(ctx, "batch_schema_contracts", file_record_id)
    ctx.require(
        BASE["_evidence"](ctx, file_record_id, "payment_export"), "ORIGINAL_BATCH_FILE_REQUIRED"
    )
    ctx.require(units in ("MAJOR", "MINOR") and fee_mode in ("NET", "GROSS"), "ROW_SCHEMA")
    raw = next((x for x in r["source_rows"] if x["id"] == line_id), None)
    ctx.require(raw is not None, "SOURCE_LINE_NOT_FOUND")
    inv = row(ctx, "invoices", invoice_id)
    ctx.require(
        not any(
            (
                x["invoice_id"] == invoice_id and x.get("status") == "SETTLED"
                for x in rows(ctx, "payments")
            )
        ),
        "OBLIGATION_ALREADY_PAID",
    )
    gross = int(Decimal(raw["amount"]) * (100 if units == "MAJOR" else 1))
    net = gross - (raw["fee_minor"] if fee_mode == "GROSS" else 0)
    ctx.require(net > 0, "NONPOSITIVE_ROW_PAYMENT")
    lines = deepcopy(f["lines"])
    line = next((x for x in lines if x["id"] == line_id))
    line.update(
        invoice_id=invoice_id,
        amount=str(Decimal(net) / 100),
        fee_minor=raw["fee_minor"],
        compiled=True,
    )
    upd(ctx, "exports", file_record_id, lines=lines)
    compiled = dict(r.get("compiled_rows", {}))
    compiled[line_id] = dict(
        invoice_id=invoice_id,
        units=units,
        fee_mode=fee_mode,
        net_minor=net,
        fee_minor=raw["fee_minor"],
        source_digest=raw["source_reference"],
    )
    return upd(ctx, "batch_schema_contracts", r["id"], compiled_rows=compiled)


@op(
    "a_post_bank_receipt_to_payables",
    "Post a bank receipt into the supplier payable ledger. RETURNED or PROCESSING reopens the invoice amount; SETTLED clears it. This legacy posting interface accepts any receipt for the invoice, so causal sequence and corrections must govern selection. Reposting before payment can correct an earlier projection.",
    invoice_id=(S, "Original invoice ID."),
    receipt_id=(S, "Actual bank receipt ID."),
)
def payable_receipt(ctx, invoice_id, receipt_id):
    inv = row(ctx, "invoices", invoice_id)
    r = linked(ctx, "supplier_payables", invoice_id)
    receipt = row(ctx, "bank_receipts", receipt_id)
    ctx.require(receipt["invoice_id"] == invoice_id, "PAYABLE_RECEIPT_INVOICE")
    new = inv["amount_minor"] if receipt["status"] in ("RETURNED", "PROCESSING") else 0
    delta = new - r["open_minor"]
    ctx.create(
        "xp_a_payable_postings",
        customer_id=r["customer_id"],
        payable_id=r["id"],
        invoice_id=invoice_id,
        receipt_id=receipt_id,
        bank_sequence=receipt["bank_sequence"],
        amount_minor=delta,
        kind="BANK_CORRECTION",
    )
    return upd(
        ctx,
        "supplier_payables",
        r["id"],
        open_minor=new,
        source_receipt_id=receipt_id,
        source_sequence=receipt["bank_sequence"],
    )


@op(
    "a_deliver_supplier_payable_statement",
    "Deliver the actual supplier payable balances and their source references. The document does not alter debt or cash and cannot convert a stale processing notice into a new obligation.",
    payable_ids=(L, "Distinct supplier payable records to include."),
)
def payable_statement(ctx, payable_ids):
    ctx.require(payable_ids and len(set(payable_ids)) == len(payable_ids), "DISTINCT_PAYABLES")
    rs = [row(ctx, "supplier_payables", i) for i in payable_ids]
    return ctx.create(
        "xp_a_supplier_statements",
        customer_id=rs[0]["customer_id"],
        lines=[
            dict(
                payable_id=r["id"],
                invoice_id=r["source_id"],
                open_minor=r["open_minor"],
                source_sequence=r.get("source_sequence"),
            )
            for r in rs
        ],
        delivered=True,
    )


@op(
    "a_obtain_legacy_market_deposit",
    "Obtain the old provider statement for the existing marketplace deposit. Account migration does not move this cash or dispose its order rights. The statement names the original deposit and the order to which it belongs.",
    binding_id=(S, "Original marketplace binding ID."),
)
def legacy_deposit(ctx, binding_id):
    r = linked(ctx, "market_deposits", binding_id)
    return upd(
        ctx, "market_deposits", r["id"], statement_received=True, **deepcopy(r["_statement"])
    )


@op(
    "a_dispose_legacy_market_deposit",
    "Apply the old deposit against one unpaid order, return it as cash, or retain it for a future contract. APPLY reduces the original order invoice principal and pays that portion from the old custody account; CASH returns the same cash to operating balance. A deposit cannot be applied and refunded twice.",
    deposit_id=(S, "Confirmed old deposit ID."),
    order_id=(S, "Marketplace order ID associated with the disposition."),
    disposition=(S, "APPLY, CASH or FUTURE."),
)
def dispose_deposit(ctx, deposit_id, order_id, disposition):
    r = row(ctx, "market_deposits", deposit_id)
    o = row(ctx, "market_orders", order_id)
    inv = row(ctx, "invoices", o["invoice_id"])
    ctx.require(
        r.get("statement_received")
        and (not r.get("disposed"))
        and (disposition in ("APPLY", "CASH", "FUTURE")),
        "MARKET_DEPOSIT_DISPOSITION",
    )
    amount = r["deposit_minor"]
    if disposition == "APPLY":
        ctx.require(
            o["status"] != "PAID" and inv["amount_minor"] >= amount, "DEPOSIT_ORDER_ALREADY_PAID"
        )
        ctx.ledger(r["custody_account_id"], -amount, "USD", "MARKET_DEPOSIT_APPLIED", r["id"])
        upd(
            ctx,
            "invoices",
            inv["id"],
            amount_minor=inv["amount_minor"] - amount,
            deposit_minor=amount,
        )
    elif disposition == "CASH":
        ctx.ledger(r["custody_account_id"], -amount, "USD", "MARKET_DEPOSIT_RETURN", r["id"])
        post(ctx, r, amount, "MARKET_DEPOSIT_CASH")
    return upd(
        ctx,
        "market_deposits",
        r["id"],
        disposed=True,
        disposition=disposition,
        disposed_order_id=order_id,
        used_minor=amount,
    )


@op(
    "a_obtain_basket_price_adjustment",
    "Obtain the merchant price-adjustment advice for the same required goods. The goods stay required; the price credit reduces qualifying spend and can trigger the used promotion repayment rules.",
    terms_id=(S, "Original checkout terms ID."),
)
def price_adjustment(ctx, terms_id):
    r = linked(ctx, "basket_adjustments", terms_id)
    return upd(ctx, "basket_adjustments", r["id"], advice_received=True, **deepcopy(r["_advice"]))


@op(
    "a_quote_promotion_combination",
    "Quote a current whole basket with selected promotions. Promotions in the same exclusive_group cannot be combined; distinct groups may combine when every method/category/threshold rule holds. Refund clawback is determined from actual adjusted qualifying spend after payment.",
    order_ids=(L, "Current basket order IDs."),
    benefit_ids=(L, "Distinct selected benefit IDs."),
    method=(S, "Published checkout method."),
    delivery=(S, "STANDARD or EXPRESS."),
)
def combination(ctx, order_ids, benefit_ids, method, delivery):
    ctx.require(benefit_ids and len(benefit_ids) == len(set(benefit_ids)), "DISTINCT_PROMOTIONS")
    benefits = [row(ctx, "benefits", i) for i in benefit_ids]
    groups = [b.get("exclusive_group", b["id"]) for b in benefits]
    ctx.require(len(groups) == len(set(groups)), "PROMOTIONS_MUTUALLY_EXCLUSIVE")
    q = BASE["quote"](
        ctx, order_ids=order_ids, method=method, benefit_id=benefit_ids[0], delivery=delivery
    )
    for b in benefits[1:]:
        ctx.require(
            not b["used"]
            and method in b["methods"]
            and (q["subtotal_minor"] >= b["minimum_minor"])
            and (ctx.minute <= b["valid_until"])
            and all(
                (row(ctx, "basket_orders", oid)["category"] in b["categories"] for oid in order_ids)
            ),
            "PROMOTION_COMBINATION_INELIGIBLE",
        )
    discount = sum((b["discount_minor"] for b in benefits))
    return upd(
        ctx,
        "basket_quotes",
        q["id"],
        benefit_ids=benefit_ids,
        discount_minor=discount,
        total_minor=q["subtotal_minor"] - discount + q["fee_minor"],
    )


@op(
    "a_claim_basket_price_adjustment",
    "Dispose the merchant price adjustment after actual checkout. REFUND credits its cash and separately debits any used promotion clawback; FUTURE retains the adjustment for another order and does not produce current cash. Neither choice changes delivered goods.",
    quote_id=(S, "Actual paid basket quote ID."),
    disposition=(S, "REFUND or FUTURE."),
)
def basket_refund(ctx, quote_id, disposition):
    q = row(ctx, "basket_quotes", quote_id)
    terms = row(ctx, "checkout_terms", row(ctx, "basket_orders", q["order_ids"][0])["terms_id"])
    r = linked(ctx, "basket_adjustments", terms["id"])
    ctx.require(
        q["status"] == "PAID"
        and r.get("advice_received")
        and (not r.get("disposed"))
        and (disposition in ("REFUND", "FUTURE")),
        "PRICE_ADJUSTMENT_STATE",
    )
    refund = r["refund_minor"]
    clawback = 0
    if disposition == "REFUND":
        post(ctx, r, refund, "MERCHANT_PRICE_REFUND")
        for bid in filter(None, q.get("benefit_ids", [q["benefit_id"]])):
            b = row(ctx, "benefits", bid)
            if q["subtotal_minor"] - refund < b.get("retained_spend_floor_minor", 0):
                clawback += b["discount_minor"]
        post(ctx, r, -clawback, "PROMOTION_CLAWBACK")
    return upd(
        ctx,
        "basket_adjustments",
        r["id"],
        disposed=True,
        disposition=disposition,
        refund_paid_minor=refund if disposition == "REFUND" else 0,
        clawback_minor=clawback,
        quote_id=quote_id,
    )


def register(api):
    BASE.update(
        _evidence=api.helper("_evidence"),
        resubmit=api.operations["a_resubmit_payment_intent"]["function"],
        quote=api.operations["a_quote_procurement_basket"]["function"],
    )
    api.operations.update(OPERATIONS)

    def wrap(name, before=None, after=None):
        original = api.operations[name]["function"]

        def fn(ctx, **args):
            if before:
                before(ctx, args)
            result = original(ctx, **args)
            return after(ctx, args, result) if after else result

        api.operations[name]["function"] = fn

    def claim_before(ctx, a):
        for r in rows(ctx, "psp_net_batches"):
            if r["source_id"] == a["incoming_id"]:
                ctx.require(r.get("net_fixed"), "PSP_NET_RELEASE_NOT_FIXED")

    wrap("a_claim_incoming_orders", claim_before)

    def disposal_before(ctx, a):
        for r in rows(ctx, "quarantine_disputes"):
            if r["source_id"] == a["incoming_id"]:
                ctx.require(not r.get("disposed"), "PRINCIPAL_ALREADY_DISPOSED")

    def disposal_after(ctx, a, result):
        for r in rows(ctx, "quarantine_disputes"):
            if r["source_id"] == a["incoming_id"]:
                principal = row(ctx, "incoming", a["incoming_id"])["amount_minor"]
                ctx.ledger(
                    r["quarantine_account_id"], -principal, "USD", "QUARANTINE_DISPOSAL", r["id"]
                )
                upd(
                    ctx,
                    "quarantine_disputes",
                    r["id"],
                    disposed=True,
                    returned_minor=principal if a["disposition"] == "RETURN_ORIGIN" else 0,
                    escrowed_minor=0,
                    claimed_minor=principal if a["disposition"] == "CLAIM" else 0,
                )
        return result

    wrap("a_dispose_incoming_balance", disposal_before, disposal_after)

    def notice_before(ctx, a):
        n = row(ctx, "notices", a["notice_id"])
        if n.get("alias_required"):
            ctx.require(n.get("identity_bound"), "NOTICE_ALIAS_UNRESOLVED")

    wrap("a_ingest_credit_notice", notice_before)

    def payout_before(ctx, a):
        for r in rows(ctx, "receivable_successions"):
            if r["source_id"] == a["receipt_id"]:
                ctx.require(r.get("bound_payee_id"), "RECEIVABLE_PAYEE_UNBOUND")

    def payout_after(ctx, a, result):
        for r in rows(ctx, "receivable_successions"):
            if r["source_id"] == a["receipt_id"]:
                upd(
                    ctx,
                    "virtual_receipts",
                    a["receipt_id"],
                    paid_legal_payee_id=r["bound_payee_id"],
                )
                upd(ctx, "receivable_successions", r["id"], paid_payee_id=r["bound_payee_id"])
        return result

    wrap("a_pay_out_shop_receipt", payout_before, payout_after)

    def file_before(ctx, a):
        for r in rows(ctx, "batch_schema_contracts"):
            if r["source_id"] == a["file_record_id"]:
                ctx.require(a["line_id"] in r.get("compiled_rows", {}), "BATCH_LINE_NOT_COMPILED")

    wrap("a_execute_payment_file_line", file_before)
    original_payment = api.operations["a_pay_business_invoice"]["function"]

    def payable_payment(ctx, **a):
        linked_payables = [
            r for r in rows(ctx, "supplier_payables") if r["source_id"] == a["invoice_id"]
        ]
        inv = row(ctx, "invoices", a["invoice_id"])
        existing = any(
            (
                p["account_id"] == inv["account_id"] and p["payment_key"] == a["payment_key"]
                for p in rows(ctx, "payments")
            )
        )
        if not existing:
            for r in linked_payables:
                ctx.require(r["open_minor"] >= a["amount_minor"], "SUPPLIER_PAYABLE_NOT_REOPENED")
        result = original_payment(ctx, **a)
        if not existing:
            for r in linked_payables:
                amount = a["amount_minor"]
                upd(ctx, "supplier_payables", r["id"], open_minor=r["open_minor"] - amount)
                ctx.create(
                    "xp_a_payable_postings",
                    customer_id=r["customer_id"],
                    payable_id=r["id"],
                    invoice_id=a["invoice_id"],
                    payment_id=result.get("id"),
                    amount_minor=-amount,
                    kind="PAYMENT",
                )
        return result

    api.operations["a_pay_business_invoice"]["function"] = payable_payment

    def promotions_before(ctx, a):
        q = row(ctx, "basket_quotes", a["quote_id"])
        for bid in q.get("benefit_ids", []):
            benefit = row(ctx, "benefits", bid)
            ctx.require(not benefit["used"], "BENEFIT_ALREADY_CONSUMED")
            ctx.require(ctx.minute <= benefit["valid_until"], "BENEFIT_EXPIRED")

    def promotions_after(ctx, a, result):
        q = row(ctx, "basket_quotes", a["quote_id"])
        for bid in q.get("benefit_ids", []):
            upd(ctx, "benefits", bid, used=True)
        return result

    wrap("a_pay_procurement_quote", promotions_before, promotions_after)
    old_event = api.event_handler

    def event(ctx, e):
        if e["kind"] == "a_diversity_fallback_retry":
            r = row(ctx, "execution_handovers", e["subject_id"])
            intent = row(ctx, "payment_intents", r["source_id"])
            if r["old_executor_active"]:
                inv = row(ctx, "invoices", intent["invoice_id"])
                account = ctx.record("xp_accounts", inv["account_id"])
                if account["balance_minor"] >= inv["amount_minor"]:
                    BASE["resubmit"](
                        ctx,
                        intent_id=intent["id"],
                        payment_key="retired-fallback:" + intent["original_key"],
                    )
                    upd(ctx, "execution_handovers", r["id"], fallback_result="EXECUTED")
                else:
                    upd(
                        ctx,
                        "execution_handovers",
                        r["id"],
                        fallback_result="REJECTED_INSUFFICIENT_CASH",
                    )
            else:
                upd(ctx, "execution_handovers", r["id"], fallback_result="SUPPRESSED")
            return True
        if e["kind"] == "a_diversity_escrow":
            p = e["_payload"]
            upd(ctx, p["table"], e["subject_id"], review_complete=True, **deepcopy(p["fields"]))
            return True
        if e["kind"] == "a_diversity_handover":
            r = row(ctx, "execution_handovers", e["subject_id"])
            cancel = (
                e["_payload"]["mode"] == "CANCEL"
                and r["_cancel_accepted"]
                and (
                    row(ctx, "payment_intents", r["source_id"])["bank_status"]
                    in ("UNKNOWN", "ACCEPTED")
                )
            )
            upd(
                ctx,
                "execution_handovers",
                r["id"],
                acknowledged=True,
                cancellation_accepted=cancel,
                bank_epoch=r["initial_epoch"] + 1,
            )
            if cancel:
                upd(
                    ctx,
                    "payment_intents",
                    r["source_id"],
                    bank_status="NOT_ACCEPTED",
                    cancelled_before_execution=True,
                )
            return True
        if e["kind"] == "a_bank_execution":
            intent = row(ctx, "payment_intents", e["subject_id"])
            if intent.get("cancelled_before_execution"):
                ctx.emit("a_bank_cancelled_execution", subject_id=intent["id"], outcome="NO_DEBIT")
                return True
        return old_event(ctx, e)

    api.event_handler = event
