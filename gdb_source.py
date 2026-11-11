#!/usr/bin/env python3
"""Read SHACL validation runs back out of a GraphDB validation-history graph.

This is the data-access half of the dashboard: it turns the named graph
written by ``validate-all-and-write-to-gdb.sh`` /
``write-validation-run-to-gdb.py`` into plain Python run records. It knows
nothing about the cube or the HTML pages -- ``build_dashboard_gdb.py`` glues
these records onto the cube in ``cube_dashboard.py``.

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
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
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
SELECT ?file ?dataRepositoryId ?finishedAt ?duration ?status ?validated ?conforms
       ?truncated ?executionError ?errorText
WHERE {
  GRAPH %(graph)s {
    ?report a sh:ValidationReport .
    %(meta)s
    OPTIONAL { ?meta ex:dataRepositoryId ?dataRepositoryId }
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
    """History GraphDB target from ``dashboard-config.json``.

    Environment overrides use the same names as
    ``validate-all-and-write-to-gdb.sh`` so reader and writer stay in step.
    """
    return dc.history_settings(config)


# Process-wide GraphDB bearer token. None means this process is anonymous.
_cached_auth: str | None = None
_auth_resolved = False


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the login password on the GraphDB host that received it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SystemExit(
            f"GraphDB login redirected ({code}) to {newurl}. "
            "Refusing to follow the redirect with credentials."
        )


def invalidate_authorization() -> None:
    """Drop the cached token so the next query logs in again."""
    global _cached_auth, _auth_resolved
    _cached_auth = None
    _auth_resolved = False


def _header_safe(value: str, label: str) -> str:
    if "\r" in value or "\n" in value:
        raise SystemExit(f"{label} must not contain newlines.")
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        raise SystemExit(
            f"{label} contains characters the GraphDB login header cannot send."
        ) from None
    return value


def _login(base_url: str, user: str, password: str) -> str:
    """POST /rest/login and return the Authorization value GraphDB issued."""
    url = f"{base_url.rstrip('/')}/rest/login/{quote(user, safe='')}"
    request = Request(
        url,
        data=b"",
        headers={"X-GraphDB-Password": _header_safe(password, "GDBPASS")},
        method="POST",
    )
    opener = urllib.request.build_opener(_RefuseRedirect)
    try:
        with opener.open(request, timeout=60) as response:
            issued = response.headers.get("Authorization")
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        if password and password in detail:
            detail = "(response omitted)"
        raise SystemExit(
            f"GraphDB login failed: HTTP {exc.code} from {url}: {detail}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SystemExit(f"GraphDB login is unreachable at {url}: {exc}") from exc
    if not issued or not issued.strip():
        raise SystemExit(f"GraphDB login failed: no Authorization header from {url}")
    token = issued.strip()
    _header_safe(token, "GraphDB Authorization token")
    print("GraphDB: authenticated for SPARQL reads.", file=sys.stderr)
    return token


def authorization_value() -> str | None:
    """Bearer value for SPARQL, or None when no credentials were supplied.

    ``GDBUSER`` and ``GDBPASS`` must both be set or both be empty. Empty values
    keep the previous anonymous requests, so a local GraphDB with security
    disabled still builds. The password is never read from ``dashboard-config.json``.
    """
    global _cached_auth, _auth_resolved
    if _auth_resolved:
        return _cached_auth
    user = os.environ.get("GDBUSER", "").strip()
    password = os.environ.get("GDBPASS", "")
    if user == "" and password == "":
        _auth_resolved = True
        _cached_auth = None
        return None
    if user == "" or password == "":
        raise SystemExit("GDBUSER and GDBPASS must both be set, or both be unset.")
    _header_safe(user, "GDBUSER")
    token = _login(history_config()["base_url"], user, password)
    _cached_auth = token
    _auth_resolved = True
    return token


def authorization_for(endpoint: str) -> str | None:
    """Token to send to ``endpoint``, or None for an anonymous request.

    The token is only attached when the query host is the same host that
    issued it.
    """
    token = authorization_value()
    if token is None:
        return None
    login_host = urlparse(history_config()["base_url"]).netloc
    query_host = urlparse(endpoint).netloc
    if not query_host or query_host != login_host:
        raise SystemExit(
            f"Refusing to send GraphDB credentials to {query_host or endpoint}; "
            f"login host is {login_host}."
        )
    return token


def redact_secret(text: str) -> str:
    """Hide ``GDBPASS`` if a GraphDB error body echoes it."""
    password = os.environ.get("GDBPASS", "")
    if password and password in text:
        return "(response omitted)"
    return text


def sparql_select(endpoint: str, query: str, timeout: int = 300) -> list[dict]:
    """POST a SELECT query and return its bindings.

    When ``GDBUSER`` and ``GDBPASS`` are set, the request carries the
    Authorization value from ``/rest/login``. A 401 logs in once more and
    retries. With neither variable set, the request stays anonymous.
    """
    payload = None
    for attempt in range(2):
        headers = {
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        auth = authorization_for(endpoint)
        if auth:
            headers["Authorization"] = auth
        request = Request(
            endpoint,
            data=urlencode({"query": query}).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode())
            break
        except HTTPError as exc:
            detail = redact_secret(exc.read().decode(errors="replace")[:500])
            if exc.code == 401 and auth and attempt == 0:
                invalidate_authorization()
                continue
            raise SystemExit(
                f"GraphDB query failed: HTTP {exc.code}: {detail}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise SystemExit(f"GraphDB is unreachable at {endpoint}: {exc}") from exc
    if payload is None:
        raise SystemExit(f"GraphDB query failed at {endpoint}")
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
    """Render one SPARQL JSON binding as the Turtle term
    ``collect-results.py`` helpers consume (IRI brackets, ASCII-escaped
    literals)."""
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


def fetch_runs(
    history: dict,
    as_of: str | None = None,
    data_repo_id: str = "",
) -> dict[str, list[dict]]:
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
                "data_repository_id": _text(row.get("dataRepositoryId")) or data_repo_id,
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
    """Load one history graph and select the latest run per SHACL file.

    Prefer :func:`load_runs_multi_repo` when more than one data repository is
    configured.
    """
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


def load_runs_multi_repo(
    data_repositories: list[dict],
    history_settings: dict,
    as_of: str | None = None,
) -> dict:
    """Load validation runs from every data repository's shared named graph."""
    by_data_repo: dict[str, dict[str, list[dict]]] = {}
    selected: dict[tuple[str, str], dict] = {}
    run_total = 0
    unresolved_total = 0

    for data_repo in data_repositories:
        data_repo_id = data_repo["id"]
        print(f"Loading runs from data repository: {data_repo_id}", file=sys.stderr)
        history = {
            "base_url": history_settings["base_url"],
            "repository": data_repo["graphdb_repository"],
            "graph": history_settings["graph"],
            "sparql": data_repo["graphdb_sparql"],
        }
        by_file = fetch_runs(history, as_of, data_repo_id)
        by_data_repo[data_repo_id] = by_file
        data_repo_selected = {shacl_file: runs[0] for shacl_file, runs in by_file.items()}
        fetch_results(history, list(data_repo_selected.values()))
        for shacl_file, run in data_repo_selected.items():
            selected[(data_repo_id, shacl_file)] = run
        run_total += sum(len(runs) for runs in by_file.values())
        unresolved_total += count_unresolved(history)

    return {
        "data_repositories": data_repositories,
        "history_settings": history_settings,
        "as_of": as_of or "",
        "by_data_repo": by_data_repo,
        "selected": selected,
        "run_total": run_total,
        "unresolved_total": unresolved_total,
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
    config = dc.load_config()
    history_settings = dc.history_settings(config)
    data_repos = dc.data_repositories_settings(config)
    snapshot = load_runs_multi_repo(data_repos, history_settings, args.as_of or None)
    print(f"Data repos : {', '.join(repo['graphdb_repository'] for repo in data_repos)}")
    print(f"Named graph: {history_settings['graph']}")
    print(
        f"Runs       : {snapshot['run_total']} recorded, "
        f"{len(snapshot['selected'])} selected (latest per data repo × SHACL file)"
    )
    if snapshot["unresolved_total"]:
        print(f"Unresolved : {snapshot['unresolved_total']} reports without any ex:file")
    for (data_repo_id, shacl_file), run in sorted(snapshot["selected"].items()):
        runs = len(snapshot["by_data_repo"][data_repo_id][shacl_file])
        flag = "ERROR" if run["execution_error"] else ("trunc" if run["truncated"] else "")
        delta = (
            f"{run['duration_delta']:+.3f}s" if run["duration_delta"] is not None else "—"
        )
        print(
            f"  [{data_repo_id}] {shacl_file:<70} {run['finished_at']:<26} "
            f"{run['duration'] if run['duration'] is not None else '?':>8}s "
            f"{delta:>10} "
            f"{len(run['results']):>6} results  {runs} run(s) {flag}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
