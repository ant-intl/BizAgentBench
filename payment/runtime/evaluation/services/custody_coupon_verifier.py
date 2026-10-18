from payment.runtime.products.settlement.custody_ledger import Journal, malformed


def collateral_closed(asset, books, due):
    """Unused property must be returned; fully applied proceeds leave nothing to return."""
    applied = sum(asset["allocations_minor"].values())
    returned_on_time = asset["returned_minute"] is not None and asset["returned_minute"] <= due
    if not asset["realized"]:
        return (
            returned_on_time
            and asset["returned_intact"]
            and applied == 0
            and asset["cash_returned_minor"] == 0
        )
    if asset["returned_intact"] or applied + asset["cash_returned_minor"] != asset["face_minor"]:
        return False
    if returned_on_time:
        return True
    fully_applied = applied == asset["face_minor"] and asset["cash_returned_minor"] == 0
    realized_on_time = any(
        b["kind"] == "realize_collateral"
        and b.get("arguments", {}).get("record_id") == asset["id"]
        and b["minute"] <= due
        for b in books
    )
    return fully_applied and realized_on_time


def verify(world, c):
    j = Journal(world, c)
    r = j.rows
    n = r["ISSUER"]
    try:
        for b in j.books:
            k = b["kind"]
            m = b["minute"]
            d = b.get("arguments", {})
            delta = 0
            fee = 0
            out = {}
            if k == "elect_claim":
                x = j.row(d["record_id"], "manufactured_claim")
                mode = d["mode"]
                j.require(
                    x["election"] is None
                    and mode in ("FINAL", "CONTINGENT")
                    and m <= x["claim_deadline"]
                )
                x.update(election=mode, elected_minute=m)
                if mode == "CONTINGENT":
                    fee = x["contingent_fee_minor"]
                    delta = -fee
                elif x["final_receipt_minute"] <= m:
                    x["received_minor"] = x["units"] * n["provisional_rate_minor"]
                    delta = x["received_minor"]
                out = dict(claim_ref=x["reference"], mode=mode, received_minor=x["received_minor"])
            elif k in ("draw_advance", "repay_advance"):
                x = r["ADVANCE"]
                amount = d["amount_minor"]
                j.require(type(amount) is int and amount > 0)
                if k == "draw_advance":
                    j.require(m == 0 and x["drawn_minor"] == 0 and amount <= x["limit_minor"])
                    x["drawn_minor"] = amount
                    fee = x["fee_minor"]
                    delta = amount - fee
                else:
                    j.require(amount <= x["drawn_minor"] - x["repaid_minor"])
                    x["repaid_minor"] += amount
                    delta = -amount
                out = dict(amount_minor=amount)
            elif k == "settle_purchase":
                x = r["DELIVERY"]
                mode = d["mode"]
                j.require(x["paid_minor"] == 0 and m == 0 and mode in ("STANDARD", "EXPRESS"))
                x.update(paid_minor=x["principal_minor"], mode=mode)
                fee = x["standard_fee_minor"] if mode == "STANDARD" else x["express_fee_minor"]
                delta = -x["principal_minor"] - fee
                if mode == "EXPRESS":
                    x.update(settled=True, settled_minute=m)
                out = dict(principal_minor=x["principal_minor"], mode=mode)
            elif k == "realize_collateral":
                asset = j.row(d["record_id"], "borrower_collateral")
                j.require(
                    not asset["realized"]
                    and asset["returned_minute"] is None
                    and n["status"] == "FINAL"
                    and type(d["allocations"]) is dict
                    and bool(d["allocations"])
                )
                alloc = {}
                total = 0
                for rid, amount in d["allocations"].items():
                    claim = j.row(rid, "manufactured_claim")
                    j.require(
                        type(amount) is int
                        and amount > 0
                        and claim["reference"] in asset["eligible_claims"]
                        and amount <= claim["arrears_minor"] - claim["collateral_recovered_minor"]
                    )
                    alloc[claim["reference"]] = amount
                    total += amount
                j.require(total <= asset["face_minor"])
                for ref, amount in alloc.items():
                    r[ref]["collateral_recovered_minor"] += amount
                asset.update(realized=True, allocations_minor=alloc)
                delta = total
                out = dict(
                    security_ref=asset["reference"],
                    allocations_minor=alloc,
                    applied_minor=total,
                    segregated_remainder_minor=asset["face_minor"] - total,
                )
            elif k == "return_collateral":
                asset = j.row(d["record_id"], "borrower_collateral")
                j.require(asset["returned_minute"] is None)
                asset["returned_minute"] = m
                if asset["realized"]:
                    asset["cash_returned_minor"] = asset["face_minor"] - sum(
                        asset["allocations_minor"].values()
                    )
                else:
                    asset["returned_intact"] = True
                out = dict(
                    security_ref=asset["reference"],
                    returned_intact=asset["returned_intact"],
                    cash_returned_minor=asset["cash_returned_minor"],
                )
            elif k in ("pay_coupon", "recover_coupon"):
                x = j.row(d["record_id"], "coupon_investor")
                amount = d["amount_minor"]
                j.require(type(amount) is int and amount > 0)
                if k == "pay_coupon":
                    x["paid_minor"] += amount
                    x["last_payment_minute"] = m
                    delta = -amount
                else:
                    j.require(
                        n["status"] == "FINAL"
                        and amount <= x["recovery_limit_minor"] - x["recovered_minor"]
                        and amount <= x["paid_minor"] - x["recovered_minor"]
                    )
                    x["recovered_minor"] += amount
                    x["last_recovery_minute"] = m
                    delta = amount
                out = dict(investor_ref=x["reference"], amount_minor=amount)
            elif k == "bank_event":
                ek = b["event_kind"]
                if ek == "purchase_settlement":
                    x = r["DELIVERY"]
                    if x["paid_minor"] == x["principal_minor"] and x["mode"] == "STANDARD":
                        x.update(settled=True, settled_minute=m)
                    paid = []
                    for x in r.values():
                        if (
                            x["kind"] == "manufactured_claim"
                            and x["election"] == "FINAL"
                            and x["received_minor"] == 0
                        ):
                            x["received_minor"] = x["units"] * n["provisional_rate_minor"]
                            delta += x["received_minor"]
                            paid.append(x["reference"])
                    out = dict(
                        purchase_settled=r["DELIVERY"]["settled"], final_claims_collected=paid
                    )
                elif ek == "coupon_correction":
                    rate = n["_final_rate_minor"]
                    j.require(rate in n["possible_final_rates_minor"])
                    issuer_delta = n["direct_custody_units"] * (rate - n["provisional_rate_minor"])
                    delta += issuer_delta
                    n.update(
                        final_rate_minor=rate,
                        status="FINAL",
                        issuer_received_minor=n["direct_custody_units"] * rate,
                        actual_borrower_shortfalls=dict(n["_borrower_shortfalls"]),
                    )
                    paid = {}
                    for x in r.values():
                        if x["kind"] == "manufactured_claim" and x["election"] == "CONTINGENT":
                            short = n["_borrower_shortfalls"].get(x["reference"], 0)
                            j.require(0 <= short <= x["units"] * rate)
                            x["arrears_minor"] = short
                            x["received_minor"] = x["units"] * rate - short
                            delta += x["received_minor"]
                            paid[x["reference"]] = x["received_minor"]
                    x = r["OPERATING"]
                    x["received_minor"] = x["amount_minor"]
                    delta += x["amount_minor"]
                    out = dict(
                        final_rate_minor=rate,
                        issuer_adjustment_minor=issuer_delta,
                        manufactured_collections=paid,
                        borrower_shortfalls=dict(n["_borrower_shortfalls"]),
                        operating_received_minor=x["amount_minor"],
                    )
                else:
                    j.require(ek == "servicing_deadline")
            else:
                raise ValueError(k)
            j.post(b, out, delta, fee)
        investors = [x for x in r.values() if x["kind"] == "coupon_investor"]
        rate = n["final_rate_minor"]
        due = r["TERMS"]["coupon_due_minute"]
        j.ck(
            "coupon.record_date_entitlements",
            rate is not None
            and all(
                x["paid_minor"] - x["recovered_minor"] == x["record_date_units"] * rate
                and all(
                    t is None or t <= due
                    for t in [x["last_payment_minute"], x["last_recovery_minute"]]
                )
                for x in investors
            ),
            "Check net actual payment and recovery deadline per person according to the registration date quantity and final coupon. ",
        )
        j.ck(
            "coupon.rights_conservation",
            sum(x["record_date_units"] for x in investors)
            == n["direct_custody_units"] + n["loaned_units"]
            and sum(x["units"] for x in r.values() if x["kind"] == "manufactured_claim")
            == n["loaned_units"]
            and all(
                x["direct_units"] + x["loaned_units"] == x["record_date_units"] for x in investors
            ),
            "On the registration date, direct holdings and lent securities shares are conserved, subsequent ex-dividend transfer does not repeatedly generate income. ",
        )
        x = r["DELIVERY"]
        j.ck(
            "purchase.timely_settlement",
            x["settled"]
            and x["settled_minute"] <= x["due_minute"]
            and x["paid_minor"] == x["principal_minor"],
            "verify actual settlement of foreign bonds, A mere request or payment does not replace settlement. ",
        )
        x = r["ADVANCE"]
        repaytimes = [b["minute"] for b in j.books if b["kind"] == "repay_advance"]
        j.ck(
            "advance.repaid",
            x["drawn_minor"] == x["repaid_minor"] and all(t <= x["due_minute"] for t in repaytimes),
            "Advance principal is repaid within the agreed period. ",
        )
        j.ck(
            "claims.closed",
            all(
                x["election"] is not None
                and x["received_minor"] > 0
                and x["collateral_recovered_minor"] == x["arrears_minor"]
                for x in r.values()
                if x["kind"] == "manufactured_claim"
            ),
            "Per-transaction securities lending compensation has actually been collected. ",
        )
        j.ck(
            "collateral.source_conservation_and_returns",
            all(
                collateral_closed(x, j.books, due)
                for x in r.values()
                if x["kind"] == "borrower_collateral"
            ),
            "Verify collateral securities and sale proceeds conservation by actual scope; Unused securities and remaining sale proceeds are returned on schedule, No zero-amount return is required when fully used and no remaining sale proceeds. ",
        )
        return j.finish()
    except (KeyError, TypeError, ValueError, AssertionError) as e:
        return malformed(j, e)
