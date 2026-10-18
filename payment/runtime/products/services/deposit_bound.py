"""Independent exhaustive dated-cash inequalities, without execution functions."""

from itertools import product


def maximum(rows, cash, reserve, receipt, forced=None):
    deposits = sorted(
        (r for r in rows.values() if r["kind"] == "dated_term_deposit"),
        key=lambda r: r["reference"],
    )
    tier = rows["TIER"]
    best = None
    winners = []
    checked = 0
    for dates in product(*[[None, *range(d["maturity_minute"])] for d in deposits]):
        schedule = {d["reference"]: date for d, date in zip(deposits, dates)}
        if forced and any(schedule[k] != date for k, date in forced.items()):
            continue
        checked += 1
        credits = {m: 0 for m in range(5)}
        credits[1] += receipt
        for d, date in zip(deposits, dates):
            if date is not None:
                credits[date] += d["principal_minor"] - d["break_charge_minor"]
                continue
            test_at = min(d["maturity_minute"], tier["qualification_through"])
            eligible = (
                sum(
                    x["principal_minor"]
                    for x, when in zip(deposits, dates)
                    if x["tier_group"] == tier["group"] and (when is None or when >= test_at)
                )
                >= tier["qualifying_original_principal_minor"]
            )
            credits[d["maturity_minute"]] += (
                d["principal_minor"]
                + d["base_interest_minor"]
                + (d["tier_bonus_minor"] if eligible else 0)
            )
        valid = True
        balance = cash
        for minute in range(5):
            # Scheduled automatic debits precede any voluntary early redemption
            # at this minute. A later payout cannot retrospectively fund them.
            before_instructions = (
                balance
                + credits[minute]
                - sum(
                    d["principal_minor"] - d["break_charge_minor"]
                    for d, date in zip(deposits, dates)
                    if date == minute
                )
            )
            auto = rows["RENT"]["amount_minor"] if minute == rows["RENT"]["due_minute"] else 0
            if auto:
                valid &= before_instructions - auto >= reserve
            balance += (
                credits[minute]
                - auto
                - sum(
                    x["amount_minor"]
                    for x in rows.values()
                    if x["kind"] == "deposit_obligation" and x["due_minute"] == minute
                )
            )
            valid &= balance >= reserve
        if valid and (best is None or balance >= best):
            if best is None or balance > best:
                best = balance
                winners = []
            winners.append(schedule)
    return dict(
        feasible=best is not None,
        maximum_final_cash_minor=best,
        calendars=winners,
        examined=checked,
        scope="Complete full-redemption date space; cash at every automatic debit and inclusive manual deadline. Early manual payment is weakly dominated for liquidity and has no interest benefit.",
    )
