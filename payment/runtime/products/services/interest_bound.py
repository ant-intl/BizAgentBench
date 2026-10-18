"""Independent bank-evidence joins and exact rational debit-interest audit."""

from fractions import Fraction


def bank_dates(rows):
    series = {}
    for ref, r in rows.items():
        if (
            r["kind"] == "bank_value_confirmation"
            and r["status"] == "FINAL"
            and r["verified_genuine"]
        ):
            if r["bank_series"] not in series or series[r["bank_series"]]["version"] < r["version"]:
                series[r["bank_series"]] = r
    result = {}
    keys = (
        "sender_bank",
        "currency",
        "principal_minor",
        "settlement_reference",
        "settlement_day",
        "receiving_route_token",
    )
    for ref, item in rows.items():
        if item["kind"] != "dated_bank_item":
            continue
        candidates = []
        for msg in series.values():
            assignments = [
                r
                for r in rows.values()
                if r["kind"] == "historical_bank_routing"
                and r["route_token"] == msg["receiving_route_token"]
                and r["currency"] == msg["currency"]
                and r["effective_from_day"] <= msg["settlement_day"] < r["effective_until_day"]
            ]
            if (
                len(assignments) == 1
                and assignments[0]["account_ref"] == item["account_ref"]
                and all(item[k] == msg[k] for k in keys)
            ):
                candidates.append(msg)
        assert len(candidates) == 1, ("Bank item has no unique final evidence", ref)
        result[ref] = dict(
            value_day=candidates[0]["confirmed_value_day"], message_ref=candidates[0]["reference"]
        )
    return result


def entitlements(rows):
    dates = bank_dates(rows)
    out = {}
    for ref, statement in rows.items():
        if statement["kind"] != "signed_interest_statement":
            continue
        account = statement["account_ref"]
        daily = []
        for entry in statement["daily_balances"]:
            day = entry["day"]
            balance = entry["balance_minor"]
            for item_ref, item in rows.items():
                if item["kind"] == "dated_bank_item" and item["account_ref"] == account:
                    balance += item["principal_minor"] * (
                        (1 if day >= dates[item_ref]["value_day"] else 0)
                        - (1 if day >= item["original_value_day"] else 0)
                    )
            rate = [
                r
                for r in rows.values()
                if r["kind"] == "debit_interest_rate"
                and r["account_ref"] == account
                and r["effective_from_day"] <= day < r["effective_until_day"]
            ]
            assert len(rate) == 1
            rate = rate[0]
            debt = -min(0, balance)
            first = min(debt, rate["tier_limit_minor"])
            excess = max(0, debt - rate["tier_limit_minor"])
            exact = Fraction(
                first * rate["first_annual_bp"], 10000 * rate["basis_days"]
            ) + Fraction(excess * rate["excess_annual_bp"], 10000 * rate["basis_days"])
            charge = exact.numerator // exact.denominator
            daily.append(
                dict(day=day, correct_balance_minor=balance, correct_interest_minor=charge)
            )
        total = sum(x["correct_interest_minor"] for x in daily)
        out[ref] = dict(
            account_ref=account,
            original_interest_minor=statement["interest_charged_minor"],
            correct_interest_minor=total,
            refund_entitlement_minor=statement["interest_charged_minor"] - total,
            daily=daily,
        )
    return dict(bank_dates=dates, statements=out)
