#!/usr/bin/env python3
"""Retention for the GraphDB validation-history graph.

The history graph used to grow without bound: every batch appended one more
``sh:ValidationReport`` per SHACL file, each carrying its full set of
``sh:ValidationResult`` nodes. Keeping every generation is expensive (one run
of the whole batch is ~19k result nodes) and the dashboard only ever shows one
state of the world, so this module enforces a bounded window instead:

* the **last two timings** per SHACL file -- enough to show the current run and
  what it changed relative to the run before it;
* the **results of the very last run only** -- older runs keep their
  ``ex:finishedAt`` / ``ex:durationSeconds`` / ``ex:httpStatus`` metadata but
  lose their ``sh:result`` nodes.

``write-validation-run-to-gdb.py`` calls :func:`prune_file` right after it
appends a run, so the window is an invariant of the write path rather than a
cleanup job. Run this module standalone to apply the same window to history
that accumulated before retention existed:

    python3 gdb_history.py --dry-run
    python3 gdb_history.py
    python3 gdb_history.py --keep-timings 3 --keep-results 1
    python3 gdb_history.py --file Equipment-AP-Con-Simple-SHACL.ttl

Pruning is ordering-driven, so a report with no ``ex:finishedAt`` is never
selected for deletion: it cannot be placed in the window and is left alone.
"""
from __future__ import annotations

import argparse
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import gdb_source as gs

# Run metadata predicates. normalize_metadata() lifts exactly these from a
# sh:result node onto the report that owns it.
META_PREDICATES = (
    "ex:file",
    "ex:finishedAt",
    "ex:durationSeconds",
    "ex:httpStatus",
    "ex:repositoryValidated",
    "ex:repository",
    "ex:executionError",
    "ex:errorText",
)

PREFIXES = """
PREFIX sh: <http://www.w3.org/ns/shacl#>
PREFIX ex: <http://example.org/ns#>
"""

# upload-results.py, and the first version of write-validation-run-to-gdb.py,
# tagged whichever subject rdflib serialized first -- for a non-conforming
# report that is one of its sh:ValidationResult nodes. Such a run is invisible
# to a "?report ex:file ?f" window query, so it would survive every prune.
# Lift the metadata onto the report first and the rest of this module (and
# gdb_source's UNION fallback) only ever has to deal with one placement.
NORMALIZE_UPDATE = PREFIXES + """
DELETE { GRAPH %(graph)s { ?meta ?p ?o } }
INSERT { GRAPH %(graph)s { ?report ?p ?o } }
WHERE {
  GRAPH %(graph)s {
    ?report a sh:ValidationReport ;
            sh:result ?meta .
    FILTER NOT EXISTS { ?report ex:file ?ownFile }
    ?meta ex:file %(file)s .
    ?meta ?p ?o .
    FILTER(?p IN (%(predicates)s))
  }
}
"""

# The window itself: order a file's runs newest first and drop everything past
# the Nth. OFFSET without LIMIT is legal SPARQL and is what makes "keep the
# newest N" expressible. The OPTIONAL clears each dropped run's result nodes
# in the same operation.
DROP_OLD_RUNS_UPDATE = PREFIXES + """
DELETE {
  GRAPH %(graph)s {
    ?report ?p ?o .
    ?result ?rp ?ro .
  }
}
WHERE {
  {
    SELECT ?report WHERE {
      GRAPH %(graph)s {
        ?report a sh:ValidationReport ;
                ex:file %(file)s ;
                ex:finishedAt ?finishedAt .
      }
    } ORDER BY DESC(?finishedAt) DESC(?report) OFFSET %(keep)d
  }
  GRAPH %(graph)s {
    ?report ?p ?o .
    OPTIONAL {
      ?report sh:result ?result .
      ?result ?rp ?ro .
    }
  }
}
"""

# Strip results from the runs that stay but are no longer the newest. The
# ex:file guard is belt-and-braces: after normalize_metadata() no result node
# carries run metadata, but skipping a metadata-bearing result keeps a
# half-normalized graph from losing a run outright.
STRIP_OLD_RESULTS_UPDATE = PREFIXES + """
DELETE {
  GRAPH %(graph)s {
    ?report sh:result ?result .
    ?result ?rp ?ro .
  }
}
WHERE {
  {
    SELECT ?report WHERE {
      GRAPH %(graph)s {
        ?report a sh:ValidationReport ;
                ex:file %(file)s ;
                ex:finishedAt ?finishedAt .
      }
    } ORDER BY DESC(?finishedAt) DESC(?report) OFFSET %(keep)d
  }
  GRAPH %(graph)s {
    ?report sh:result ?result .
    FILTER NOT EXISTS { ?result ex:file ?metaFile }
    ?result ?rp ?ro .
  }
}
"""

FILES_QUERY = PREFIXES + """
SELECT DISTINCT ?file WHERE {
  GRAPH %(graph)s {
    ?report a sh:ValidationReport .
    { ?report ex:file ?file }
    UNION
    {
      FILTER NOT EXISTS { ?report ex:file ?ownFile }
      ?report sh:result/ex:file ?file
    }
  }
}
"""

STATS_QUERY = PREFIXES + """
SELECT (COUNT(DISTINCT ?report) AS ?reports) (COUNT(?result) AS ?results)
WHERE {
  GRAPH %(graph)s {
    ?report a sh:ValidationReport .
    OPTIONAL { ?report sh:result ?result }
  }
}
"""

TRIPLES_QUERY = "SELECT (COUNT(*) AS ?triples) WHERE { GRAPH %(graph)s { ?s ?p ?o } }"


def quote_literal(value: str) -> str:
    """Render a plain string as a SPARQL literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def sparql_update(history: dict, update: str, auth_header: str = "", timeout: int = 300) -> None:
    """POST a SPARQL 1.1 UPDATE to the history repository."""
    endpoint = (
        f"{history['base_url'].rstrip('/')}/repositories/{history['repository']}/statements"
    )
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if auth_header:
        name, _, value = auth_header.partition(":")
        if name.strip():
            headers[name.strip()] = value.strip()
    request = Request(
        endpoint, data=urlencode({"update": update}).encode(), headers=headers, method="POST"
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            response.read()
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise SystemExit(f"GraphDB update failed: HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SystemExit(f"GraphDB is unreachable at {endpoint}: {exc}") from exc


def graph_stats(history: dict) -> dict:
    """Report/result/triple counts for the whole history graph."""
    graph = f"<{history['graph']}>"
    counts = gs.sparql_select(history["sparql"], STATS_QUERY % {"graph": graph})
    triples = gs.sparql_select(history["sparql"], TRIPLES_QUERY % {"graph": graph})
    row = counts[0] if counts else {}
    return {
        "reports": int(gs._text(row.get("reports")) or 0),
        "results": int(gs._text(row.get("results")) or 0),
        "triples": int(gs._text(triples[0].get("triples")) or 0) if triples else 0,
    }


def history_files(history: dict) -> list[str]:
    """Every SHACL filename that has at least one run in the graph."""
    rows = gs.sparql_select(history["sparql"], FILES_QUERY % {"graph": f"<{history['graph']}>"})
    return sorted({gs._text(row.get("file")) for row in rows if gs._text(row.get("file"))})


def retention(config: dict | None = None) -> tuple[int, int]:
    """Window sizes from dashboard-config.json ``validation_history``."""
    settings = gs.history_config(config)
    keep_timings = max(1, int(settings["keep_timings"]))
    keep_results = max(1, int(settings["keep_results"]))
    return keep_timings, min(keep_results, keep_timings)


def prune_file(
    history: dict,
    shacl_file: str,
    keep_timings: int = 2,
    keep_results: int = 1,
    auth_header: str = "",
) -> None:
    """Apply the retention window to one SHACL file.

    Normalizes first so legacy runs of this file are inside the window rather
    than invisible to it, then drops runs past ``keep_timings`` and strips
    results from runs past ``keep_results``.
    """
    params = {
        "graph": f"<{history['graph']}>",
        "file": quote_literal(shacl_file),
    }
    sparql_update(
        history,
        NORMALIZE_UPDATE % {**params, "predicates": ", ".join(META_PREDICATES)},
        auth_header,
    )
    sparql_update(history, DROP_OLD_RUNS_UPDATE % {**params, "keep": keep_timings}, auth_header)
    sparql_update(
        history, STRIP_OLD_RESULTS_UPDATE % {**params, "keep": keep_results}, auth_header
    )


def prune_all(
    history: dict,
    keep_timings: int = 2,
    keep_results: int = 1,
    auth_header: str = "",
    only: str = "",
    verbose: bool = True,
) -> int:
    """Apply the retention window to every SHACL file in the graph."""
    files = [only] if only else history_files(history)
    for number, shacl_file in enumerate(files, 1):
        prune_file(history, shacl_file, keep_timings, keep_results, auth_header)
        if verbose:
            print(f"  [{number}/{len(files)}] pruned {shacl_file}", file=sys.stderr)
    return len(files)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--keep-timings", type=int, default=0,
                        help="Runs to keep per SHACL file (default: validation_history.keep_timings)")
    parser.add_argument("--keep-results", type=int, default=0,
                        help="Runs that keep their sh:result nodes (default: validation_history.keep_results)")
    parser.add_argument("--file", default="", help="Prune only this SHACL filename")
    parser.add_argument("--auth-header", default="", help='Full header, e.g. "Authorization: Bearer xyz"')
    parser.add_argument("--dry-run", action="store_true",
                        help="Report current graph size and the window, change nothing")
    return parser.parse_args()


def main() -> int:
    import dashboard_config as dc

    args = parse_args()
    config = dc.load_config()
    history = gs.history_config(config)
    keep_timings, keep_results = retention(config)
    keep_timings = args.keep_timings or keep_timings
    keep_results = args.keep_results or keep_results

    print(f"Repository : {history['repository']}")
    print(f"Named graph: {history['graph']}")
    print(f"Window     : {keep_timings} timing(s) per file, results on the newest {keep_results}")
    before = graph_stats(history)
    print(
        f"Before     : {before['reports']} reports, {before['results']} results, "
        f"{before['triples']} triples"
    )
    if args.dry_run:
        print("Dry run: nothing changed.")
        return 0

    count = prune_all(history, keep_timings, keep_results, args.auth_header, args.file)
    after = graph_stats(history)
    print(
        f"After      : {after['reports']} reports, {after['results']} results, "
        f"{after['triples']} triples"
    )
    print(
        f"Pruned {count} SHACL file(s): "
        f"-{before['reports'] - after['reports']} reports, "
        f"-{before['results'] - after['results']} results, "
        f"-{before['triples'] - after['triples']} triples"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
