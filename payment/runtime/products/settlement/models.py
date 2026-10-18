"""Business capability metadata shared across payment tasks."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Mechanism:
    kind: str
    tool_name: str
    tool_description: str
    implementation_prefix: str
    operation_class: str | None
    operation_method: str | None
    event_kind: str
    event_method: str | None

    def capability(self):
        return dict(
            name=self.tool_name,
            rule_id="DIVERSE-" + self.kind,
            description=self.tool_description,
            parameters={"case_id": "string", "action": "string", "data": "object"},
        )
