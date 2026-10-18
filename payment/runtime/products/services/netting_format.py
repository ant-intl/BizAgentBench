"""Canonical instruction representation only; no business or scoring logic."""

import hashlib
import json


def fingerprint(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
