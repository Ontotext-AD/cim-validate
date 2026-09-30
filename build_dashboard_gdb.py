#!/usr/bin/env python3
"""The dashboard entry point. Validation runs are sourced from GraphDB.

Runs are read out of the validation-history named graph filled by
``validate-all-and-write-to-gdb.sh`` and rendered through the repository →
family → ontology profile → individual shape cube in ``cube_dashboard.py``.
Output goes to ``index.html`` and ``dashboard/``, configured under
``validation_history`` in ``dashboard-config.json``.

The history graph keeps the last runs of every SHACL file. A dashboard shows
one state of the world, so the cube is built from the **latest run per SHACL
file**; ``--as-of`` rebuilds it as of an earlier point in time, and the
generated run-history page lists all recorded runs.

Usage:

    python3 build_dashboard_gdb.py
    python3 build_dashboard_gdb.py --as-of 2026-09-20T00:00:00+03:00
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cube_dashboard as cd
import dashboard_config as dc
import gdb_source as gs

ROOT = Path(__file__).resolve().parent


def run_meta(record: dict | None, shacl_file: str, history: dict) -> dict:
    """Mirror cube_dashboard.report_meta() for a GraphDB-sourced run."""
    if record is None:
        return {
            "exists": False,
            "turtle": False,
            "conforms": False,
            "truncated": False,
            "execution_error": True,
            "error_text": (
                f"(no validation run for {shacl_file} in named graph "
                f"<{history['graph']}> of repository {history['repository']})"
            ),
        }
    if record["execution_error"]:
        return {
            "exists": True,
            "turtle": False,
            "conforms": False,
            "truncated": record["truncated"],
            "execution_error": True,
            "error_text": record["error_text"] or "(ex:executionError without ex:errorText)",
        }
    return {
        "exists": True,
        "turtle": True,
        "conforms": record["conforms"],
        "truncated": record["truncated"],
        "execution_error": False,
        "error_text": "",
    }


def make_collector(snapshot: dict):
    """Return a ``(catalog, index) -> runs`` collector over the GraphDB snapshot."""
    history = snapshot["history"]
    selected = snapshot["selected"]

    def collect(catalog: dict, index: dict) -> list[dict]:
        runs = []
        all_items = []
        for source in sorted(catalog["sources"].values(), key=lambda item: item["id"]):
            record = selected.get(source["shacl_file"])
            meta = run_meta(record, source["shacl_file"], history)
            items = (
                [cd.result_item(raw, source, index) for raw in record["results"]]
                if meta["turtle"] else []
            )
            all_items.extend(items)
            href = gs.run_href(history, source["shacl_file"])
            duration = record["duration"] if record else None
            runs.append({
                **source,
                "duration": duration,
                "duration_disp": cd.ct.format_duration(duration),
                "http_status": record["http_status"] if record else "",
                "finished_at": record["finished_at"] if record else "",
                "timing_href": href,
                "report_href": href,
                "meta": meta,
                "shapes": cd.parse_shapes(source["shacl_path"]),
                "items": items,
            })

        present = cd.cr.iris_present_in_graphdb(
            {item["focus_iri"] for item in all_items if item["focus_iri"]}
        )
        for item in all_items:
            item["in_gdb"] = item["focus_iri"] in present
        return runs

    return collect


# Timings are recorded to milliseconds, so anything below half a millisecond
# is reported as unchanged rather than as a signed delta.
DELTA_EPSILON = 0.0005


def delta_cell(record: dict) -> str:
    """Change in run time against the previous run of the same SHACL file."""
    delta = record.get("duration_delta")
    if delta is None:
        return '<td class="num"></td>'
    if abs(delta) < DELTA_EPSILON:
        return '<td class="num">&plusmn;0</td>'
    sign = "+" if delta > 0 else "&minus;"
    css = "slow" if delta > 0 else "good-delta"
    return (
        f'<td class="num {css}">{sign}{cd.esc(cd.ct.format_duration(abs(delta)))}</td>'
    )


def write_run_history(snapshot: dict, dash_dir: Path, index_path: Path) -> Path:
    """Page listing every run in the graph, marking the one the cube uses."""
    history = snapshot["history"]
    rows = []
    for shacl_file in sorted(snapshot["by_file"]):
        chosen = snapshot["selected"].get(shacl_file)
        for record in snapshot["by_file"][shacl_file]:
            is_chosen = record is chosen
            state = (
                "Execution error" if record["execution_error"]
                else "Conforms" if record["conforms"]
                else "Truncated" if record["truncated"]
                else "Violations"
            )
            rows.append(
                "<tr>"
                f'<td class="profile">'
                f'<a href="{cd.esc(gs.run_href(history, shacl_file))}" target="_blank"'
                f' rel="noopener">{cd.brk(shacl_file)}</a></td>'
                f'<td>{cd.esc(record["finished_at"] or "unknown")}</td>'
                f'<td class="num">{cd.esc(cd.ct.format_duration(record["duration"]))}</td>'
                f"{delta_cell(record)}"
                f'<td class="num">{cd.esc(record["http_status"] or "")}</td>'
                f'<td class="num">{len(record["results"]) or ""}</td>'
                f"<td>{cd.esc(state)}</td>"
                f'<td class="num">{"&#9679;" if is_chosen else ""}</td>'
                "</tr>"
            )
    path = dash_dir / "runs" / "index.html"
    chunks = [
        rows[offset:offset + cd.PAGE_SIZE]
        for offset in range(0, max(len(rows), 1), cd.PAGE_SIZE)
    ]
    for page_number, chunk in enumerate(chunks, 1):
        page_path = path if page_number == 1 else path.parent / f"p{page_number}.html"
        page_nav = cd.pager(len(chunks), page_number)
        body = f"""
<p class="crumb">{cd.crumb([("All repositories", index_path)], page_path.parent, "Run history")}</p>
<h1>Validation run history</h1>
<p class="meta">{snapshot["run_total"]} runs recorded for
{len(snapshot["by_file"])} SHACL files in named graph
<code>{cd.esc(history["graph"])}</code> of repository
<code>{cd.esc(history["repository"])}</code>.
Finished and Duration are <code>ex:finishedAt</code> and <code>ex:durationSeconds</code>;
Δ is the change against the previous run of the same file.
History is trimmed to the last {history["keep_timings"]} timing(s) per SHACL file, with
<code>sh:result</code> nodes kept on the newest {history["keep_results"]} only, so older runs
report a duration and no result count.
The run marked under Shown is the one the dashboard is built from
({"latest recorded" if not snapshot["as_of"] else f'latest at or before {cd.esc(snapshot["as_of"])}'}).
Each SHACL file links to a GraphDB SPARQL query over its own runs.</p>
{page_nav}
<div class="table-wrap"><table class="compact">
<thead><tr><th class="profile">SHACL file</th><th>Finished</th>
<th class="num">Duration</th><th class="num">&Delta;</th><th class="num">HTTP</th>
<th class="num">Results</th><th>Outcome</th><th class="num">Shown</th></tr></thead>
<tbody>{"".join(chunk) if chunk else '<tr><td colspan="8">No runs recorded.</td></tr>'}</tbody>
</table></div>
{page_nav}
"""
        cd.write(page_path, "Validation run history", body)
    return path


def comparison_note(snapshot: dict, runs_page: Path) -> str:
    """One line on how the shown runs compare with the previous ones.

    This is the point of keeping two timings per file, so say it on the
    landing page rather than only in the run-history table.
    """
    history = snapshot["history"]
    compared = [
        run for run in snapshot["selected"].values()
        if run.get("duration_delta") is not None
    ]
    retention = (
        f'History keeps the last {history["keep_timings"]} timing(s) per SHACL file and '
        f'the results of the newest {history["keep_results"]}.'
    )
    if not compared:
        return f"{retention} No previous run to compare against yet."
    slower = sum(1 for run in compared if run["duration_delta"] >= DELTA_EPSILON)
    faster = sum(1 for run in compared if run["duration_delta"] <= -DELTA_EPSILON)
    unchanged = len(compared) - slower - faster
    net = sum(run["duration_delta"] for run in compared)
    breakdown = f"{slower} slower, {faster} faster"
    if unchanged:
        breakdown += f", {unchanged} unchanged"
    direction = "slower" if net > 0 else "faster"
    return (
        f"{retention} Against the previous run, {len(compared)} of "
        f'{len(snapshot["selected"])} files are comparable: {breakdown}, '
        f"{cd.ct.format_duration(abs(net))} {direction} overall "
        f'(<a href="{cd.esc(cd.rel(ROOT, runs_page))}">per-file &Delta;</a>).'
    )


def source_note(snapshot: dict, runs_page: Path, unmatched: set[str]) -> str:
    history = snapshot["history"]
    scope = (
        f"latest run at or before {cd.esc(snapshot['as_of'])}"
        if snapshot["as_of"] else "latest recorded run"
    )
    note = (
        f'Sourced from GraphDB repository <code>{cd.esc(history["repository"])}</code>, '
        f'named graph <code>{cd.esc(history["graph"])}</code> — '
        f'{snapshot["run_total"]} runs recorded for {len(snapshot["by_file"])} SHACL files, '
        f'one {scope} per file '
        f'(<a href="{cd.esc(cd.rel(ROOT, runs_page))}">run history</a>).'
    )
    note += " " + comparison_note(snapshot, runs_page)
    if snapshot["unresolved"]:
        note += (
            f' {snapshot["unresolved"]} report(s) in the graph carry no <code>ex:file</code> '
            "and cannot be attributed to a SHACL file; their results are excluded."
        )
    if unmatched:
        note += (
            f' {len(unmatched)} run(s) name a SHACL file that is not in the configured '
            "checkout and are excluded."
        )
    return note


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--as-of",
        default="",
        help="Build the cube from the latest run at or before this ISO-8601 timestamp",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = dc.load_config()
    history = gs.history_config(config)
    print(
        f"Reading validation history from {history['sparql']} "
        f"graph <{history['graph']}>"
    )
    snapshot = gs.load_runs(history, args.as_of or None)

    known = {source["shacl_file"] for source in dc.build_catalog(config)["sources"].values()}
    unmatched = set(snapshot["selected"]) - known
    if unmatched:
        print(
            f"{len(unmatched)} run(s) reference SHACL files outside the configured checkout: "
            + ", ".join(sorted(unmatched)[:5]),
            file=sys.stderr,
        )

    dash_dir = ROOT / history["dashboard_dir"]
    index_path = ROOT / history["index"]
    runs_page = dash_dir / "runs" / "index.html"

    output = cd.build(
        collect=make_collector(snapshot),
        dash_dir=dash_dir,
        index_path=index_path,
        source_note=source_note(snapshot, runs_page, unmatched),
    )
    write_run_history(snapshot, dash_dir, index_path)

    print(f"Dashboard: {output}")
    print(f"  Details: {dash_dir}")
    print(f"  Runs   : {runs_page}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
