#!/usr/bin/env python3
"""The dashboard entry point. Validation runs are sourced from GraphDB.

Runs are read from each configured data repository's copy of the shared
validation-history named graph (filled by ``validate-all-and-write-to-gdb.sh``)
and rendered through the data-repository → family → ontology profile → SHACL
file → individual shape cube in ``cube_dashboard.py``. Output goes to
``index.html`` and ``dashboard/``, configured under ``validation_history`` in
``dashboard-config.json``.

Each data repository keeps the last runs of every SHACL file. A dashboard shows
one state of the world, so the cube is built from the **latest run per
(data repository, SHACL file)** pair; ``--as-of`` rebuilds it as of an earlier
point in time, and the generated run-history page lists all recorded runs.

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
    """Return a ``(catalog, index) -> runs`` collector over all data repositories."""
    history_settings = snapshot["history_settings"]
    selected = snapshot["selected"]

    def collect(catalog: dict, index: dict) -> list[dict]:
        runs = []
        items_by_repo: dict[str, list[dict]] = {
            repo["id"]: [] for repo in snapshot["data_repositories"]
        }
        for data_repo in snapshot["data_repositories"]:
            data_repo_id = data_repo["id"]
            history = {
                "base_url": history_settings["base_url"],
                "repository": data_repo["graphdb_repository"],
                "graph": history_settings["graph"],
                "sparql": data_repo["graphdb_sparql"],
            }
            for source in sorted(catalog["sources"].values(), key=lambda item: item["id"]):
                record = selected.get((data_repo_id, source["shacl_file"]))
                meta = run_meta(record, source["shacl_file"], history)
                items = (
                    [
                        cd.result_item(
                            raw,
                            {
                                **source,
                                "data_repository_id": data_repo_id,
                                "instance_github_repository": data_repo[
                                    "instance_github_repository"
                                ],
                            },
                            index,
                        )
                        for raw in record["results"]
                    ]
                    if meta["turtle"] else []
                )
                items_by_repo[data_repo_id].extend(items)
                href = gs.run_href(history, source["shacl_file"])
                duration = record["duration"] if record else None
                runs.append({
                    **source,
                    "id": f"{data_repo_id}::{source['id']}",
                    "data_repository_id": data_repo_id,
                    "data_repository_title": data_repo["title"],
                    "graphdb_repository": data_repo["graphdb_repository"],
                    "instance_github_repository": data_repo["instance_github_repository"],
                    "duration": duration,
                    "duration_disp": cd.ct.format_duration(duration),
                    "previous_duration": record.get("previous_duration") if record else None,
                    "previous_finished_at": (
                        record.get("previous_finished_at") if record else ""
                    ),
                    "duration_delta": record.get("duration_delta") if record else None,
                    "http_status": record["http_status"] if record else "",
                    "finished_at": record["finished_at"] if record else "",
                    "timing_href": href,
                    "report_href": href,
                    "meta": meta,
                    "shapes": cd.parse_shapes(source["shacl_path"]),
                    "items": items,
                })

        for data_repo_id, items in items_by_repo.items():
            present = cd.cr.iris_present_in_graphdb(
                {item["focus_iri"] for item in items if item["focus_iri"]},
                data_repository_id=data_repo_id,
            )
            for item in items:
                item["in_gdb"] = item["focus_iri"] in present
        return runs

    return collect


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
    """Page listing every run in every data repository, marking the cube's pick."""
    history_settings = snapshot["history_settings"]
    rows = []
    for data_repo in snapshot["data_repositories"]:
        data_repo_id = data_repo["id"]
        by_file = snapshot["by_data_repo"].get(data_repo_id) or {}
        history = {
            "base_url": history_settings["base_url"],
            "repository": data_repo["graphdb_repository"],
            "graph": history_settings["graph"],
        }
        for shacl_file in sorted(by_file):
            chosen = snapshot["selected"].get((data_repo_id, shacl_file))
            for record in by_file[shacl_file]:
                is_chosen = record is chosen
                state = (
                    "Execution error" if record["execution_error"]
                    else "Conforms" if record["conforms"]
                    else "Truncated" if record["truncated"]
                    else "Violations"
                )
                rows.append(
                    "<tr>"
                    f"<td>{cd.esc(data_repo.get('title') or data_repo_id)}</td>"
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
<p class="crumb">{cd.crumb([("All data repositories", index_path)], page_path.parent, "Run history")}</p>
<h1>Validation run history</h1>
<p class="meta">{snapshot["run_total"]} runs recorded across
{len(snapshot["data_repositories"])} data repositories in named graph
<code>{cd.esc(history_settings["graph"])}</code>.
Finished and Duration are <code>ex:finishedAt</code> and <code>ex:durationSeconds</code>;
&Delta; is the change against the previous run of the same file in the same data repository.
History is trimmed to the last {history_settings["keep_timings"]} timing(s) per SHACL file, with
<code>sh:result</code> nodes kept on the newest {history_settings["keep_results"]} only.
The run marked under Shown is the one the dashboard is built from
({"latest recorded" if not snapshot["as_of"] else f'latest at or before {cd.esc(snapshot["as_of"])}'}).</p>
{page_nav}
<div class="table-wrap"><table class="compact">
<thead><tr><th>Data repository</th><th class="profile">SHACL file</th><th>Finished</th>
<th class="num">Duration</th><th class="num">&Delta;</th><th class="num">HTTP</th>
<th class="num">Results</th><th>Outcome</th><th class="num">Shown</th></tr></thead>
<tbody>{"".join(chunk) if chunk else '<tr><td colspan="9">No runs recorded.</td></tr>'}</tbody>
</table></div>
{page_nav}
"""
        cd.write(page_path, "Validation run history", body)
    return path


def comparison_note(snapshot: dict, runs_page: Path) -> str:
    history = snapshot["history_settings"]
    compared = [
        run for run in snapshot["selected"].values()
        if run.get("duration_delta") is not None
    ]
    retention = (
        f'History keeps the last {history["keep_timings"]} timing(s) per SHACL file '
        f'per data repository and the results of the newest {history["keep_results"]}.'
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
        f'{len(snapshot["selected"])} file-runs are comparable: {breakdown}, '
        f"{cd.ct.format_duration(abs(net))} {direction} overall "
        f'(<a href="{cd.esc(cd.rel(ROOT, runs_page))}">per-file &Delta;</a>).'
    )


def source_note(snapshot: dict, runs_page: Path, unmatched: set[tuple[str, str]]) -> str:
    history = snapshot["history_settings"]
    data_repos = snapshot["data_repositories"]
    scope = (
        f"latest run at or before {cd.esc(snapshot['as_of'])}"
        if snapshot["as_of"] else "latest recorded run"
    )
    repo_bits = ", ".join(
        f"<code>{cd.esc(repo['graphdb_repository'])}</code>" for repo in data_repos
    )
    note = (
        f"Sourced from {len(data_repos)} GraphDB data "
        f"{'repository' if len(data_repos) == 1 else 'repositories'} "
        f"({repo_bits}), each using named graph "
        f"<code>{cd.esc(history['graph'])}</code> — "
        f"{snapshot['run_total']} runs recorded, one {scope} per "
        f"(data repository, SHACL file) pair "
        f'(<a href="{cd.esc(cd.rel(ROOT, runs_page))}">run history</a>). <br>\n'
    )
    note += comparison_note(snapshot, runs_page)
    if snapshot.get("unresolved_total"):
        note += (
            f' <br>\n{snapshot["unresolved_total"]} report(s) across the graphs carry no '
            "<code>ex:file</code> and cannot be attributed to a SHACL file; "
            "their results are excluded."
        )
    if unmatched:
        note += (
            f" <br>\n{len(unmatched)} run(s) name a SHACL file that is not in the configured "
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
    history_settings = dc.history_settings(config)
    data_repos = dc.data_repositories_settings(config)
    print(
        f"Reading validation history from {len(data_repos)} data repository(ies), "
        f"named graph <{history_settings['graph']}>"
    )
    snapshot = gs.load_runs_multi_repo(data_repos, history_settings, args.as_of or None)

    known = {source["shacl_file"] for source in dc.build_catalog(config)["sources"].values()}
    unmatched = {
        (data_repo_id, shacl_file)
        for (data_repo_id, shacl_file) in snapshot["selected"]
        if shacl_file not in known
    }
    if unmatched:
        sample = ", ".join(
            f"{data_repo_id}/{shacl_file}"
            for data_repo_id, shacl_file in sorted(unmatched)[:5]
        )
        print(
            f"{len(unmatched)} run(s) reference SHACL files outside the configured checkout: "
            + sample,
            file=sys.stderr,
        )

    dash_dir = ROOT / history_settings["dashboard_dir"]
    index_path = ROOT / history_settings["index"]
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
