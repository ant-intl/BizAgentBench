"""Lexical normalization for the frozen retrieval index."""

import re


def tokens(text):
    return re.findall(r"[a-z0-9_]+", text.lower())
