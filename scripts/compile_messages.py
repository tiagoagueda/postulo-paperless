"""Compile every ``django.po`` under ``locale/`` into the ``django.mo`` Django reads.

Django's own ``compilemessages`` needs GNU gettext installed, which not every machine has.
This does the same job for the simple catalogues a plugin carries, in pure Python, so a
plugin author on any system can run ``python scripts/compile_messages.py`` and commit the
result. Plural forms and fuzzy entries are handled the way ``msgfmt`` handles them:
fuzzy entries are left out, plurals are written with their NUL-separated variants.

Run it from the repository root. Nothing else is needed.
"""

from __future__ import annotations

import ast
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def parse_po(text: str) -> dict[str, str]:
    """The catalogue as msgid -> msgstr, plurals joined the way .mo files expect."""
    entries: dict[str, str] = {}
    msgid: list[str] = []
    msgid_plural: list[str] = []
    msgstr: dict[int, list[str]] = {}
    current: list[str] | None = None
    fuzzy = False

    def flush() -> None:
        nonlocal msgid, msgid_plural, msgstr, current, fuzzy
        if msgid is not None and (msgid or msgstr):
            key = "".join(msgid)
            if msgid_plural:
                key += "\0" + "".join(msgid_plural)
            value = "\0".join("".join(msgstr[index]) for index in sorted(msgstr))
            if not fuzzy and (value or key == ""):
                entries[key] = value
        msgid, msgid_plural, msgstr, current, fuzzy = [], [], {}, None, False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith("#"):
            if line.startswith("#,") and "fuzzy" in line:
                fuzzy = True
            continue
        if line.startswith("msgid_plural "):
            current = msgid_plural
            current.append(ast.literal_eval(line[len("msgid_plural ") :]))
        elif line.startswith("msgid "):
            if msgid or msgstr:
                flush()
            current = msgid
            current.append(ast.literal_eval(line[len("msgid ") :]))
        elif line.startswith("msgstr["):
            index = int(line[len("msgstr[") : line.index("]")])
            current = msgstr.setdefault(index, [])
            current.append(ast.literal_eval(line[line.index("]") + 1 :].strip()))
        elif line.startswith("msgstr "):
            current = msgstr.setdefault(0, [])
            current.append(ast.literal_eval(line[len("msgstr ") :]))
        elif line.startswith('"') and current is not None:
            current.append(ast.literal_eval(line))
    flush()
    return entries


def write_mo(entries: dict[str, str], target: Path) -> None:
    keys = sorted(entries)
    ids = [key.encode("utf-8") for key in keys]
    strs = [entries[key].encode("utf-8") for key in keys]
    count = len(keys)
    header = 7 * 4
    table = count * 8
    ids_start = header + 2 * table
    strs_start = ids_start + sum(len(item) + 1 for item in ids)

    out = struct.pack("<7I", 0x950412DE, 0, count, header, header + table, 0, 0)
    offset = ids_start
    for item in ids:
        out += struct.pack("<2I", len(item), offset)
        offset += len(item) + 1
    offset = strs_start
    for item in strs:
        out += struct.pack("<2I", len(item), offset)
        offset += len(item) + 1
    out += b"".join(item + b"\0" for item in ids)
    out += b"".join(item + b"\0" for item in strs)
    target.write_bytes(out)


def main() -> int:
    compiled = 0
    for po in sorted(ROOT.glob("src/*/locale/*/LC_MESSAGES/django.po")):
        entries = parse_po(po.read_text(encoding="utf-8"))
        write_mo(entries, po.with_suffix(".mo"))
        compiled += 1
        print(f"{po.relative_to(ROOT)}: {len(entries) - 1} strings")
    if not compiled:
        print("No catalogues found under src/*/locale/.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
