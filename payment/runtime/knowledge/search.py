"""Unchanged BM25 ranking and excerpt selection with an augmented corpus."""

from functools import lru_cache
from collections import Counter
import math
import re
from payment.runtime.core.storage import ROOT, read
from payment.runtime.knowledge.tokenization import tokens


@lru_cache(maxsize=1)
def documents():
    return [read(p) for p in sorted((ROOT / "knowledge").glob("*.json"))]


def make_index(ds):
    rows = []
    df = Counter()
    for d in ds:
        counts = Counter(tokens(d["title"] + " " + d["content"]))
        df.update(counts.keys())
        rows.append((d, counts, sum(counts.values())))
    return rows, df, sum(n for _, _, n in rows) / len(rows)


@lru_cache(maxsize=1)
def index():
    return make_index(documents())


def search(query, index_override=None):
    rows, df, avg = index_override or index()
    terms = set(tokens(query))
    ranked = []
    for d, counts, n in rows:
        score = 0
        for word in terms:
            f = counts.get(word, 0)
            if f:
                score += (
                    math.log(1 + (len(rows) - df[word] + 0.5) / (df[word] + 0.5))
                    * f
                    * 2.2
                    / (f + 1.2 * (0.25 + 0.75 * n / avg))
                )
        if score:
            sentences = re.split(r"(?<=[.!?])\s+", d["content"])
            excerpt = max(sentences, key=lambda s: len(terms & set(tokens(s))))
            ranked.append(
                (
                    score,
                    dict(
                        id=d["id"],
                        title=d["title"],
                        applicability=d["applicability"],
                        excerpt=excerpt[:900],
                    ),
                )
            )
    return [d for _, d in sorted(ranked, key=lambda v: (-v[0], v[1]["id"]))[:5]]
