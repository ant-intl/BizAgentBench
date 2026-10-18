"""Frozen task contracts and independent initial worlds."""

from copy import deepcopy
from pathlib import Path
from payment.runtime.core.storage import ROOT as DIRECTORY, read


def read_contract(task_id, directory=DIRECTORY):
    directory = Path(directory)
    contract = read(directory / "private" / f"{task_id}.json")
    manifest = read(directory / "manifest.json")
    if manifest.get("version") == "bizagentbench-payment-v1":
        contract["_dimension_registry"] = "bizagentbench-v1"
    world = read(directory / "base_world.json")
    patch = read(directory / "patches" / f"{task_id}.json")
    for key in patch.get("remove", []):
        world.pop(key, None)
    world.update(deepcopy(patch["replace"]))
    contract["initial_world"] = world
    return contract
