#!/usr/bin/env bash
# The whole pipeline, end to end:
#
#   1. validate every SHACL file in CGMES/NCP against the relicapgrid
#      repository, one POST per file;
#   2. parse each response and append it to the GraphDB validation-history
#      graph (write-validation-run-to-gdb.py) -- no report or timing files are
#      written to disk;
#   3. re-apply the retention window over the whole graph (gdb_history.py);
#   4. rebuild the dashboard from that graph (build_dashboard_gdb.py).
#
# Re-running does not overwrite previous runs, but history is bounded: each
# SHACL file keeps its last two timings and the validation results of the very
# last run only, so the dashboard can show the current state and how it differs
# from the run before it.
set -euo pipefail
shopt -s nullglob

APL="$(cd "$(dirname "$0")/../application-profiles-library" && pwd)"
echo "Application Profiles Library: $APL"
SHACL_DIRS=(
  "$APL/CGMES/SHACL"
  "$APL/NCP/SHACL"
)
echo "SHACL Directories: ${SHACL_DIRS[*]}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

GDB_URL="${GDB_BASE_URL:-http://localhost:7200/}"
VALIDATE_REPO="relicapgrid"
VALIDATE_ENDPOINT="${GDB_URL}rest/repositories/${VALIDATE_REPO}/validate/file"

# Repository/named graph the validation-run history is written into. This is
# deliberately separate from $VALIDATE_REPO (the instance data being
# validated) -- it accumulates report + timing history over time and is
# never dropped by this script. Override via env vars if needed.
HISTORY_REPO="${GDB_HISTORY_REPOSITORY:-cim-shacl}"
HISTORY_GRAPH="${GDB_HISTORY_GRAPH:-https://github.com/entsoe/application-profiles/SHACL/reports}"

LOG="$SCRIPT_DIR/batch-gdb.log"
: > "$LOG"

# GDBUSER/GDBPASS must be exported in the environment before running this script.
: "${GDBUSER:?GDBUSER env var must be set}"
: "${GDBPASS:?GDBPASS env var must be set}"

GDB_AUTH_URL="${GDB_URL}rest/login/${GDBUSER}"
auth_header=$(curl -s "$GDB_AUTH_URL" -X POST -H "X-GraphDB-Password: $GDBPASS" -I | grep -i "authorization:")
token=${auth_header#*: }
token=${token%$'\r'}
AUTH_HEADER="Authorization: Bearer $token"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

done_count=0
fail_count=0

for dir in "${SHACL_DIRS[@]}"; do
  for file in "$dir"/*.ttl; do
    base=$(basename "$file")
    [[ "$base" == "validation-report.ttl" || "$base" == "relicap-val-report.ttl" ]] && continue

    name="${base%.ttl}"
    tmp_report="$tmp_dir/$name.ttl"

    echo "[$(date -Iseconds)] START $name" | tee -a "$LOG"
    start=$(python3 -c 'import time; print(time.perf_counter())')
    http_code=$(curl -s -X POST --header 'Accept: text/turtle' -H "$AUTH_HEADER" \
      "$VALIDATE_ENDPOINT" \
      -F "file=@$file;type=text/turtle" \
      -o "$tmp_report" \
      -w "%{http_code}")
    end=$(python3 -c 'import time; print(time.perf_counter())')
    duration=$(python3 -c "print(f'{$end - $start:.3f}')")
    finished_at="$(date -Iseconds)"

    set +e
    py_out=$(python3 "$SCRIPT_DIR/write-validation-run-to-gdb.py" \
      --shacl-file "$file" \
      --report "$tmp_report" \
      --repository-validated "$VALIDATE_REPO" \
      --http-status "$http_code" \
      --duration-seconds "$duration" \
      --finished-at "$finished_at" \
      --gdb-url "$GDB_URL" \
      --repository "$HISTORY_REPO" \
      --graph "$HISTORY_GRAPH" \
      --auth-header "$AUTH_HEADER" 2>&1)
    py_status=$?
    set -e
    echo "$py_out" >> "$LOG"
    rm -f "$tmp_report"

    if [[ "$http_code" == "200" && $py_status -eq 0 ]]; then
      echo "[$(date -Iseconds)] DONE $name (${duration}s) -> $HISTORY_REPO" | tee -a "$LOG"
      ((done_count++)) || true
    else
      echo "[$(date -Iseconds)] FAIL $name (HTTP $http_code, ${duration}s)" | tee -a "$LOG"
      ((fail_count++)) || true
    fi
  done
done

echo "[$(date -Iseconds)] BATCH COMPLETE: $done_count succeeded, $fail_count failed" | tee -a "$LOG"
echo "[$(date -Iseconds)] History written to repository '$HISTORY_REPO', named graph <$HISTORY_GRAPH>" | tee -a "$LOG"

# Retention is applied per file as each run is written, but a batch that was
# interrupted can leave files that were never revisited outside the window.
# Re-applying it over the whole graph is idempotent and takes seconds.
echo "[$(date -Iseconds)] Applying retention window to the whole graph" | tee -a "$LOG"
python3 "$SCRIPT_DIR/gdb_history.py" --auth-header "$AUTH_HEADER" 2>>"$LOG" | tee -a "$LOG"

# The dashboard is rebuilt from the graph, never from files, so this is the
# only generation step: it rewrites index.html and dashboard/ in place.
echo "[$(date -Iseconds)] GENERATING DASHBOARD FROM GRAPHDB" | tee -a "$LOG"
python3 "$SCRIPT_DIR/build_dashboard_gdb.py" 2>>"$LOG" | tee -a "$LOG"
