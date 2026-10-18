from copy import deepcopy

from payment.runtime.products.evidence.sources import HIDDEN, ref, advice_body
from payment.runtime.products.payments.specification import TABLES
from payment.runtime.products.banking.schema import TABLES as CORE_TABLES


class EvidenceExecution:
    def public(self, r, resource=None):
        out = self.next_handler(EvidenceExecution).public(r, resource)
        if not isinstance(out, dict):
            return out
        table = resource
        if table is None:
            table = next((k for k in HIDDEN if r.get("id") in self.w.get(k, {})), None)
        for key in HIDDEN.get(table, ()):
            out.pop(key, None)
        if table == "remittance_advice":
            out["payer_letter"] = advice_body(r, self.w)
        if table == "remittances":
            out["advice_reference"] = ref(r)
        if table == "compensation_entries":
            out["shipment_reference"] = self.w["loss_components"][r["component_id"]][
                "delivery_reference"
            ]
        return out

    def search_records(self, resource, filters=None, cursor=0, limit=20):
        # Reject filtering on hidden derived facts, including an empty-result oracle.
        self.check(not (set(filters or {}) & HIDDEN.get(resource, set())), "FIELD_NOT_PUBLIC")
        self.check(
            resource in set(TABLES) | set(CORE_TABLES) | {"business_documents"},
            "RESOURCE_NOT_PUBLIC",
        )
        self.check(0 < limit <= 100 and cursor >= 0, "INVALID_PAGE")
        self.check(
            not any(k.startswith("_") or k == "scenario_minute" for k in (filters or {})),
            "FIELD_NOT_PUBLIC",
        )

        def matches(r):
            for k, v in (filters or {}).items():
                actual = r.get(k)
                if isinstance(v, dict):
                    for op, n in v.items():
                        self.check(op in ("in", "contains", "gte", "lte"), "UNSUPPORTED_FILTER")
                        if op == "in" and actual not in n:
                            return False
                        if op == "contains" and str(n).lower() not in str(actual).lower():
                            return False
                        if op in ("gte", "lte") and (
                            actual is None or not (actual >= n if op == "gte" else actual <= n)
                        ):
                            return False
                elif actual != v:
                    return False
            return True

        rows = [
            self.public(r, resource)
            for r in self.w[resource].values()
            if r.get("customer_access", True)
            and r.get("scope", self.w["_session_scope"]) in (self.w["_session_scope"], "ARCHIVE")
        ]
        rows = [r for r in rows if matches(r)]
        rows.sort(key=lambda r: r["id"])
        keys = (
            "id",
            "name",
            "kind",
            "reference",
            "commercial_reference",
            "status",
            "scope",
            "business_area",
            "entity_id",
            "account_id",
            "portfolio_id",
            "currency",
            "amount_minor",
            "case_id",
        )
        return dict(
            records=[{k: r[k] for k in keys if k in r} for r in rows[cursor : cursor + limit]],
            total=len(rows),
            next_cursor=cursor + limit if cursor + limit < len(rows) else None,
        )

    def get_record(self, resource, record_id):
        if resource == "business_documents":
            r = self.w[resource].get(record_id)
            self.check(r and r["scope"] == self.w["_session_scope"], "RECORD_NOT_FOUND")
            return deepcopy(r)
        return self.next_handler(EvidenceExecution).get_record(resource, record_id)

    def propose_purpose_payment(self, instruction_id, allocations):
        # Bank restrictions and legal eligibility stay enforced. Commercial
        # earmarking is a service requirement; a bank-success receipt is not proof.
        i = self.row("purpose_instructions", instruction_id)
        self.amounts(allocations)
        for lid, n in allocations.items():
            l = self.row("restricted_lots", lid)
            self.check(
                l["scope"] == i["scope"]
                and l["currency"] == i["currency"]
                and i["purpose"] in l["bank_permitted_purposes"]
                and self.lot_available(lid),
                "BANK_SOURCE_RESTRICTION",
            )
            self.check(n <= l["remaining_minor"], "SOURCE_BALANCE_INSUFFICIENT")
            l["remaining_minor"] -= n
        return self.new(
            "desk_payments",
            kind="PURPOSE",
            scope=i["scope"],
            instruction_id=instruction_id,
            allocations=deepcopy(allocations),
            amount_minor=sum(allocations.values()),
            currency=i["currency"],
            status="SETTLED",
            minute=self.w["minute"],
        )
