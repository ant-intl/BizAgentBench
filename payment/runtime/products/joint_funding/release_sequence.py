"""Reconstruct release facts from native journals, independently of the engine."""

from copy import deepcopy

from payment.runtime.products.joint_funding.milestones import booking_delta, predicates_met


def release_sequence(initial, state, seqs, condition):
    case_id = condition["case_id"]
    books = sorted(
        (
            booking
            for booking_id, booking in state["service_bookings"].items()
            if booking.get("case_id") == case_id and booking_id not in initial["service_bookings"]
        ),
        key=lambda booking: (seqs.get(booking["id"], 10**9), booking["id"]),
    )
    if condition["type"] == "actual_cash_debit":
        return min(
            (
                seqs.get(booking["id"], 10**9)
                for booking in books
                if -booking_delta(booking) >= condition["minimum_minor"]
            ),
            default=None,
        )
    if condition["type"] == "actual_service_action":
        return min(
            (
                seqs.get(booking["id"], 10**9)
                for booking in books
                if booking.get("contract") == condition["contract"]
                and booking.get("kind") in condition["actions"]
            ),
            default=None,
        )
    if condition["type"] == "executed_service_instruction":
        return min(
            (
                seqs.get(booking["id"], 10**9)
                for booking in books
                if booking.get("contract") == initial["service_cases"][case_id]["kind"]
                and booking["kind"] != "bank_event"
            ),
            default=None,
        )

    rows = {
        record["reference"]: deepcopy(record)
        for record in initial["service_records"].values()
        if record.get("case_id") == case_id
    }
    ids = {record["id"]: reference for reference, record in rows.items()}
    kind = initial["service_cases"][case_id]["kind"].removeprefix("payment_")
    if predicates_met(rows, condition):
        return 0
    for booking in books:
        data = booking.get("arguments", {})
        action = booking["kind"]
        amount = data.get("amount_minor", 0)
        event_kind = booking.get("event_kind")
        receipt = booking.get("receipt", {})
        if kind == "split_capture":
            if action == "capture":
                rows[ids[data["line_id"]]].setdefault("captures", []).append(
                    dict(amount_minor=amount, minute=booking["minute"])
                )
            elif action == "cash_payment":
                rows[ids[data["line_id"]]]["cash_paid_minor"] += amount
            elif action == "bank_event":
                if event_kind == "fulfillment":
                    rows["SECOND"].update(
                        final_minor=receipt["final_shipment_minor"], fulfilled=True
                    )
                for record in rows.values():
                    if record["kind"] == "fulfillment_line":
                        record["cleared_minor"] = sum(
                            item["amount_minor"]
                            for item in record.get("captures", [])
                            if item["minute"] < booking["minute"]
                        )
        elif kind == "cross_calendar_delivery":
            if action == "send_currency":
                rows[ids[data["invoice_id"]]]["sent_minor"] = amount
            elif event_kind == "calendar_delivery":
                for record in rows.values():
                    if record["kind"] == "calendar_invoice" and record.get("sent_minor"):
                        record.update(paid_minor=record["sent_minor"], status="SETTLED")
        elif kind == "installment_return":
            plan = rows["PLAN"]
            if action == "close_plan":
                plan["closed"] = True
            elif action == "refund":
                plan["refunded_minor"] += amount
            elif action == "pay_balance":
                plan["paid_minor"] += amount
            elif event_kind == "installment_return":
                plan["return_minor"] = receipt["accepted_return_minor"]
                if receipt["collection_status"] == "SETTLED":
                    plan["collected_minor"] += rows["PENDING"]["amount_minor"]
            elif (
                event_kind == "future_installment"
                and receipt["future_collection_status"] == "SETTLED"
            ):
                plan["collected_minor"] += rows["FUTURE"]["amount_minor"]
        elif kind == "marketplace_split_refund":
            if action == "refund_buyer":
                rows["REFUND"]["manual_paid_minor"] += amount
            elif event_kind == "returns":
                rows["A"]["accepted_return_minor"] = receipt["accepted_return_minor"]
                if receipt["refund_status"] == "SETTLED":
                    rows["REFUND"]["paid_minor"] = rows["REFUND"]["submitted_minor"]
        elif kind == "cheque_stop_and_replacement":
            if action == "replacement_payment":
                rows[ids[data["invoice_id"]]]["paid_minor"] += amount
            elif event_kind == "cheque_presentment":
                if receipt["stop_result"] != "STOPPED":
                    rows["A"]["paid_minor"] += rows["CHEQUE-A"]["amount_minor"]
                rows["B"]["paid_minor"] += receipt["packaging_cleared_minor"]
        elif kind == "group_purchase_commitment":
            if action == "commit_group":
                record = rows[ids[data["group_id"]]]
                record.update(committed=True, merchant_paid_minor=record["deposit_minor"])
            elif action == "pay_merchant":
                rows[ids[data["group_id"]]]["merchant_paid_minor"] += amount
        elif kind == "authentication_binding":
            if action == "pay_checkout":
                rows[ids[data["order_id"]]]["paid_minor"] += amount
            elif event_kind == "price_revision":
                rows["A"]["final_minor"] = rows["A"]["_final_minor"]
        elif kind == "stored_value_restitution":
            if action == "pay_order":
                rows["KEEP"]["paid_minor"] += sum(data["allocations"].values())
            elif action == "return_topup":
                rows["TOPUP"]["returned_minor"] += amount
            elif event_kind == "wallet_bank_result":
                rows["TOPUP"]["duplicate_minor"] = receipt["duplicate_minor"]
        elif kind == "acquirer_batch_close":
            if action == "post_batch":
                rows[ids[data["batch_id"]]]["posted"] = True
            elif action == "pay_merchant":
                rows[ids[data["invoice_id"]]]["paid_minor"] += amount
            elif event_kind == "acquirer_final":
                rows["B2"].update(fee_minor=rows["B2"]["_fee_minor"], status="FINAL")
                rows["BANK2"].update(
                    amount_minor=receipt["actual_bank_credit_minor"], status="SETTLED"
                )
        elif kind == "subscription_proration":
            period = rows["PERIOD"]
            if action == "amend_invoice":
                period["declared_due_minor"] = amount
            elif action == "refund":
                period["refunded_minor"] += amount
            elif action == "pay_balance":
                period["additional_paid_minor"] += amount
            elif event_kind == "usage_collection":
                period["usage_units"] = receipt["usage_units"]
                rows["PENDING"]["status"] = receipt["collection_status"]
                if receipt["collection_status"] == "SETTLED":
                    period["collected_minor"] += rows["PENDING"]["amount_minor"]
            elif event_kind == "renewal" and receipt["renewal_paid"]:
                rows["RENEWAL"]["paid_minor"] = rows["RENEWAL"]["amount_minor"]
        else:
            raise ValueError("Unsupported release audit " + kind)
        if predicates_met(rows, condition):
            return seqs.get(booking["id"])
    return None
