"""Restore the already-published record filter contract for coverage resources."""

from payment.runtime.products.resources.specification import RESOURCE_TABLES


class ResourceQueries:
    def search_records(self, resource, filters=None, cursor=0, limit=20):
        if resource not in RESOURCE_TABLES:
            return self.next_handler(ResourceQueries).search_records(
                resource, filters, cursor, limit
            )
        self.check(0 < limit <= 100 and cursor >= 0, "INVALID_PAGE")
        rows = []
        for r in self.w[resource].values():
            matched = True
            for k, wanted in (filters or {}).items():
                self.check(not k.startswith("_"), "FIELD_NOT_PUBLIC")
                value = r.get(k)
                if isinstance(wanted, dict):
                    for operation, arg in wanted.items():
                        self.check(
                            operation in ("in", "contains", "gte", "lte"), "UNSUPPORTED_FILTER"
                        )
                        if operation == "in":
                            matched &= value in arg
                        elif operation == "contains":
                            matched &= str(arg).lower() in str(value).lower()
                        else:
                            matched &= value is not None and (
                                value >= arg if operation == "gte" else value <= arg
                            )
                else:
                    matched &= value == wanted
            if matched:
                rows.append(self.public(r))
        rows.sort(key=lambda r: r["id"])
        keys = ("id", "reference", "status", "scope", "account_id", "name")
        return dict(
            records=[{k: r[k] for k in keys if k in r} for r in rows[cursor : cursor + limit]],
            total=len(rows),
            next_cursor=cursor + limit if cursor + limit < len(rows) else None,
        )
