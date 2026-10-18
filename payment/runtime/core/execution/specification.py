"""One public tool protocol, with customer actions scoped by product portal."""

from __future__ import annotations

from copy import deepcopy
from typing import Literal

from payment.runtime.products.psp.domain import SPECS as PSP_SPECS
from payment.runtime.core.operations.specification import SPECS as OPERATIONS_SPECS

ASSISTANT_DESCRIPTIONS = {
    "search_knowledge": (
        "Search the shared English Payment library with natural business terms. Returns up to five titles, "
        "applicability scopes and excerpts. Read the complete article for conditions and exact capability names."
    ),
    "read_document": (
        "Read one complete public article by document_id. PAYMENT-CATALOG routes products and services; each "
        "portal's database guide lists its accessible record collections and fields."
    ),
    "search_records": (
        "Search one public record collection in the current product portal using documented exact fields or "
        "supported operators. Returns summaries, total and next_cursor. Start from the portal entry collection "
        "named in its database guide; use returned IDs for joins and full-record reads. Cross-portal collections "
        "are unavailable."
    ),
    "get_record": (
        "Read one full public record in the current product portal by resource and record_id. Obtain record IDs "
        "from a record search or a prior business receipt; relation fields name IDs in documented collections."
    ),
    "get_clock": "Read the current logical business time. Read-only calls do not advance time.",
    "unlock_discoverable_agent_tool": (
        "Unlock an exact specialized capability name published in a business-service document. Returns its "
        "authoritative JSON Schema, units, product scope and approval requirement."
    ),
    "call_discoverable_agent_tool": (
        "Invoke an unlocked capability in the current product portal. A read capability returns facts. A write "
        "capability creates an immutable PENDING request and does not itself execute the business operation."
    ),
    "execute_request": (
        "Execute one customer-approved immutable request after rechecking current prerequisites and the bound "
        "approval. Repeating an already executed request returns its receipt without duplicating effects."
    ),
    "record_case_outcome": (
        "Record the factual terminal outcome exactly once, after verifying the agreed result. Put explanatory "
        "text in summary. This does not make unfinished work successful, and subsequent business writes are blocked."
    ),
}

CaseOutcome = Literal[
    "COMPLETED",
    "PENDING_EXTERNAL_ACTION",
    "CUSTOMER_DECLINED",
    "ESCALATED",
]

COMMON_USER_DESCRIPTIONS = {
    "list_my_files": (
        "Inspect your actual private file inventory. A label or draft is not an upload or an approved submission."
    ),
    "upload_file": (
        "Upload one existing private file to the exact business target and purpose requested by support. Returns "
        "a submission ID; uploading does not approve evidence or complete a review."
    ),
    "get_my_requests": (
        "Inspect the exact immutable requests in your current product portal before deciding. Amounts use the "
        "units shown in the request and approval binds only those terms."
    ),
    "decide_request": (
        "APPROVE, DECLINE or REVOKE one inspected open request. The decision binds only its immutable operation, "
        "arguments and current operator identity."
    ),
    "wait_for_update": (
        "Advance logical time to the next actual bank or portal event only when support explicitly asks you to "
        "wait. You cannot select the event result."
    ),
}

ASSISTANT_NAMES = tuple(ASSISTANT_DESCRIPTIONS)
COMMON_USER_NAMES = tuple(COMMON_USER_DESCRIPTIONS)
OPERATIONS_ONLY_USER_NAMES = tuple(
    name
    for name, spec in OPERATIONS_SPECS.items()
    if spec["actor"] == "user" and name not in COMMON_USER_NAMES
)


def _with_description(spec, description):
    result = deepcopy(spec)
    result["description"] = description
    return result


def for_product_scope(product_scope=None):
    """Return the fixed assistant surface and portal-valid customer actions."""
    result = {}
    for name, description in ASSISTANT_DESCRIPTIONS.items():
        # The Operations definition uses an empty mapping for an omitted
        # record filter. Both native query libraries accept that canonical
        # representation.
        result[name] = _with_description(OPERATIONS_SPECS[name], description)
    # The terminal label is a protocol field, so its legal values belong in
    # the public JSON Schema rather than being repeated across prompts and KB
    # articles.  The free-text explanation remains in ``summary``.
    result["record_case_outcome"]["params"] = (
        ("outcome", CaseOutcome),
        ("summary", str),
    )
    for name, description in COMMON_USER_DESCRIPTIONS.items():
        result[name] = _with_description(PSP_SPECS[name], description)
    if product_scope in (None, "operations_desk"):
        for name in OPERATIONS_ONLY_USER_NAMES:
            result[name] = deepcopy(OPERATIONS_SPECS[name])
    return result


# Complete registry for common validation. Environment construction calls
# ``for_product_scope`` so the customer sees only its portal-valid actions.
SPECS = for_product_scope()
