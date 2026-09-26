"""Vietnamese text normalization used for matching (product names, evidence grounding)."""
from __future__ import annotations

import re
import unicodedata


def strip_accents(text: str) -> str:
    text = text.replace("đ", "d").replace("Đ", "D")
    nfkd = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfkd if unicodedata.category(c) != "Mn")


def norm_key(text: str) -> str:
    """Lowercase, accent-free, single-spaced key: 'Thịt  Heo' -> 'thit heo'."""
    text = strip_accents(text or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def display_name(text: str) -> str:
    """Tidy human-facing name, keeps accents: '  thịt   heo ' -> 'Thịt heo'."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text[:1].upper() + text[1:] if text else text


def compact(text: str) -> str:
    """Accent-free, no spaces/punctuation — for loose substring grounding checks."""
    return re.sub(r"[^a-z0-9]", "", strip_accents(text or "").lower())
