"""Independent evidence utilities. Deliberately imports no execution or strategy code."""

from copy import deepcopy


class Journal:
    def __init__(self, world, c):
        self.world = world
        self.c = c
        self.w = world.get("_closure_state") or world
        self.initial = c["initial_world"]
        self.cid = c["case_id"]
        self.case = deepcopy(self.initial["service_cases"][self.cid])
        self.rows = {
            r["reference"]: deepcopy(r)
            for r in self.initial["service_records"].values()
            if r.get("case_id") == self.cid
        }
        self.ids = {r["id"]: k for k, r in self.rows.items()}
        self.cash = self.case["cash_minor"]
        self.fees = self.case["fees_minor"]
        self.valid = True
        self.seen = set()
        self.checks = []
        self.books = sorted(
            (
                b
                for b in self.w["service_bookings"].values()
                if b.get("case_id") == self.cid and b["id"] not in self.initial["service_bookings"]
            ),
            key=lambda b: (b["minute"], b["id"]),
        )

    def row(self, record_id, kind):
        r = self.rows[self.ids[record_id]]
        assert r["kind"] == kind
        return r

    def require(self, condition):
        assert condition, "illegal journal transition"

    def post(self, b, receipt, delta=0, fee=0):
        self.cash += delta
        self.fees += fee
        self.valid &= (
            self.cash >= 0
            and b["receipt"] == receipt
            and b["cash_delta_minor"] == delta
            and b["cash_after_minor"] == self.cash
        )
        if b["kind"] != "bank_event":
            self.valid &= b.get("fee_minor") == fee and b.get("contract") == self.c["kind"]
        else:
            eid = b["event_id"]
            event = self.initial["events"][eid]
            self.valid &= (
                eid not in self.seen
                and event["due_minute"] == b["minute"]
                and self.initial["_event_payloads"][eid]["event_kind"] == b["event_kind"]
                and self.w["events"][eid]["status"] == "COMPLETED"
            )
            self.seen.add(eid)

    def ck(self, name, ok, zh, **evidence):
        self.checks.append(dict(name=name, passed=bool(ok), description_zh=zh, evidence=evidence))

    def finish(self):
        self.case.update(cash_minor=self.cash, fees_minor=self.fees)
        self.ck(
            "ledger.independent_reconstruction",
            self.valid and self.case == self.w["service_cases"][self.cid],
            "Independently reconstruct per-transaction funds and fees, Verify actual receipt and event identity. ",
        )
        actual = {
            r["reference"]: r
            for r in self.w["service_records"].values()
            if r.get("case_id") == self.cid
        }
        self.ck(
            "frame.records",
            self.rows == actual,
            "Independently reconcile all business records, Immutable clauses and securities and rights balances. ",
        )
        self.ck(
            "events.identity",
            all(
                (r["status"] == "COMPLETED") == (eid in self.seen)
                for eid, r in self.w["events"].items()
                if r.get("scope") == self.c["scope"]
            ),
            "Completion status must be unique, periodic actual event receipt. ",
        )
        return result(self.checks)


def result(checks):
    failed = [x["name"] for x in checks if not x["passed"]]
    return dict(success=not failed, failed=failed, checks=checks, allowed_terminals=["COMPLETED"])


def malformed(j, e):
    j.ck(
        "state.well_formed",
        False,
        "Independent books cannot be reconstructed from the public contract. ",
        error=str(e),
    )
    return result(j.checks)
