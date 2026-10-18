"""Customer-portal tool specifications for PSP accounts."""


def s(actor, description, params=(), read=False):
    return dict(actor=actor, description=description, params=params, read=read)


SPECS = {
    "search_knowledge": s(
        "assistant",
        "Search the shared English product library with BM25. Returns up to five titles, applicability scopes and excerpts. Read full articles for conditions and capability names.",
        (("query", str),),
        True,
    ),
    "read_document": s(
        "assistant",
        "Read an entire public article by document_id. PX-COMMON describes consent/time/units; PX-DATABASE lists collections and their public fields.",
        (("document_id", str),),
        True,
    ),
    "search_records": s(
        "assistant",
        "Search one collection by exact public fields or in/contains/gte/lte filters. Start with xp_cases; the complete field directory is PX-DATABASE. Returns summaries and next_cursor, not full rows. Primary key is id; relation fields reference another row id. Only the signed-in customer and shared records are visible.",
        (("resource", str), ("filters", dict, None), ("cursor", int, 0), ("limit", int, 20)),
        True,
    ),
    "get_record": s(
        "assistant",
        "Read a full public record from resource by record_id. Obtain keys from record search or business receipts; hidden bank event payloads and private customer files are not returned.",
        (("resource", str), ("record_id", str)),
        True,
    ),
    "get_clock": s(
        "assistant",
        "Read current logical minute. Reads and request preparation do not advance time.",
        read=True,
    ),
    "unlock_discoverable_agent_tool": s(
        "assistant",
        "Unlock a published business capability by name and get its parameter schema, units and approval requirements. All episodes use the same capability catalog.",
        (("capability_name", str),),
    ),
    "call_discoverable_agent_tool": s(
        "assistant",
        "Invoke an unlocked capability. A read capability returns facts; a write capability creates an immutable PENDING proposal. It does not execute, upload private files, or grant approval.",
        (("capability_name", str), ("arguments", dict)),
    ),
    "execute_request": s(
        "assistant",
        "Execute a request actually approved by the customer with its exact operation and arguments. Current business conditions still apply. Retrying the same executed request returns its original receipt without duplicating effects.",
        (("request_id", str),),
    ),
    "record_case_outcome": s(
        "assistant",
        "Record the factual terminal outcome exactly once: COMPLETED, PENDING_EXTERNAL_ACTION, CUSTOMER_DECLINED or ESCALATED. This does not make unfinished work successful. Business writes stop after closure. Then give the customer your final reply.",
        (("outcome", str), ("summary", str)),
    ),
    "list_my_files": s(
        "user",
        "List your personal file inventory and contents. These are visible to you, not the assistant. Use them to answer natural factual questions or upload requested authentic files. Do not recite internal database keys or interpret bank rules.",
        read=True,
    ),
    "upload_file": s(
        "user",
        "Upload one existing file to the specific bank target_id with the requested purpose. Supply a genuine file_id from your inventory. Upload does not approve evidence or complete a review.",
        (("file_id", str), ("target_id", str), ("purpose", str)),
    ),
    "get_my_requests": s(
        "user",
        "Read the exact outstanding proposals in your portal before deciding whether they match your expressed goals and preferences.",
        read=True,
    ),
    "decide_request": s(
        "user",
        "APPROVE, DECLINE or REVOKE a specific open request. Your decision binds only its immutable terms, not any future operation.",
        (("request_id", str), ("decision", str)),
    ),
    "wait_for_update": s(
        "user",
        "Advance logical time to the next scheduled bank event. You cannot choose its result; inspect and report the returned status, then allow the assistant to respond. If no event exists time remains unchanged.",
    ),
}
