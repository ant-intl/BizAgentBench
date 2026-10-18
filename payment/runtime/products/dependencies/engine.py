"""Native business effects plus read-only cross-process contract records."""

from copy import deepcopy


from payment.runtime.products.dependencies.records import TABLES


class WorkflowLinks:

    def search_records(self, resource, filters=None, cursor=0, limit=20):
        if resource not in TABLES:
            return self.next_handler(WorkflowLinks).search_records(resource, filters, cursor, limit)
        self.check(0 < limit <= 100 and cursor >= 0, "INVALID_PAGE")
        self.check(not any(key.startswith("_") for key in (filters or {})), "FIELD_NOT_PUBLIC")
        rows = [
            self.public(row, resource)
            for row in self.w.get(resource, {}).values()
            if row["scope"] == self.w["_session_scope"]
        ]
        for key, value in (filters or {}).items():
            rows = [row for row in rows if row.get(key) == value]
        rows.sort(key=lambda row: row["id"])
        fields = (
            "id",
            "name",
            "reference",
            "status",
            "operator_id",
            "operator_category",
            "primary_case_name",
            "secondary_case_name",
        )
        return dict(
            records=[
                {key: row[key] for key in fields if key in row}
                for row in rows[cursor : cursor + limit]
            ],
            total=len(rows),
            next_cursor=cursor + limit if cursor + limit < len(rows) else None,
        )

    def get_record(self, resource, record_id):
        if resource not in TABLES:
            return self.next_handler(WorkflowLinks).get_record(resource, record_id)
        row = self.row(resource, record_id)
        self.check(row["scope"] == self.w["_session_scope"], "ACCESS_DENIED")
        return self.public(row, resource)
