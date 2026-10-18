"""Settlement capability and event dispatch registry."""

from pathlib import Path
from importlib import import_module
from dataclasses import asdict, is_dataclass
from payment.runtime.products.settlement.models import Mechanism

ENTRIES = []
for path in sorted(Path(__file__).parent.glob("*_registry.py")):
    for item in import_module("." + path.stem, __package__).REGISTRY:
        m = Mechanism(**(asdict(item) if is_dataclass(item) else item))
        ENTRIES.append(m)
ENTRIES = tuple(ENTRIES)
BY_KIND = {m.kind: m for m in ENTRIES}
BY_TOOL = {m.tool_name: m for m in ENTRIES}
BY_EVENT = {m.event_kind: m for m in ENTRIES}
assert len(BY_KIND) == len(BY_TOOL) == len(BY_EVENT) == len(ENTRIES)
