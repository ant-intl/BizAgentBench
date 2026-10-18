"""Scoped incident settlement finality and version-bound delegated authority."""

from copy import deepcopy

VERSION = "b-reasoning-1"
INCIDENT = "xp_b_reasoning_incidents"
MANDATES = "xp_b_reasoning_mandates"
DELIVERIES = "xp_b_incident_supplier_deliveries"
S = {"type": "string"}
I = {"type": "integer"}
A = {"type": "array", "items": S}
OPS = {}


def context(ctx, table):
    if not any((x.get("reasoning_version") == VERSION for x in ctx.records("xp_cases"))):
        return None
    return next(
        (
            x
            for x in ctx.records(table)
            if x.get("customer_id") == ctx.customer_id and x.get("reasoning_version") == VERSION
        ),
        None,
    )


def op(name, description, params):

    def register(fn):
        OPS[name] = dict(
            description=description,
            parameters=dict(
                type="object", properties=params, required=list(params), additionalProperties=False
            ),
            read_only=False,
            function=fn,
        )
        return fn

    return register


def supplier_receipt(ctx, r, source, amount):
    return ctx.create(
        DELIVERIES,
        customer_id=r["customer_id"],
        incident_id=r["id"],
        obligation_id=r["legitimate_transfer_id"],
        supplier_reference=r["supplier_reference"],
        source=source,
        amount_minor=amount,
        delivered_minute=ctx.minute,
    )


@op(
    "send_incident_supplier_replacement",
    "Submit an independent replacement for the original supplier invoice using an active credential. regular and priority have public fees and settlement delays. An in-flight original is not cancelled by this new transfer and can still settle, creating duplicate supplier delivery. The principal is reserved until its actual delivery event.",
    {"incident_id": S, "credential_id": S, "service": S},
)
def replace(ctx, incident_id, credential_id, service):
    r = ctx.record(INCIDENT, incident_id)
    ctx.require(
        context(ctx, INCIDENT) is not None and service in r["replacement_services"],
        "INCIDENT_REPLACEMENT_SERVICE",
    )
    credential = ctx.record("xp_b_credentials", credential_id)
    ctx.require(credential["status"] == "active", "CREDENTIAL_REVOKED")
    terms = r["replacement_services"][service]
    original = ctx.record("xp_b_security_transfers", r["legitimate_transfer_id"])
    amount = original["amount_minor"]
    ctx.ledger(r["account_id"], -amount, "USD", "incident_replacement_principal", r["id"])
    ctx.ledger(r["account_id"], -terms["fee_minor"], "USD", "incident_replacement_fee", r["id"])
    instruction = ctx.create(
        "xp_b_incident_replacements",
        customer_id=r["customer_id"],
        incident_id=r["id"],
        credential_id=credential_id,
        service=service,
        amount_minor=amount,
        fee_minor=terms["fee_minor"],
        submitted_minute=ctx.minute,
        status="accepted",
    )
    if terms["delay"] == 0:
        receipt = supplier_receipt(ctx, r, instruction["id"], amount)
        instruction.update(status="settled", receipt_id=receipt["id"], settled_minute=ctx.minute)
    else:
        ctx.schedule(
            "b_reasoning_incident_replacement", ctx.minute + terms["delay"], instruction["id"]
        )
    return instruction


def register(operations, old_event):
    operations.update(OPS)

    def wrap(name, fn):
        previous = operations[name]["function"]
        operations[name]["function"] = lambda ctx, _old=previous, _fn=fn, **kw: _fn(ctx, _old, kw)

    def resolve(ctx, old, k):
        r = context(ctx, INCIDENT)
        if r and k["transfer_id"] == r["legitimate_transfer_id"] and (k["action"] == "execute"):
            ctx.require(
                r.get("original_final_status") == "rejected", "ORIGINAL_NETWORK_SETTLEMENT_PENDING"
            )
        return old(ctx, **k)

    wrap("resolve_queued_transfer", resolve)

    def callback(ctx, old, k):
        r = context(ctx, INCIDENT)
        if not r:
            return old(ctx, **k)
        h = ctx.record("xp_b_credential_continuity", k["record_id"])
        receipts = ctx.records(DELIVERIES, incident_id=r["id"])
        ctx.require(receipts and h.get("old_callback_revoked"), "SUPPLIER_CALLBACK_PENDING")
        fields = dict(
            supplier_callback_delivered=True,
            callback_recipient=h["callback_endpoints"][h["callback_endpoint_id"]],
            callback_payment_id=r["legitimate_transfer_id"],
            callback_delivery_ids=[x["id"] for x in receipts],
        )
        ctx.emit("supplier_payment_callback_delivered", subject_id=h["id"], **fields)
        return ctx.update("xp_b_credential_continuity", h["id"], **fields)

    wrap("deliver_supplier_payment_callback", callback)

    def officer(ctx, old, k):
        r = context(ctx, MANDATES)
        if not r:
            return old(ctx, **k)
        h = ctx.record("xp_b_delegate_countersigns", k["record_id"])
        g = ctx.record("xp_b_delegate_grants", r["grant_id"])
        choice = k["choice"]
        ctx.require(choice in h["officer_channels"], "OFFICER_CHANNEL")
        mode = r["officer_modes"].get(choice, "current")
        revision = g["authority_revision"] if mode == "current" else r["application_revision"]
        delay = r["officer_delays"][mode]
        h.update(countersign_requested=True, officer_channel=choice, officer_acknowledged=False)
        ctx.schedule(
            "b_enhance_officer_countersign",
            ctx.minute + delay,
            h["id"],
            table="xp_b_delegate_countersigns",
            fields=dict(
                officer_acknowledged=True,
                acknowledged_business_id=h["officer_channels"][choice],
                acknowledged_limit_minor=h["_acknowledged_limit_minor"],
                acknowledged_authority_revision=revision,
                acknowledged_minute=ctx.minute + delay,
            ),
        )
        return dict(
            status="pending",
            business_id=h["officer_channels"][choice],
            authority_revision=revision,
            mode=mode,
            due_minute=ctx.minute + delay,
        )

    wrap("request_company_officer_countersign", officer)

    def delegated(ctx, old, k):
        r = context(ctx, MANDATES)
        if not r:
            return old(ctx, **k)
        g = ctx.record("xp_b_delegate_grants", k["grant_id"])
        h = ctx.record("xp_b_delegate_countersigns", r["countersign_id"])
        result = old(ctx, **k)
        snapshot = dict(
            payment_authority_revision=g["authority_revision"],
            payment_mandate_revision=h.get("acknowledged_authority_revision"),
            payment_grant_business_id=g["business_id"],
            payment_mandate_business_id=h.get("acknowledged_business_id"),
        )
        result.update(snapshot)
        ctx.emit("delegate_payment_revision_used", business_id=k["business_id"], **snapshot)
        return result

    wrap("execute_delegated_payment", delegated)
    deliver = operations["deliver_delegate_data"]["function"]

    def plain_delivery(ctx, **k):
        d = deliver(ctx, **k)
        r = context(ctx, MANDATES)
        if r:
            g = ctx.record("xp_b_delegate_grants", k["grant_id"])
            d.update(
                authority_revision=g.get("authority_revision", 1),
                effective_revision_at_delivery=g.get("authority_revision", 1),
                columns_at_delivery=(
                    deepcopy(r["transaction_catalog_columns"])
                    if "transactions" in k["field_names"]
                    else []
                ),
                permitted_columns_at_delivery=deepcopy(r.get("permitted_transaction_columns", [])),
            )
        return d

    operations["deliver_delegate_data"]["function"] = plain_delivery

    def projected(ctx, grant_id, business_id, column_names, authority_revision):
        r = context(ctx, MANDATES)
        ctx.require(
            r is not None and set(column_names) <= set(r["transaction_catalog_columns"]),
            "UNKNOWN_TRANSACTION_COLUMN",
        )
        d = deliver(ctx, grant_id=grant_id, business_id=business_id, field_names=["transactions"])
        g = ctx.record("xp_b_delegate_grants", grant_id)
        d.update(
            authority_revision=authority_revision,
            effective_revision_at_delivery=g.get("authority_revision", 1),
            columns_at_delivery=deepcopy(column_names),
            permitted_columns_at_delivery=deepcopy(r.get("permitted_transaction_columns", [])),
        )
        return d

    operations["deliver_projected_delegate_transactions"] = dict(
        description="Permanently deliver selected transaction columns for one company, stamped with an explicit authority_revision. This is a transport operation and permits any catalog columns; company, effective grant revision, column authorization and customer minimum disclosure remain the caller responsibility. A later grant change does not authorize earlier disclosure.",
        parameters=dict(
            type="object",
            properties=dict(grant_id=S, business_id=S, column_names=A, authority_revision=I),
            required=["grant_id", "business_id", "column_names", "authority_revision"],
            additionalProperties=False,
        ),
        read_only=False,
        function=projected,
    )

    def event(ctx, e):
        if e["kind"] == "b_reasoning_incident_original":
            r = ctx.record(INCIDENT, e["subject_id"])
            t = ctx.record("xp_b_security_transfers", r["legitimate_transfer_id"])
            if t["status"] == "cancelled":
                r["original_final_status"] = "cancelled"
                return True
            result = r["_original_result"]
            r["original_final_status"] = result
            r["original_final_minute"] = ctx.minute
            if result == "settled":
                amount = t["amount_minor"]
                balance = ctx.record("xp_accounts", r["account_id"])["balance_minor"]
                if balance < amount:
                    t.update(status="rejected", bank_result="insufficient_cash")
                    r["original_final_status"] = "rejected"
                else:
                    ctx.ledger(r["account_id"], -amount, "USD", "security_transfer", t["id"])
                    t.update(status="executed", resolved_minute=ctx.minute)
                    supplier_receipt(ctx, r, t["id"], amount)
            else:
                t.update(status="rejected", resolved_minute=ctx.minute)
            return True
        if e["kind"] == "b_reasoning_suspect_settlement":
            r = ctx.record(INCIDENT, e["subject_id"])
            for ident in r["suspect_transfer_ids"]:
                t = ctx.record("xp_b_security_transfers", ident)
                cred = ctx.record("xp_b_credentials", t["credential_id"])
                if t["status"] == "accepted" or (
                    t["status"] == "queued" and cred["status"] == "active"
                ):
                    if (
                        ctx.record("xp_accounts", t["account_id"])["balance_minor"]
                        >= t["amount_minor"]
                    ):
                        ctx.ledger(
                            t["account_id"], -t["amount_minor"], "USD", "security_transfer", t["id"]
                        )
                        t.update(status="executed", resolved_minute=ctx.minute)
                    else:
                        t.update(
                            status="rejected",
                            bank_result="insufficient_cash",
                            resolved_minute=ctx.minute,
                        )
            return True
        if e["kind"] == "b_reasoning_incident_replacement":
            x = ctx.record("xp_b_incident_replacements", e["subject_id"])
            r = ctx.record(INCIDENT, x["incident_id"])
            d = supplier_receipt(ctx, r, x["id"], x["amount_minor"])
            x.update(status="settled", settled_minute=ctx.minute, receipt_id=d["id"])
            return True
        result = old_event(ctx, e)
        if e["kind"] == "b_delegate_review":
            r = context(ctx, MANDATES)
            if r and e["subject_id"] == r["grant_id"]:
                g = ctx.record("xp_b_delegate_grants", r["grant_id"])
                g["authority_revision"] = r["_effective_revision"]
                r.update(
                    permitted_transaction_columns=deepcopy(r["_approved_columns"]),
                    review_received=True,
                    review_minute=ctx.minute,
                )
        return result

    return event
