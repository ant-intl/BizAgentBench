"""Explicit dependencies used while assembling a PSP product's operations."""

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ProductRegistration:
    operations: dict
    event_handler: Callable
    helpers: dict = field(default_factory=dict)

    def helper(self, name):
        """Only explicitly supplied product helpers are available to a layer."""
        return self.helpers[name]
