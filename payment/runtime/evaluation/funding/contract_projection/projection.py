"""Project native records while allowing structured IDs in read-only query filters."""

from copy import deepcopy
from payment.runtime.products.joint_funding.records import TABLES
from payment.runtime.evaluation.products.joint_funding.verifier import audit_sequences


def project(world, c, component):
    initial = c["initial_world"]
    cid = component["case_id"]
    cc = deepcopy(component)
    own_ids = {cid} | {
        rid
        for rows in initial.values()
        if isinstance(rows, dict)
        for rid, r in rows.items()
        if isinstance(r, dict) and r.get("case_id") == cid
    }
    event_ids = {eid for eid, e in initial["events"].items() if e.get("target_id") in own_ids}

    def select(w, start=False):
        out = deepcopy(w)
        out.pop("_closure_state", None)
        for table in TABLES:
            out.pop(table, None)
        for table, rows in list(out.items()):
            if not isinstance(rows, dict) or table.startswith("_"):
                continue
            if table == "events":
                out[table] = {
                    rid: r
                    for rid, r in rows.items()
                    if rid in event_ids or r.get("target_id") in own_ids
                }
                continue
            if table == "requests":
                out[table] = {
                    rid: r
                    for rid, r in rows.items()
                    if r.get("arguments", {}).get("case_id") == cid
                    and r.get("operation") != "propose_joint_capital_delivery"
                }
                continue
            projected = {}
            for rid, r in rows.items():
                if (
                    not isinstance(r, dict)
                    or r.get("scope") != c["scope"]
                    or rid in own_ids
                    or r.get("case_id") == cid
                ):
                    projected[rid] = r
                elif rid in initial.get(table, {}):
                    projected[rid] = deepcopy(initial[table][rid])
            out[table] = projected
        out["_event_payloads"] = {
            eid: p for eid, p in out.get("_event_payloads", {}).items() if eid in out["events"]
        }
        o = initial["joint_agreements"][c["agreement_id"]]
        cap = o["capital_by_case"][cid]
        opening = o["initial_operating_by_case"][cid]
        if start:
            out["service_cases"][cid]["cash_minor"] = cap
            for rid, amount in o.get("capital_by_account", {}).items():
                if out["service_records"][rid].get("case_id") == cid:
                    out["service_records"][rid]["cash_minor"] = amount
        else:
            seq = audit_sequences(world, w)
            fs = [f for f in w["joint_fundings"].values() if f["case_id"] == cid]
            adjustments = {}
            for bid, b in out["service_bookings"].items():
                if (
                    b.get("case_id") != cid
                    or bid in initial["service_bookings"]
                    or not (
                        "cash_after_minor" in b
                        or "usd_after_minor" in b
                        or "wallet_after_minor" in b
                    )
                ):
                    continue
                delivered = sum(
                    f["amount_minor"] for f in fs if seq.get(f["id"], 10**9) < seq.get(bid, -1)
                )
                adjustments[bid] = deepcopy(b)
                virtual = cap - opening - delivered
                if "cash_after_minor" in b:
                    adjustments[bid]["cash_after_minor"] += virtual
                elif "usd_after_minor" in b:
                    adjustments[bid]["usd_after_minor"] += virtual
                else:
                    adjustments[bid]["wallet_after_minor"]["USD"] += virtual
                receipt = adjustments[bid].get("receipt")
                if isinstance(receipt, dict) and isinstance(receipt.get("cash_path_minor"), list):
                    receipt["cash_path_minor"] = [
                        value + virtual for value in receipt["cash_path_minor"]
                    ]

            def normalize(x):
                if isinstance(x, dict):
                    if (
                        isinstance(x.get("id"), str)
                        and x.get("id") in adjustments
                        and (
                            "cash_after_minor" in x
                            or "usd_after_minor" in x
                            or "wallet_after_minor" in x
                        )
                    ):
                        return deepcopy(adjustments[x["id"]])
                    return {k: normalize(v) for k, v in x.items()}
                if isinstance(x, list):
                    return [normalize(v) for v in x]
                return x

            out = normalize(out)
            out["service_cases"][cid]["cash_minor"] += (
                cap - opening - sum(f["amount_minor"] for f in fs)
            )
            for rid, amount in o.get("capital_by_account", {}).items():
                if out["service_records"][rid].get("case_id") == cid:
                    ds = sum(
                        f["amount_minor"]
                        for f in fs
                        if initial["joint_routes"][f["route_id"]].get("destination_account_id")
                        == rid
                    )
                    out["service_records"][rid]["cash_minor"] += amount - ds
        return out

    cc["initial_world"] = select(initial, True)
    ww = select(world)
    if world.get("_closure_state"):
        ww["_closure_state"] = select(world["_closure_state"])
    return ww, cc
