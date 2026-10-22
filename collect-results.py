#!/usr/bin/env python3
"""Shared helpers for turning SHACL validation results into dashboard links.

This module is not a pipeline step: ``cube_dashboard.py`` loads it by filename
and uses it to shorten IRIs, index SHACL declarations for GitHub line links,
and ask GraphDB which focus nodes exist in the validated repository. Runs
themselves come from the GraphDB validation-history graph (``gdb_source.py``),
not from files, so nothing here reads a ``validation-report.ttl``.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from urllib.error import URLError, HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import dashboard_config as dc

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIG = dc.load_config()
CATALOG = dc.build_catalog(CONFIG)
_PRIMARY_REPO = CATALOG["repositories"][0]
APL_DIR = _PRIMARY_REPO["checkout_path"]  # Backwards-compatible public constant.
GITHUB_REPO_BLOB = (
    f"https://github.com/{_PRIMARY_REPO['github_repository']}/blob/"
    f"{_PRIMARY_REPO.get('github_ref') or 'main'}"
)
SHACL_SOURCES = [
    (family["id"], subpath)
    for family in _PRIMARY_REPO["families_index"]
    for subpath in family.get("shacl_dirs") or []
]

_DATA_REPOS = {repo["id"]: repo for repo in dc.data_repositories_settings(CONFIG)}
_DATA = dc.data_settings(CONFIG)  # first data repo; module constants stay backward-compatible
GRAPHDB_RESOURCE = _DATA["graphdb_resource"]
GRAPHDB_REPO = _DATA["graphdb_repository"]
GRAPHDB_SPARQL = _DATA["graphdb_sparql"]
GDB_LOOKUP_CHUNK = 100


def data_repo_settings(data_repository_id: str = "") -> dict:
    """Resolve GraphDB link/SPARQL settings for one data repository."""
    if data_repository_id and data_repository_id in _DATA_REPOS:
        return _DATA_REPOS[data_repository_id]
    return _DATA

# Prefixes used only to shorten URIs for display.
DISPLAY_PREFIXES = {
    "https://cim.ucaiug.io/ns#": "cim:",
    "https://cim.ucaiug.io/ns/eu#": "eu:",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#": "rdf:",
    "http://www.w3.org/2000/01/rdf-schema#": "rdfs:",
    "http://www.w3.org/ns/shacl#": "sh:",
    "http://www.w3.org/2001/XMLSchema#": "xsd:",
}


# ---------------------------------------------------------------------------
# Build an index mapping every full subject URI declared in each SHACL file to
# the line number where it is declared. Turtle subjects in these files always
# start at column 0, which makes this reliable.
# ---------------------------------------------------------------------------
def build_shacl_index() -> dict[str, dict]:
    """Return a source-aware subject index, with filename aliases when unique."""
    index: dict[str, dict] = {}
    for source in CATALOG["sources"].values():
        ttl = source["shacl_path"]
        prefixes: dict[str, str] = {}
        base: str | None = None
        uri_to_line: dict[str, int] = {}
        lines = ttl.read_text(encoding="utf-8").splitlines()
        for line in lines:
            m = re.match(r'\s*(?:@prefix|PREFIX)\s+([\w.-]*):\s+<([^>]*)>', line, re.I)
            if m:
                prefixes[m.group(1)] = m.group(2)
                continue
            m = re.match(r'\s*(?:@base|BASE)\s+<([^>]*)>', line, re.I)
            if m:
                base = m.group(1)
        for n, line in enumerate(lines, start=1):
            if not line or line[0] in " \t#@":
                continue
            if line.upper().startswith(("PREFIX ", "BASE ")):
                continue
            token = line.split(None, 1)[0]
            full = resolve_term(token, prefixes, base)
            if full and full not in uri_to_line:
                uri_to_line[full] = n
        entry = {
            **source,
            "family": source["family_id"],
            "subpath": source["shacl_subpath"].rsplit("/", 1)[0],
            "blob_base": (
                f"https://github.com/{source['repo']['github_repository']}/blob/"
                f"{source['repo'].get('github_ref') or 'main'}"
            ),
            "lines": uri_to_line,
        }
        index[source["id"]] = entry
        if len(CATALOG["by_filename"].get(ttl.name, [])) == 1:
            index[ttl.name] = entry
    return index


def resolve_term(token: str, prefixes: dict[str, str], base: str | None) -> str | None:
    """Resolve a Turtle term (``pfx:local`` or ``<iri>``) to a full URI string."""
    token = token.rstrip(";,.")
    if token.startswith("<") and token.endswith(">"):
        iri = token[1:-1]
        if iri.startswith("http://") or iri.startswith("https://") or iri.startswith("urn:"):
            return iri
        if base is not None:  # relative IRI, e.g. <#foo> or <>
            return base + iri
        return iri
    m = re.match(r'([\w.-]*):(\S*)$', token)
    if m and m.group(1) in prefixes:
        return prefixes[m.group(1)] + m.group(2)
    return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def shorten(term: str | None) -> str:
    if not term:
        return ""
    if term.startswith("<") and term.endswith(">"):
        iri = term[1:-1]
        for ns, pfx in DISPLAY_PREFIXES.items():
            if iri.startswith(ns):
                return pfx + iri[len(ns):]
        return iri
    return term


def strip_brackets(term: str | None) -> str | None:
    if term and term.startswith("<") and term.endswith(">"):
        return term[1:-1]
    return None


def strip_literal(term: str | None) -> str:
    if not term:
        return ""
    m = re.match(r'^"((?:[^"\\]|\\.)*)"', term)
    if m:
        return m.group(1).encode().decode("unicode_escape")
    return term


def graphdb_link(
    focus_term: str | None,
    data_repository_id: str = "",
) -> tuple[str, str]:
    """Return (display, href) for a focus node in the given data repository."""
    iri = strip_brackets(focus_term)
    if iri is None:
        return (shorten(focus_term), "")
    data = data_repo_settings(data_repository_id)
    href = (
        f"{data['graphdb_resource']}?uri={quote(iri, safe=':/')}"
        f"&repositoryId={data['graphdb_repository']}&role=all"
    )
    return (iri, href)


def _sparql_iri(iri: str) -> str:
    return "<" + iri.replace("\\", "\\\\").replace(">", "\\>") + ">"


def iris_present_in_graphdb(
    iris: set[str],
    chunk: int = GDB_LOOKUP_CHUNK,
    data_repository_id: str = "",
    sparql: str = "",
) -> set[str]:
    """Return the subset of IRIs that appear in one data repository."""
    endpoint = sparql or data_repo_settings(data_repository_id)["graphdb_sparql"]
    found: set[str] = set()
    ordered = [i for i in iris if i and not i.startswith("_:")]
    if not ordered:
        return found
    total = (len(ordered) + chunk - 1) // chunk
    for n, start in enumerate(range(0, len(ordered), chunk), start=1):
        batch = ordered[start:start + chunk]
        values = " ".join(_sparql_iri(u) for u in batch)
        query = (
            "SELECT DISTINCT ?iri WHERE {\n"
            f"  VALUES ?iri {{ {values} }}\n"
            "  FILTER(\n"
            "    EXISTS { ?iri ?p ?o }\n"
            "    || EXISTS { ?s ?iri ?o }\n"
            "    || EXISTS { ?s ?p ?iri }\n"
            "    || EXISTS { GRAPH ?iri { ?s ?p ?o } }\n"
            "  )\n"
            "}"
        )
        req = Request(
            endpoint,
            data=urlencode({"query": query}).encode(),
            headers={
                "Accept": "application/sparql-results+json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        payload = None
        for attempt in range(2):
            try:
                with urlopen(req, timeout=120) as resp:
                    payload = json.loads(resp.read().decode())
                break
            except (URLError, HTTPError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                if isinstance(exc, URLError) and isinstance(exc.reason, ConnectionRefusedError):
                    print(f"GraphDB presence lookup batch {n}/{total} failed: {exc}", file=sys.stderr)
                    print("GraphDB endpoint is unavailable; skipping remaining presence batches.", file=sys.stderr)
                    return found
                if attempt == 0:
                    print(
                        f"GraphDB presence lookup batch {n}/{total} failed: {exc}; retrying",
                        file=sys.stderr,
                    )
                    time.sleep(0.25)
                else:
                    print(f"GraphDB presence lookup batch {n}/{total} failed: {exc}", file=sys.stderr)
        if payload is None:
            if len(batch) > 1:
                split_size = max(1, len(batch) // 2)
                print(
                    f"Splitting failed GraphDB batch {n}/{total} into chunks of {split_size}.",
                    file=sys.stderr,
                )
                found.update(
                    iris_present_in_graphdb(
                        set(batch),
                        chunk=split_size,
                        data_repository_id=data_repository_id,
                        sparql=endpoint,
                    )
                )
            continue
        for binding in payload.get("results", {}).get("bindings", []):
            val = binding.get("iri", {}).get("value")
            if val:
                found.add(val)
        print(f"GraphDB presence lookup {n}/{total} ({len(found)} found so far)", file=sys.stderr)
    return found


def github_link(term: str | None, shacl_file: str,
                index: dict[str, dict]) -> tuple[str, str]:
    """Return (display, href) for a shape/constraint, linking to its GitHub line."""
    iri = strip_brackets(term)
    if iri is None:
        return (shorten(term), "")
    # Prefer the SHACL file matching this report; fall back to any file.
    candidates = [shacl_file] + [f for f in index if f != shacl_file]
    for fname in candidates:
        entry = index.get(fname)
        if entry and iri in entry["lines"]:
            line = entry["lines"][iri]
            return (shorten(term),
                    f"{entry.get('blob_base') or GITHUB_REPO_BLOB}/"
                    f"{entry['subpath']}/{entry.get('shacl_file') or fname}#L{line}")
    return (shorten(term), "")

