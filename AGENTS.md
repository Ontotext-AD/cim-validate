# AGENTS.md — ReliCapGrid SHACL evaluation dashboard

This repository validates **ReliCapGrid ENTSO-E** test data against CGMES and
NCP Application-Profile shapes, stores every validation run in a GraphDB named
graph, and turns the latest run of each SHACL file into a static HTML
dashboard.

There is exactly **one pipeline and one dashboard**. `validate-all-and-write-to-gdb.sh`
validates, records, prunes, and rebuilds; `index.html` + `dashboard/` are the
only generated site. Validation runs are never written to disk as
`validation-report.ttl` / `timing.txt` files — the earlier file-based pipeline
is retired and git-ignored (see *Retired file-based pipeline*).

Agents working here should read this file first, then `README.md` for user-facing
documentation.

## Technical stack

| Layer | Technology |
|-------|------------|
| Validation | GraphDB SHACL validation API (`data.graphdb_repository` on `validation_history.base_url`) |
| Run storage | GraphDB named graph `validation_history.graph` in `validation_history.repository` |
| Build language | Python 3.10+ (stdlib only, except the writer — see below) |
| Writer dependencies | `rdflib` + `requests` (`requirements.txt`), used only by `write-validation-run-to-gdb.py` |
| Batch orchestration | Bash (`validate-all-and-write-to-gdb.sh`) |
| Dashboard output | Static HTML + CSS + small inline JavaScript |
| Styling | `dashboard.css` — Prime/Bootstrap design tokens, Rubik font, gray ground, white cards |
| Hosting | GitHub Pages. The workflow rebuilds `index.html` + `dashboard/` on the runner and publishes that copy. Those files are git-ignored. |
| External repos | [`entsoe/CGMES-AP`](https://github.com/entsoe/CGMES-AP) and [`entsoe/NC-AP`](https://github.com/entsoe/NC-AP) (SHACL shapes), [`entsoe/relicapgrid`](https://github.com/entsoe/relicapgrid) (instance data) |
| GraphDB links | Resource explorer + SPARQL presence lookup; GraphDB link omitted when IRI absent |

There is no test suite. Correctness is verified by rebuilding the dashboard and
spot-checking generated HTML.

## Repository layout

```
relicap-ap-shacl-eval/
├── validate-all-and-write-to-gdb.sh  # THE pipeline: validate → record → prune → build
├── write-validation-run-to-gdb.py    # one run (report + timing) -> GraphDB named graph
├── gdb_history.py                # retention window for that graph + standalone pruner
├── gdb_source.py                 # SPARQL reader for the GraphDB validation-history graph
├── build_dashboard_gdb.py        # dashboard entry point + run-history page
├── cube_dashboard.py             # cube metrics + page generator
├── dashboard_config.py           # config validation + PROF catalog
├── dashboard-config.json         # source repositories/families, history graph, link config
├── collect-results.py            # library: IRI shortening, SHACL index, GitHub/GraphDB links
├── collect-timings.py            # library: format_duration()
├── dashboard.css                 # shared styles (referenced with relative paths)
├── validation-result-model.ttl   # the shape of what the writer puts in the graph
├── index.html                    # landing dashboard (generated, committed)
└── dashboard/                    # generated drill-down pages (git-ignored; rebuilt on the runner)
    ├── cube.json                 # whole cube, one machine-readable artifact
    ├── runs/                     # run history with per-file Δ against the previous run
    ├── all/rows.json             # sort payload for the landing page
    ├── all/m/execution_err/      # run-level execution-error evidence
    └── r/<REPOSITORY>/
        ├── rows.json             # sort payload, one per scope that has children
        ├── m/execution_err/
        └── f/<FAMILY>/
            ├── rows.json
            ├── m/execution_err/
            └── p/<PROFILE>/
                ├── rows.json
                └── sf/<SHACL-FILE>/
                    ├── rows.json
                    └── s/<SHAPE>/    # source grounding + focus-node evidence
```

`collect-results.py` and `collect-timings.py` are **libraries, not pipeline
steps**. They are loaded by filename from `cube_dashboard.py` and no longer
have a `main()`; never add one back and never invoke them from a script. They
read no validation report and no timing file.

Source checkouts are declared in `dashboard-config.json`; paths are resolved
relative to this repository.

### Retired file-based pipeline

These are git-ignored and must not be re-added, re-run, or referenced from new
code: `validate-all.sh`, `continue-validate.sh`, `build_dashboard.py`,
`upload-results.py`, the per-profile `*-SHACL/` folders with their
`validation-report.ttl` / `timing.txt`, `.timing-history.json`,
`validation-results.csv`, `validation-results.html`, and `batch*.log`. They may
still exist in a working copy; they are not part of the project.

## Architecture patterns

### Static site generation

The dashboard is fully pre-rendered HTML. There is no server-side runtime.
`build_dashboard_gdb.py` delegates to `cube_dashboard.py`, which:

1. Deletes and recreates `dashboard/`.
2. Loads configured repositories/families and PROF-derived profile mappings.
3. Reads each run from the injected source and parses each declared SHACL shape.
4. Builds individual-shape leaves and rolls them up through SHACL file,
   ontology profile, family, repository, and global scopes.
5. Asserts validation sums and unique timing-run identities at every boundary.
6. Queries GraphDB (SPARQL) to decide focus-node link targets and writes pages.

Keep pages small: paginate large evidence tables at 1,500 rows.

### Hybrid rendering: pre-rendered pages, client-side ordering

Presentation state that used to be materialised as files is now computed in the
browser. Pre-render the hierarchy and the evidence; do **not** pre-render sort
orders.

- `write_rows_json()` emits a `rows.json` sidecar next to every scope page that
  has children: one compact array per child row (`ROW_COLUMNS`), carrying both
  the numeric value and the pre-formatted display string for each measure, so
  the browser never reimplements `ct.format_duration()`.
- `SORT_SCRIPT` fetches that sidecar **lazily on the first sort**, sorts the
  whole child set, re-renders `<tbody>`, and rewrites the `[data-pager]` slots
  with hash-based page links. The default view issues no extra request.
- Sort state lives in the URL fragment
  (`#sort=<metric>&dir=<asc|desc>&page=<n>`) so sorted views stay shareable and
  reproducible. `readHash()` is the single source of truth; header clicks only
  set `location.hash` and let `hashchange` re-render.
- Header clicks cycle descending → ascending → default; the default restores the
  captured original `<tbody>` HTML, so it is byte-identical to the static page.
- Rows sort across the whole scope, not just the current static page. Sorting
  must stay correct on profiles large enough to paginate (one exceeds 7,000
  shapes).
- Degradation is mandatory: no JavaScript, an old browser without `fetch`, or a
  failed sidecar request must leave the pre-rendered tables and their
  `p<n>.html` pages fully usable.
- `write_cube_json()` emits `dashboard/cube.json`, the whole cube as one
  artifact for cross-cutting questions. No page reads it. Shape entries omit
  `page` because it is always the parent SHACL file's page directory plus
  `s/<id>/index.html`, and spelling out 13k long paths costs more than the rest
  of the file.

Any new measure must be free: add it to `METRICS` and `ROW_COLUMNS`, not to a
new tree of generated pages.

### The run source is injected, not hardcoded

`cube_dashboard.build()` takes a **required** `collect` callable
(`(catalog, index) -> list[run]`) plus optional `dash_dir`, `index_path`, and
`source_note`. The cube itself knows nothing about where runs come from, so a
new source supplies runs and output paths — never a forked copy of the cube or
the page generators. `build()` cannot be called without a source; there is no
file-based fallback to drop back to.

`build_dashboard_gdb.py` is the only source: `gdb_source.py` reads runs from
the GraphDB validation-history graph and the entry point maps them onto
catalog sources, reusing `parse_shapes()`, `result_item()`, and
`iris_present_in_graphdb()`. It writes `index.html` + `dashboard/`, taken from
`validation_history.index` / `.dashboard_dir` in `dashboard-config.json`.

The history graph keeps the last two runs per SHACL file (see *History
retention*); the cube uses the **latest run per file**, or the latest at or
before `--as-of`. `gdb_source.link_previous()` attaches the preceding run's
timing to each run, which is what the run-history Δ column and the landing
comparison line report. A configured SHACL file with no run in the graph counts
as one validation execution error. Run metadata is resolved from the
`sh:ValidationReport` node, falling back to a metadata-bearing `sh:result` node
(how the retired `upload-results.py` wrote it); reports with no `ex:file`
anywhere are excluded and counted in the landing frontmatter.

When adding a run source, keep `run_meta()`'s keys (`exists`, `turtle`,
`conforms`, `truncated`, `execution_error`, `error_text`) and feed
`result_item()` Turtle-shaped terms — `gdb_source.term_to_turtle()` shows the
required escaping, including ASCII-only literals because
`collect-results.py` unescapes with `unicode_escape`.

### Module loading

`cube_dashboard.py` loads sibling scripts by filename (not package import):

```python
cr = _load("collect_results", "collect-results.py")
ct = _load("collect_timings", "collect-timings.py")
```

Reuse helpers from `collect-results.py` and `collect-timings.py` rather than
duplicating IRI, link, or formatting logic. Both are library-only: they expose
no `main()` and are never executed as a pipeline step.

### Hierarchical navigation

```
index.html
  └── dashboard/r/<REPOSITORY>/index.html
        └── f/<FAMILY>/index.html
              └── p/<ONTOLOGY-PROFILE>/index.html
                    └── sf/<SHACL-FILE>/index.html
                          └── s/<INDIVIDUAL-SHAPE>/index.html
```

- Each non-zero aggregate measure links to the next level down with
  `#sort=<METRIC>`, which loads that page already ordered by the measure.
- Ontology-profile frontmatter links the mapped RDFS artifact on GitHub.
- SHACL-file frontmatter links that file on GitHub. Its table lists the
  declared shapes and sorts like every aggregate above it.
- Individual-shape frontmatter links the exact SHACL declaration line.
- Shape error/warning counts link to focus-node evidence; distinct/good/total
  counts link to source grounding.
- Landing, repository, and family frontmatter includes a **Validation execution
  errors** tile linking to affected validation runs and their returned error text.

### Metrics model (additive, reproducible)

The canonical leaf is one declared SHACL shape in one SHACL file:

| Column | Meaning |
|--------|---------|
| **Errors** (Total) | Distinct `sh:focusNode` values with `sh:Violation`, per shape |
| **Distinct errors** | Shapes that produced at least one violation |
| **Warnings** (Total) | Distinct `sh:focusNode` values with `sh:Warning`, per shape |
| **Distinct warnings** | Warning shapes with no violation |
| **Good** | Declared shapes with neither violation nor warning |
| **Total** | Distinct errors + Distinct warnings + Good |

All validation measures are sums of leaves. Error precedence keeps shape buckets
exclusive. Timing sum/max uses the set of unique SHACL-file runs attached to a
scope, so shared shapes never multiply runtime. Shape pages repeat the containing
file-run time as non-additive context until per-shape SPARQL timings are available.

**Validation execution errors** counts unique runs that produced no SHACL
`sh:ValidationReport`: a run recorded with `ex:executionError true`, or a
configured SHACL file with no run in the graph at all. It is a run-level
measure, not a shape-level measure, and is shown only in landing, repository,
and family frontmatter.

### Focus-node link resolution

During `build()`, `assign_focus_gdb_presence()` batch-queries GraphDB and sets
`in_gdb` on each result item:

- **GraphDB** — `sh:focusNode` column links to resource explorer (`…/resource?uri=…&repositoryId=<data.graphdb_repository>&role=all`) when the IRI is present in the repository; the cell is plain text otherwise.
- **GitHub** — separate column: code search in `data.instance_github_repository` (UUID substring or full IRI) when a search query can be formed.

Always use `role=all`, not `role=subject`. Presence check uses SPARQL batched in
chunks of 100 (`GDB_LOOKUP_CHUNK` in `collect-results.py`). Network failure logs
to stderr and omits GraphDB links for that batch.

### SHACL shape indexing

`build_shacl_index()` maps every subject URI in each SHACL file to its line
number. Turtle subjects start at column 0 in these files — rely on that for
GitHub `#L` links. Search order: current profile file first, then other indexed
files.

PROF RDF/XML manifests map constraint artifacts to vocabulary artifacts.
If an artifact appears in several manifests, keep one synthetic shared profile;
never duplicate its validation measures across ontology profiles.

## Build commands

### Regenerate the dashboard only (most common for agents)

Requires the local GraphDB with the validation-history graph populated, both
to read the runs and for the focus-node presence lookup:

```bash
python3 build_dashboard_gdb.py
python3 build_dashboard_gdb.py --as-of 2026-09-20T00:00:00+03:00
python3 gdb_source.py            # list the recorded runs without writing pages
```

This rewrites `index.html` and `dashboard/` in place. Nothing else generates
pages; there is no CSV step and no timing-history step.

### Full pipeline (requires GraphDB credentials)

```bash
export GDBUSER=<username>
export GDBPASS=<password>
./validate-all-and-write-to-gdb.sh
```

One script, four stages: validate every configured SHACL file against
`data.graphdb_repository`, append each response to
`validation_history.repository` via `write-validation-run-to-gdb.py` (rdflib +
requests), re-apply the retention window with `gdb_history.py`, then rebuild
the dashboard with `build_dashboard_gdb.py`. No report or timing files are
written to disk. GraphDB endpoints and repository names come from
`dashboard-config.json`.

The graph is never dropped, so each SHACL file gets one more
`sh:ValidationReport` node per run, tagged `ex:file` / `ex:finishedAt` /
`ex:durationSeconds` / `ex:httpStatus`, with the `sh:ValidationResult` nodes
valid at that time. Non-report responses are stored with `ex:executionError
true` and `ex:errorText`. Destination overrides: `GDB_HISTORY_REPOSITORY`,
`GDB_HISTORY_GRAPH`, `GDB_BASE_URL`.

There is no resume script. The batch is re-runnable: re-running appends fresh
runs and retention discards what falls outside the window.

### History retention

The graph is bounded, not unbounded. `gdb_history.py` keeps, per SHACL file:

- the **last `keep_timings` runs** (default 2) — enough to show the current
  run and how it differs from the one before it;
- `sh:result` nodes on the **newest `keep_results` runs** only (default 1) —
  older runs keep `ex:finishedAt` / `ex:durationSeconds` / `ex:httpStatus` and
  report no results.

Window sizes come from `validation_history.keep_timings` /
`keep_results` in `dashboard-config.json`.

`write-validation-run-to-gdb.py` calls `gdb_history.prune_file()` for the file
it just wrote, so the window is an invariant of the write path rather than a
cleanup job (`--no-prune` opts out). A prune failure is a warning, not an
error: the run is already recorded correctly, and the batch script re-applies
the window over the whole graph at the end.

```bash
python3 gdb_history.py --dry-run    # report graph size and the window
python3 gdb_history.py              # apply it everywhere (idempotent)
python3 gdb_history.py --file Equipment-AP-Con-Simple-SHACL.ttl
```

`prune_file()` normalizes before it prunes: runs written by the retired
`upload-results.py` (and the first version of the writer) carry their metadata
on a `sh:ValidationResult` node instead of the report, which makes them
invisible to a window query keyed on `?report ex:file`. Normalization lifts
`ex:*` metadata onto the report node so every run is inside the window.

Pruning is ordering-driven, so a report with no `ex:finishedAt` is never
selected for deletion.

**Back up before widening or narrowing the window** — deletion is not
reversible:

```bash
url=$(python3 -c "import dashboard_config as dc; from urllib.parse import quote; h=dc.history_settings(); print(f\"{h['sparql']}/statements?context={quote('<'+h['graph']+'>')}\")")
curl -s -H 'Accept: application/n-quads' "$url" -o history-backup.nq
```

### Local preview

Serve over HTTP (not `file://` — browser security blocks local navigation):

```bash
python3 -m http.server 8765 --bind 127.0.0.1
# open http://127.0.0.1:8765/index.html
```

## Coding rules and constraints

### HTML generation

- Escape all dynamic text with `esc()` (`html.escape(..., quote=True)`).
- Long IRIs use `brk()` for `<wbr>` break opportunities.
- Relative links: compute with `rel_href(from_dir, target)` so nested pages resolve
  correctly.
- CSS href: `css_href(from_dir)` — depth-relative path to root `dashboard.css`.
- Wrap tables in `<div class="table-wrap">` for resize support.
- **Zero counts render as empty cells**, not `"0"` (`count_cell()`). `SORT_SCRIPT`
  re-renders rows in the browser, so any change to cell rendering must be made
  in both `cube_table()` and the script's `cellHtml()`/`rowHtml()`.
- **Icons in column headers only** (red circle = Violation, yellow triangle =
  Warning, green circle = Good). Cells contain clickable numbers, not icons.
- **Column alignment**: numeric columns centered (`.num.cell`); repository,
  family, profile, and shape columns left-aligned.

### Table column order

**All aggregation tables** (`cube_table`):

Next-level member · Timing (Sum / Max) · Errors (Total / Distinct) ·
Warnings (Total / Distinct) · Good · Total

**Drill-down focus-node lists**:

`sh:focusNode` · GitHub · `sh:message` · `sh:sourceConstraint` (no separate severity or
SHACL columns — severity is implied by the page path). The focus-node IRI links to
GraphDB when present; GitHub is a separate column.

### Client-side behaviour

Two independent inline scripts, both vanilla and dependency-free:

- `TABLE_SCRIPT` adds column drag handles on `.table-wrap`. Emitted whenever the
  body contains a `<table>`.
- `SORT_SCRIPT` drives column sorting. Emitted only when the body contains
  `data-rows`, so evidence and shape pages stay script-light.

Sortable headers carry `class="sortable" data-sort="<metric>"`; the active one
gets `data-dir`, and the arrow is a CSS `::after`. The sort handler ignores
clicks landing on a `.col-resize` grip so resizing never triggers a sort.

Keep both scripts framework-free and inline. Do not add npm, a bundler, or a
lockfile — the project is stdlib-Python plus hand-written CSS and JS.

### Configuration

Edit `dashboard-config.json` for shape repositories, local checkouts, GitHub
repository/ref, families, SHACL/RDFS/PROF directories, and optional
SHACL-file-to-profile overrides. Families and repositories are extensible
lists. GraphDB and instance GitHub settings live in the top-level `data`
object; the history graph, published output paths, and retention window live
in `validation_history`. Scripts read those objects through
`dashboard_config.data_settings()` and `history_settings()` — do not hardcode
GraphDB repository names or endpoints.

### Source report limits

GraphDB caps reports at **1,000 results per constraint component** and
**10,000 per report**. Truncated reports carry `rdf4j:truncated true`; dashboard
counts for those profiles are lower bounds. Do not silently treat truncated counts
as exact.

### Validation execution errors

GraphDB error responses are recorded in the history graph as a report with
`ex:executionError true` and the returned text in `ex:errorText`. That, and a
configured SHACL file with no run in the graph, each contribute one run-level
**Validation execution errors** count and appear on the tile's evidence page
with the SHACL file, HTTP status, and returned text. They contribute no SHACL
result rows.

### Do not

- Commit secrets (`GDBUSER`, `GDBPASS`).
- Add third-party Python dependencies without explicit user request. Only
  `write-validation-run-to-gdb.py` may use `rdflib`/`requests`; everything the
  dashboard build touches is stdlib.
- Reintroduce a file-based run source. Runs come from the GraphDB history
  graph; `cube_dashboard.build()` has no default collector on purpose.
- Hardcode GraphDB repository names or endpoints (`cim-shacl`, `relicapgrid`,
  `localhost:7200`). Read them from `dashboard-config.json`.
- Give `collect-results.py` or `collect-timings.py` a `main()`, or call them
  from a script. They are libraries.
- Link focus nodes only to GraphDB — always offer GraphDB (when present) and GitHub links separately.
- Link shapes or constraints to RDFS vocabulary files — link to the profile SHACL
  file on GitHub.
- Duplicate family listings on the index page (family name in section title is
  the link; no separate “Open X results table” links).
- Show “reports parsed” in frontmatter (removed intentionally).
- Use full-width layout — dashboard content stays in a constrained card width
  (`max-width` in CSS).
- Pre-render sort orders as pages again. Ordering is a client-side concern; the
  `m/<METRIC>/` tree was removed for that reason and only `m/execution_err/`
  remains (it is evidence, not an ordering).
- Make sorting a hard requirement for reading the dashboard. Every number must
  stay reachable with JavaScript disabled.

## File ownership

| Task | Primary file |
|------|----------------|
| The whole pipeline | `validate-all-and-write-to-gdb.sh` |
| Writing one validation run into GraphDB | `write-validation-run-to-gdb.py` |
| History retention / pruning the GraphDB graph | `gdb_history.py` |
| Reading validation runs back out of GraphDB | `gdb_source.py` |
| Dashboard entry point, run-history page, Δ rendering | `build_dashboard_gdb.py` |
| Cube structure, pages, metrics, client-side sorting | `cube_dashboard.py` |
| IRI shortening, SHACL index, GraphDB/GitHub links | `collect-results.py` |
| Duration formatting | `collect-timings.py` |
| Visual theme, table layout, icons | `dashboard.css` |

When changing link behaviour, update both the implementation and the meta text on
`index.html` / drill-down pages / `README.md`.

## CI

`.github/workflows/dashboard.yml` rebuilds the dashboard on the self-hosted
runner, where the GraphDB history graph is reachable, then copies the generated
`index.html` and `dashboard/` into `_site` alongside `dashboard.css` and deploys
that copy to GitHub Pages. It does not rerun SHACL validation.

`index.html` and `dashboard/` are git-ignored. Do not commit them. A published
update is the workflow run, not a commit of generated pages.
