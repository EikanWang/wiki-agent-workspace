"""Read .xlsx workbooks as plain tables (values only), with the standard library: an xlsx is a zip of XML parts."""

from __future__ import annotations

import posixpath
import re
import zipfile
from typing import Any
from xml.etree import ElementTree as ET

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "pr": "http://schemas.openxmlformats.org/package/2006/relationships"}
_REF = re.compile(r"([A-Z]+)(\d+)")


def _col(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _text(el: ET.Element | None) -> str:
    return "" if el is None else "".join(t.text or "" for t in el.iter(f"{{{NS['m']}}}t"))


def read_xlsx(path: str, max_rows: int = 2000, max_cols: int = 200) -> list[dict[str, Any]]:
    """Each sheet: name, rows (lists of strings, trailing empties trimmed), and whether rows / columns were cut."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            shared = [_text(si) for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS)]
        rels = {r.get("Id"): r.get("Target", "") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")).findall("pr:Relationship", NS)}
        out = []
        for sh in ET.fromstring(z.read("xl/workbook.xml")).findall("m:sheets/m:sheet", NS):
            target = rels.get(sh.get(f"{{{NS['r']}}}id"), "")
            part = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
            rows: list[list[str]] = []
            cut_rows = cut_cols = False
            if part in names:
                for _, row in ET.iterparse(z.open(part)):
                    if row.tag != f"{{{NS['m']}}}row":
                        continue
                    r = int(row.get("r") or len(rows) + 1) - 1
                    if r >= max_rows:
                        cut_rows = True
                        break
                    vals: list[str] = []
                    for i, c in enumerate(row.findall("m:c", NS)):
                        m = _REF.match(c.get("r") or "")
                        ci = _col(m.group(1)) if m else i
                        if ci >= max_cols:
                            cut_cols = True
                            continue
                        t, v = c.get("t"), c.find("m:v", NS)
                        if t == "s" and v is not None and v.text:
                            s = shared[int(v.text)] if int(v.text) < len(shared) else ""
                        elif t == "inlineStr":
                            s = _text(c.find("m:is", NS))
                        elif t == "b":
                            s = "TRUE" if (v is not None and v.text == "1") else "FALSE"
                        else:
                            s = (v.text or "") if v is not None else ""
                        vals += [""] * (ci - len(vals)) + [s] if ci >= len(vals) else []
                    while vals and vals[-1] == "":
                        vals.pop()
                    rows += [[] for _ in range(r - len(rows))]
                    rows.append(vals)
                    row.clear()
            while rows and not rows[-1]:
                rows.pop()
            out.append({"name": sh.get("name", ""), "rows": rows, "truncated_rows": cut_rows, "truncated_cols": cut_cols})
        return out
