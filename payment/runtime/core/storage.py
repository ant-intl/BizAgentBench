"""Repository-relative paths and deterministic JSON storage."""

from pathlib import Path
import json

PROJECT = Path(__file__).resolve().parents[2]
ROOT = PROJECT / "data"


def read(path):
    return json.loads(Path(path).read_text())


def append(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
