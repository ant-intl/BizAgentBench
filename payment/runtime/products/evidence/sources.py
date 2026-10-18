"""Business source records. IDs link storage; commercial references link evidence.

Internal economic truth remains available to transitions, never to participants.
There is no customer-visible resolver or task-specific capability.
"""

from hashlib import sha256

HIDDEN = {
    "remittances": {
        "eligible_invoice_ids",
        "allocation_lines",
        "pending_revision",
        "current_advice_id",
    },
    "remittance_advice": {"allocation_lines"},
    "rail_profiles": {"entity_regions", "markets", "categories", "rails", "new_accounts"},
    "treasury_obligations": {"commercial_debtor"},
    "restricted_lots": {"purposes"},
    "loss_components": {"loss_minor"},
    "compensation_entries": {"component_id"},
    "reserve_routes": {"conditions", "assessment_conditions", "release_terms", "assessment_terms"},
}


def ref(row):
    return (
        row.get("commercial_reference")
        or row.get("reference")
        or "DOC-" + sha256(row["id"].encode()).hexdigest()[:10].upper()
    )


def advice_body(row, world):
    rem = world["remittances"][row["remittance_id"]]
    return dict(
        remittance_reference=ref(rem),
        revision=row["revision"],
        status=row["status"],
        designations=[
            dict(
                purchase_order=world["trade_invoices"][iid]["purchase_order"],
                buyer_name=world["trade_invoices"][iid]["buyer_name"],
                invoice_month="2026-09",
                amount_minor=n,
            )
            for iid, n in row.get("allocation_lines", {}).items()
        ],
    )
