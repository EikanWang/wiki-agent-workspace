"""Deterministic anchor resolution (B7) and text model rules (§10)."""

from __future__ import annotations

from backend.app import anchors
from backend.app.anchors import AMBIGUOUS, ATTACHED, MOVED, ORPHANED, Anchor, resolve, validate_offsets
from backend.app.textmodel import Utf16Text, build_text_model, utf16_len

DOC = "# T\n\nAlpha beta gamma.\n\nDelta epsilon zeta.\n\n## S\n\nEta theta iota.\n"


def model(md: str) -> str:
    return build_text_model(md).text


def mk(md: str, quote: str, sha="v1") -> Anchor:
    m = model(md)
    i = m.index(quote)
    return Anchor(sha, quote, m[max(0, i - 40):i], m[i + len(quote):i + len(quote) + 40], i, i + len(quote))


def test_attached_when_same_version():
    a = mk(DOC, "beta gamma")
    r = resolve(a, model(DOC), "v1")
    assert r.state == ATTACHED and (r.start, r.end) == (a.start, a.end)


def test_insert_before_moves_anchor():
    a = mk(DOC, "Delta epsilon")
    new = DOC.replace("Alpha beta gamma.", "Inserted paragraph.\n\nAlpha beta gamma.")
    r = resolve(a, model(new), "v2")
    assert r.state == MOVED
    assert model(new)[r.start:r.end] == "Delta epsilon"


def test_duplicate_quote_disambiguated_by_context():
    a = mk(DOC, "epsilon")
    new = DOC + "\nAnother epsilon mention.\n"
    r = resolve(a, model(new), "v2")
    assert r.state == MOVED
    assert model(new)[r.start - 6:r.end] == "Delta epsilon"


def test_duplicate_quote_with_identical_context_is_ambiguous():
    a = mk(DOC, "Delta epsilon zeta.")
    new = DOC + "\nDelta epsilon zeta.\n"  # paragraph duplicated; both have different context -> prefix differs
    r = resolve(a, model(new), "v2")
    # the first occurrence keeps its context, so it is unique by context
    assert r.state == MOVED
    # now make both occurrences share identical context: whole section duplicated twice
    dup = "Alpha beta gamma.\n\nDelta epsilon zeta.\n\n"
    new2 = "# T\n\n" + dup + dup + "## S\n\nEta theta iota.\n"
    a2 = Anchor("v1", "Delta epsilon zeta.", "Alpha beta gamma.\n", "", 0, 1)
    r2 = resolve(a2, model(new2), "v3")
    assert r2.state == AMBIGUOUS
    assert r2.start is None


def test_modified_or_deleted_quote_is_orphaned():
    a = mk(DOC, "Eta theta iota.")
    r = resolve(a, model(DOC.replace("Eta theta iota.", "Eta THETA iota.")), "v2")
    assert r.state == ORPHANED
    r = resolve(a, model(DOC.replace("Eta theta iota.\n", "")), "v3")
    assert r.state == ORPHANED


def test_utf16_offsets_and_surrogate_split():
    text = "a😀b"
    assert utf16_len(text) == 4
    u = Utf16Text(text)
    assert u.slice(0, 1) == "a"
    assert u.slice(1, 3) == "😀"
    assert u.py_index(2) is None  # middle of surrogate pair
    assert validate_offsets(text, 1, 2, "x") == "offset splits a surrogate pair"
    assert validate_offsets(text, 3, 4, "b") is None
    assert validate_offsets(text, 0, 0, "") is not None
    assert "exceed" in validate_offsets(text, 0, 9, "a")


def test_text_model_rules():
    md = "# H\n\nPara **bold** text.\n\n- a\n- b\n\n| x | y |\n|---|---|\n| 1 | 2 |\n\n```\ncode\n```\n\nl1  \nl2\n"
    m = build_text_model(md)
    assert m.text == "H\nPara bold text.\na\nb\nx\ny\n1\n2\ncode\nl1\nl2\n"
    assert [h.text for h in m.headings] == ["H"]
    assert m.heading_for(5) == "H"
    crlf = build_text_model("# A\r\n\r\nline\r\n")
    assert "\r" not in crlf.text


def test_context_trimmed_to_40_code_units():
    m = "x" * 100 + "QUOTE" + "y" * 100
    u = Utf16Text(m)
    p, s = anchors.context_at(u, 100, 105)
    assert len(p) == 40 and len(s) == 40
