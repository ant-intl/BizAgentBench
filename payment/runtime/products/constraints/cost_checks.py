"""Independent subset lower bound, deliberately not the witness search kernel.

Eligibility is relaxed, never strengthened: a subset passing funding/deadline
checks may still be impossible operationally. Achieving this lower bound in a
full legal replay certifies optimal fees in the declared contract space.
"""

from collections import defaultdict
from itertools import combinations


def lower_bound(initial, routes, requirements):
    goals = {}
    necessity = []
    for req in requirements:
        account = req["account_id"]
        cash = initial["desk_accounts"][account]["wallet"]["USD"]
        if req["kind"] == "review_fee":
            apps = [
                a
                for a in initial["onboarding_applications"].values()
                if a.get("billing_account_id") == account
            ]
            assert len(apps) == 1
            need = apps[0]["submission_fee_minor"] - cash
            evidence = "Required institutional approval entails a charged submission; initial billing cash is subtracted."
        elif req["kind"] == "issuance_fee":
            profiles = [
                r
                for r in initial["rail_profiles"].values()
                if r.get("billing_account_id") == account and r.get("new_accounts")
            ]
            existing = [
                initial["rail_profiles"][a["profile_id"]]
                for a in initial["desk_accounts"].values()
                if a.get("profile_id") in initial["rail_profiles"] and a["status"] == "OPEN"
            ]
            uncovered = [
                s
                for s in initial["rail_sites"].values()
                if not any(
                    s["market"] in a["markets"]
                    and s["category"] in a["categories"]
                    and s["currency"] == a["currency"]
                    and s["sender_rail"] in a["rails"]
                    for a in existing
                )
            ]
            assert uncovered and profiles
            need = min(r["issuance_fee_minor"] for r in profiles) - cash
            evidence = "Existing accounts cannot cover every site; at least one paid new account is necessary. Use the smallest product fee as a relaxed lower bound, not an assumed optimal set cover."
        elif req["kind"] == "service_cash":
            bills = [
                b for b in initial["billing_instances"].values() if b.get("account_id") == account
            ]
            need = (
                sum(b["amount_minor"] for b in bills if b["status"] == "ACCEPTED")
                - cash
                - sum(b["amount_minor"] for b in bills if b["status"] == "RETURN_PENDING")
            )
            evidence = "Accepted bills must be paid. Even granting all pending returns immediately, bills exceed initial cash plus returns; outside funding and sweeps into this account are not authorized."
        else:
            raise ValueError("Unsupported independent cash-necessity proof")
        assert need > 0
        goals[account] = need
        necessity.append(dict(account=account, necessary_new_cash_minor=need, evidence=evidence))
    deadlines = {r["account_id"]: r["latest_minute"] for r in requirements}
    found = []
    examined = 0
    for size in range(len(routes) + 1):
        for subset in combinations(routes, size):
            examined += 1
            debits = defaultdict(int)
            credits = defaultdict(int)
            fee = 0
            valid = True
            for r in subset:
                start = max(initial["minute"], r.get("available_from", 0))
                arrival = start + r.get("settlement_delay", 0)
                valid &= start <= r.get("expires_minute", 1000000)
                debits[r["reserve_account_id"]] += r["debit_minor"]
                fee += r["debit_minor"] - r["net_minor"]
                dst = r["destination_account_id"]
                if dst in goals and arrival <= deadlines[dst]:
                    credits[dst] += r["net_minor"]
            valid &= all(
                n <= initial["desk_accounts"][id]["wallet"]["USD"] for id, n in debits.items()
            )
            if valid and all(credits[id] >= n for id, n in goals.items()):
                found.append({"routes": [r["id"] for r in subset], "fee_minor": fee})
    assert found, "No funded candidate subset"
    bound = min(x["fee_minor"] for x in found)
    return dict(
        minimum_fee_minor=bound,
        optimal_subsets=[x for x in found if x["fee_minor"] == bound],
        feasible_subsets=found,
        examined_subsets=examined,
        necessity_evidence=necessity,
        scope="All subsets of published single-use drawings; milestones relaxed. A matching full witness is required to certify attainability.",
    )
