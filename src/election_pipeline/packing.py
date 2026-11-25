"""Build a multi-file manual import's zip from the previous import and an additions file.

A state whose results come in several files is imported as one zip (see
docs/manual-results.md), and each import is the previous one with files added or
replaced. Its parser declares the zip's layout as ``PACK``:

- ``{"manifest": "manifest.json", "section": "counties", "review": "{key}.review.json"}``:
  a manifest whose section names each place's files (every ``{"file": ...}`` in its
  entry) with their SHA-256s (Illinois, Wisconsin), and a place's signed review beside
  them.
- ``{"listing": "sources.json"}``: a listing keyed by member, each with its SHA-256
  (New Jersey).
- ``{"members": (...)}``: a fixed set of the state's files (Maine, New Hampshire,
  Kentucky), or ``{"members": None}`` for files the parser checks against a manifest of
  the state's own (Nevada).

An additions file (JSON) names what changes:

    {"remove": [key or member, ...],
     "members": {member: path relative to the additions file, ...},
     "entries": {key: manifest or listing entry, ...}}

An entry replaces its key's entry and files. Every ``{"file": member}`` in it gets the
member's SHA-256, computed here from the bytes, so a hash is never typed by hand. The zip
is written deterministically (``write_zip``): the same inputs give the same SHA-256.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path


def sha256(body):
    return hashlib.sha256(body).hexdigest()


def read_zip(raw):
    """{member: bytes} of a zip."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def write_zip(path, members):
    """Write ``members`` as a zip the same way on every system: sorted, dated 1980-01-01,
    deflated, marked as made on the system that packed the first imports."""
    with zipfile.ZipFile(path, "w") as archive:
        for name, body in sorted(members.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            archive.writestr(info, body)


def named_files(entry):
    """Every member an entry names (``{"file": member}`` anywhere in it), in order."""
    if isinstance(entry, dict):
        own = [entry["file"]] if isinstance(entry.get("file"), str) else []
        return own + [
            n for v in entry.values() if isinstance(v, (dict, list)) for n in named_files(v)
        ]
    if isinstance(entry, list):
        return [n for item in entry for n in named_files(item)]
    return []


def with_hashes(entry, members):
    """``entry`` with each ``{"file": member}``'s SHA-256 set from ``members``."""
    if isinstance(entry, list):
        return [with_hashes(item, members) for item in entry]
    if not isinstance(entry, dict):
        return entry
    result = {k: with_hashes(v, members) for k, v in entry.items()}
    if isinstance(entry.get("file"), str):
        if entry["file"] not in members:
            raise ValueError(f"No file for member {entry['file']!r}")
        result["sha256"] = sha256(members[entry["file"]])
    return result


def document(body):
    return json.dumps(body, indent=1, sort_keys=True).encode()


def pack(layout, base, *, remove=(), files=None, entries=None):
    """The members of a new import: ``base`` (the previous import's members) with
    ``remove`` taken out, ``files`` ({member: bytes}) added and ``entries`` (manifest or
    listing entries by key) replacing their keys'. Checked against ``layout``."""
    members, files, entries = dict(base), dict(files or {}), dict(entries or {})
    if "manifest" in layout:
        name, section = layout["manifest"], layout["section"]
        manifest = json.loads(members.pop(name)) if name in members else {section: {}}
        places = manifest[section]
        for key in [*remove, *entries]:
            if key in places:
                review = layout.get("review", "").format(key=key)
                for member in [*named_files(places.pop(key)), review]:
                    members.pop(member, None)
            elif key in remove:
                raise ValueError(f"{key} is not in the previous import")
        owners = {m: key for key, entry in places.items() for m in named_files(entry)}
        for key, entry in sorted(entries.items()):
            clash = set(named_files(entry)) & set(owners)
            if clash:
                raise ValueError(f"{key}: {sorted(clash)} belong to another entry")
            places[key] = with_hashes(entry, {**members, **files})
        members.update(files)
        members[name] = document(manifest)
    elif "listing" in layout:
        name = layout["listing"]
        listing = json.loads(members.pop(name)) if name in members else {}
        for member in remove:
            if member not in listing:
                raise ValueError(f"{member} is not in the previous import")
            listing.pop(member)
            members.pop(member, None)
        members.update(files)
        for member, entry in sorted(entries.items()):
            if member not in members:
                raise ValueError(f"No file for member {member!r}")
            listing[member] = {**entry, "sha256": sha256(members[member])}
        members[name] = document(listing)
    else:
        for member in remove:
            if member not in members:
                raise ValueError(f"{member} is not in the previous import")
            members.pop(member)
        if entries:
            raise ValueError("This import has no manifest: give files only")
        members.update(files)
    check(layout, members)
    return members


def check(layout, members):
    """Refuse members that do not match the layout: a manifest or listing naming a file
    that is missing or has another SHA-256, a file nothing names, or a fixed set that is
    not exactly the state's files."""
    if "manifest" in layout:
        places = json.loads(members[layout["manifest"]])[layout["section"]]
        named = {layout["manifest"]}
        for key, entry in places.items():
            for item in _with_file(entry):
                if item["file"] not in members or sha256(members[item["file"]]) != item.get(
                    "sha256"
                ):
                    raise ValueError(f"{key}: {item['file']} is missing or differs from its hash")
                named.add(item["file"])
            review = layout.get("review", "").format(key=key)
            if review and review in members:
                named.add(review)
        if set(members) != named:
            raise ValueError(f"Files nothing names: {sorted(set(members) - named)}")
    elif "listing" in layout:
        listing = json.loads(members[layout["listing"]])
        for member, entry in listing.items():
            if member not in members or sha256(members[member]) != entry.get("sha256"):
                raise ValueError(f"{member} is missing or differs from its listed hash")
        if set(members) != {*listing, layout["listing"]}:
            raise ValueError("The listing does not name exactly the import's files")
    elif layout["members"] is not None and set(members) != set(layout["members"]):
        raise ValueError(
            f"The import must be exactly {sorted(layout['members'])}; it has {sorted(members)}"
        )


def _with_file(entry):
    if isinstance(entry, dict):
        own = [entry] if isinstance(entry.get("file"), str) else []
        return own + [
            i for v in entry.values() if isinstance(v, (dict, list)) for i in _with_file(v)
        ]
    if isinstance(entry, list):
        return [i for item in entry for i in _with_file(item)]
    return []


def additions(path):
    """(remove, files, entries) from an additions file; paths are relative to it."""
    path = Path(path)
    spec = json.loads(path.read_text(encoding="utf-8"))
    unknown = set(spec) - {"remove", "members", "entries"}
    if unknown:
        raise ValueError(f"Unknown additions fields: {sorted(unknown)}")
    files = {m: (path.parent / p).read_bytes() for m, p in (spec.get("members") or {}).items()}
    return list(spec.get("remove") or []), files, dict(spec.get("entries") or {})
