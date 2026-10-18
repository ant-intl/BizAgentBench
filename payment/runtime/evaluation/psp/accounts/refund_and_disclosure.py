from payment.runtime.evaluation.psp.accounts.checks import *

CUSTOMS_DISBURSEMENTS = "xp_b_reasoning_customs_disbursements"
PAYMENT_PROOF_VERSIONS = "xp_b_reasoning_proof_versions"
PAYOUTS = "xp_b_reasoning_customs_payouts"


def verify(w, c, base):
    n = c["number"]
    checks, add = checker(c, base, ["B-39-goal", "B-39-coupling"] if n == 39 else [])
    r = initial_record(c, CUSTOMS_DISBURSEMENTS if n == 39 else PAYMENT_PROOF_VERSIONS)
    live = w[CUSTOMS_DISBURSEMENTS if n == 39 else PAYMENT_PROOF_VERSIONS][r["id"]]
    if n == 39:
        entries = scoped(c["initial_world"], c, "xp_b_customs_entries")
        gross = (
            sum((x["duty_minor"] for x in entries if x["_eligible"])) + r["_additional_award_minor"]
        )
        payouts = scoped(w, c, PAYOUTS)
        received = [x for x in payouts if x["status"] == "received"]
        lien = initial_record(c, "xp_b_customs_liens")
        actual = w["xp_b_customs_liens"][lien["id"]]
        cert = lien["release_certificates"].get(actual.get("certificate_id"), {})
        released = (
            min(lien["_lien_minor"], cert.get("amount_minor", 0))
            if cert.get("entry_id") == lien["_lien_entry_id"] and actual.get("lien_released")
            else 0
        )
        applications = [
            q
            for q in scoped(w, c, "xp_requests")
            if q.get("operation") == "apply_customs_encumbrance_release"
            and q.get("arguments", {}).get("record_id") == lien["id"]
            and (q.get("status") == "EXECUTED")
        ]
        release_minute = min((q.get("executed_minute", 999) for q in applications), default=999)
        early = sum(
            (x["amount_minor"] for x in received if x["received_minute"] <= r["early_deadline"])
        ) - (
            0
            if released == lien["_lien_minor"] and release_minute <= r["early_deadline"]
            else lien["_lien_minor"]
        )
        total = sum(
            (w["xp_accounts"][aid]["balance_minor"] for aid in r["receiving_accounts"].values())
        )
        free = sum(
            (
                w["xp_b_destinations"][did].get("available_minor", 0)
                for did in r["receiving_accounts"]
            )
        )
        add(
            "cash_dates",
            sum((x["amount_minor"] for x in received)) == gross
            and total == gross
            and (early >= r["early_available_minor"])
            and (released == lien["_lien_minor"])
            and (release_minute <= r["final_deadline"])
            and all((x["received_minute"] <= r["final_deadline"] for x in received)),
            dict(
                gross=gross,
                early=early,
                total=total,
                release_applied_minute=release_minute,
                payouts=payouts,
            ),
            "original approved amount and actual review increase are genuinely credited item by item; The lien must be actually released before the final deadline, Only then is it fully freely available",
        )
        good = (
            live.get("classification_final")
            and live.get("additional_award_minor") == r["_additional_award_minor"]
            and (released == lien["_lien_minor"])
            and (free == gross)
            and all(
                (
                    w["xp_b_destinations"][x["destination_id"]]["owner"]
                    == w["xp_b_importers"][r["importer_id"]]["legal_name"]
                    for x in received
                )
            )
        )
        add(
            "entitlement",
            good,
            dict(free=free, release=released, review=live.get("additional_award_minor")),
            "Each original import record qualification of this company and correction assessment decision is truly attributed, Original lien must be released with a full-amount certificate from the same record",
        )
        add(
            "statement",
            actual.get("statement_delivered")
            and live.get("statement_received_minor") == gross
            and (live.get("statement_available_minor") == gross),
            live.get("statement_available_minor"),
            "Final report must include the actual credited increase and freely available amount, Cannot deliver old preliminary-review total",
        )
        valid = all(
            (
                x["due_minute"]
                == x["accepted_minute"]
                + r["receiving_channels"][x["channel"]][
                    "supplemental_delay_minutes" if x["supplemental"] else "initial_delay_minutes"
                ]
                and x["received_minute"] == x["due_minute"]
                for x in received
            )
        )
        add(
            "payout_terms",
            valid,
            received,
            "Each transaction is treated as the first at execution time/Verify true value date of reissued identity and selected channel, Do not eliminate reissue constraints through re-registration",
        )
    else:
        request = initial_record(c, "xp_b_evidence_requests")
        access = initial_record(c, "xp_b_proof_access")
        documents = scoped(w, c, "xp_b_bank_evidence")
        delivered = [d for d in documents if d["delivered"]]
        allowed = set(c["acceptance"]["allowed_fields"])
        tx = set(c["acceptance"]["allowed_transactions"])
        privacy = True
        for d in delivered:
            render = r["renderings"][d["rendering_id"]]
            effective = set(d["field_names"]) | set(render["source_fields"])
            transactions = (
                set(request["all_transaction_ids"])
                if render["source_scope"] == "ACCOUNT" or d["kind"] == "statement"
                else set(d["transaction_ids"])
            )
            privacy = (
                privacy
                and effective <= allowed
                and (transactions <= tx)
                and (set(d["effective_fields"]) == effective)
                and (set(d["effective_transaction_ids"]) == transactions)
            )
        links = [
            q
            for q in scoped(w, c, "xp_requests")
            if q.get("status") == "EXECUTED"
            and q.get("operation") == "issue_scoped_bank_proof_link"
        ]
        privacy = privacy and all(
            (
                q.get("receipt", {}).get("authorized_readers") == [access["verified_contact"]]
                for q in links
            )
        )
        add(
            "source_consent",
            privacy,
            delivered,
            "Independently rebuild display fields and source attachment exposure from the original rendered product., A later corrected version or closed link cannot erase prior outbound",
        )
        preliminary = any(
            (
                d["kind"] == "receipt"
                and d["sealed"]
                and (set(request["required_fields"]) <= set(d["field_names"]))
                and (d["delivered_minute"] <= r["preliminary_deadline"])
                for d in delivered
            )
        )
        liveaccess = w["xp_b_proof_access"][access["id"]]
        linked = next((d for d in documents if d["id"] == liveaccess.get("linked_evidence_id")), {})
        read_events = [
            e
            for e in scoped(w, c, "xp_events")
            if e["kind"] == "b_enhance_proof_delivery" and e["status"] == "COMPLETED"
        ]
        read_minute = min([e.get("completed_minute", 999) for e in read_events], default=999)
        required = set(access["_required_fields"])
        final = (
            linked.get("proof_version") == r["_final_version"]
            and required <= set(linked.get("field_names", []))
            and liveaccess.get("recipient_read")
            and (read_minute <= r["final_read_deadline"])
            and (liveaccess.get("link_active") is False)
        )
        values = linked.get("derived_values", {})
        if "net_received" in required:
            final = final and values.get("net_received") == r["_net_received_minor"]
        else:
            final = (
                final
                and c["initial_world"]["xp_b_original_transactions"][request["transaction_id"]][
                    "amount_minor"
                ]
                == r["_net_received_minor"]
            )
        add(
            "proof_dates",
            preliminary and final,
            dict(preliminary=preliminary, final=final, read_minute=read_minute, proof=linked),
            "Initial version actual outbound and final current version actual read are two independent commitments, Final net amount must come from substantive review by the original bank",
        )
    return result(checks)
