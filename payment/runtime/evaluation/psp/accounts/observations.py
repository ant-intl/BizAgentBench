

def initial_record(contract, table):
    """A verifier/CF helper scoped by the active contract, including merged worlds."""
    world = contract["initial_world"]
    customer = world.get("_customer_id", world["xp_cases"][contract["id"]]["customer_id"])
    rows = [r for r in world.get(table, {}).values() if r.get("customer_id") == customer]
    if len(rows) != 1:
        raise ValueError((contract["id"], table, "expected one scoped initial record", len(rows)))
    return rows[0]
