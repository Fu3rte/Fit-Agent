#!/usr/bin/env bash
# F0-06 bounded probe #3: cancel mid-stream, with corrected run_id extraction (probe#2 bug:
# RID was taken from the HTTP status code stream instead of the response body).
set -u
PORT=5199; BASE="http://127.0.0.1:$PORT"
OUT=/tmp/f0-06-probe3-out.txt; DEV_LOG=/tmp/f0-06-vite3.log; SSE_LOG=/tmp/f0-06-sse3.log
START=$(date +%s); DEADLINE=$((START + 60))
: > "$OUT"; : > "$SSE_LOG"
CURL=(curl -sS --noproxy '*' -m 8)
sse_pid=""
cleanup() { [[ -n "$sse_pid" ]] && kill "$sse_pid" 2>/dev/null; pkill -f "vite --port $PORT" 2>/dev/null; wait 2>/dev/null; echo "--- cleanup at +$(( $(date +%s) - START ))s ---" >> "$OUT"; }
trap cleanup EXIT INT TERM
log() { echo "$*" | tee -a "$OUT"; }
jget() { node -e '
let s="";process.stdin.on("data",c=>s+=c).on("end",()=>{
  let o; try { o = JSON.parse(s); } catch { console.log("<PARSE_ERR>"); return; }
  let v=o; for (const k of process.argv[1].split(".")) { if (v==null){v=undefined;break;} v=v[k]; }
  if (v===undefined) return console.log("<absent>");
  console.log(typeof v === "object" ? JSON.stringify(v) : String(v));
});' "$1"; }
http() { local m=$1 p=$2 b=${3:-}; local a=(-o /tmp/f0-06-body3 -w '%{http_code}' -X "$m" "$BASE$p" -H 'Content-Type: application/json'); [[ -n "$b" ]] && a+=(-d "$b"); "${CURL[@]}" "${a[@]}"; }
body() { cat /tmp/f0-06-body3; }
PASS=0; FAIL=0; TOTAL=0
check() { TOTAL=$((TOTAL+1)); if [[ "$3" == "$4" ]]; then PASS=$((PASS+1)); log "[$1] PASS  $2 | expected=$3 actual=$4"; else FAIL=$((FAIL+1)); log "[$1] FAIL  $2 | expected=$3 actual=$4"; fi; }

log "== F0-06 probe #3 (cancel mid-stream) | $(date -Is) | budget 60s =="
( cd /home/finnian/code/agent/Fit-Agent/frontend && npm run dev -- --port "$PORT" --strictPort --host 127.0.0.1 >"$DEV_LOG" 2>&1 ) &
up=no
for _ in $(seq 1 60); do [[ $(date +%s) -gt $DEADLINE ]] && break; "${CURL[@]}" -o /dev/null "$BASE/api/provider" && { up=yes; break; }; sleep 0.5; done
check C0 "vite dev server reachable on $PORT" yes "$up"
[[ "$up" != yes ]] && { tail -20 "$DEV_LOG" >> "$OUT"; exit 1; }
"${CURL[@]}" -N --max-time 55 "$BASE/api/events" >"$SSE_LOG" 2>/dev/null &
sse_pid=$!
sleep 1
http POST /api/dev/reset '{}' >/dev/null
http PUT /api/provider/api-key '{"api_key":"sk-mock-probe-not-a-real-key"}' >/dev/null
SID=$("${CURL[@]}" "$BASE/api/sessions" | jget 0.id)

http POST /api/runs "{\"session_id\":\"$SID\",\"message\":\"今天卧推 80kg 4组x8 打卡\",\"client_request_id\":\"probe3-cancel-1\"}" >/dev/null
RID=$(body | jget run_id)
check C1 "run_id parsed from body" true "$([[ -n "$RID" && "$RID" != "<absent>" ]] && echo true || echo false)"
SAVED=0
for _ in $(seq 1 40); do [[ $(date +%s) -gt $DEADLINE ]] && break; S=$("${CURL[@]}" "$BASE/api/runs/active" | jget run.saved_text); if [[ ${#S} -gt 0 && "$S" != "<absent>" ]]; then SAVED=${#S}; break; fi; sleep 0.25; done
check C2 "streamed text present before cancel (chars>0)" true "$([[ $SAVED -gt 0 ]] && echo true || echo "false($SAVED)")"
check C3 "run active before cancel" true "$(S=$("${CURL[@]}" "$BASE/api/runs/active" | jget run.status); [[ "$S" == running || "$S" == pending ]] && echo true || echo "false($S)")"
http POST "/api/runs/$RID/cancel" '{}' >/dev/null
check C4 "cancel response status=cancelled" cancelled "$(body | jget status)"
POSTSNAP=$("${CURL[@]}" "$BASE/api/runs/active")
check C5 "runs/active status=cancelled after cancel" cancelled "$(echo "$POSTSNAP" | jget run.status)"
SAVED_POST=$(echo "$POSTSNAP" | jget run.saved_text); SAVED_POST_LEN=${#SAVED_POST}
check C6 "saved_text retained after cancel (>= chars at cancel, >0)" true "$([[ $SAVED_POST_LEN -ge $SAVED && $SAVED_POST_LEN -gt 0 ]] && echo true || echo "false(pre=$SAVED,post=$SAVED_POST_LEN)")"
check C7 "no draft on cancelled run (mid-stream)" 0 "$(echo "$POSTSNAP" | jget run.drafts.length)"
check C8 "error_code absent on cancelled run" "<absent>" "$(echo "$POSTSNAP" | jget run.error_code)"
sleep 1
check C9 "SSE delivered run.cancelled" true "$(grep -aq 'event: run.cancelled' "$SSE_LOG" && echo true || echo false)"
log "-- saved_text tail --"; echo "$POSTSNAP" | jget run.saved_text | tail -c 200 | tee -a "$OUT"
kill "$sse_pid" 2>/dev/null; sse_pid=""
log "== RESULT: PASS=$PASS FAIL=$FAIL TOTAL=$TOTAL | elapsed=$(( $(date +%s) - START ))s =="
exit 0
