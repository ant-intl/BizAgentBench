"""Construct the single public execution protocol for a frozen task."""

from payment.runtime.core.storage import ROOT as DIRECTORY
from payment.runtime.tasks.assets import assets


def make_engine(world, profile, directory=DIRECTORY):
    if profile == "unified":
        from payment.runtime.core.execution.engine import Engine as Unified

        data = assets(directory)
        return Unified(
            world,
            data["capabilities"],
            data["documents"],
            data["search_index"],
            data["operations"],
            data["events"],
        )
    raise ValueError("Unsupported execution profile: " + str(profile))


def specifications(profile, directory=DIRECTORY, product_scope=None):
    if profile == "unified":
        from payment.runtime.core.execution.specification import for_product_scope

        return for_product_scope(product_scope)
    raise ValueError("Unsupported execution profile: " + str(profile))
