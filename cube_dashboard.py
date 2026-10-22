#!/usr/bin/env python3
"""Repository → family → ontology profile → SHACL file → shape dashboard."""
from __future__ import annotations

import hashlib
import html
import importlib.util
import json
import os
import re
import shutil
from pathlib import Path
from urllib.parse import quote

import dashboard_config as dc

ROOT = Path(__file__).resolve().parent
# Output targets. build() rebinds them from validation_history in
# dashboard-config.json, so the published location is configured in one place.
DASH = ROOT / "dashboard"
INDEX = ROOT / "index.html"
PAGE_SIZE = 1500
METRICS = ("time_sum", "time_max", "v_count", "dv_count", "w_count", "dw_count", "g_count", "t_count")
VALIDATION_METRICS = ("v_count", "dv_count", "w_count", "dw_count", "g_count", "t_count")
EXECUTION_ERROR_TILE_LEVELS = ("global", "repository", "family")
METRIC_LABEL = {
    "time_sum": "Time total",
    "time_max": "Time max",
    "v_count":  "Errors",
    "dv_count": "Distinct errors",
    "w_count":  "Warnings",
    "dw_count": "Distinct warnings",
    "g_count":  "Good shapes",
    "t_count":  "Total shapes",
    "execution_err": "Validation execution errors",
}
ICON = {
    "Violation": '<span class="icon violation" title="sh:Violation">&#10060;</span>',
    "Warning": '<span class="icon warning" title="sh:Warning">&#9888;</span>',
    "Good": '<span class="icon good" title="good shape">&#128994;</span>',
}
SHAPE_TYPE = re.compile(r"\b(?:a|rdf:type)\s+sh:(PropertyShape|NodeShape)\b")
SEVERITY = re.compile(r"\bsh:severity\s+sh:(Violation|Warning|Info)\b")
CLOSED = re.compile(r"\bsh:closed\s+true\b")
SPARQL = re.compile(r"\bsh:sparql\b")
LABEL = re.compile(r'\brdfs:label\s+"((?:[^"\\]|\\.)*)"')
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def is_relative_to(path: Path, other: Path) -> bool:
    """Back-port of Path.is_relative_to() for Python < 3.9."""
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    if spec is None or spec.loader is None:
        raise ImportError(filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cr = _load("cube_collect_results", "collect-results.py")
ct = _load("cube_collect_timings", "collect-timings.py")


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def brk(value: object) -> str:
    return re.sub(r"(?<=[/:#._?=-])(?!$)", "<wbr>", esc(value))


def rel(from_dir: Path, target: Path | str) -> str:
    path = target if isinstance(target, Path) else ROOT / target
    return Path(os.path.relpath(path.resolve(), from_dir.resolve())).as_posix()


def css_href(from_dir: Path) -> str:
    stylesheet = ROOT / "dashboard.css"
    version = hashlib.sha1(stylesheet.read_bytes()).hexdigest()[:12]
    return f"{rel(from_dir, stylesheet)}?v={version}"


TABLE_SCRIPT = """
<script>
(function () {
  document.querySelectorAll("table").forEach(function (table) {
    table.querySelectorAll("thead th").forEach(function (th) {
      if (th.colSpan > 1) return;
      var grip = document.createElement("span");
      grip.className = "col-resize";
      th.appendChild(grip);
      grip.addEventListener("mousedown", function (event) {
        event.preventDefault();
        var x = event.pageX, width = th.getBoundingClientRect().width;
        function move(e) { th.style.width = Math.max(36, width + e.pageX - x) + "px"; }
        function up() {
          document.removeEventListener("mousemove", move);
          document.removeEventListener("mouseup", up);
        }
        document.addEventListener("mousemove", move);
        document.addEventListener("mouseup", up);
      });
    });
  });
})();
</script>
"""

# Column layout of one row in a scope's rows.json sidecar. Values are carried
# next to their pre-formatted display string so the browser never reimplements
# duration formatting.
ROW_COLUMNS = (
    "name", "href", "leaf",
    "time_sum", "time_sum_disp", "time_max", "time_max_disp",
    "v_count", "dv_count", "w_count", "dw_count", "g_count", "t_count",
)

SORT_SCRIPT = """
<script>
(function () {
  var table = document.querySelector("table[data-rows]");
  if (!table || !window.fetch) return;
  var url = table.getAttribute("data-rows");
  var size = parseInt(table.getAttribute("data-page-size"), 10) || 1500;
  var tbody = table.tBodies[0];
  var pagers = [].slice.call(document.querySelectorAll("[data-pager]"));
  var baseBody = tbody.innerHTML;
  var basePagers = pagers.map(function (el) { return el.innerHTML; });
  var COUNTS = ["v_count", "dv_count", "w_count", "dw_count", "g_count", "t_count"];
  var COLS = {
    time_sum: [3, 4], time_max: [5, 6],
    v_count: [7, null], dv_count: [8, null], w_count: [9, null],
    dw_count: [10, null], g_count: [11, null], t_count: [12, null]
  };
  var rows = null, pending = false;

  function esc(text) {
    return String(text).replace(/[&<>"]/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch];
    });
  }
  function brk(text) { return esc(text).replace(/([\\/:#._?=-])(?!$)/g, "$1<wbr>"); }

  function readHash() {
    var name = /(?:^|[#&])sort=([a-z_]+)/.exec(location.hash);
    if (!name || (name[1] !== "name" && !COLS[name[1]])) return null;
    var dir = /(?:^|[#&])dir=(asc|desc)/.exec(location.hash);
    var page = /(?:^|[#&])page=(\\d+)/.exec(location.hash);
    return {
      metric: name[1],
      dir: dir ? dir[1] : (name[1] === "name" ? "asc" : "desc"),
      page: page ? Math.max(1, parseInt(page[1], 10)) : 1
    };
  }
  function hashFor(state, page) {
    return "#sort=" + state.metric + "&dir=" + state.dir + "&page=" + page;
  }

  function hrefFor(row, metric) {
    var href = row[1];
    if (!row[2]) return href + "#sort=" + metric;
    var dir = href.replace(/index\\.html$/, "");
    if (metric === "v_count" && row[7]) return dir + "violation/index.html";
    if (metric === "w_count" && row[9]) return dir + "warning/index.html";
    return href;
  }
  function cellHtml(row, metric) {
    var col = COLS[metric];
    var disp = col[1] === null ? (row[col[0]] > 0 ? String(row[col[0]]) : "") : row[col[1]];
    if (!disp) return "";
    return '<a href="' + esc(hrefFor(row, metric)) + '">' + esc(disp) + "</a>";
  }
  function rowHtml(row) {
    return '<tr><td class="profile"><a href="' + esc(row[1]) + '">' + brk(row[0]) + "</a></td>"
      + '<td class="dur">' + cellHtml(row, "time_sum") + "</td>"
      + '<td class="dur">' + cellHtml(row, "time_max") + "</td>"
      + COUNTS.map(function (metric) {
          return '<td class="num cell">' + cellHtml(row, metric) + "</td>";
        }).join("")
      + "</tr>";
  }

  function compare(first, second, state) {
    if (state.metric !== "name") {
      var col = COLS[state.metric][0];
      var left = first[col], right = second[col];
      var leftNull = left === null || left === undefined;
      var rightNull = right === null || right === undefined;
      if (leftNull !== rightNull) return leftNull ? 1 : -1;
      if (!leftNull && left !== right) return state.dir === "asc" ? left - right : right - left;
    }
    var a = String(first[0]).toLowerCase(), b = String(second[0]).toLowerCase();
    if (a === b) return 0;
    var order = a < b ? -1 : 1;
    return state.metric === "name" && state.dir === "desc" ? -order : order;
  }

  function mark(state) {
    table.querySelectorAll("th[data-sort]").forEach(function (th) {
      if (state && th.getAttribute("data-sort") === state.metric) {
        th.setAttribute("data-dir", state.dir);
      } else {
        th.removeAttribute("data-dir");
      }
    });
  }

  function apply(state) {
    mark(state);
    if (!state || !rows) {
      tbody.innerHTML = baseBody;
      pagers.forEach(function (el, i) { el.innerHTML = basePagers[i]; });
      return;
    }
    var sorted = rows.slice().sort(function (a, b) { return compare(a, b, state); });
    var pages = Math.max(1, Math.ceil(sorted.length / size));
    var page = Math.min(state.page, pages);
    tbody.innerHTML = sorted.slice((page - 1) * size, page * size).map(rowHtml).join("")
      || '<tr><td colspan="9">No lower-level records.</td></tr>';
    var nav = "";
    if (pages > 1) {
      var links = [];
      for (var number = 1; number <= pages; number++) {
        links.push(number === page
          ? "<strong>" + number + "</strong>"
          : '<a href="' + hashFor(state, number) + '">' + number + "</a>");
      }
      nav = "Pages: " + links.join(" \\u00b7 ");
    }
    pagers.forEach(function (el) { el.innerHTML = nav; });
  }

  function render() {
    var state = readHash();
    if (!state || rows) { apply(state); return; }
    if (pending) return;
    pending = true;
    table.classList.add("sorting");
    fetch(url).then(function (response) {
      if (!response.ok) throw new Error(response.status);
      return response.json();
    }).then(function (payload) {
      rows = payload.rows;
      size = payload.size || size;
      pending = false;
      table.classList.remove("sorting");
      apply(readHash());
    }).catch(function () {
      pending = false;
      table.classList.remove("sorting");
      table.classList.add("sort-failed");
      apply(null);
    });
  }

  table.querySelectorAll("th[data-sort]").forEach(function (th) {
    th.addEventListener("click", function (event) {
      if (event.target.classList.contains("col-resize")) return;
      var metric = th.getAttribute("data-sort");
      var state = readHash();
      var dir = metric === "name" ? "asc" : "desc";
      if (state && state.metric === metric) {
        if (state.dir !== dir) {
          history.replaceState(null, "", location.pathname + location.search);
          apply(null);
          return;
        }
        dir = dir === "asc" ? "desc" : "asc";
      }
      location.hash = "sort=" + metric + "&dir=" + dir + "&page=1";
    });
  });
  window.addEventListener("hashchange", render);
  render();
})();
</script>
"""


def shell(title: str, body: str, out_dir: Path) -> str:
    resolved_dir = out_dir.resolve()
    level_zero_metrics = (DASH / "all").resolve()
    is_level_zero = (
        resolved_dir == ROOT
        or is_relative_to(resolved_dir, level_zero_metrics)
    )
    body_class = ' class="level-zero"' if is_level_zero else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Rubik:wght@400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="{esc(css_href(out_dir))}">
</head>
<body{body_class}>{body}{TABLE_SCRIPT if "<table" in body else ""}{SORT_SCRIPT if "data-rows" in body else ""}</body>
</html>
"""


def write(path: Path, title: str, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(shell(title, body, path.parent), encoding="utf-8")
    return path.relative_to(ROOT).as_posix()


def local_name(uri: str) -> str:
    short = cr.shorten(uri)
    return re.split(r"[/#:]", short)[-1] or short


def shape_slug(source_file: str, uri: str) -> str:
    stem = dc.slug(Path(source_file).stem, "shape-file")[:55]
    name = dc.slug(local_name(uri), "shape")[:55]
    digest = hashlib.sha1(f"{source_file}\0{uri}".encode()).hexdigest()[:9]
    return f"{stem}--{name}--{digest}"


def parse_blocks(path: Path) -> tuple[dict[str, str], str | None, list[tuple[int, str, str]]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    prefixes: dict[str, str] = {}
    base = None
    for line in lines:
        match = re.match(r"\s*(?:@prefix|PREFIX)\s+([\w.-]*):\s+<([^>]*)>", line, re.I)
        if match:
            prefixes[match.group(1)] = match.group(2)
        match = re.match(r"\s*(?:@base|BASE)\s+<([^>]*)>", line, re.I)
        if match:
            base = match.group(1)
    blocks: list[tuple[int, str, str]] = []
    current: list[str] = []
    subject = ""
    start = 1
    for number, line in enumerate(lines, 1):
        is_subject = (
            bool(line) and line[0] not in " \t#@"
            and not line.upper().startswith(("PREFIX ", "BASE "))
        )
        if is_subject:
            if current and subject:
                blocks.append((start, subject, "\n".join(current)))
            token = line.split(None, 1)[0]
            subject = cr.resolve_term(token, prefixes, base) or token
            start = number
            current = [line]
        else:
            current.append(line)
    if current and subject:
        blocks.append((start, subject, "\n".join(current)))
    return prefixes, base, blocks


def parse_shapes(path: Path) -> list[dict]:
    _prefixes, _base, blocks = parse_blocks(path)
    shapes = []
    seen: set[str] = set()
    for line, uri, block in blocks:
        kind_match = SHAPE_TYPE.search(block)
        if not kind_match or uri in seen:
            continue
        kind = kind_match.group(1)
        severity = SEVERITY.search(block)
        if kind == "NodeShape" and not (severity or CLOSED.search(block) or SPARQL.search(block)):
            continue
        label = LABEL.search(block)
        shapes.append({
            "uri": uri,
            "label": (
                label.group(1).encode().decode("unicode_escape")
                if label else local_name(uri)
            ),
            "kind": kind,
            "line": line,
            "declared": True,
        })
        seen.add(uri)
    return shapes


def result_iri(result: dict, field: str) -> str:
    term = result.get(field)
    return cr.strip_brackets(term) or (term or "")


def result_item(result: dict, source: dict, index: dict) -> dict:
    severity = cr.shorten(result.get("sh:resultSeverity"))
    severity = severity.rsplit(":", 1)[-1].rsplit("#", 1)[-1] or "Info"
    focus_term = result.get("sh:focusNode")
    data_repo_id = str(source.get("data_repository_id") or "")
    focus_display, graphdb_href = cr.graphdb_link(focus_term, data_repo_id)
    constraint = result_iri(result, "sh:sourceConstraint")
    source_shape = result_iri(result, "sh:sourceShape")
    _display, source_href = cr.github_link(
        result.get("sh:sourceConstraint") or result.get("sh:sourceShape"),
        source["id"],
        index,
    )
    iri = result_iri(result, "sh:focusNode")
    found = UUID_RE.search(iri)
    query_value = found.group(0) if found else iri
    instance_repo = str(source.get("instance_github_repository") or "")
    github_href = (
        "https://github.com/search?q="
        + quote(f"repo:{instance_repo} {query_value}")
        + "&type=code"
        if query_value and instance_repo else ""
    )
    return {
        "severity": severity,
        "focus": focus_display,
        "focus_iri": iri,
        "graphdb_href": graphdb_href,
        "github_href": github_href,
        "message": cr.strip_literal(result.get("sh:resultMessage")),
        "constraint_uri": constraint,
        "source_shape_uri": source_shape,
        "source_href": source_href,
        "in_gdb": False,
    }


def unique_focus(items: list[dict]) -> list[dict]:
    seen = set()
    answer = []
    for item in items:
        key = item["focus"]
        if key not in seen:
            seen.add(key)
            answer.append(item)
    return answer


def make_leaf(run: dict, shape: dict, items: list[dict]) -> dict:
    violations = [item for item in items if item["severity"] == "Violation"]
    warnings = [item for item in items if item["severity"] == "Warning"]
    has_error = bool(violations)
    has_warning = bool(warnings)
    duration = run["duration"] if isinstance(run["duration"], (int, float)) else None
    return {
        "id": shape_slug(run["shacl_file"], shape["uri"]),
        "name": shape["label"],
        "uri": shape["uri"],
        "kind": shape["kind"],
        "line": shape["line"],
        "declared": shape.get("declared", False),
        "run": run,
        "runs": {run["id"]: run},
        "v_items": violations,
        "w_items": warnings,
        "v_count": len(unique_focus(violations)),
        "dv_count": 1 if has_error else 0,
        "w_count": len(unique_focus(warnings)),
        "dw_count": 1 if has_warning and not has_error else 0,
        "g_count": 1 if not has_error and not has_warning else 0,
        "t_count": 1,
        "time_sum": duration or 0.0,
        "time_max": duration,
        "http_err": 1 if run["http_status"] not in ("", "200") else 0,
        "truncated": 1 if run["meta"]["truncated"] else 0,
    }


def leaves_for_run(run: dict) -> list[dict]:
    declared = {shape["uri"]: shape for shape in run["shapes"]}
    assigned: dict[str, list[dict]] = {uri: [] for uri in declared}
    synthetic: dict[str, dict] = {}
    for item in run["items"]:
        uri = (
            item["constraint_uri"] if item["constraint_uri"] in declared
            else item["source_shape_uri"] if item["source_shape_uri"] in declared
            else item["constraint_uri"] or item["source_shape_uri"]
        )
        if not uri:
            uri = f"unresolved:{item['severity']}:{item['source_href']}"
        if uri not in assigned:
            synthetic.setdefault(uri, {
                "uri": uri,
                "label": local_name(uri) or "Unresolved result shape",
                "kind": "ResultShape",
                "line": 0,
                "declared": False,
            })
            assigned[uri] = []
        assigned[uri].append(item)
    shapes = list(run["shapes"]) + list(synthetic.values())
    return [make_leaf(run, shape, assigned.get(shape["uri"], [])) for shape in shapes]


def aggregate(children: list[dict], extra_runs: dict[str, dict] | None = None) -> dict:
    runs = dict(extra_runs or {})
    for child in children:
        runs.update(child.get("runs") or {})
    durations = [
        float(run["duration"])
        for run in runs.values()
        if isinstance(run.get("duration"), (int, float))
    ]
    result = {
        key: sum(int(child.get(key) or 0) for child in children)
        for key in VALIDATION_METRICS
    }
    result.update({
        "runs": runs,
        "time_sum": sum(durations),
        "time_max": max(durations) if durations else None,
        "http_err": sum(
            1 for run in runs.values() if str(run.get("http_status") or "") not in ("", "200")
        ),
        "truncated": sum(1 for run in runs.values() if run.get("meta", {}).get("truncated")),
        "execution_err": sum(
            1 for run in runs.values() if run.get("meta", {}).get("execution_error")
        ),
    })
    return result


def node(node_id: str, name: str, children: list[dict], **extra) -> dict:
    return {"id": node_id, "name": name, "children": children, **aggregate(children), **extra}


def _unique_id(stem: str, used: set[str]) -> str:
    base = dc.slug(stem)
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _file_nodes(leaves: list[dict], profile_runs: dict[str, dict]) -> list[dict]:
    """Group a profile's shapes under one node per SHACL file."""
    by_file: dict[str, list[dict]] = {}
    for leaf in leaves:
        by_file.setdefault(leaf["run"]["shacl_file"], []).append(leaf)
    for run in profile_runs.values():
        by_file.setdefault(run["shacl_file"], [])
    used: set[str] = set()
    files = []
    for shacl_file, file_leaves in by_file.items():
        sample = file_leaves[0]["run"] if file_leaves else next(
            run for run in profile_runs.values() if run["shacl_file"] == shacl_file
        )
        shacl_file_node = node(
            _unique_id(Path(shacl_file).stem, used),
            shacl_file,
            sorted(file_leaves, key=lambda item: item["name"].lower()),
            level="file",
            shacl_file=shacl_file,
            shacl_subpath=sample["shacl_subpath"],
            repo=sample["repo"],
        )
        shacl_file_node.update(aggregate(file_leaves, {sample["id"]: sample}))
        files.append(shacl_file_node)
    return sorted(files, key=lambda item: item["name"].lower())


def build_cube(catalog: dict, runs: list[dict]) -> dict:
    """Build data-repository → family → profile → SHACL file → shape.

    The top ``repository`` dimension is the GraphDB *data* repository that was
    validated (``data_repositories[]``), not the shapes-library checkout.
    """
    # data_repo → family → profile → leaves
    by_repo: dict[str, dict[str, dict[str, list[dict]]]] = {}
    runs_by_profile: dict[tuple[str, str, str], dict[str, dict]] = {}
    data_repo_meta: dict[str, dict] = {}
    family_titles: dict[str, str] = {}
    for run in runs:
        data_repo_id = run["data_repository_id"]
        data_repo_meta[data_repo_id] = {
            "id": data_repo_id,
            "title": run.get("data_repository_title") or data_repo_id,
            "graphdb_repository": run.get("graphdb_repository") or "",
            "instance_github_repository": run.get("instance_github_repository") or "",
        }
        family_titles[run["family_id"]] = run.get("family_title") or run["family_id"]
        key = (data_repo_id, run["family_id"], run["profile_id"])
        runs_by_profile.setdefault(key, {})[run["id"]] = run
        by_repo.setdefault(data_repo_id, {}).setdefault(
            run["family_id"], {}
        ).setdefault(run["profile_id"], []).extend(leaves_for_run(run))

    shape_repos = {repo["id"]: repo for repo in catalog["repositories"]}
    repositories = []
    for data_repo_id, families_data in by_repo.items():
        families = []
        for family_id, profiles_data in families_data.items():
            profiles = []
            for profile_id, leaves in profiles_data.items():
                profile_runs = runs_by_profile[(data_repo_id, family_id, profile_id)]
                sample = next(iter(profile_runs.values()))
                shapes_repo = shape_repos.get(sample["repository_id"], sample.get("repo") or {})
                files = _file_nodes(leaves, profile_runs)
                profile = node(
                    profile_id,
                    sample.get("profile_name") or profile_id,
                    files,
                    level="profile",
                    ontology_file=sample.get("ontology_file") or "",
                    ontology_subpath=sample.get("ontology_subpath") or "",
                    repo=shapes_repo,
                )
                profile.update(aggregate(files, profile_runs))
                profiles.append(profile)
            family = node(
                family_id,
                family_titles.get(family_id) or family_id,
                sorted(profiles, key=lambda item: item["name"].lower()),
                level="family",
            )
            families.append(family)
        meta = data_repo_meta[data_repo_id]
        repository = node(
            data_repo_id,
            meta["title"],
            sorted(families, key=lambda item: item["name"].lower()),
            level="repository",
            data_repo=meta,
        )
        repositories.append(repository)
    return node(
        "all",
        "All data repositories",
        sorted(repositories, key=lambda item: item["name"].lower()),
        level="global",
    )


def assert_rollups(parent: dict, path: str = "all") -> None:
    children = parent.get("children") or []
    if not children:
        return
    for key in VALIDATION_METRICS:
        actual = sum(int(child[key]) for child in children)
        if actual != int(parent[key]):
            raise AssertionError(f"{path}: {key}={parent[key]}, children sum to {actual}")
    # Shape leaves do not carry execution_err; that measure lives on the file run.
    if parent.get("level") != "file":
        execution_errors = sum(int(child["execution_err"]) for child in children)
        if execution_errors != int(parent["execution_err"]):
            raise AssertionError(
                f"{path}: execution_err={parent['execution_err']}, "
                f"children sum to {execution_errors}"
            )
    expected_runs = set().union(*(set(child["runs"]) for child in children))
    if not expected_runs.issubset(set(parent["runs"])):
        raise AssertionError(f"{path}: child timing runs are missing from parent")
    if parent["dv_count"] + parent["dw_count"] + parent["g_count"] != parent["t_count"]:
        raise AssertionError(f"{path}: distinct error + warning + good != total")
    for child in children:
        assert_rollups(child, f"{path}/{child['id']}")


def crumb(items: list[tuple[str, Path]], out_dir: Path, current: str) -> str:
    links = [
        f'<a href="{esc(rel(out_dir, target))}">{esc(label)}</a>'
        for label, target in items
    ]
    links.append(esc(current))
    return " · ".join(links)


def metric_value(metric: str, value) -> str:
    if metric.startswith("time_"):
        return ct.format_duration(float(value)) if isinstance(value, (int, float)) else "—"
    return str(value) if int(value or 0) > 0 else ""


def metric_href(child: dict, metric: str, out_dir: Path) -> str:
    """Where a measure on a child row is grounded one level down.

    Aggregates open the child page with the table already ordered by that
    measure; leaves open their focus-node evidence or their source grounding.
    """
    base = child["page"]
    if child.get("children"):
        return f"{rel(out_dir, base)}#sort={metric}"
    if metric == "v_count" and child["v_count"]:
        return rel(out_dir, base.parent / "violation" / "index.html")
    if metric == "w_count" and child["w_count"]:
        return rel(out_dir, base.parent / "warning" / "index.html")
    return rel(out_dir, base)


def cell(child: dict, metric: str, out_dir: Path) -> str:
    display = metric_value(metric, child.get(metric))
    if not display:
        return ""
    return f'<a href="{esc(metric_href(child, metric, out_dir))}">{esc(display)}</a>'


def summary_tiles(scope: dict, out_dir: Path) -> str:
    tiles = []
    for metric in METRICS:
        value = metric_value(metric, scope.get(metric))
        href = f"#sort={metric}" if scope.get("children") else rel(out_dir, scope["page"])
        linked = (
            f'<a href="{esc(href)}">{esc(value)}</a>'
            if value and value != "—" else esc(value)
        )
        tiles.append(f'<div class="stat"><span>{esc(METRIC_LABEL[metric])}</span><strong>{linked}</strong></div>')
    if scope.get("level") in EXECUTION_ERROR_TILE_LEVELS:
        value = metric_value("execution_err", scope.get("execution_err"))
        href = scope["metric_dir"] / "execution_err" / "index.html"
        linked = (
            f'<a href="{esc(rel(out_dir, href))}">{esc(value)}</a>'
            if value else ""
        )
        tiles.append(
            f'<div class="stat"><span>{esc(METRIC_LABEL["execution_err"])}</span>'
            f"<strong>{linked}</strong></div>"
        )
    return f'<div class="stats-panel"><div class="stats">{"".join(tiles)}</div></div>'


def cube_table(scope: dict, children: list[dict], out_dir: Path, first_label: str) -> str:
    rows = []
    for child in children:
        name = f'<a href="{esc(rel(out_dir, child["page"]))}">{brk(child["name"])}</a>'
        rows.append(
            f"""<tr>
<td class="profile">{name}</td>
<td class="dur">{cell(child, "time_sum", out_dir)}</td>
<td class="dur">{cell(child, "time_max", out_dir)}</td>
<td class="num cell">{cell(child, "v_count", out_dir)}</td>
<td class="num cell">{cell(child, "dv_count", out_dir)}</td>
<td class="num cell">{cell(child, "w_count", out_dir)}</td>
<td class="num cell">{cell(child, "dw_count", out_dir)}</td>
<td class="num cell">{cell(child, "g_count", out_dir)}</td>
<td class="num cell">{cell(child, "t_count", out_dir)}</td>
</tr>"""
        )
    sortable = (
        f' data-rows="{esc(rel(out_dir, scope["rows_json"]))}" data-page-size="{PAGE_SIZE}"'
        if scope.get("children") else ""
    )
    return f"""<div class="table-wrap">
<table class="family-table compact"{sortable}>
<colgroup></colgroup><colgroup span="2"></colgroup>
<colgroup class="errors" span="2"></colgroup>
<colgroup class="warnings" span="2"></colgroup>
<thead>
<tr>
<th class="profile sortable" rowspan="2" data-sort="name">{esc(first_label)}</th>
<th class="num sortable" colspan="2" data-sort="time_sum">Timing</th>
<th class="num group-err sortable" colspan="2" data-sort="v_count">{ICON["Violation"]} Errors</th>
<th class="num group-warn sortable" colspan="2" data-sort="w_count">{ICON["Warning"]} Warnings</th>
<th class="num sortable" rowspan="2" data-sort="g_count">{ICON["Good"]} Good</th>
<th class="num sortable" rowspan="2" data-sort="t_count">Total</th>
</tr>
<tr>
<th class="num sortable" data-sort="time_sum">Sum</th>
<th class="num sortable" data-sort="time_max">Max</th>
<th class="num group-err sortable" data-sort="v_count">Total</th>
<th class="num group-err sortable" data-sort="dv_count">Distinct</th>
<th class="num group-warn sortable" data-sort="w_count">Total</th>
<th class="num group-warn sortable" data-sort="dw_count">Distinct</th>
</tr>
</thead>
<tbody>{"".join(rows) if rows else '<tr><td colspan="9">No lower-level records.</td></tr>'}</tbody>
</table></div>"""


def row_payload(child: dict, out_dir: Path) -> list:
    time_max = child.get("time_max")
    return [
        child["name"],
        rel(out_dir, child["page"]),
        0 if child.get("children") else 1,
        round(float(child.get("time_sum") or 0.0), 6),
        metric_value("time_sum", child.get("time_sum")),
        round(float(time_max), 6) if isinstance(time_max, (int, float)) else None,
        metric_value("time_max", time_max),
        *(int(child.get(key) or 0) for key in VALIDATION_METRICS),
    ]


def write_rows_json(scope: dict) -> None:
    """Compact sort payload for one scope: every child row, all measures.

    Fetched lazily by the browser on the first sort, so the default view costs
    no extra request. Sorting happens over the whole child set, not just the
    rows that happen to be on the current static page.
    """
    payload = {
        "columns": list(ROW_COLUMNS),
        "size": PAGE_SIZE,
        "rows": [row_payload(child, scope["page"].parent) for child in scope["children"]],
    }
    path = scope["rows_json"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def pager(page_count: int, current: int) -> str:
    if page_count <= 1:
        return ""
    links = []
    for number in range(1, page_count + 1):
        filename = "index.html" if number == 1 else f"p{number}.html"
        links.append(
            f"<strong>{number}</strong>"
            if number == current else f'<a href="{filename}">{number}</a>'
        )
    return f'<p class="pager" data-pager>Pages: {" · ".join(links)}</p>'


def write_execution_error_page(
    scope: dict,
    breadcrumbs: list[tuple[str, Path]],
) -> None:
    runs = sorted(
        (
            run for run in scope["runs"].values()
            if run.get("meta", {}).get("execution_error")
        ),
        key=lambda run: (
            str(run.get("data_repository_id") or ""),
            str(run.get("family_id") or ""),
            str(run.get("profile_name") or ""),
            str(run.get("shacl_file") or ""),
        ),
    )
    if not runs:
        return
    path = scope["metric_dir"] / "execution_err" / "index.html"
    rows = []
    for run in runs:
        report_href = str(run.get("report_href") or "")
        shacl_file = esc(run.get("shacl_file") or "")
        report = (
            f'<a href="{esc(report_href)}" target="_blank" rel="noopener">{shacl_file}</a>'
            if report_href else shacl_file
        )
        rows.append(
            "<tr>"
            f'<td>{esc(run.get("data_repository_title") or run.get("data_repository_id") or "")}</td>'
            f'<td>{esc(run.get("family_id") or "")}</td>'
            f'<td>{esc(run.get("profile_name") or "")}</td>'
            f"<td>{report}</td>"
            f'<td class="num">{esc(run.get("http_status") or "Unknown")}</td>'
            f'<td><pre class="execution-error">{esc(run["meta"]["error_text"])}</pre></td>'
            "</tr>"
        )
    body = f"""
<p class="crumb">{crumb(breadcrumbs + [(scope["name"], scope["page"])], path.parent, METRIC_LABEL["execution_err"])}</p>
<h1>{esc(scope["name"])} · {esc(METRIC_LABEL["execution_err"])}</h1>
<p class="meta">{len(runs)} validation runs returned non-empty error text instead of a SHACL validation report.</p>
<div class="table-wrap"><table>
<thead><tr><th>Data repository</th><th>Family</th><th>Ontology profile</th><th>SHACL file / response</th><th class="num">HTTP</th><th>Error</th></tr></thead>
<tbody>{"".join(rows)}</tbody>
</table></div>
"""
    write(path, f'{METRIC_LABEL["execution_err"]} · {scope["name"]}', body)


def write_scope_pages(
    scope: dict,
    breadcrumbs: list[tuple[str, Path]],
    first_label: str,
    meta: str,
) -> None:
    base = scope["page"]
    out_dir = base.parent
    chunks = [
        scope["children"][offset:offset + PAGE_SIZE]
        for offset in range(0, max(len(scope["children"]), 1), PAGE_SIZE)
    ]
    for page_number, chunk in enumerate(chunks, 1):
        page_path = base if page_number == 1 else out_dir / f"p{page_number}.html"
        page_nav = pager(len(chunks), page_number)
        base_body = f"""
<p class="crumb">{crumb(breadcrumbs, page_path.parent, scope["name"])}</p>
<h1>{esc(scope["name"])}</h1>
<p class="meta">{meta}</p>
{summary_tiles(scope, page_path.parent) if page_number == 1 else ""}
{page_nav}
{cube_table(scope, chunk, page_path.parent, first_label)}
{page_nav}
"""
        write(page_path, f"{scope['name']} · SHACL cube", base_body)
    if scope["children"]:
        write_rows_json(scope)
    if scope.get("level") in EXECUTION_ERROR_TILE_LEVELS:
        write_execution_error_page(scope, breadcrumbs)


def source_link(leaf: dict) -> str:
    run = leaf["run"]
    return dc.github_blob(run["repo"], run["shacl_subpath"], leaf["line"] or None)


def write_focus_page(leaf: dict, severity: str, breadcrumbs: list[tuple[str, Path]]) -> Path:
    folder = "violation" if severity == "Violation" else "warning"
    path = leaf["page"].parent / folder / "index.html"
    items = unique_focus(leaf["v_items"] if severity == "Violation" else leaf["w_items"])
    rows: list[str] = []
    for item in items:
        focus = brk(item["focus"])
        if item["in_gdb"] and item["graphdb_href"]:
            focus = f'<a href="{esc(item["graphdb_href"])}" target="_blank" rel="noopener">{focus}</a>'
        github = (
            f'<a href="{esc(item["github_href"])}" target="_blank" rel="noopener">GitHub</a>'
            if item["github_href"] else ""
        )
        constraint = (
            f'<a href="{esc(item["source_href"])}" target="_blank" rel="noopener">'
            f'{brk(local_name(item["constraint_uri"] or item["source_shape_uri"]))}</a>'
            if item["source_href"] else brk(local_name(item["constraint_uri"] or item["source_shape_uri"]))
        )
        rows.append(
            f"<tr><td class=\"focus\">{focus}</td><td>{github}</td>"
            f"<td class=\"msg\">{esc(item['message'])}</td><td>{constraint}</td></tr>"
        )
    chunks = [
        rows[offset:offset + PAGE_SIZE]
        for offset in range(0, max(len(rows), 1), PAGE_SIZE)
    ]
    for page_number, chunk in enumerate(chunks, 1):
        page_path = path if page_number == 1 else path.parent / f"p{page_number}.html"
        page_nav = pager(len(chunks), page_number)
        body = f"""
<p class="crumb">{crumb(breadcrumbs, page_path.parent, severity)}</p>
<h1>{ICON[severity]} {esc(severity)} · {brk(leaf["name"])}</h1>
<p class="meta">{len(items)} distinct focus nodes for this individual shape.</p>
{page_nav}
<div class="table-wrap"><table>
<thead><tr><th>sh:focusNode</th><th>GitHub</th><th>sh:message</th><th>sh:sourceConstraint</th></tr></thead>
<tbody>{"".join(chunk) if chunk else '<tr><td colspan="4">None.</td></tr>'}</tbody>
</table></div>
{page_nav}"""
        write(page_path, f"{severity} · {leaf['name']}", body)
    return path


def write_shape_page(leaf: dict, breadcrumbs: list[tuple[str, Path]]) -> None:
    out = leaf["page"]
    out_dir = out.parent
    source = source_link(leaf)
    run = leaf["run"]
    status = (
        "Distinct error" if leaf["dv_count"]
        else "Distinct warning" if leaf["dw_count"]
        else "Good shape" if leaf["g_count"]
        else "Result-only shape"
    )
    violation = write_focus_page(leaf, "Violation", breadcrumbs + [(leaf["name"], out)]) if leaf["v_count"] else None
    warning = write_focus_page(leaf, "Warning", breadcrumbs + [(leaf["name"], out)]) if leaf["w_count"] else None

    def leaf_tile(label: str, value: object, href: Path | str) -> str:
        display = str(value)
        if isinstance(href, str) and href.startswith(("http://", "https://")):
            target = href
        else:
            target = rel(out_dir, href)
        linked = f'<a href="{esc(target)}">{esc(display)}</a>' if display not in ("0", "") else ""
        return f'<div class="stat"><span>{esc(label)}</span><strong>{linked}</strong></div>'

    tiles = (
        leaf_tile("File run time", run["duration_disp"], out)
        + leaf_tile("Errors", leaf["v_count"], violation or out)
        + leaf_tile("Distinct errors", leaf["dv_count"], source)
        + leaf_tile("Warnings", leaf["w_count"], warning or out)
        + leaf_tile("Distinct warnings", leaf["dw_count"], source)
        + leaf_tile("Good shape", leaf["g_count"], source)
        + leaf_tile("Total shapes", leaf["t_count"], source)
    )
    source_label = f'{run["shacl_file"]}:L{leaf["line"]}' if leaf["line"] else run["shacl_file"]
    source_html = f'<a id="evidence" href="{esc(source)}" target="_blank" rel="noopener">{esc(source_label)}</a>'
    body = f"""
<p class="crumb">{crumb(breadcrumbs, out_dir, leaf["name"])}</p>
<h1>{brk(leaf["name"])}</h1>
<p class="meta">Shape file {source_html} · {esc(leaf["kind"])} · {esc(status)}.<br>
The SHACL-file run time is repeated as context and is not summed across shape rows.</p>
<div class="stats-panel"><div class="stats">{tiles}</div></div>
<h2>Grounding</h2>
<p class="note">The source declaration grounds the distinct/good/total measures.
Error and warning measures open their focus-node evidence pages.</p>
"""
    write(out, f"{leaf['name']} · SHACL shape", body)


def assign_pages(cube: dict) -> None:
    # The landing page lives at the repository root, so its sidecar and
    # run-level evidence go under dashboard/all/ to keep the root clean.
    cube["page"] = INDEX
    cube["metric_dir"] = DASH / "all" / "m"
    cube["rows_json"] = DASH / "all" / "rows.json"
    for repo in cube["children"]:
        repo["page"] = DASH / "r" / repo["id"] / "index.html"
        repo["metric_dir"] = repo["page"].parent / "m"
        repo["rows_json"] = repo["page"].parent / "rows.json"
        for family in repo["children"]:
            family["page"] = repo["page"].parent / "f" / family["id"] / "index.html"
            family["metric_dir"] = family["page"].parent / "m"
            family["rows_json"] = family["page"].parent / "rows.json"
            for profile in family["children"]:
                profile["page"] = family["page"].parent / "p" / profile["id"] / "index.html"
                profile["metric_dir"] = profile["page"].parent / "m"
                profile["rows_json"] = profile["page"].parent / "rows.json"
                for shacl_file in profile["children"]:
                    shacl_file["page"] = (
                        profile["page"].parent / "sf" / shacl_file["id"] / "index.html"
                    )
                    shacl_file["rows_json"] = shacl_file["page"].parent / "rows.json"
                    for leaf in shacl_file["children"]:
                        leaf["page"] = shacl_file["page"].parent / "s" / leaf["id"] / "index.html"


def cube_node_json(scope: dict) -> dict:
    record = {
        "id": scope["id"],
        "name": scope["name"],
        **{key: scope.get(key) for key in METRICS},
    }
    # An aggregate can have an empty child list (a profile whose SHACL files
    # declare no shapes). That is not a shape leaf, and it has no "run".
    if "children" in scope:
        record.update({
            "level": scope["level"],
            "page": scope["page"].relative_to(ROOT).as_posix(),
            "execution_err": scope.get("execution_err", 0),
            "children": [cube_node_json(child) for child in scope["children"]],
        })
    else:
        # Shape pages carry no "page": they are always the parent SHACL file's
        # page directory + s/<id>/index.html, and spelling out 13k long paths
        # costs more than the whole rest of the file.
        run = scope["run"]
        record.update({
            "level": "shape",
            "uri": scope["uri"],
            "kind": scope["kind"],
            "declared": scope["declared"],
            "shacl_file": run["shacl_file"],
            "line": scope["line"],
        })
    return record


def write_cube_json(cube: dict) -> Path:
    """The whole cube as one machine-readable artifact.

    Not used by the pages — it exists so cross-cutting questions ("every shape
    above 50 errors, across all families") can be answered without scraping
    HTML or re-running the build.
    """
    path = DASH / "cube.json"
    payload = {
        "metrics": list(METRICS),
        "shape_page": "<parent page directory>/s/<id>/index.html",
        "root": cube_node_json(cube),
    }
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return path


def build(
    collect,
    dash_dir: Path | None = None,
    index_path: Path | None = None,
    source_note: str = "",
) -> Path:
    """Render the cube from an injected run source.

    ``collect`` is any ``(catalog, index) -> list[run]`` callable; the only one
    in the repository reads the GraphDB validation-history graph (see
    ``build_dashboard_gdb.py``). ``dash_dir``/``index_path`` default to the
    published ``dashboard/`` + ``index.html``, and ``source_note`` is appended
    to the landing frontmatter to say where the numbers came from.
    """
    global DASH, INDEX
    DASH = dash_dir or ROOT / "dashboard"
    INDEX = index_path or ROOT / "index.html"
    config = dc.load_config()
    catalog = dc.build_catalog(config)
    if not catalog["sources"]:
        looked = []
        for repo in catalog["repositories"]:
            dirs = [
                directory
                for family in repo.get("families") or []
                for directory in (family.get("shacl_dirs") or [])
            ]
            looked.append(f"{repo['checkout_path']} [{', '.join(dirs)}]")
        raise SystemExit(
            "No configured SHACL files were found under "
            + "; ".join(looked)
            + ". Clone entsoe/CGMES-AP and entsoe/NC-AP at the configured "
            "checkout paths before building."
        )
    index = cr.build_shacl_index()
    if DASH.exists():
        shutil.rmtree(DASH)
    DASH.mkdir()
    runs = collect(catalog, index)
    cube = build_cube(catalog, runs)
    assign_pages(cube)
    assert_rollups(cube)

    cube_json = write_cube_json(cube)
    root_meta = (
        "Configured GraphDB data repositories. Drill-down is "
        "data repository → family → ontology profile → SHACL file → individual shape. "
        "Timing sum/max use unique SHACL-file validation runs.<br>\n"
        "Click any column header to reorder the table by that measure.<br>\n"
        f'Full cube data: <a href="{esc(rel(INDEX.parent, cube_json))}">cube.json</a>.'
    )
    if source_note:
        root_meta = f"{root_meta}<br>\n{source_note}"
    write_scope_pages(cube, [], "Data repository", root_meta)
    for repo in cube["children"]:
        data_repo = repo.get("data_repo") or {}
        gdb_name = data_repo.get("graphdb_repository") or repo["id"]
        github = data_repo.get("instance_github_repository") or ""
        github_html = (
            f' · instance data <a href="https://github.com/{esc(github)}" '
            f'target="_blank" rel="noopener">{esc(github)}</a>'
            if github else ""
        )
        repo_meta = (
            f'GraphDB repository <code>{esc(gdb_name)}</code>{github_html}. '
            "Families group the configured SHACL sources validated against this repository."
        )
        repo_crumbs = [("All data repositories", cube["page"])]
        write_scope_pages(repo, repo_crumbs, "Family", repo_meta)
        for family in repo["children"]:
            family_crumbs = repo_crumbs + [(repo["name"], repo["page"])]
            write_scope_pages(
                family,
                family_crumbs,
                "Ontology profile",
                "Ontology profiles are resolved from PROF manifests; shared artifacts are counted once.",
            )
            for profile in family["children"]:
                profile_crumbs = family_crumbs + [(family["name"], family["page"])]
                ontology = (
                    f'<a href="{esc(dc.github_blob(profile["repo"], profile["ontology_subpath"]))}" '
                    f'target="_blank" rel="noopener">{esc(profile["ontology_file"])}</a>'
                    if profile["ontology_subpath"] else "No single ontology mapping"
                )
                write_scope_pages(
                    profile,
                    profile_crumbs,
                    "SHACL file",
                    f"Ontology: {ontology}. Each row is one SHACL file. "
                    "Timing is that file's validation run.",
                )
                file_crumbs = profile_crumbs + [(profile["name"], profile["page"])]
                for shacl_file in profile["children"]:
                    file_href = dc.github_blob(
                        shacl_file["repo"], shacl_file["shacl_subpath"]
                    )
                    write_scope_pages(
                        shacl_file,
                        file_crumbs,
                        "Individual shape",
                        f'SHACL file <a href="{esc(file_href)}" target="_blank" '
                        f'rel="noopener">{esc(shacl_file["shacl_file"])}</a>. '
                        "Each shape row repeats this file's validation time; "
                        "the file total counts that run once.",
                    )
                    shape_crumbs = file_crumbs + [(shacl_file["name"], shacl_file["page"])]
                    for leaf in shacl_file["children"]:
                        write_shape_page(leaf, shape_crumbs)
    print(
        f"Cube: {len(cube['children'])} data repositories, {len(runs)} runs, "
        f"{cube['t_count']} individual shapes, "
        f"cube.json {cube_json.stat().st_size / 1048576:.1f} MiB"
    )
    return INDEX