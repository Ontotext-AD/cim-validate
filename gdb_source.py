#!/usr/bin/env python3
"""Read SHACL validation runs back out of a GraphDB validation-history graph.

This is the data-access half of the GraphDB-backed dashboard: it turns the
named graph written by ``validate-all-and-write-to-gdb.sh`` /
``write-validation-run-to-gdb.py`` into plain Python run records. It knows
nothing about the cube or the HTML pages -- ``build_dashboard_gdb.py`` glues
these records onto the same cube used by the file-based dashboard.

The history graph holds one ``sh:ValidationReport`` node per validation of one
SHACL file, trimmed by ``gdb_history.py`` to the **last two timings** per file
with ``sh:result`` nodes on the **newest run only**. A dashboard shows one
state of the world, so ``load_runs()`` selects the latest run per SHACL file,
optionally as of a cut-off timestamp, and :func:`link_previous` attaches the
preceding timing so the pages can report what changed between the two.

Standalone usage (prints the selected snapshot as a table):

    python3 gdb_source.py
    python3 gdb_source.py --as-of 2026-09-20T00:00:00+03:00
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import dashboard_config as dc

ROOT = Path(__file__).resolve().parent

SH = "http://www.w3.org/ns/shacl#"
EX = "http://example.org/ns#"

# Run metadata normally sits on the sh:ValidationReport node. Reports uploaded
# by upload-results.py -- and by the first version of
# write-validation-run-to-gdb.py -- attached it to whichever subject rdflib
# happened to serialize first, which for a non-conforming report is one of its
# sh:ValidationResult nodes. Resolve both placements so history that is
# already in the graph stays readable.
META_PATTERN = """
    { ?report ex:file ?file . BIND(?report AS ?meta) }
    UNION
    {
      FILTER NOT EXISTS { ?report ex:file ?ownFile }
      ?report sh:result ?meta .
      ?meta ex:file ?file .
    }
"""

RUNS_QUERY = """
PREFIX sh: <http://www.w3.org/ns/shacl#>
PREFIX ex: <http://example.org/ns#>
PREFIX rdf4j: <http://rdf4j.org/schema/rdf4j#>
SELECT ?file ?finishedAt ?duration ?status ?validated ?conforms ?truncated
       ?executionError ?errorText
WHERE {
  GRAPH %(graph)s {
    ?report a sh:ValidationReport .
    %(meta)s
    OPTIONAL { ?meta ex:finishedAt ?finishedAt }
    OPTIONAL { ?meta ex:durationSeconds ?duration }
    OPTIONAL { ?meta ex:httpStatus ?status }
    OPTIONAL { ?meta ex:executionError ?executionError }
    OPTIONAL { ?meta ex:errorText ?errorText }
    OPTIONAL { ?meta ex:repositoryValidated ?validatedNew }
    OPTIONAL { ?meta ex:repository ?validatedOld }
    OPTIONAL { ?report sh:conforms ?conforms }
    OPTIONAL { ?report rdf4j:truncated ?truncated }
    BIND(COALESCE(?validatedNew, ?validatedOld) AS ?validated)
  }
}
"""

RESULTS_QUERY = """
PREFIX sh: <http://www.w3.org/ns/shacl#>
PREFIX ex: <http://example.org/ns#>
SELECT ?file ?finishedAt ?focusNode ?severity ?message ?sourceConstraint
       ?sourceShape ?component ?path ?value
WHERE {
  VALUES (?file ?finishedAt) { %(values)s }
  GRAPH %(graph)s {
    ?report a sh:ValidationReport ;
            sh:result ?result .
    %(meta)s
    ?meta ex:finishedAt ?finishedAt .
    ?result sh:focusNode ?focusNode ;
            sh:resultSeverity ?severity .
    OPTIONAL { ?result sh:resultMessage ?message }
    OPTIONAL { ?result sh:sourceConstraint ?sourceConstraint }
    OPTIONAL { ?result sh:sourceShape ?sourceShape }
    OPTIONAL { ?result sh:sourceConstraintComponent ?component }
    OPTIONAL { ?result sh:resultPath ?path }
    OPTIONAL { ?result sh:value ?value }
  }
}
"""

# Reports whose run metadata cannot be resolved at all: they carry results but
# neither the report nor any of its results names the validated SHACL file, so
# they cannot be attached to a source and are reported as skipped.
UNRESOLVED_QUERY = """
PREFIX sh: <http://www.w3.org/ns/shacl#>
PREFIX ex: <http://example.org/ns#>
SELECT (COUNT(DISTINCT ?report) AS ?reports)
WHERE {
  GRAPH %(graph)s {
    ?report a sh:ValidationReport .
    FILTER NOT EXISTS { ?report ex:file ?ownFile }
    FILTER NOT EXISTS { ?report sh:result/ex:file ?resultFile }
  }
}
"""

# Runs per SPARQL request when fetching validation results. Each run can carry
# thousands of results, so keep the batches small enough to stay responsive.
RUN_CHUNK = 12


def history_config(config: dict | None = None) -> dict:
    """Resolve the history repository/graph from config, then environment.

    Environment overrides use the same names as
    ``validate-all-and-write-to-gdb.sh`` so reader and writer stay in step.
    """
    config = config if config is not None else dc.load_config()
    raw = config.get("validation_history") or {}
    base_url = os.environ.get("GDB_BASE_URL") or raw.get("base_url") or "http://localhost:7200/"
    repository = (
        os.environ.get("GDB_HISTORY_REPOSITORY") or raw.get("repository") or "cim-shacl"
    )
    graph = (
        os.environ.get("GDB_HISTORY_GRAPH")
        or raw.get("graph")
        or "https://github.com/entsoe/application-profiles/SHACL/reports"
    )
    return {
        "base_url": base_url,
        "repository": repository,
        "graph": graph,
        "sparql": raw.get("sparql") or f"{base_url.rstrip('/')}/repositories/{repository}",
        "index": raw.get("index") or "index.html",
        "dashboard_dir": raw.get("dashboard_dir") or "dashboard",
        "keep_timings": int(raw.get("keep_timings") or 2),
        "keep_results": int(raw.get("keep_results") or 1),
    }


def sparql_select(endpoint: str, query: str, timeout: int = 300) -> list[dict]:
    """POST a SELECT query and return its bindings."""
    request = Request(
        endpoint,
        data=urlencode({"query": query}).encode(),
        headers={
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise SystemExit(f"GraphDB query failed: HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SystemExit(f"GraphDB is unreachable at {endpoint}: {exc}") from exc
    return payload.get("results", {}).get("bindings", [])


SHORT_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def escape_literal(value: str) -> str:
    """Escape a lexical form as an ASCII-only Turtle string body.

    ``collect-results.py`` unescapes literals with ``unicode_escape``, which
    is a latin-1 codec and corrupts raw non-ASCII text, so escape everything
    outside printable ASCII numerically.
    """
    characters = []
    for character in value:
        code = ord(character)
        if character in SHORT_ESCAPES:
            characters.append(SHORT_ESCAPES[character])
        elif 0x20 <= code <= 0x7E:
            characters.append(character)
        elif code <= 0xFFFF:
            characters.append(f"\\u{code:04X}")
        else:
            characters.append(f"\\U{code:08X}")
    return "".join(characters)


def term_to_turtle(binding: dict | None) -> str:
    """Render one SPARQL JSON binding as the Turtle term the file-based parser
    produces, so ``collect-results.py`` helpers can consume it unchanged."""
    if not binding:
        return ""
    kind = binding.get("type")
    value = binding.get("value", "")
    if kind == "uri":
        return f"<{value}>"
    if kind == "bnode":
        return f"_:{value}"
    literal = f'"{escape_literal(value)}"'
    language = binding.get("xml:lang")
    if language:
        return f"{literal}@{language}"
    datatype = binding.get("datatype")
    if datatype and datatype != "http://www.w3.org/2001/XMLSchema#string":
        return f"{literal}^^<{datatype}>"
    return literal


def _text(binding: dict | None) -> str:
    return (binding or {}).get("value", "")


def _flag(binding: dict | None) -> bool:
    return _text(binding).strip().lower() == "true"


def _number(binding: dict | None) -> float | None:
    raw = _text(binding)
    try:
        return float(raw)
    except ValueError:
        return None


def sort_key(finished_at: str) -> tuple[int, object]:
    """Order runs by instant, falling back to lexical order when unparsable."""
    try:
        return (1, datetime.fromisoformat(finished_at))
    except ValueError:
        return (0, finished_at)


def run_href(history: dict, shacl_file: str) -> str:
    """GraphDB SPARQL editor link listing every recorded run of one file."""
    query = (
        "PREFIX sh: <http://www.w3.org/ns/shacl#>\n"
        "PREFIX ex: <http://example.org/ns#>\n"
        "SELECT ?finishedAt ?durationSeconds ?httpStatus ?conforms WHERE {\n"
        f"  GRAPH <{history['graph']}> {{\n"
        "    ?report a sh:ValidationReport ;\n"
        f'            ex:file "{shacl_file}" ;\n'
        "            ex:finishedAt ?finishedAt ;\n"
        "            ex:durationSeconds ?durationSeconds ;\n"
        "            ex:httpStatus ?httpStatus .\n"
        "    OPTIONAL { ?report sh:conforms ?conforms }\n"
        "  }\n"
        "} ORDER BY DESC(?finishedAt)"
    )
    return (
        f"{history['base_url'].rstrip('/')}/sparql"
        f"?repositoryId={quote(history['repository'])}&query={quote(query)}"
    )


def fetch_runs(history: dict, as_of: str | None = None) -> dict[str, list[dict]]:
    """Return every recorded run, grouped by SHACL filename, newest first."""
    query = RUNS_QUERY % {"graph": f"<{history['graph']}>", "meta": META_PATTERN}
    by_file: dict[str, list[dict]] = {}
    cutoff = sort_key(as_of) if as_of else None
    for row in sparql_select(history["sparql"], query):
        shacl_file = _text(row.get("file"))
        if not shacl_file:
            continue
        finished_at = _text(row.get("finishedAt"))
        if cutoff and sort_key(finished_at) > cutoff:
            continue
        by_file.setdefault(shacl_file, []).append(
            {
                "shacl_file": shacl_file,
                "finished_at": finished_at,
                "duration": _number(row.get("duration")),
                "http_status": _text(row.get("status")),
                "repository_validated": _text(row.get("validated")),
                "conforms": _flag(row.get("conforms")),
                "truncated": _flag(row.get("truncated")),
                "execution_error": _flag(row.get("executionError")),
                "error_text": _text(row.get("errorText")),
                "results": [],
            }
        )
    for runs in by_file.values():
        runs.sort(key=lambda run: sort_key(run["finished_at"]), reverse=True)
        link_previous(runs)
    return by_file


def link_previous(runs: list[dict]) -> None:
    """Attach each run's predecessor timing, in place.

    ``runs`` is newest first. Retention keeps the last two timings per SHACL
    file precisely so this comparison exists: the dashboard reports how the
    run it is built from differs from the one before it.
    """
    for position, run in enumerate(runs):
        older = runs[position + 1] if position + 1 < len(runs) else None
        run["previous_finished_at"] = older["finished_at"] if older else ""
        run["previous_duration"] = older["duration"] if older else None
        run["duration_delta"] = (
            run["duration"] - older["duration"]
            if older
            and isinstance(run["duration"], (int, float))
            and isinstance(older["duration"], (int, float))
            else None
        )


def fetch_results(history: dict, runs: list[dict]) -> None:
    """Attach ``sh:ValidationResult`` rows to each run, in place.

    Runs are identified by (``ex:file``, ``ex:finishedAt``) because their
    report nodes are blank nodes, which a SPARQL query cannot address.
    """
    selectable = [run for run in runs if run["finished_at"]]
    by_key = {(run["shacl_file"], run["finished_at"]): run for run in selectable}
    total = (len(selectable) + RUN_CHUNK - 1) // RUN_CHUNK
    for number, start in enumerate(range(0, len(selectable), RUN_CHUNK), start=1):
        batch = selectable[start:start + RUN_CHUNK]
        values = " ".join(
            f'("{run["shacl_file"]}" "{run["finished_at"]}"'
            "^^<http://www.w3.org/2001/XMLSchema#dateTime>)"
            for run in batch
        )
        query = RESULTS_QUERY % {
            "graph": f"<{history['graph']}>",
            "meta": META_PATTERN,
            "values": values,
        }
        rows = sparql_select(history["sparql"], query)
        for row in rows:
            key = (_text(row.get("file")), _text(row.get("finishedAt")))
            run = by_key.get(key)
            if run is None:
                continue
            run["results"].append(
                {
                    "sh:focusNode": term_to_turtle(row.get("focusNode")),
                    "sh:resultSeverity": term_to_turtle(row.get("severity")),
                    "sh:resultMessage": term_to_turtle(row.get("message")),
                    "sh:sourceConstraint": term_to_turtle(row.get("sourceConstraint")),
                    "sh:sourceShape": term_to_turtle(row.get("sourceShape")),
                    "sh:sourceConstraintComponent": term_to_turtle(row.get("component")),
                    "sh:resultPath": term_to_turtle(row.get("path")),
                    "sh:value": term_to_turtle(row.get("value")),
                }
            )
        print(
            f"GraphDB result fetch {number}/{total} ({len(rows)} rows)",
            file=sys.stderr,
        )


def count_unresolved(history: dict) -> int:
    query = UNRESOLVED_QUERY % {"graph": f"<{history['graph']}>"}
    rows = sparql_select(history["sparql"], query)
    return int(_text(rows[0].get("reports")) or 0) if rows else 0


def load_runs(history: dict, as_of: str | None = None) -> dict:
    """Load the history graph and select the latest run per SHACL file."""
    by_file = fetch_runs(history, as_of)
    selected = {shacl_file: runs[0] for shacl_file, runs in by_file.items()}
    fetch_results(history, list(selected.values()))
    return {
        "history": history,
        "as_of": as_of or "",
        "by_file": by_file,
        "selected": selected,
        "run_total": sum(len(runs) for runs in by_file.values()),
        "unresolved": count_unresolved(history),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--as-of",
        default="",
        help="Only consider runs finished at or before this ISO-8601 timestamp",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    history = history_config()
    snapshot = load_runs(history, args.as_of or None)
    print(f"Repository : {history['repository']}")
    print(f"Named graph: {history['graph']}")
    print(
        f"Runs       : {snapshot['run_total']} recorded, "
        f"{len(snapshot['selected'])} selected (latest per SHACL file)"
    )
    if snapshot["unresolved"]:
        print(f"Unresolved : {snapshot['unresolved']} reports without any ex:file")
    for shacl_file, run in sorted(snapshot["selected"].items()):
        runs = len(snapshot["by_file"][shacl_file])
        flag = "ERROR" if run["execution_error"] else ("trunc" if run["truncated"] else "")
        delta = (
            f"{run['duration_delta']:+.3f}s" if run["duration_delta"] is not None else "—"
        )
        print(
            f"  {shacl_file:<78} {run['finished_at']:<26} "
            f"{run['duration'] if run['duration'] is not None else '?':>8}s "
            f"{delta:>10} "
            f"{len(run['results']):>6} results  {runs} run(s) {flag}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
