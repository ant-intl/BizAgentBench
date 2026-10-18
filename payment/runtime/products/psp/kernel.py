"""Deterministic transport for new Payment products.

Business transitions and acceptance predicates are supplied by separate modules.
This kernel knows neither task answers nor reference plans. It provides scoped
records, transactional calls, immutable consent, cash journals and logical time.
"""

from copy import deepcopy
import hashlib
import json


class BusinessError(ValueError):
    pass


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def public(value):
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [public(v) for v in value]
    return deepcopy(value)


def validate(value, schema, location="arguments"):
    types = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "object": dict,
        "array": list,
        "boolean": bool,
        "null": type(None),
    }
    kind = schema.get("type")
    if isinstance(kind, list):
        if not any(
            type(value) in ((types[k],) if isinstance(types[k], type) else types[k]) for k in kind
        ):
            raise BusinessError("INVALID_TYPE:" + location)
    elif kind and type(value) not in (
        (types[kind],) if isinstance(types[kind], type) else types[kind]
    ):
        raise BusinessError("INVALID_TYPE:" + location)
    if "enum" in schema and value not in schema["enum"]:
        raise BusinessError("INVALID_ENUM:" + location)
    if type(value) in (int, float):
        if "minimum" in schema and value < schema["minimum"]:
            raise BusinessError("BELOW_MINIMUM:" + location)
        if "maximum" in schema and value > schema["maximum"]:
            raise BusinessError("ABOVE_MAXIMUM:" + location)
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if not set(schema.get("required", [])) <= value.keys():
            raise BusinessError("MISSING_ARGUMENT:" + location)
        if (
            schema.get("additionalProperties", False) is False
            and not value.keys() <= properties.keys()
        ):
            raise BusinessError("UNKNOWN_ARGUMENT:" + location)
        for key, val in value.items():
            if key in properties:
                validate(val, properties[key], location + "." + key)
            elif isinstance(schema.get("additionalProperties"), dict):
                validate(val, schema["additionalProperties"], location + "." + key)
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise BusinessError("TOO_FEW_ITEMS:" + location)
        for i, val in enumerate(value):
            if "items" in schema:
                validate(val, schema["items"], f"{location}[{i}]")


class Context:
    def __init__(self, engine, actor):
        self.engine = engine
        self.world = engine.w
        self.actor = actor

    @property
    def minute(self):
        return self.world["minute"]

    @property
    def customer_id(self):
        return self.world["_customer_id"]

    def require(self, condition, code):
        if not condition:
            raise BusinessError(code)

    def record(self, collection, record_id):
        self.require(
            isinstance(collection, str) and isinstance(record_id, str), "RECORD_KEYS_MUST_BE_TEXT"
        )
        self.require(collection.startswith("xp_"), "RESOURCE_NOT_PUBLIC")
        row = self.world.get(collection, {}).get(record_id)
        self.require(row is not None, "RECORD_NOT_FOUND")
        self.require(row.get("customer_id") in (self.customer_id, "SHARED"), "ACCESS_DENIED")
        return row

    def records(self, collection, **filters):
        return [
            r
            for r in self.world.get(collection, {}).values()
            if r.get("customer_id") in (self.customer_id, "SHARED")
            and all(r.get(k) == v for k, v in filters.items())
        ]

    def update(self, collection, record_id, **fields):
        self.require(not {"id", "customer_id"} & fields.keys(), "IDENTITY_IMMUTABLE")
        row = self.record(collection, record_id)
        row.update(deepcopy(fields))
        return row

    def create(self, collection, **fields):
        self.require(collection.startswith("xp_"), "RESOURCE_NOT_PUBLIC")
        table = self.world.setdefault(collection, {})
        row = deepcopy(fields)
        row.setdefault("customer_id", self.customer_id)
        self.require(row["customer_id"] in (self.customer_id, "SHARED"), "ACCESS_DENIED")
        if "id" not in row:
            self.world["_serial"] += 1
            row["id"] = f"{collection.upper()}-{self.world['_serial']:06d}"
        self.require(row["id"] not in table, "DUPLICATE_RECORD")
        table[row["id"]] = row
        return row

    def ledger(self, account_id, amount_minor, currency, kind, reference_id):
        self.require(type(amount_minor) is int, "AMOUNT_MUST_BE_INTEGER_MINOR_UNITS")
        account = self.record("xp_accounts", account_id)
        self.require(account["currency"] == currency, "CURRENCY_MISMATCH")
        self.require(type(account["balance_minor"]) is int, "INVALID_BALANCE")
        balance = account["balance_minor"] + amount_minor
        self.require(balance >= 0, "INSUFFICIENT_AVAILABLE_CASH")
        account["balance_minor"] = balance
        return self.create(
            "xp_journal",
            record_type="cash_posting",
            account_id=account_id,
            amount_minor=amount_minor,
            currency=currency,
            kind=kind,
            reference_id=reference_id,
            balance_after_minor=balance,
            minute=self.minute,
            request_id=self.world.get("_executing_request_id"),
            event_id=self.world.get("_executing_event_id"),
        )

    def emit(self, kind, **fields):
        return self.create(
            "xp_journal",
            record_type="business_event",
            kind=kind,
            minute=self.minute,
            request_id=self.world.get("_executing_request_id"),
            event_id=self.world.get("_executing_event_id"),
            **fields,
        )

    def uploaded(self, target_id, purpose):
        return self.records("xp_submissions", target_id=target_id, purpose=purpose)

    def schedule(self, kind, due_minute, subject_id, **hidden_payload):
        self.require(type(due_minute) is int and due_minute >= self.minute, "INVALID_EVENT_TIME")
        return self.create(
            "xp_events",
            kind=kind,
            due_minute=due_minute,
            subject_id=subject_id,
            status="SCHEDULED",
            _payload=deepcopy(hidden_payload),
        )


class Engine:
    def __init__(self, world, operations=None, documents=None, event_handlers=None):
        if operations is None or documents is None or event_handlers is None:
            from payment.runtime.products.psp.registry import load

            registry = load()
            operations = registry["operations"] if operations is None else operations
            documents = registry["documents"] if documents is None else documents
            event_handlers = registry["events"] if event_handlers is None else event_handlers
        self.operations = operations
        self.documents = (
            {d["id"]: d for d in documents} if isinstance(documents, list) else documents
        )
        self.event_handlers = event_handlers
        self.w = world
        self.w.setdefault("minute", 0)
        self.w.setdefault("audit", [])
        self.w.setdefault("unlocked", [])
        self.w.setdefault("_serial", 0)
        self.w.setdefault("_user_files", [])
        self.w.setdefault("_payment_expansion", True)
        for name in (
            "xp_accounts",
            "xp_cases",
            "xp_requests",
            "xp_submissions",
            "xp_events",
            "xp_journal",
            "xp_outcomes",
        ):
            self.w.setdefault(name, {})
        if "_customer_id" not in self.w:
            owners = {
                r["customer_id"]
                for r in self.w["xp_cases"].values()
                if r.get("customer_id") != "SHARED"
            }
            if len(owners) != 1:
                raise ValueError("Specify _customer_id for a shared database")
            self.w["_customer_id"] = owners.pop()
        self.ctx = Context(self, "assistant")

    def search_records(self, resource, filters=None, cursor=0, limit=20):
        self.ctx.require(
            resource.startswith("xp_") and isinstance(self.w.get(resource), dict),
            "RESOURCE_NOT_PUBLIC",
        )
        self.ctx.require(
            type(cursor) is int and cursor >= 0 and type(limit) is int and 1 <= limit <= 100,
            "INVALID_PAGE",
        )
        filters = filters or {}
        self.ctx.require(
            isinstance(filters, dict) and all(not k.startswith("_") for k in filters),
            "FIELD_NOT_PUBLIC",
        )
        for value in filters.values():
            if isinstance(value, dict):
                self.ctx.require(
                    set(value) <= {"in", "contains", "gte", "lte"}, "UNSUPPORTED_FILTER"
                )
                if "in" in value:
                    self.ctx.require(isinstance(value["in"], list), "IN_FILTER_REQUIRES_ARRAY")

        def match(row):
            for key, value in filters.items():
                actual = row.get(key)
                if not isinstance(value, dict):
                    if actual != value:
                        return False
                else:
                    for op, target in value.items():
                        self.ctx.require(
                            op in ("in", "contains", "gte", "lte"), "UNSUPPORTED_FILTER"
                        )
                        if op == "in" and actual not in target:
                            return False
                        if op == "contains" and str(target).lower() not in str(actual).lower():
                            return False
                        if op in ("gte", "lte"):
                            if actual is None or type(actual) is not type(target):
                                return False
                            if op == "gte" and actual < target or op == "lte" and actual > target:
                                return False
            return True

        rows = [public(r) for r in self.ctx.records(resource)]
        rows = sorted((r for r in rows if match(r)), key=lambda r: r["id"])
        summary_fields = (
            "id",
            "name",
            "label",
            "status",
            "kind",
            "business_area",
            "reference",
            "case_id",
            "account_id",
            "customer_id",
            "currency",
            "amount_minor",
            "number",
        )
        return dict(
            records=[
                {k: r[k] for k in summary_fields if k in r} for r in rows[cursor : cursor + limit]
            ],
            total=len(rows),
            next_cursor=cursor + limit if cursor + limit < len(rows) else None,
        )

    def get_record(self, resource, record_id):
        return public(self.ctx.record(resource, record_id))

    def get_clock(self):
        return {"minute": self.w["minute"], "time_basis": "logical minutes since the session began"}

    def call_discoverable_agent_tool(self, capability_name, arguments):
        self.ctx.require(capability_name in self.w["unlocked"], "TOOL_NOT_UNLOCKED")
        op = self.operations[capability_name]
        validate(arguments, op["parameters"])
        if op["read_only"]:
            before = deepcopy({k: v for k, v in self.w.items() if k != "audit"})
            receipt = op["function"](self.ctx, **arguments)
            self.ctx.require(
                before == {k: v for k, v in self.w.items() if k != "audit"},
                "READ_TOOL_MUTATED_STATE",
            )
            return public(receipt)
        return public(
            self.ctx.create(
                "xp_requests",
                operation=capability_name,
                arguments=deepcopy(arguments),
                terms_hash=fingerprint({"operation": capability_name, "arguments": arguments}),
                status="PENDING",
                prepared_minute=self.w["minute"],
            )
        )

    def list_my_files(self):
        files = self.w["_user_files"]
        if isinstance(files, dict):
            files = list(files.values())
        return [public(f) for f in files if f.get("customer_id") == self.w["_customer_id"]]

    def upload_file(self, file_id, target_id, purpose):
        self.ctx.require(isinstance(purpose, str) and bool(purpose), "PURPOSE_REQUIRED")
        files = {f["id"]: f for f in self.list_my_files()}
        self.ctx.require(file_id in files, "FILE_NOT_IN_USER_INVENTORY")
        self.ctx.require(
            any(
                target_id in table and table[target_id].get("customer_id") == self.w["_customer_id"]
                for key, table in self.w.items()
                if key.startswith("xp_") and isinstance(table, dict)
            ),
            "TARGET_NOT_FOUND",
        )
        file = files[file_id]
        return public(
            self.ctx.create(
                "xp_submissions",
                file_id=file_id,
                target_id=target_id,
                purpose=purpose,
                content=deepcopy(file.get("content", file.get("contents"))),
                label=file.get("label", ""),
                status="SUBMITTED",
                submitted_minute=self.w["minute"],
            )
        )

    def get_my_requests(self):
        return [
            public(r)
            for r in self.ctx.records("xp_requests")
            if r["status"] in ("PENDING", "APPROVED")
        ]

    def decide_request(self, request_id, decision):
        request = self.ctx.record("xp_requests", request_id)
        self.ctx.require(request["status"] in ("PENDING", "APPROVED"), "REQUEST_NOT_OPEN")
        self.ctx.require(decision in ("APPROVE", "DECLINE", "REVOKE"), "INVALID_DECISION")
        request.update(
            status={"APPROVE": "APPROVED", "DECLINE": "DECLINED", "REVOKE": "REVOKED"}[decision],
            approved_by=self.w["_customer_id"],
            approved_terms_hash=request["terms_hash"],
            approval_seq=len(self.w["audit"]) + 1,
            approval_minute=self.w["minute"],
        )
        return public(request)

    def execute_request(self, request_id):
        request = self.ctx.record("xp_requests", request_id)
        if request["status"] == "EXECUTED":
            return deepcopy(request["receipt"])
        self.ctx.require(request["status"] == "APPROVED", "CUSTOMER_APPROVAL_REQUIRED")
        self.ctx.require(
            request.get("approved_by") == self.w["_customer_id"], "CUSTOMER_APPROVAL_REQUIRED"
        )
        terms = fingerprint({"operation": request["operation"], "arguments": request["arguments"]})
        self.ctx.require(
            terms == request["terms_hash"] == request["approved_terms_hash"],
            "APPROVAL_TERMS_CHANGED",
        )
        self.w["_executing_request_id"] = request_id
        receipt = self.operations[request["operation"]]["function"](
            self.ctx, **deepcopy(request["arguments"])
        )
        self.w.pop("_executing_request_id", None)
        request.update(
            status="EXECUTED",
            executed_minute=self.w["minute"],
            executed_seq=len(self.w["audit"]) + 1,
            receipt=public(receipt),
        )
        return deepcopy(request["receipt"])

    def wait_for_update(self):
        scheduled = [r for r in self.ctx.records("xp_events") if r["status"] == "SCHEDULED"]
        if not scheduled:
            return dict(advanced=False, minute=self.w["minute"], events=[])
        due = min(r["due_minute"] for r in scheduled)
        self.ctx.require(due >= self.w["minute"], "EVENT_TIME_REGRESSION")
        self.w["minute"] = due
        completed = []
        for event in sorted(scheduled, key=lambda r: r["id"]):
            if event["due_minute"] != due:
                continue
            self.w["_executing_event_id"] = event["id"]
            handler = self.event_handlers.get(event["kind"].split("_", 1)[0])
            self.ctx.require(handler is not None, "EVENT_HANDLER_NOT_FOUND")
            handler(self.ctx, event)
            event["status"] = "COMPLETED"
            event["completed_minute"] = due
            self.w.pop("_executing_event_id", None)
            completed.append(public(event))
        return dict(advanced=True, minute=due, events=completed)

    def record_case_outcome(self, outcome, summary):
        self.ctx.require(
            outcome in ("COMPLETED", "PENDING_EXTERNAL_ACTION", "CUSTOMER_DECLINED", "ESCALATED"),
            "INVALID_OUTCOME",
        )
        self.ctx.require(isinstance(summary, str) and bool(summary.strip()), "SUMMARY_REQUIRED")
        row = self.ctx.create(
            "xp_outcomes",
            outcome=outcome,
            summary=summary,
            minute=self.w["minute"],
            submitted_seq=len(self.w["audit"]) + 1,
        )
        return public(row)
