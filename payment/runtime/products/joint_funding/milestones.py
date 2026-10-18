"""Public settlement predicates used by source release conditions."""


def booking_delta(booking):
    values = []
    for key in ("cash_delta_minor", "usd_delta_minor", "eur_delta_minor"):
        value = booking.get(key)
        if isinstance(value, int):
            values.append(value)
    wallet = booking.get("wallet_delta_minor", {})
    if isinstance(wallet, dict):
        values.extend(value for value in wallet.values() if isinstance(value, int))
    return min(values or [0])


def predicates_met(rows, condition):
    for test in condition["all"]:
        record = rows.get(test.get("record_reference"), {})
        kind = test["type"]
        if kind == "settled_installment":
            if record.get("return_minor") is None or not record.get("closed"):
                return False
            target = (
                record["principal_minor"]
                - record["return_minor"]
                + record["earned_periods"] * record["period_fee_minor"]
            )
            if (
                record["collected_minor"] + record["paid_minor"] - record["refunded_minor"]
                != target
            ):
                return False
        elif kind == "equals":
            if record.get(test["field"]) != test["value"]:
                return False
        elif kind == "subscription_complete":
            pending = rows.get(test["pending_record"], {})
            renewal = rows.get(test["renewal_record"], {})
            usage = record.get("usage_units")
            if usage is None:
                return False
            due = (
                record["old_monthly_minor"] * record["old_days"]
                + record["new_monthly_minor"] * (record["days"] - record["old_days"])
            ) // record["days"] + usage * record["usage_unit_minor"]
            actual = (
                record["historical_paid_minor"]
                + (pending.get("amount_minor", 0) if pending.get("status") == "SETTLED" else 0)
                + record.get("additional_paid_minor", 0)
                - record.get("refunded_minor", 0)
            )
            if (
                record.get("declared_due_minor") != due
                or actual != due
                or not renewal.get("active")
                or renewal.get("paid_minor") != renewal.get("amount_minor")
            ):
                return False
        else:
            if "target_field" in test:
                if kind == "related_sum_at_least":
                    target = rows.get(test["target_record"], {}).get(test["target_field"])
                else:
                    target = record.get(test["target_field"])
            else:
                target = test["target_minor"]
            if (
                target is None
                or sum(record.get(field, 0) or 0 for field in test["sum_fields"]) < target
            ):
                return False
    return True


def satisfied(world, condition):
    case_id = condition["case_id"]
    if condition["type"] == "actual_cash_debit":
        return any(
            booking.get("case_id") == case_id
            and booking.get("minute", -1) >= 0
            and -booking_delta(booking) >= condition["minimum_minor"]
            for booking in world["service_bookings"].values()
        )
    if condition["type"] == "actual_service_action":
        return any(
            booking.get("case_id") == case_id
            and booking.get("contract") == condition["contract"]
            and booking.get("kind") in condition["actions"]
            and booking.get("minute", -1) >= 0
            for booking in world["service_bookings"].values()
        )
    if condition["type"] == "executed_service_instruction":
        return any(
            booking.get("case_id") == case_id
            and booking.get("contract") == world["service_cases"][case_id]["kind"]
            and booking.get("kind") != "bank_event"
            for booking in world["service_bookings"].values()
        )
    rows = {
        record["reference"]: record
        for record in world["service_records"].values()
        if record.get("case_id") == case_id
    }
    return predicates_met(rows, condition)
