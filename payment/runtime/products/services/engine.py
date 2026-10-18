"""Business execution only; no reference strategy or acceptance imports."""

from copy import deepcopy
from functools import partial


from payment.runtime.products.services.specification import CAPABILITIES, NEW
from payment.runtime.products.services.mechanisms import ENTRIES, BY_KIND, BY_TOOL, BY_EVENT


class BusinessServices:
    def __getattr__(self, name):
        if name in BY_TOOL:
            return partial(self.service_instruction, BY_TOOL[name].kind)
        return self.next_handler(BusinessServices).dynamic_attribute(name)

    def call_discoverable_agent_tool(self, capability_name, arguments):
        if capability_name not in NEW:
            return self.next_handler(BusinessServices).call_discoverable_agent_tool(
                capability_name, arguments
            )
        self.check(capability_name in self.w["unlocked"], "TOOL_NOT_UNLOCKED")
        self.check(
            set(arguments) == {"case_id", "action", "data"}
            and type(arguments["case_id"]) is str
            and type(arguments["action"]) is str
            and type(arguments["data"]) is dict,
            "INVALID_CAPABILITY_ARGUMENTS",
        )
        self.row("service_cases", arguments["case_id"])
        return self.public(
            self.new(
                "requests",
                operation=capability_name,
                arguments=deepcopy(arguments),
                status="PENDING",
                prepared_minute=self.w["minute"],
            )
        )

    def dated_eligible(self, a, loan):
        return (
            a["owner"] == loan["borrower"]
            and a["currency"] == loan["currency"]
            and a["security_class"] in loan["accepted_classes"]
            and a["status"] == "SETTLED"
            and a["eligible_from"] <= self.w["minute"] <= a["eligible_through"]
        )

    def dated_cover(self, c, loan, excluded=None):
        buckets = {}
        for a in self.records(c, "asset"):
            if (
                a["id"] != excluded
                and a["pledged_to"] == loan["reference"]
                and self.dated_eligible(a, loan)
            ):
                issuer = a.get("issuer", a["id"])
                buckets[issuer] = (
                    buckets.get(issuer, 0) + a["face_minor"] * (10000 - a["haircut_bp"]) // 10000
                )
        return sum(
            min(n, loan.get("issuer_caps_minor", {}).get(issuer, n))
            for issuer, n in buckets.items()
        )

    def service_instruction(self, kind, case_id, action, data):
        mechanism = BY_KIND.get(kind)
        if mechanism and mechanism.operation_method:
            return getattr(self, mechanism.operation_method)(case_id, action, data)
        if kind != "dated_collateral":
            return self.next_handler(BusinessServices).service_instruction(
                kind, case_id, action, data
            )
        c = self.row("service_cases", case_id)
        self.check(c["kind"] == kind, "SERVICE_CONTRACT_MISMATCH")
        cash = c["cash_minor"]
        fees = c["fees_minor"]
        if action == "repay":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "loan")
            n = data["amount_minor"]
            p = self.correspondence(c)
            self.positive(n)
            self.check(
                n % p["repayment_increment_minor"] == 0 and n <= r["debt_minor"],
                "INVALID_REPAYMENT",
            )
            self.cash(c, -n)
            if not r.get("repayment_fee_paid"):
                self.charge(c, r["repayment_fee_minor"])
                r["repayment_fee_paid"] = True
            r["debt_minor"] -= n
        elif action == "pledge":
            self.args(data, "record_id", "loan_id")
            a = self.local(c, data["record_id"], "asset")
            r = self.local(c, data["loan_id"], "loan")
            self.check(
                a["pledged_to"] is None and not a.get("delivered") and self.dated_eligible(a, r),
                "COLLATERAL_UNAVAILABLE_OR_OUTSIDE_PRODUCT_TERMS",
            )
            a["pledged_to"] = r["reference"]
        elif action == "release":
            self.args(data, "record_id")
            a = self.local(c, data["record_id"], "asset")
            self.check(a["pledged_to"] is not None, "ASSET_NOT_PLEDGED")
            r = self.byref(c, a["pledged_to"])
            self.check(
                self.dated_cover(c, r, excluded=a["id"]) >= r["debt_minor"],
                "INSUFFICIENT_REMAINING_COLLATERAL",
            )
            a["pledged_to"] = None
            if a.get("delivery_order"):
                a.update(delivered=True, delivered_minute=self.w["minute"])
        elif action == "pay_bill":
            self.args(data, "record_id", "amount_minor")
            r = self.local(c, data["record_id"], "bill")
            self.positive(data["amount_minor"])
            self.cash(c, -data["amount_minor"])
            r["paid_minor"] += data["amount_minor"]
        else:
            self.check(False, "UNKNOWN_ACTION")
        return self.book(
            c,
            action,
            contract=kind,
            arguments=deepcopy(data),
            cash_delta_minor=c["cash_minor"] - cash,
            fee_minor=c["fees_minor"] - fees,
            cash_after_minor=c["cash_minor"],
        )

    def apply_event(self, event):
        mechanism = BY_EVENT.get(event["kind"])
        if mechanism and mechanism.event_method:
            return getattr(self, mechanism.event_method)(event)
        if event["kind"] != "dated_collateral_event":
            return self.next_handler(BusinessServices).apply_event(event)
        c = self.w["service_cases"][event["target_id"]]
        p = self.w["_event_payloads"][event["id"]]
        cash = c["cash_minor"]
        out = {}
        if p["event_kind"] == "custody_settlement":
            a = self.byref(c, p["reference"])
            a.update(status="SETTLED", face_minor=a["_settled_value_minor"])
            out = dict(record_id=a["id"], settled_value_minor=a["face_minor"])
        elif p["event_kind"] == "automatic_debit":
            paid = c["cash_minor"] >= p["amount_minor"]
            if paid:
                self.cash(c, -p["amount_minor"])
            out = dict(amount_minor=p["amount_minor"], paid=paid)
        elif p["event_kind"] != "marker":
            self.check(False, "UNKNOWN_BANK_EVENT")
        self.book(
            c,
            "bank_event",
            event_kind=p["event_kind"],
            event_id=event["id"],
            cash_delta_minor=c["cash_minor"] - cash,
            cash_after_minor=c["cash_minor"],
            receipt=out,
        )
