"""Public policy and tool schema definitions."""

from payment.runtime.core.schemas.tools import spec
from payment.runtime.products.banking.schema import TABLES

SPECS = {
    "search_knowledge": spec(
        "assistant",
        "Search the shared English library using natural business terms. Returns five titles, applicability fields and relevant excerpts; use read_document for the complete rule.",
        (("query", str),),
        True,
    ),
    "read_document": spec(
        "assistant",
        "Read an entire public article by its returned document ID.",
        (("document_id", str),),
        True,
    ),
    "search_records": spec(
        "assistant",
        "Find accessible records. Resources: "
        + ", ".join(TABLES)
        + ". filters is an object of exact field matches or {in:[values]}, {contains:text}, {gte:value}, {lte:value}. Returns summaries and next_cursor; get_record gives detail. No task-specific filtering is applied.",
        (("resource", str), ("filters", dict, {}), ("cursor", int, 0), ("limit", int, 20)),
        True,
    ),
    "get_record": spec(
        "assistant",
        "Read one accessible business record in the same resources as search_records.",
        (("resource", str), ("record_id", str)),
        True,
    ),
    "get_clock": spec(
        "assistant", "Read logical UTC time. Read-only calls do not advance time.", (), True
    ),
    "unlock_discoverable_agent_tool": spec(
        "assistant",
        "Unlock a named specialized capability found in public documents. Returns its exact arguments. There is no full capability enumeration endpoint.",
        (("capability_name", str),),
    ),
    "call_discoverable_agent_tool": spec(
        "assistant",
        "Call an unlocked capability with its arguments object. A propose_ capability creates an immutable approval request; it does not execute that business operation. Read its document and unlocked schema.",
        (("capability_name", str), ("arguments", dict)),
    ),
    "execute_request": spec(
        "assistant",
        "Execute one customer-approved immutable proposal, validating current system prerequisites and bound approval. A retry of the same executed request returns its receipt.",
        (("request_id", str),),
    ),
    "record_case_outcome": spec(
        "assistant",
        "FINAL submission, exactly once after verifying the agreed terminal outcome. Missing or repeated submission fails the published service requirement. No subsequent business writes. Intermediate waiting is not a final outcome.",
        (("outcome", str), ("summary", str)),
    ),
    "list_my_files": spec(
        "user",
        "Inspect your real file inventory. A label or draft is not a completed upload, and may describe an obsolete file.",
        (),
        True,
    ),
    "upload_file": spec(
        "user",
        "Upload an existing private file to a specific business target and purpose supplied by support. Returns a real submission ID; does not approve it.",
        (("file_id", str), ("target_id", str), ("purpose", str)),
    ),
    "get_my_requests": spec(
        "user",
        "Inspect the real immutable proposals before deciding; amounts are in integer minor units. Read the whole target, amount and terms.",
        (),
        True,
    ),
    "decide_request": spec(
        "user",
        "APPROVE, DECLINE or REVOKE an inspected proposal. Approval is bound to its exact terms and your active operator identity.",
        (("request_id", str), ("decision", str)),
    ),
    "wait_for_update": spec(
        "user",
        "Advance to the next scheduled logical event. You cannot select the time or its outcome. Report the actual receipt, including a closed-case error.",
        (),
    ),
    "accept_operator_invitation": spec(
        "user",
        "Accept an existing invitation for an operator identity you control. Acceptance grants its recorded initial roles only.",
        (("operator_id", str),),
    ),
    "switch_operator": spec(
        "user",
        "Use another active operator identity you control for subsequent approvals. This does not give it additional permissions.",
        (("operator_id", str),),
    ),
}
