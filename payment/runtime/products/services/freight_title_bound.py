"""Independent branch-and-bound schedule certificate; no planner imports."""


def minimum(rows):
    lots = sorted(
        (x for x in rows.values() if x["kind"] == "titled_cargo_lot"),
        key=lambda x: (x["due_minute"], -x["handling_units"], x["reference"]),
    )
    windows = sorted(
        (x for x in rows.values() if x["kind"] == "pickup_window"), key=lambda x: x["minute"]
    )
    free = {w["minute"]: w["capacity_units"] for w in windows}
    best = None
    plans = []
    visited = 0

    def cost(lot, minute):
        tariff = rows[lot["tariff_ref"]]
        d = max(0, minute - tariff["free_through"])
        return (
            lot["handling_units"]
            * (
                min(d, 1) * tariff["first_window_rate_minor"]
                + max(0, d - 1) * tariff["later_window_rate_minor"]
            )
            + tariff["loading_fees_minor"][str(minute)]
        )

    lower = [0] * (len(lots) + 1)
    for i in range(len(lots) - 1, -1, -1):
        lower[i] = lower[i + 1] + min(
            cost(lots[i], w["minute"]) for w in windows if w["minute"] <= lots[i]["due_minute"]
        )

    def dfs(i, paid, plan):
        nonlocal best, plans, visited
        visited += 1
        if best is not None and paid + lower[i] > best:
            return
        if i == len(lots):
            if best is None or paid < best:
                best, plans = paid, [dict(plan)]
            elif paid == best:
                plans.append(dict(plan))
            return
        lot = lots[i]
        for w in windows:
            m = w["minute"]
            if m > lot["due_minute"] or free[m] < lot["handling_units"]:
                continue
            free[m] -= lot["handling_units"]
            plan[lot["reference"]] = m
            dfs(i + 1, paid + cost(lot, m), plan)
            del plan[lot["reference"]]
            free[m] += lot["handling_units"]

    dfs(0, 0, {})
    return dict(
        feasible=best is not None,
        minimum_dispatch_fee_minor=best,
        plans=plans,
        search_nodes=visited,
        scope="All whole-lot assignments to published windows, respecting capacity and inclusive deadlines; every tariff is recalculated independently.",
    )
