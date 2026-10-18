"""Load business operations and event handlers for the selected product domains."""

from importlib import import_module

PRODUCT_MODULES = {
    "receivables": ("receivables.operations", "a"),
    "accounts": ("accounts.operations", "b"),
    "commerce": ("commerce.operations", "c"),
    "cash_and_serial_rights": ("cash_and_serial_rights", "d"),
    "payment_workflows": ("payment_workflows", "v9"),
}


def load(domains=("receivables", "accounts", "commerce")):
    operations, events = {}, {}
    for name in domains:
        module_name, event_prefix = PRODUCT_MODULES[name]
        module = import_module("." + module_name, __package__)
        for key, operation in module.OPERATIONS.items():
            if key in operations:
                raise ValueError("Duplicate capability: " + key)
            operations[key] = operation
        if hasattr(module, "apply_event"):
            events[event_prefix] = module.apply_event
    return dict(operations=operations, events=events)
