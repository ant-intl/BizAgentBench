"""Scoped customs-award installments and versioned single-payment evidence."""

from copy import deepcopy

CUSTOMS_DISBURSEMENTS = "xp_b_reasoning_customs_disbursements"
PAYMENT_PROOF_VERSIONS = "xp_b_reasoning_proof_versions"
PAYOUTS = "xp_b_reasoning_customs_payouts"
SEALS = "xp_b_reasoning_proof_seals"
OPS = {}


def context(ctx, collection):
    return next(
        (
            r
            for r in ctx.records(collection)
            if r["customer_id"] == ctx.customer_id and r.get("reasoning_version") == "b-reasoning-1"
        ),
        None,
    )


def op(name, desc, fields, fn):
    OPS[name] = dict(
        description=desc,
        parameters=dict(
            type="object",
            properties={k: {"type": t} for (k, t) in fields.items()},
            required=list(fields),
            additionalProperties=False,
        ),
        read_only=False,
        function=fn,
    )


def customs_disbursement(ctx, schedule_id):
    r = ctx.record(CUSTOMS_DISBURSEMENTS, schedule_id)
    ctx.require(context(ctx, CUSTOMS_DISBURSEMENTS) is not None, "CUSTOMS_DISBURSEMENT_SCOPE")
    return r


def charge(ctx, r, amount, kind, ref):
    if amount:
        ctx.ledger(r["account_id"], -amount, "USD", kind, ref)


def availability(ctx, r):
    lien = ctx.record("xp_b_customs_liens", r["lien_id"])
    enc = max(0, lien.get("lien_minor", r["_original_lien_minor"]) - lien.get("release_minor", 0))
    total = 0
    for did, account in r["receiving_accounts"].items():
        d = ctx.record("xp_b_destinations", did)
        hold = enc if did == r.get("lien_destination_id") else 0
        cash = ctx.record("xp_accounts", account)["balance_minor"]
        ctx.update(
            "xp_b_destinations", did, encumbered_minor=hold, available_minor=max(0, cash - hold)
        )
        total += max(0, cash - hold)
    if total >= r["early_available_minor"] and r.get("early_satisfied_minute") is None:
        ctx.update(CUSTOMS_DISBURSEMENTS, r["id"], early_satisfied_minute=ctx.minute)
    ctx.update(CUSTOMS_DISBURSEMENTS, r["id"], total_available_minor=total)


def review_classification(ctx, schedule_id, entry_id, channel):
    r = customs_disbursement(ctx, schedule_id)
    imp = ctx.record("xp_b_importers", r["importer_id"])
    entry = ctx.record("xp_b_customs_entries", entry_id)
    ctx.require(
        imp.get("award_status") in ("reviewed", "received", "payment_pending")
        and entry_id == r["amendment_entry_id"]
        and (entry.get("review_status") == "reviewed")
        and (entry["importer_id"] == imp["id"])
        and (channel in r["review_channels"])
        and (not r.get("classification_requested")),
        "CLASSIFICATION_REVIEW_STATE",
    )
    docs = ctx.uploaded(entry_id, "classification_amendment")
    ctx.require(
        any(
            (
                isinstance(d["content"], dict)
                and d["content"].get("document_type") == "classification_amendment"
                and (d["content"].get("importer") == imp["legal_name"])
                and (d["content"].get("entry_id") == entry_id)
                for d in docs
            )
        ),
        "CLASSIFICATION_EVIDENCE_REQUIRED",
    )
    s = r["review_channels"][channel]
    charge(ctx, r, s["fee_minor"], "customs_classification_review_fee", entry_id)
    ctx.update(CUSTOMS_DISBURSEMENTS, r["id"], classification_requested=True, classification_channel=channel)
    ctx.schedule("b_reasoning_customs_classification", ctx.minute + s["delay_minutes"], r["id"])
    return {"status": "under_review", "entry_id": entry_id}


def select_payout(ctx, schedule_id, channel):
    r = customs_disbursement(ctx, schedule_id)
    ctx.require(channel in r["receiving_channels"], "CUSTOMS_CHANNEL")
    s = r["receiving_channels"][channel]
    d = ctx.record("xp_b_destinations", s["destination_id"])
    imp = ctx.record("xp_b_importers", r["importer_id"])
    ctx.require(d["owner"] == imp["legal_name"] and d.get("ach"), "IMPORTER_ACH_REQUIRED")
    ctx.update("xp_b_importers", imp["id"], destination_id=d["id"])
    return ctx.update(CUSTOMS_DISBURSEMENTS, r["id"], selected_channel=channel)


op(
    "submit_customs_classification_review",
    "Submit the existing classification correction for a reviewed entry owned by the importer. Requires the genuine classification_amendment for that entry; pays the published review-channel fee. Review can confirm additional eligible duty, including zero; filing is not approval.",
    {"schedule_id": "string", "entry_id": "string", "channel": "string"},
    review_classification,
)
op(
    "select_customs_disbursement_channel",
    "Select a published importer-owned ACH disbursement channel for the next confirmed award balance. Channel fee and cash delay apply when accepting that payout. Original and supplemental amounts can use different published channels; already accepted payouts cannot be recalled.",
    {"schedule_id": "string", "channel": "string"},
    select_payout,
)


def reserve_seal(ctx, schedule_id, product_id):
    r = ctx.record(PAYMENT_PROOF_VERSIONS, schedule_id)
    ctx.require(context(ctx, PAYMENT_PROOF_VERSIONS) and product_id in r["seal_products"], "PROOF_SEAL_PRODUCT")
    s = r["seal_products"][product_id]
    charge(ctx, r, s["fee_minor"], "proof_seal_license", product_id)
    return ctx.create(
        SEALS,
        customer_id=r["customer_id"],
        schedule_id=schedule_id,
        product_id=product_id,
        version=r["version"],
        flexible=s["flexible"],
        ready=True,
        purchased_minute=ctx.minute,
    )


def migrate_seal(ctx, seal_id, channel):
    seal = ctx.record(SEALS, seal_id)
    r = ctx.record(PAYMENT_PROOF_VERSIONS, seal["schedule_id"])
    ctx.require(
        seal["ready"]
        and (not seal["flexible"])
        and (channel in r["migration_channels"])
        and (seal["version"] != r["version"]),
        "PROOF_SEAL_MIGRATION_STATE",
    )
    s = r["migration_channels"][channel]
    charge(ctx, r, s["fee_minor"], "proof_seal_migration", seal_id)
    ctx.update(SEALS, seal_id, ready=False, target_version=r["version"])
    ctx.schedule("b_reasoning_proof_seal_migrated", ctx.minute + s["delay_minutes"], seal_id)
    return {"status": "migrating", "target_version": r["version"]}


def select_render(ctx, schedule_id, seal_id, rendering_id):
    r = ctx.record(PAYMENT_PROOF_VERSIONS, schedule_id)
    seal = ctx.record(SEALS, seal_id)
    ctx.require(
        context(ctx, PAYMENT_PROOF_VERSIONS)
        and seal["schedule_id"] == schedule_id
        and seal["ready"]
        and (rendering_id in r["renderings"]),
        "PROOF_RENDERING_STATE",
    )
    return ctx.update(
        PAYMENT_PROOF_VERSIONS, schedule_id, selected_seal_id=seal_id, selected_rendering_id=rendering_id
    )


op(
    "purchase_payment_proof_seal",
    "Purchase a bank proof sealing license for this one original payment. A fixed-version license requires paid migration when the bank advice version changes; a flexible license follows that payment’s current version. This fee does not create or deliver a document.",
    {"schedule_id": "string", "product_id": "string"},
    reserve_seal,
)
op(
    "migrate_payment_proof_seal",
    "Migrate a fixed bank proof license to the currently published advice version. The service fee is spent now; the license becomes ready only on its migration completion event. Existing issued copies and past disclosure remain unchanged.",
    {"seal_id": "string", "channel": "string"},
    migrate_seal,
)
op(
    "select_payment_proof_rendering",
    "Select an owned ready sealing license and a published rendering for subsequent evidence creation. Rendering descriptions include supporting source fields and transaction attachments. Selecting or correcting a rendering does not itself disclose information.",
    {"schedule_id": "string", "seal_id": "string", "rendering_id": "string"},
    select_render,
)


def register(operations, old_event):
    operations.update(OPS)
    old_accept = operations["accept_customs_award"]["function"]
    old_apply = operations["apply_customs_encumbrance_release"]["function"]
    old_statement = operations["deliver_customs_availability_statement"]["function"]
    old_create = operations["create_bank_evidence"]["function"]

    def accept(ctx, importer_id, decision, amount_minor):
        r = context(ctx, CUSTOMS_DISBURSEMENTS)
        if not r or r["importer_id"] != importer_id:
            return old_accept(
                ctx, importer_id=importer_id, decision=decision, amount_minor=amount_minor
            )
        imp = ctx.record("xp_b_importers", importer_id)
        ctx.require(
            imp.get("award_status") in ("reviewed", "received", "payment_pending")
            and amount_minor == r.get("approved_minor", 0) - r.get("committed_minor", 0)
            and (amount_minor > 0),
            "OUTSTANDING_CUSTOMS_AWARD",
        )
        ctx.require(decision in ("accept", "withdraw"), "AWARD_DECISION")
        if decision == "withdraw":
            ctx.update(
                CUSTOMS_DISBURSEMENTS,
                r["id"],
                withdrawn_minor=r.get("withdrawn_minor", 0) + amount_minor,
                committed_minor=r.get("committed_minor", 0) + amount_minor,
            )
            return ctx.update("xp_b_importers", importer_id, award_status="withdrawn")
        ctx.require(r.get("selected_channel"), "CUSTOMS_PAYOUT_CHANNEL")
        channel = r["receiving_channels"][r["selected_channel"]]
        ctx.require(
            amount_minor <= channel["max_principal_minor"], "CUSTOMS_CHANNEL_PRINCIPAL_LIMIT"
        )
        supplement = r.get("committed_minor", 0) > 0
        charge(ctx, r, channel["fee_minor"], "customs_payout_fee", r["selected_channel"])
        due = (
            ctx.minute
            + channel["supplemental_delay_minutes" if supplement else "initial_delay_minutes"]
        )
        payout = ctx.create(
            PAYOUTS,
            customer_id=r["customer_id"],
            schedule_id=r["id"],
            destination_id=channel["destination_id"],
            channel=r["selected_channel"],
            amount_minor=amount_minor,
            supplemental=supplement,
            accepted_minute=ctx.minute,
            due_minute=due,
            status="accepted",
        )
        ctx.update(CUSTOMS_DISBURSEMENTS, r["id"], committed_minor=r.get("committed_minor", 0) + amount_minor)
        ctx.update("xp_b_importers", importer_id, award_status="payment_pending")
        ctx.schedule("b_reasoning_customs_cash", due, payout["id"])
        return payout

    def apply(ctx, **args):
        out = old_apply(ctx, **args)
        r = context(ctx, CUSTOMS_DISBURSEMENTS)
        if r:
            availability(ctx, r)
        return out

    def statement(ctx, **args):
        out = old_statement(ctx, **args)
        r = context(ctx, CUSTOMS_DISBURSEMENTS)
        if r:
            ctx.update(
                CUSTOMS_DISBURSEMENTS,
                r["id"],
                statement_minute=ctx.minute,
                statement_available_minor=r["total_available_minor"],
                statement_received_minor=sum(
                    (
                        x["amount_minor"]
                        for x in ctx.records(PAYOUTS, schedule_id=r["id"])
                        if x["status"] == "received"
                    )
                ),
            )
        return out

    def create(ctx, evidence_request_id, kind, field_names, format):
        r = context(ctx, PAYMENT_PROOF_VERSIONS)
        if not r or r["request_id"] != evidence_request_id:
            return old_create(
                ctx,
                evidence_request_id=evidence_request_id,
                kind=kind,
                field_names=field_names,
                format=format,
            )
        ctx.require(
            r.get("selected_seal_id") and r.get("selected_rendering_id"), "PROOF_SEAL_AND_RENDERING"
        )
        seal = ctx.record(SEALS, r["selected_seal_id"])
        render = r["renderings"][r["selected_rendering_id"]]
        version = r["version"] if seal["flexible"] else seal["version"]
        ctx.require(
            seal["ready"] and (render.get("archived") or version == r["version"]),
            "PROOF_LICENSE_VERSION",
        )
        ctx.require(
            not {"net_received", "transfer_fee"} & set(field_names)
            or (r.get("advice_final") and version == r["version"]),
            "PAYMENT_ADVICE_PENDING",
        )
        out = old_create(
            ctx,
            evidence_request_id=evidence_request_id,
            kind=kind,
            field_names=field_names,
            format=format,
        )
        request = ctx.record("xp_b_evidence_requests", evidence_request_id)
        transactions = (
            request["all_transaction_ids"]
            if render["source_scope"] == "ACCOUNT"
            else out["transaction_ids"]
        )
        fields = sorted(set(field_names) | set(render["source_fields"]))
        values = {
            k: r[k + "_minor"]
            for k in ("net_received", "transfer_fee")
            if k in field_names and r.get("advice_final")
        }
        return ctx.update(
            "xp_b_bank_evidence",
            out["id"],
            proof_version=version,
            seal_id=seal["id"],
            rendering_id=r["selected_rendering_id"],
            effective_fields=fields,
            effective_transaction_ids=transactions,
            derived_values=values,
            created_minute=ctx.minute,
        )

    operations["accept_customs_award"]["function"] = accept
    operations["apply_customs_encumbrance_release"]["function"] = apply
    operations["deliver_customs_availability_statement"]["function"] = statement
    operations["create_bank_evidence"]["function"] = create

    def event(ctx, e):
        kind = e["kind"]
        sid = e["subject_id"]
        if kind == "b_reasoning_customs_classification":
            r = ctx.record(CUSTOMS_DISBURSEMENTS, sid)
            amount = r["_additional_award_minor"]
            ctx.update(
                CUSTOMS_DISBURSEMENTS,
                sid,
                classification_final=True,
                additional_award_minor=amount,
                approved_minor=r.get("approved_minor", 0) + amount,
            )
            ctx.update(
                "xp_b_importers",
                r["importer_id"],
                award_minor=r["approved_minor"],
                award_status=(
                    "reviewed"
                    if r["approved_minor"] > r.get("committed_minor", 0)
                    else ctx.record("xp_b_importers", r["importer_id"])["award_status"]
                ),
            )
            return
        if kind == "b_reasoning_customs_cash":
            p = ctx.record(PAYOUTS, sid)
            r = ctx.record(CUSTOMS_DISBURSEMENTS, p["schedule_id"])
            d = ctx.record("xp_b_destinations", p["destination_id"])
            ctx.ledger(
                r["receiving_accounts"][d["id"]],
                p["amount_minor"],
                "USD",
                "customs_award_received",
                p["id"],
            )
            ctx.update(
                "xp_b_destinations",
                d["id"],
                received_minor=d.get("received_minor", 0) + p["amount_minor"],
            )
            ctx.update(PAYOUTS, sid, status="received", received_minute=ctx.minute)
            if not r.get("lien_destination_id"):
                ctx.update(CUSTOMS_DISBURSEMENTS, r["id"], lien_destination_id=d["id"])
            availability(ctx, r)
            pending = any(
                (x["status"] == "accepted" for x in ctx.records(PAYOUTS, schedule_id=r["id"]))
            )
            ctx.update(
                "xp_b_importers",
                r["importer_id"],
                award_status="payment_pending" if pending else "received",
            )
            return
        if kind == "b_reasoning_proof_seal_migrated":
            seal = ctx.record(SEALS, sid)
            ctx.update(
                SEALS, sid, ready=True, version=seal["target_version"], migrated_minute=ctx.minute
            )
            return
        out = old_event(ctx, e)
        if kind == "b_customs_review":
            r = context(ctx, CUSTOMS_DISBURSEMENTS)
            if r and r["importer_id"] == sid:
                ctx.update(
                    CUSTOMS_DISBURSEMENTS,
                    r["id"],
                    approved_minor=ctx.record("xp_b_importers", sid)["award_minor"],
                )
        if kind == "b_enhance_proof_recipient":
            r = context(ctx, PAYMENT_PROOF_VERSIONS)
            if r and r["access_id"] == sid:
                ctx.update(
                    PAYMENT_PROOF_VERSIONS,
                    r["id"],
                    version=r["_final_version"],
                    advice_final=True,
                    net_received_minor=r["_net_received_minor"],
                    transfer_fee_minor=r["_transfer_fee_minor"],
                )
        return out

    return event
