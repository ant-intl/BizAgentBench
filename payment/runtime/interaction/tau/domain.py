"""State and response serialization shared with the Tau2 orchestrator."""

import json

from pydantic import Field
from tau2.environment.db import DB
from tau2.environment.environment import Environment


class CrossBorderDB(DB):
    world: dict = Field(default_factory=dict)


class CrossBorderEnvironment(Environment):
    def get_response(self, message):
        response = super().get_response(message)
        if not response.error:
            payload = json.loads(response.content)
            # Business refusals return structured receipts rather than raising.
            # Preserve the receipt and audit event while counting the failed call.
            if isinstance(payload, dict) and payload.get("ok") is False:
                response.error = True
        return response

    @classmethod
    def to_json_str(cls, resp):
        """Keep JSON numbers and booleans typed across live execution and replay."""
        from pydantic import BaseModel

        if isinstance(resp, BaseModel):
            resp = resp.model_dump(mode="json")
        return json.dumps(resp, ensure_ascii=False, sort_keys=True)

    def sync_tools(self):
        if self.tools is not None and self.user_tools is not None:
            self.user_tools.db = self.tools.db
