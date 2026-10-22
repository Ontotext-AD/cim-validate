#!/usr/bin/env python3
"""Parse one live GraphDB SHACL validation response and append it -- as one
more timestamped *run* -- into a GraphDB repository/named graph that
accumulates validation history over time.

Companion to ``validate-all-and-write-to-gdb.sh``. For every SHACL file that
script POSTs to each instance-data validate endpoint
(``data_repositories[].graphdb_repository`` in ``dashboard-config.json``), it
invokes this script once with the raw Turtle response plus timing metadata.
This script:

  1. Parses the returned ``validation-report.ttl`` with rdflib.
  2. Flattens it one level deep (report -> ``sh:result`` blank nodes -> their
     properties, e.g. ``sh:focusNode``, ``sh:sourceShape``,
     ``sh:resultSeverity``, ``sh:value``, ``sh:resultMessage``), the same
     shape sketched by ``validation-result-model.ttl``.
  3. Tags the report's (blank-node) subject with this run's provenance:
     ``ex:file``, ``ex:dataRepositoryId``, ``ex:repositoryValidated``,
     ``ex:httpStatus``, ``ex:durationSeconds``, ``ex:finishedAt``.
  4. POSTs the resulting triples into **that** data repository under the
     shared named-graph IRI (``validation_history.graph``).

The graph is never dropped: each invocation *appends* one more
``sh:ValidationReport`` blank node ("run") with its own ``ex:finishedAt`` /
``ex:durationSeconds`` / ``ex:httpStatus`` and its own ``sh:result`` ->
``sh:ValidationResult`` nodes.

History is then trimmed to a bounded window by ``gdb_history.prune_file()``
(disable with ``--no-prune``): a SHACL file keeps its **last two timings** and
the ``sh:result`` nodes of the **very last run only**. That is enough for the
dashboard to show the current run and how it differs from the previous one,
without the graph growing by ~19k result nodes per batch. Both runs stay
queryable from the same named graph, e.g.:

    PREFIX sh: <http://www.w3.org/ns/shacl#>
    PREFIX ex: <http://example.org/ns#>
    SELECT ?finishedAt ?durationSeconds ?result ?focusNode ?severity WHERE {
      ?report a sh:ValidationReport ;
              ex:file "Foo-SHACL.ttl" ;
              ex:finishedAt ?finishedAt ;
              ex:durationSeconds ?durationSeconds ;
              sh:result ?result .
      ?result sh:focusNode ?focusNode ;
              sh:resultSeverity ?severity .
    } ORDER BY ?finishedAt

Standalone usage:

    python3 write-validation-run-to-gdb.py \\
        --shacl-file /path/to/Foo-SHACL.ttl \\
        --report /tmp/xyz/validation-report.ttl \\
        --http-status 200 \\
        --duration-seconds 0.437 \\
        --finished-at 2026-09-29T10:00:00+03:00 \\
        --auth-header "Authorization: Bearer ..."

Destination ``--gdb-url``, ``--graph``, ``--graphdb-repository``, and
``--repository-validated`` default to ``dashboard-config.json``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests
from rdflib import RDF, BNode, Graph, Literal, Namespace, XSD
from rdflib.plugins.parsers.notation3 import BadSyntax

import dashboard_config as dc

EX = Namespace("http://example.org/ns#")
SH = Namespace("http://www.w3.org/ns/shacl#")
RDF4J = Namespace("http://rdf4j.org/schema/rdf4j#")

# Keep the report subject, its direct properties, and -- one level deeper --
# the properties of any blank node it points to (e.g. each sh:result).
# ``rsx:shapesGraph rdf4j:nil`` is removed first; see drop_nil_bookkeeping().
CLEAN_QUERY = """
    PREFIX sh: <http://www.w3.org/ns/shacl#>
    SELECT * WHERE {
        ?s a sh:ValidationReport .
        ?s ?p ?o .
        OPTIONAL { ?o ?pp ?oo . }
    }
"""


def drop_nil_bookkeeping(graph: Graph) -> int:
    """Remove ``?o ?pp rdf4j:nil`` triples hanging off the validation report.

    GraphDB repeats ``rsx:shapesGraph rdf4j:nil`` on every ``sh:ValidationResult``.
    Those triples carry no history value. Removing them in Python replaces the
    SPARQL ``MINUS`` that used to do it: rdflib evaluates ``MINUS`` as a nested
    loop over every result, which does not finish on a report of this size.
    """
    removed = 0
    nil = RDF4J.nil
    for report in list(graph.subjects(RDF.type, SH.ValidationReport)):
        for obj in list(graph.objects(report, None)):
            for triple in list(graph.triples((obj, None, nil))):
                graph.remove(triple)
                removed += 1
    return removed


def parse_args() -> argparse.Namespace:
    history = dc.history_settings()
    data = dc.data_settings()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--shacl-file", required=True, help="Path to the validated .ttl shapes file")
    p.add_argument("--report", required=True, help="Path to the raw validation response (Turtle or error text)")
    p.add_argument(
        "--data-repository-id",
        default=data.get("id") or "default",
        help="Data repository id from data_repositories[].id",
    )
    p.add_argument(
        "--repository-validated",
        default=data["graphdb_repository"],
        help="GraphDB repository that was validated against",
    )
    p.add_argument(
        "--graphdb-repository",
        default="",
        help="GraphDB repository to write the report into (default: --repository-validated)",
    )
    p.add_argument("--http-status", required=True)
    p.add_argument("--duration-seconds", required=True)
    p.add_argument("--finished-at", required=True, help="ISO-8601 timestamp, e.g. from `date -Iseconds`")
    p.add_argument(
        "--gdb-url",
        default=history["base_url"],
        help="GraphDB base URL (default: validation_history.base_url)",
    )
    p.add_argument(
        "--repository",
        default=history.get("repository") or "",
        help="DEPRECATED: use --graphdb-repository",
    )
    p.add_argument(
        "--graph",
        default=history["graph"],
        help="Named graph (default: validation_history.graph)",
    )
    p.add_argument("--auth-header", default="", help='Full header, e.g. "Authorization: Bearer xyz"')
    p.add_argument("--dry-run", action="store_true", help="Print n-triples instead of uploading")
    p.add_argument("--keep-timings", type=int, default=0,
                    help="Runs to keep for this SHACL file (default: validation_history.keep_timings)")
    p.add_argument("--keep-results", type=int, default=0,
                    help="Runs that keep their sh:result nodes (default: validation_history.keep_results)")
    p.add_argument("--no-prune", action="store_true",
                    help="Append without applying the retention window")
    return p.parse_args()


def get_cleaned_report_graph(graph: Graph) -> Graph:
    result_graph = Graph()
    for row in graph.query(CLEAN_QUERY):
        if row.s is not None and row.p is not None and row.o is not None:
            result_graph.add((row.s, row.p, row.o))
        if row.o is not None and row.pp is not None and row.oo is not None:
            result_graph.add((row.o, row.pp, row.oo))
    return result_graph


def append_run_metadata(original_graph: Graph, cleaned_graph: Graph, meta: dict[str, str]) -> Graph:
    """Tag the report's own (blank-node) subject with this run's timing and
    provenance.

    The subject is selected by type (``sh:ValidationReport``), not by graph
    iteration order, so timing metadata always lands on the report node.
    ``gdb_source.py`` still recovers older runs that stored ``ex:file`` on a
    ``sh:ValidationResult`` instead.
    """
    cleaned_graph.bind("ex", EX)
    report_subject = next(original_graph.subjects(RDF.type, SH.ValidationReport), None)
    if report_subject is None:
        return cleaned_graph
    cleaned_graph.add((report_subject, EX.file, Literal(meta["file"])))
    cleaned_graph.add((report_subject, EX.dataRepositoryId, Literal(meta["data_repository_id"])))
    cleaned_graph.add((report_subject, EX.repositoryValidated, Literal(meta["repository_validated"])))
    cleaned_graph.add((report_subject, EX.httpStatus, Literal(meta["http_status"], datatype=XSD.integer)))
    cleaned_graph.add((report_subject, EX.durationSeconds, Literal(meta["duration_seconds"], datatype=XSD.decimal)))
    cleaned_graph.add((report_subject, EX.finishedAt, Literal(meta["finished_at"], datatype=XSD.dateTime)))
    return cleaned_graph


def load_report(path: Path) -> tuple[Graph | None, str | None]:
    """Return (graph, error). ``error`` is set (and graph is None) when the
    response is empty, not Turtle, or not a SHACL validation report -- e.g. a
    SPARQL parser error or an HTTP error body."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return None, "(empty response)"
    if "ValidationReport" not in text:
        return None, text.strip()[:4000]
    graph = Graph()
    try:
        graph.parse(data=text, format="turtle")
    except BadSyntax as exc:
        return None, f"Turtle syntax error: {exc}"
    except Exception as exc:  # pragma: no cover - defensive, keep the run visible
        return None, f"Parse error: {exc}"
    return graph, None


def build_error_run(meta: dict[str, str], error_text: str) -> Graph:
    """Record a failed/erroring validation attempt as its own tiny run, so
    it still shows up in the history alongside successful runs."""
    graph = Graph()
    graph.bind("ex", EX)
    run = BNode()
    graph.add((run, RDF.type, SH.ValidationReport))
    graph.add((run, EX.executionError, Literal(True)))
    graph.add((run, EX.errorText, Literal(error_text)))
    graph.add((run, EX.file, Literal(meta["file"])))
    graph.add((run, EX.dataRepositoryId, Literal(meta["data_repository_id"])))
    graph.add((run, EX.repositoryValidated, Literal(meta["repository_validated"])))
    graph.add((run, EX.httpStatus, Literal(meta["http_status"], datatype=XSD.integer)))
    graph.add((run, EX.durationSeconds, Literal(meta["duration_seconds"], datatype=XSD.decimal)))
    graph.add((run, EX.finishedAt, Literal(meta["finished_at"], datatype=XSD.dateTime)))
    return graph


def target_repository(args: argparse.Namespace) -> str:
    repo = args.graphdb_repository or args.repository_validated or args.repository
    if not repo:
        raise SystemExit("ERROR: Must provide --graphdb-repository or --repository-validated")
    return repo


def post_graph(graph: Graph, args: argparse.Namespace) -> None:
    triples_data = graph.serialize(format="ntriples")
    endpoint = args.gdb_url.rstrip("/") + f"/repositories/{target_repository(args)}/statements"
    headers = {"Content-Type": "application/n-triples"}
    if args.auth_header:
        name, _, value = args.auth_header.partition(":")
        if name.strip():
            headers[name.strip()] = value.strip()
    resp = requests.post(
        endpoint,
        params={"context": f"<{args.graph}>"},
        data=triples_data.encode("utf-8"),
        headers=headers,
        timeout=60,
    )
    if resp.status_code >= 300:
        raise SystemExit(f"GraphDB write failed: HTTP {resp.status_code}: {resp.text[:500]}")


def prune_history(args: argparse.Namespace, shacl_file: str) -> None:
    """Re-apply the retention window to the file that was just written.

    A failure here leaves a correctly recorded run in an over-long history, so
    it is reported but not fatal: ``python3 gdb_history.py`` re-establishes the
    window for the whole graph.
    """
    if args.no_prune or args.dry_run:
        return
    import gdb_history as gh

    target = target_repository(args)
    history = dict(
        dc.history_settings(),
        base_url=args.gdb_url,
        repository=target,
        graph=args.graph,
    )
    history["sparql"] = f"{args.gdb_url.rstrip('/')}/repositories/{target}"
    keep_timings, keep_results = gh.retention(dc.load_config())
    try:
        gh.prune_file(
            history,
            shacl_file,
            args.keep_timings or keep_timings,
            args.keep_results or keep_results,
            args.auth_header,
        )
    except SystemExit as exc:
        print(f"WARNING: retention not applied to {shacl_file}: {exc}", file=sys.stderr)


def main() -> int:
    args = parse_args()
    if not args.graphdb_repository:
        args.graphdb_repository = args.repository_validated or args.repository
    meta = {
        "file": Path(args.shacl_file).name,
        "data_repository_id": args.data_repository_id,
        "repository_validated": args.repository_validated,
        "http_status": str(args.http_status),
        "duration_seconds": str(args.duration_seconds),
        "finished_at": args.finished_at,
    }

    graph, error = load_report(Path(args.report))
    if graph is None:
        run_graph = build_error_run(meta, error or "(unknown error)")
        if args.dry_run:
            print(run_graph.serialize(format="ntriples"))
        else:
            post_graph(run_graph, args)
            prune_history(args, meta["file"])
        print(f"RUN {meta['file']} @ {meta['finished_at']}: execution error recorded")
        return 1

    drop_nil_bookkeeping(graph)
    cleaned = get_cleaned_report_graph(graph)
    cleaned = append_run_metadata(graph, cleaned, meta)
    n_results = len(list(cleaned.objects(predicate=SH.result)))

    if args.dry_run:
        print(cleaned.serialize(format="ntriples"))
    else:
        post_graph(cleaned, args)
        prune_history(args, meta["file"])

    print(f"RUN {meta['file']} @ {meta['finished_at']}: uploaded ({n_results} results, {len(cleaned)} triples)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
