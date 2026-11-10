#!/usr/bin/env bash
# The whole pipeline, end to end:
#
#   1. validate every configured SHACL file against the instance-data
#      repository in dashboard-config.json, one POST per file;
#   2. parse each response and append it to the GraphDB validation-history
#      graph (write-validation-run-to-gdb.py) -- no report or timing files are
#      written to disk;
#   3. re-apply the retention window over the whole graph (gdb_history.py);
#   4. rebuild the dashboard from that graph (build_dashboard_gdb.py), unless
#      SKIP_DASHBOARD_BUILD=1 (the GitHub validation workflow sets this and
#      then starts the dashboard workflow, which publishes the pages).
#      The script exits non-zero when any SHACL file failed, after that rebuild
#      or skip, so the workflow does not publish a partial batch.
#
# GraphDB URLs, repositories, the history named graph, and the SHACL file
# list all come from dashboard-config.json. GDB_BASE_URL,
# GDB_HISTORY_REPOSITORY, and GDB_HISTORY_GRAPH still override the matching
# JSON fields.
#
# Re-running does not overwrite previous runs, but history is bounded: each
# SHACL file keeps its last two timings and the validation results of the very
# last run only, so the dashboard can show the current state and how it differs
# from the run before it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
eval "$(python3 "$SCRIPT_DIR/dashboard_config.py" --export-shell)"
: "${GDB_URL:?}" "${VALIDATE_REPO:?}" "${HISTORY_REPO:?}" "${HISTORY_GRAPH:?}"

echo "GraphDB: $GDB_URL"
echo "Validate repository: $VALIDATE_REPO"
echo "History repository: $HISTORY_REPO"
echo "History graph: $HISTORY_GRAPH"

VALIDATE_ENDPOINT="${GDB_URL}rest/repositories/${VALIDATE_REPO}/validate/file"

LOG="$SCRIPT_DIR/batch-gdb.log"
: > "$LOG"

# GDBUSER/GDBPASS must be exported in the environment before running this script.
: "${GDBUSER:?GDBUSER env var must be set}"
: "${GDBPASS:?GDBPASS env var must be set}"

GDB_AUTH_URL="${GDB_URL}rest/login/${GDBUSER}"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
login_headers="$tmp_dir/login.headers"
login_body="$tmp_dir/login.body"
login_code=$(curl -sS -D "$login_headers" -o "$login_body" -X POST \
  -H "X-GraphDB-Password: $GDBPASS" \
  -w "%{http_code}" \
  "$GDB_AUTH_URL" || true)
auth_header=$(grep -i "^authorization:" "$login_headers" || true)
auth_header=${auth_header%$'\r'}
if [[ -z "$auth_header" ]]; then
  echo "GraphDB login failed: HTTP ${login_code:-curl error} from $GDB_AUTH_URL" | tee -a "$LOG"
  if [[ -s "$login_body" ]]; then
    cat "$login_body" | tee -a "$LOG"
    echo | tee -a "$LOG"
  fi
  echo "Export GDBUSER and GDBPASS for an account that can log in to this GraphDB, then re-run." | tee -a "$LOG"
  exit 1
fi
# GraphDB returns the scheme with the token (Authorization: GDB <jwt>).
# Send that value back unchanged.
token=${auth_header#*: }
token=${token#"${token%%[![:space:]]*}"}
AUTH_HEADER="Authorization: $token"

done_count=0
fail_count=0

while IFS= read -r file; do
    [[ -n "$file" ]] || continue
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

    if [[ "$http_code" != "200" ]]; then
      body=""
      if [[ -s "$tmp_report" ]]; then
        body=$(head -c 500 "$tmp_report" || true)
      fi
      if [[ -n "${GDBPASS:-}" && -n "$body" ]]; then
        body=${body//"$GDBPASS"/"(password omitted)"}
      fi
      printf '%s\n' "[$(date -Iseconds)] RESPONSE $name (HTTP $http_code): ${body:-"(empty response)"}" | tee -a "$LOG"
    fi

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
done < <(python3 "$SCRIPT_DIR/dashboard_config.py" --list-shacl)

echo "[$(date -Iseconds)] BATCH COMPLETE: $done_count succeeded, $fail_count failed" | tee -a "$LOG"
echo "[$(date -Iseconds)] History written to repository '$HISTORY_REPO', named graph <$HISTORY_GRAPH>" | tee -a "$LOG"

# Retention is applied per file as each run is written, but a batch that was
# interrupted can leave files that were never revisited outside the window.
# Re-applying it over the whole graph is idempotent and takes seconds.
echo "[$(date -Iseconds)] Applying retention window to the whole graph" | tee -a "$LOG"
python3 "$SCRIPT_DIR/gdb_history.py" --auth-header "$AUTH_HEADER" 2>>"$LOG" | tee -a "$LOG"

# The dashboard is rebuilt from the graph, never from files, so this is the
# only generation step: it rewrites index.html and dashboard/ in place.
# SKIP_DASHBOARD_BUILD=1 leaves that to .github/workflows/dashboard.yml, which
# the validation workflow starts after a successful batch.
if [[ "${SKIP_DASHBOARD_BUILD:-}" == "1" ]]; then
  echo "[$(date -Iseconds)] Skipping dashboard build (SKIP_DASHBOARD_BUILD=1)" | tee -a "$LOG"
else
  echo "[$(date -Iseconds)] GENERATING DASHBOARD FROM GRAPHDB" | tee -a "$LOG"
  python3 "$SCRIPT_DIR/build_dashboard_gdb.py" 2>>"$LOG" | tee -a "$LOG"
fi

# Every file was attempted. A non-zero count keeps the workflow from publishing
# the dashboard over a partial or refused batch.
if [[ "$fail_count" -gt 0 ]]; then
  echo "[$(date -Iseconds)] Batch finished with $fail_count failed file(s)" | tee -a "$LOG"
  exit 1
fi
