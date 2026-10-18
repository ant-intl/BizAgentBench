"""Assemble frozen documents, schemas, and registered product operations."""

from functools import lru_cache
from pathlib import Path
from payment.runtime.core.storage import ROOT as DIRECTORY, read


@lru_cache(4)
def _assets(directory, revision):
    from payment.runtime.products.psp.registry import load
    from payment.runtime.knowledge.search import make_index

    root = Path(directory)
    manifest = read(root / "manifest.json")
    registry = load(manifest["business_domains"])
    registry["documents"] = {
        d["id"]: d for p in sorted((root / "knowledge").glob("*.json")) for d in [read(p)]
    }
    searchable = set(manifest["searchable_document_ids"])
    registry["search_index"] = make_index(
        [d for k, d in registry["documents"].items() if k in searchable]
    )
    action_path = root / (
        "action_catalog.json" if (root / "action_catalog.json").exists() else "tool_catalog.json"
    )
    registry["capabilities"] = read(action_path)
    return registry


def assets(directory=DIRECTORY):
    root = Path(directory)
    return _assets(str(root.resolve()), read(root / "manifest.json")["asset_revision"])
