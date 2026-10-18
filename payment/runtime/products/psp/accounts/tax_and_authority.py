"""Tax-agent handover and recovery execution authority."""

from copy import deepcopy

TAX_HANDOVERS = "xp_b_tax_handovers"
RECOVERY_HANDOFFS = "xp_b_recovery_handoffs"
OPS = {}
S = {"type": "string"}
I = {"type": "integer", "minimum": 0}


def op(name, description, params):

    def wrap(fn):
        OPS[name] = {
            "description": description,
            "parameters": {
                "type": "object",
                "properties": params,
                "required": list(params),
                "additionalProperties": False,
            },
            "read_only": False,
            "function": fn,
        }
        return fn

    return wrap


def first(ctx, table):
    rows = ctx.records(table)
    return rows[0] if rows else None


def fee(ctx, r, amount, kind):
    if amount:
        ctx.ledger(r["account_id"], -amount, "USD", kind, r["id"])


def tax_clearance_facts(world, handover):
    """The facts attested by a tax handover statement at its issue time."""
    owner = handover["customer_id"]
    obligations = world.get("xp_b_tax_obligations", {})
    identifiers = handover["original_liabilities"]
    return dict(
        census_received=bool(handover.get("census_received")),
        settlement_published=bool(handover.get("settlement_published")),
        remaining_scope=deepcopy(handover["active_jurisdictions"]),
        obligations={
            key: {
                field: obligations[key].get(field)
                for field in ("amount_minor", "paid", "discharged", "recipient_id", "paid_minute")
            }
            for key in identifiers
        },
        receipt_allocations=sorted(
            (
                [x["receipt_id"], x["obligation_id"], x["amount_minor"]]
                for x in world.get("xp_b_tax_receipt_allocations", {}).values()
                if x.get("customer_id") == owner
            )
        ),
        instruction_statuses={
            key: row["status"]
            for (key, row) in world.get("xp_b_retiring_tax_instructions", {}).items()
            if row.get("handover_id") == handover["id"]
        },
    )


@op(
    "request_retiring_tax_agent_census",
    "Ask the retiring tax agent for bank-accepted versus queued instructions. The delayed bank census reports actual acceptance and the immutable authority receipt allocation.",
    {"handover_id": S},
)
def census(ctx, handover_id):
    r = ctx.record(TAX_HANDOVERS, handover_id)
    ctx.require(not r.get("census_requested"), "CENSUS_ALREADY_REQUESTED")
    ctx.update(TAX_HANDOVERS, r["id"], census_requested=True)
    ctx.schedule("b_revision_tax_census", ctx.minute + r["census_delay"], r["id"])
    return {"status": "awaiting_bank_census"}


@op(
    "cancel_unaccepted_tax_instruction",
    "Cancel a retiring-agent instruction that is not yet bank accepted. An accepted instruction must settle and cannot be cancelled here. instruction_id is from the census.",
    {"handover_id": S, "instruction_id": S},
)
def cancel_tax(ctx, handover_id, instruction_id):
    r = ctx.record(TAX_HANDOVERS, handover_id)
    x = ctx.record("xp_b_retiring_tax_instructions", instruction_id)
    ctx.require(
        r.get("census_received") and x["status"] == "queued", "TAX_INSTRUCTION_ALREADY_ACCEPTED"
    )
    ctx.update(
        "xp_b_retiring_tax_instructions", x["id"], status="cancelled", cancelled_minute=ctx.minute
    )
    return {"instruction_id": x["id"], "status": "cancelled"}


@op(
    "end_retiring_tax_agent_mandate",
    "End the named jurisdiction scope of the retiring agent. Select ALL for all its listed jurisdictions. This prevents future submissions, without cancelling instructions already accepted by the bank.",
    {"handover_id": S, "jurisdiction": S},
)
def end_agent(ctx, handover_id, jurisdiction):
    r = ctx.record(TAX_HANDOVERS, handover_id)
    ctx.require(
        jurisdiction == "ALL" or jurisdiction in r["original_jurisdictions"], "UNKNOWN_JURISDICTION"
    )
    remaining = (
        [] if jurisdiction == "ALL" else [j for j in r["active_jurisdictions"] if j != jurisdiction]
    )
    return ctx.update(TAX_HANDOVERS, r["id"], active_jurisdictions=remaining)


@op(
    "withdraw_pending_tax_agent_application",
    "Withdraw the new agent application before approval. The later review response records withdrawal rather than creating authority. Existing authority recipients remain usable; the withdrawal service fee is charged once.",
    {"handover_id": S, "agent_id": S},
)
def withdraw(ctx, handover_id, agent_id):
    r = ctx.record(TAX_HANDOVERS, handover_id)
    a = ctx.record("xp_b_tax_agents", agent_id)
    ctx.require(
        agent_id == r["new_agent_id"] and a["status"] in ("new", "reviewing"),
        "APPLICATION_NOT_WITHDRAWABLE",
    )
    fee(ctx, r, r["withdrawal_fee_minor"], "tax_application_withdrawal")
    return ctx.update("xp_b_tax_agents", a["id"], status="withdrawn", configured=False)


@op(
    "allocate_settled_tax_receipt",
    "Allocate an amount of a settled retiring-agent receipt to one original tax obligation. This is a bookkeeping instruction: allocation cannot exceed unallocated cash or the unpaid item. The authority receipt jurisdiction still determines which debt the payment really discharged.",
    {"handover_id": S, "receipt_id": S, "obligation_id": S, "amount_minor": I},
)
def allocate(ctx, handover_id, receipt_id, obligation_id, amount_minor):
    r = ctx.record(TAX_HANDOVERS, handover_id)
    p = ctx.record("xp_b_tax_settlement_receipts", receipt_id)
    o = ctx.record("xp_b_tax_obligations", obligation_id)
    used = sum(
        (
            x["amount_minor"]
            for x in ctx.records("xp_b_tax_receipt_allocations")
            if x["receipt_id"] == p["id"]
        )
    )
    ctx.require(
        amount_minor > 0
        and (not o["paid"])
        and (amount_minor <= p["amount_minor"] - used)
        and (amount_minor <= o["amount_minor"]),
        "RECEIPT_ALLOCATION_AMOUNT",
    )
    q = ctx.create(
        "xp_b_tax_receipt_allocations",
        customer_id=r["customer_id"],
        receipt_id=receipt_id,
        obligation_id=obligation_id,
        amount_minor=amount_minor,
        minute=ctx.minute,
    )
    fields = {"amount_minor": o["amount_minor"] - amount_minor}
    if fields["amount_minor"] == 0:
        authority = next(
            (
                a
                for a in ctx.records("xp_b_tax_agents")
                if a["kind"] == "authority" and o["jurisdiction"] in a["jurisdictions"]
            )
        )
        fields.update(
            paid=True,
            discharged=True,
            recipient_id=authority["id"],
            paid_minute=p["received_minute"],
            fee_minor=0,
        )
    ctx.update("xp_b_tax_obligations", o["id"], **fields)
    return q


@op(
    "publish_tax_handover_clearance",
    "Publish the two original liabilities, actual predecessor cash receipts, new payments and remaining agent scopes. Publication cannot repair a wrong receipt allocation or duplicate predecessor payment.",
    {"handover_id": S},
)
def tax_close(ctx, handover_id):
    r = ctx.record(TAX_HANDOVERS, handover_id)
    snapshot = tax_clearance_facts(ctx.world, r)
    return ctx.update(
        TAX_HANDOVERS,
        r["id"],
        clearance_published=True,
        clearance_minute=ctx.minute,
        remaining_scope_at_clearance=deepcopy(r["active_jurisdictions"]),
        clearance_snapshot=snapshot,
    )


@op(
    "select_recovered_payment_executor",
    "Select an execution desk after the recovery provider publishes its actual staff availability. A desk may have preparation-only authority. Selection pays its public fee and schedules handover after its delay; bank selection itself does not certify the commercial execution mandate.",
    {"handoff_id": S, "desk_id": S},
)
def select_executor(ctx, handoff_id, desk_id):
    r = ctx.record(RECOVERY_HANDOFFS, handoff_id)
    ctx.require(r.get("staff_result_received"), "RECOVERY_STAFF_RESULT_PENDING")
    d = r["desks"].get(desk_id)
    ctx.require(d is not None, "UNKNOWN_EXECUTION_DESK")
    fee(ctx, r, d["fee_minor"], "payment_execution_handover")
    ctx.update(RECOVERY_HANDOFFS, r["id"], selected_desk_id=desk_id, executor_ready=False)
    ctx.schedule("b_revision_executor", ctx.minute + d["delay"], r["id"], desk_id=desk_id)
    return {"desk_id": desk_id, "ready_at": ctx.minute + d["delay"]}


@op(
    "migrate_recovery_payment_instruction",
    "Migrate one held original payment instruction to the current payment credential version, preserving its commercial identity and amount. An instruction for another quote remains another quote. A previously retired instruction is unavailable.",
    {"handoff_id": S, "instruction_id": S},
)
def migrate(ctx, handoff_id, instruction_id):
    r = ctx.record(RECOVERY_HANDOFFS, handoff_id)
    p = ctx.record("xp_b_access_profiles", r["profile_id"])
    x = ctx.record("xp_b_recovery_instructions", instruction_id)
    ctx.require(
        p["payment_status"] == "active" and x["status"] == "held", "RECOVERY_INSTRUCTION_STATE"
    )
    return ctx.update(
        "xp_b_recovery_instructions",
        x["id"],
        status="ready",
        credential_version=p["payment_version"],
        migrated_from_version=x["credential_version"],
        prepared_minute=ctx.minute,
    )


@op(
    "replace_recovery_payment_instruction",
    "Retire a held instruction and create a new immutable instruction for the same obligation and amount under the recovered credential. The public replacement fee is charged. It does not expand the selected executor authority.",
    {"handoff_id": S, "instruction_id": S},
)
def replace(ctx, handoff_id, instruction_id):
    r = ctx.record(RECOVERY_HANDOFFS, handoff_id)
    p = ctx.record("xp_b_access_profiles", r["profile_id"])
    x = ctx.record("xp_b_recovery_instructions", instruction_id)
    ctx.require(
        p["payment_status"] == "active" and x["status"] == "held", "RECOVERY_INSTRUCTION_STATE"
    )
    fee(ctx, r, r["replacement_fee_minor"], "recovery_instruction_replacement")
    ctx.update("xp_b_recovery_instructions", x["id"], status="retired")
    return ctx.create(
        "xp_b_recovery_instructions",
        customer_id=r["customer_id"],
        obligation_id=x["obligation_id"],
        amount_minor=x["amount_minor"],
        credential_version=p["payment_version"],
        status="ready",
        replaces_id=x["id"],
        prepared_minute=ctx.minute,
    )


@op(
    "bind_recovered_payment_instruction",
    "Bind one ready instruction to payment execution. The bank keeps its exact obligation, amount and credential version; selecting a different instruction does not amend its contents.",
    {"handoff_id": S, "instruction_id": S},
)
def bind_instruction(ctx, handoff_id, instruction_id):
    r = ctx.record(RECOVERY_HANDOFFS, handoff_id)
    x = ctx.record("xp_b_recovery_instructions", instruction_id)
    ctx.require(x["status"] == "ready", "INSTRUCTION_NOT_READY")
    return ctx.update(RECOVERY_HANDOFFS, r["id"], bound_instruction_id=x["id"])


@op(
    "complete_recovery_execution_handover",
    "Close temporary execution access after the supplier payment. This removes the selected temporary desk while preserving the restored owner payment channel and immutable used instruction.",
    {"handoff_id": S},
)
def close_handoff(ctx, handoff_id):
    r = ctx.record(RECOVERY_HANDOFFS, handoff_id)
    o = ctx.record("xp_b_secure_obligations", r["obligation_id"])
    ctx.require(o["paid"], "ORIGINAL_PAYMENT_NOT_COMPLETE")
    return ctx.update(
        RECOVERY_HANDOFFS, r["id"], handover_closed=True, executor_ready=False, closed_minute=ctx.minute
    )


def register(operations, old_event):
    for name in (
        "review_jurisdiction_tax_credit",
        "allocate_jurisdiction_tax_credit",
        "deliver_jurisdiction_tax_receipt",
        "verify_supplier_beneficiary_amendment",
        "authorize_supplier_beneficiary",
        "request_supplier_credit_receipt",
    ):
        operations.pop(name, None)
    operations.update(OPS)
    originals = {
        k: operations[k]["function"]
        for k in (
            "recover_access_channel",
            "create_payment_challenge",
            "execute_authenticated_payment",
        )
    }

    def recover(ctx, profile_id, channel):
        r = first(ctx, RECOVERY_HANDOFFS)
        if r:
            p = ctx.record("xp_b_access_profiles", profile_id)
            documents = ctx.uploaded(profile_id, "identity_recovery")
            ctx.require(
                any(
                    (
                        isinstance(s.get("content"), dict)
                        and s["content"].get("identity_revision") == r["required_identity_revision"]
                        and (s["content"].get("valid_until", -1) >= ctx.minute)
                        for s in documents
                    )
                ),
                "CURRENT_IDENTITY_COPY_REQUIRED",
            )
        return originals["recover_access_channel"](ctx, profile_id=profile_id, channel=channel)

    operations["recover_access_channel"]["function"] = recover

    def challenge(ctx, **args):
        r = first(ctx, RECOVERY_HANDOFFS)
        if r:
            x = ctx.record("xp_b_recovery_instructions", r.get("bound_instruction_id", ""))
            ctx.require(
                x["status"] == "ready" and x["obligation_id"] == args["obligation_id"],
                "PAYMENT_INSTRUCTION_BINDING",
            )
        result = originals["create_payment_challenge"](ctx, **args)
        if r:
            result = ctx.update("xp_b_payment_challenges", result["id"], instruction_id=x["id"])
        return result

    operations["create_payment_challenge"]["function"] = challenge

    def payment(ctx, **args):
        r = first(ctx, RECOVERY_HANDOFFS)
        if r:
            x = ctx.record("xp_b_recovery_instructions", r.get("bound_instruction_id", ""))
            p = ctx.record("xp_b_access_profiles", args["profile_id"])
            h = ctx.record("xp_b_payment_challenges", args["challenge_id"])
            ctx.require(
                x["status"] == "ready"
                and x["credential_version"] == p["payment_version"]
                and (x["obligation_id"] == args["obligation_id"])
                and (x["amount_minor"] == h["amount_minor"])
                and (h.get("instruction_id") == x["id"]),
                "STALE_EXECUTION_INSTRUCTION",
            )
            ctx.require(
                not r["handover_required"] or r.get("executor_ready"), "EXECUTOR_HANDOVER_PENDING"
            )
        result = originals["execute_authenticated_payment"](ctx, **args)
        if r:
            desk = deepcopy(r["desks"].get(r.get("selected_desk_id"), {}))
            ctx.update(
                "xp_b_recovery_instructions",
                x["id"],
                status="executed",
                executed_minute=ctx.minute,
                executor_snapshot=desk,
            )
            ctx.update(
                RECOVERY_HANDOFFS,
                r["id"],
                used_instruction_id=x["id"],
                execution_desk_snapshot=desk,
                execution_minute=ctx.minute,
            )
        return result

    operations["execute_authenticated_payment"]["function"] = payment

    def handle(ctx, e):
        k = e["kind"]
        sid = e["subject_id"]
        p = e.get("_payload", {})
        if k == "b_revision_tax_census":
            r = ctx.record(TAX_HANDOVERS, sid)
            ctx.update(TAX_HANDOVERS, sid, census_received=True)
            for x in ctx.records("xp_b_retiring_tax_instructions"):
                if x["handover_id"] == sid and x["status"] == "pending_census":
                    ctx.update("xp_b_retiring_tax_instructions", x["id"], status=x["_bank_status"])
        elif k == "b_revision_tax_settle":
            r = ctx.record(TAX_HANDOVERS, sid)
            for x in ctx.records("xp_b_retiring_tax_instructions"):
                if x["handover_id"] != sid or x["status"] == "cancelled":
                    continue
                if x["status"] in ("settled", "failed_insufficient_cash"):
                    continue
                cash = sum(x["authority_allocations"].values())
                if ctx.record("xp_accounts", r["account_id"])["balance_minor"] < cash:
                    ctx.update(
                        "xp_b_retiring_tax_instructions", x["id"], status="failed_insufficient_cash"
                    )
                    continue
                fee(ctx, r, cash, "retiring_tax_settlement")
                ctx.update(
                    "xp_b_retiring_tax_instructions",
                    x["id"],
                    status="settled",
                    settled_minute=ctx.minute,
                )
                for jurisdiction, amount in x["authority_allocations"].items():
                    ctx.create(
                        "xp_b_tax_settlement_receipts",
                        customer_id=r["customer_id"],
                        instruction_id=x["id"],
                        jurisdiction=jurisdiction,
                        amount_minor=amount,
                        authority_reference=x["reference"] + "/" + jurisdiction,
                        received_minute=ctx.minute,
                    )
            ctx.update(TAX_HANDOVERS, sid, settlement_published=True)
        elif k == "b_revision_executor":
            r = ctx.record(RECOVERY_HANDOFFS, sid)
            if r.get("selected_desk_id") == p["desk_id"]:
                ctx.update(RECOVERY_HANDOFFS, sid, executor_ready=True, ready_minute=ctx.minute)
        elif (
            k == "b_tax_agent"
            and first(ctx, TAX_HANDOVERS)
            and (ctx.record("xp_b_tax_agents", sid)["status"] == "withdrawn")
        ):
            ctx.update("xp_events", e["id"], outcome="application_withdrawn")
        else:
            result = old_event(ctx, e)
            if k == "b_access_recovery" and p.get("_channel") == "payment":
                r = first(ctx, RECOVERY_HANDOFFS)
                if r:
                    ctx.update(
                        RECOVERY_HANDOFFS,
                        r["id"],
                        staff_result_received=True,
                        desks=deepcopy(r["_staff_result"]),
                        recovery_result_minute=ctx.minute,
                    )
            return result
        return True

    return handle
