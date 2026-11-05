"""Deterministic annotation anchors (DESIGN §10)."""

from __future__ import annotations

from dataclasses import dataclass

from .textmodel import Utf16Text

CONTEXT_LEN = 40

ATTACHED = "attached"
MOVED = "moved"
AMBIGUOUS = "ambiguous"
ORPHANED = "orphaned"


@dataclass
class Anchor:
    version_sha256: str
    quote: str
    prefix: str
    suffix: str
    start: int
    end: int
    heading: str | None = None

    def as_dict(self) -> dict:
        return {
            "version_sha256": self.version_sha256,
            "quote": self.quote,
            "prefix": self.prefix,
            "suffix": self.suffix,
            "start": self.start,
            "end": self.end,
            "heading": self.heading,
        }


@dataclass
class Resolution:
    state: str
    start: int | None
    end: int | None
    prefix: str | None = None
    suffix: str | None = None
    candidates: int = 0


def _find_all(hay: str, needle: str) -> list[int]:
    out: list[int] = []
    if not needle:
        return out
    i = hay.find(needle)
    while i != -1:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


def context_at(u: Utf16Text, py_start: int, py_end: int) -> tuple[str, str]:
    prefix = u.text[max(0, py_start - CONTEXT_LEN * 2):py_start]
    suffix = u.text[py_end:py_end + CONTEXT_LEN * 2]
    # trim to CONTEXT_LEN UTF-16 code units
    while len(prefix) and sum(2 if ord(c) > 0xFFFF else 1 for c in prefix) > CONTEXT_LEN:
        prefix = prefix[1:]
    while len(suffix) and sum(2 if ord(c) > 0xFFFF else 1 for c in suffix) > CONTEXT_LEN:
        suffix = suffix[:-1]
    return prefix, suffix


def resolve(anchor: Anchor, model_text: str, current_sha256: str) -> Resolution:
    u = Utf16Text(model_text)
    # 1. exact position in same version
    if anchor.version_sha256 == current_sha256:
        s = u.slice(anchor.start, anchor.end)
        if s is not None and s == anchor.quote:
            return Resolution(ATTACHED, anchor.start, anchor.end, anchor.prefix, anchor.suffix, 1)
    # Also accept exact position match even when version differs (content at that place unchanged)
    s = u.slice(anchor.start, anchor.end)
    if s is not None and s == anchor.quote and anchor.quote:
        state = ATTACHED if anchor.version_sha256 == current_sha256 else MOVED
        if state == MOVED:
            # verify the context too; otherwise treat as a candidate like any other occurrence
            py_s = u.py_index(anchor.start)
            py_e = u.py_index(anchor.end)
            if py_s is not None and py_e is not None:
                p, sfx = context_at(u, py_s, py_e)
                if (not anchor.prefix or p.endswith(anchor.prefix) or anchor.prefix.endswith(p)) and (
                    not anchor.suffix or sfx.startswith(anchor.suffix) or anchor.suffix.startswith(sfx)
                ):
                    return Resolution(MOVED, anchor.start, anchor.end, p, sfx, 1)
    # 2. exact occurrences
    occ = _find_all(model_text, anchor.quote)
    if not occ:
        return Resolution(ORPHANED, None, None, None, None, 0)
    qlen = len(anchor.quote)
    if len(occ) == 1:
        i = occ[0]
        p, sfx = context_at(u, i, i + qlen)
        return Resolution(MOVED, u.u16_offset(i), u.u16_offset(i + qlen), p, sfx, 1)
    # 3. disambiguate by context (all nonempty stored prefix/suffix must match exactly)
    keep: list[int] = []
    for i in occ:
        before = model_text[max(0, i - len(anchor.prefix)):i] if anchor.prefix else ""
        after = model_text[i + qlen:i + qlen + len(anchor.suffix)] if anchor.suffix else ""
        if anchor.prefix and before != anchor.prefix:
            continue
        if anchor.suffix and after != anchor.suffix:
            continue
        keep.append(i)
    if len(keep) == 1:
        i = keep[0]
        p, sfx = context_at(u, i, i + qlen)
        return Resolution(MOVED, u.u16_offset(i), u.u16_offset(i + qlen), p, sfx, 1)
    return Resolution(AMBIGUOUS, None, None, None, None, len(keep) if keep else len(occ))


def validate_offsets(model_text: str, start: int, end: int, quote: str) -> str | None:
    """Return an error message when the offsets do not describe `quote` in the model.

    Returns None on success. Detects surrogate-splitting offsets.
    """
    if start < 0 or end <= start:
        return "start must be >= 0 and end > start"
    u = Utf16Text(model_text)
    if u.py_index(start) is None or u.py_index(end) is None:
        if end > u.length or start > u.length:
            return "offsets exceed document length"
        return "offset splits a surrogate pair"
    s = u.slice(start, end)
    if s != quote:
        return "quote does not match the text model at [start,end)"
    return None
