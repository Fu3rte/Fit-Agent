#!/usr/bin/env bash
# F0-06 bounded runtime probe against the mock (frontend/plans/stage0.md sections 6-8).
# One-shot: starts vite dev on 5199, exercises contract endpoints + dev controls, prints PASS/FAIL rows.
set -u
PORT=5199
BASE="http://127.0.0.1:$PORT"
DEV_LOG=/tmp/f0-06-vite.log
SSE_LOG=/tmp/f0-06-sse.log
OUT=/tmp/f0-06-probe-out.txt
START=$(date +%s)
DEADLINE=$((START + 90))
: > "$SSE_LOG"; : > "$OUT"

CURL=(curl -sS --noproxy '*' -m 8)
dev_pid=""; sse_pid=""

cleanup() {
  [[ -n "$sse_pid" ]] && kill "$sse_pid" 2>/dev/null
  [[ -n "$dev_pid" ]] && kill "$dev_pid" 2>/dev/null
  wait 2>/dev/null
  echo "--- cleanup: vite(pid=${dev_pid:-none}) sse curl(pid=${sse_pid:-none}) stopped at +$(( $(date +%s) - START ))s ---" >> "$OUT"
}
trap cleanup EXIT INT TERM

log() { echo "$*" | tee -a "$OUT"; }

# json field getter: jget <dotted.path> reads JSON on stdin
jget() {
  node -e '
let s="";process.stdin.on("data",c=>s+=c).on("end",()=>{
  let o; try { o = JSON.parse(s); } catch { console.log("<PARSE_ERR>"); return; }
  let v=o; for (const k of process.argv[1].split(".")) { if (v==null){v=undefined;break;} v=v[k]; }
  if (v===undefined) return console.log("<absent>");
  console.log(typeof v === "object" ? JSON.stringify(v) : String(v));
});' "$1"
}

# http <method> <path> [json-body] -> writes body to /tmp/f0-06-body, echoes "HTTP_CODE"
http() {
  local m=$1 p=$2 b=${3:-}
  local args=(-o /tmp/f0-06-body -w '%{http_code}' -X "$m" "$BASE$p" -H 'Content-Type: application/json')
  [[ -n "$b" ]] && args+=(-d "$b")
  "${CURL[@]}" "${args[@]}"
}
body() { cat /tmp/f0-06-body; }

PASS=0; FAIL=0; TOTAL=0
# check <step-id> <description> <expected> <actual> [--eq]
check() {
  local id=$1 desc=$2 exp=$3 act=$4
  TOTAL=$((TOTAL+1))
  if [[ "$exp" == "$act" ]]; then PASS=$((PASS+1)); log "[$id] PASS  $desc | expected=$exp actual=$act";
  else FAIL=$((FAIL+1)); log "[$id] FAIL  $desc | expected=$exp actual=$act"; fi
}

log "== F0-06 runtime probe (mock only; no real backend) | start $(date -Is) | budget 90s =="

# ---------- 0. dev server ----------
( cd /home/finnian/code/agent/Fit-Agent/frontend && npm run dev -- --port "$PORT" --strictPort --host 127.0.0.1 >"$DEV_LOG" 2>&1 ) &
dev_pid=$!
up=no
for _ in $(seq 1 60); do
  [[ $(date +%s) -gt $DEADLINE ]] && break
  if "${CURL[@]}" -o /dev/null "$BASE/api/provider"; then up=yes; break; fi
  sleep 0.5
done
check P0 "vite dev server reachable on $PORT (VITE_PORT not read by vite.config.ts; --port used)" yes "$up"
if [[ "$up" != yes ]]; then log "-- vite log --"; tail -20 "$DEV_LOG" >> "$OUT"; exit 1; fi

# SSE observer (bounded; killed in cleanup)
"${CURL[@]}" -N --max-time 80 "$BASE/api/events" >"$SSE_LOG" 2>/dev/null &
sse_pid=$!
sleep 1

# ---------- 1. reset seed + api key ----------
http POST /api/dev/reset '{}' >/dev/null
check P1a "dev reset ok" true "$(body | jget ok)"
check P1b "dev reset context_version" 0 "$(body | jget context_version)"
check P1c "dev reset drafts cleared" 0 "$(body | jget drafts)"
http PUT /api/provider/api-key '{"api_key":"sk-mock-probe-not-a-real-key"}' >/dev/null
check P1d "api key set (has_api_key only, no plaintext)" true "$(body | jget has_api_key)"
SID=$("${CURL[@]}" "$BASE/api/sessions" | jget 0.id)
check P1e "seed session available" true "$([[ -n "$SID" && "$SID" != "<absent>" ]] && echo true || echo false)"

# ---------- 2. create run: pending -> running -> completed ----------
R1=$(http POST /api/runs "{\"session_id\":\"$SID\",\"message\":\"今天卧推 80kg 4组x8 打卡\",\"client_request_id\":\"probe-cancel-1\"}")
R1ID=$(body | jget run_id)
check P2a "POST /api/runs -> 200" 200 "$R1"
check P2b "run created (run_id)" true "$([[ -n "$R1ID" && "$R1ID" != "<absent>" ]] && echo true || echo false)"
SNAP=$("${CURL[@]}" "$BASE/api/runs/active")
check P2c "runs/active snapshot status is pending|running" true "$(S=$(echo "$SNAP" | jget run.status); [[ "$S" == pending || "$S" == running ]] && echo true || echo "false($S)")"
check P2d "runs/active snapshot carries saved_text field" true "$(echo "$SNAP" | jget run.saved_text >/dev/null; echo "$([[ $(echo "$SNAP" | jget run.saved_text) != '<absent>' ]] && echo true || echo false)")"

# ---------- 3. busy 409 (global single run slot) ----------
B=$(http POST /api/runs "{\"session_id\":\"$SID\",\"message\":\"第二条并发消息\",\"client_request_id\":\"probe-busy-1\"}")
check P3a "second run while active -> 409" 409 "$B"
check P3b "busy error_code" conversation_busy "$(body | jget error_code)"

# ---------- 4. mid-stream cancel keeps saved_text ----------
sleep 1.3
PRE=$("${CURL[@]}" "$BASE/api/runs/active")
SAVED_PRE=$(echo "$PRE" | jget run.saved_text)
check P4a "mid-stream saved_text non-empty before cancel" true "$([[ ${#SAVED_PRE} -gt 0 && "$SAVED_PRE" != "<absent>" ]] && echo true || echo "false($SAVED_PRE)")"
C=$(http POST "/api/runs/$R1ID/cancel" '{}')
check P4b "cancel -> 200 status cancelled" cancelled "$(body | jget status)"
POST=$("${CURL[@]}" "$BASE/api/runs/active")
check P4c "after cancel status=cancelled" cancelled "$(echo "$POST" | jget run.status)"
SAVED_POST=$(echo "$POST" | jget run.saved_text)
check P4d "saved_text retained after cancel" true "$([[ ${#SAVED_POST} -ge ${#SAVED_PRE} && ${#SAVED_POST} -gt 0 ]] && echo true || echo "false(pre=${#SAVED_PRE},post=${#SAVED_POST})")"
check P4e "cancelled run proposed no draft (mid-stream)" 0 "$(echo "$POST" | jget run.drafts.length)"

# wait for execution slot release (08 8.3)
for _ in $(seq 1 20); do
  SLOT=$("${CURL[@]}" "$BASE/api/dev/status" | jget execution_slot_run_id)
  [[ "$SLOT" == "<absent>" ]] && break
  sleep 0.5
done
check P4f "execution slot released after cancel" "<absent>" "$SLOT"

# ---------- helpers for draft runs ----------
run_and_wait_draft() { # $1 client_request_id  $2 message -> echoes draft json
  local cid=$1 msg=$2 rid d
  http POST /api/runs "{\"session_id\":\"$SID\",\"message\":\"$msg\",\"client_request_id\":\"$cid\"}" >/dev/null
  rid=$(body | jget run_id)
  for _ in $(seq 1 40); do
    [[ $(date +%s) -gt $DEADLINE ]] && break
    d=$("${CURL[@]}" "$BASE/api/runs/active")
    [[ "$(echo "$d" | jget run.status)" == completed ]] && break
    sleep 0.4
  done
  echo "$d" | node -e 'let s="";process.stdin.on("data",c=>s+=c).on("end",()=>{const o=JSON.parse(s);console.log(JSON.stringify(o.run.drafts[0]??null))})'
}
wait_slot_free() {
  for _ in $(seq 1 20); do
    [[ $("${CURL[@]}" "$BASE/api/dev/status" | jget execution_slot_run_id) == "<absent>" ]] && return 0
    sleep 0.4
  done
}

# ---------- 5. discard -> confirm rejected ----------
wait_slot_free
DA=$(run_and_wait_draft "probe-discard-1" "今天硬拉 120kg 3组x5 打卡")
DAID=$(echo "$DA" | jget id)
check P5a "run completed and draft.proposed (draft id)" true "$([[ "$DAID" != "<absent>" && "$DAID" != "null" ]] && echo true || echo false)"
http POST "/api/drafts/$DAID/discard" '{}' >/dev/null
check P5b "discard -> status discarded" discarded "$(body | jget status)"
D=$(http POST "/api/drafts/$DAID/confirm" '{"revision":1}')
check P5c "confirm discarded draft -> 409" 409 "$D"
check P5d "rejected with invalid_request (01 1.3 已丢弃拒绝)" invalid_request "$(body | jget error_code)"

# ---------- 6. revise revision+1 -> wrong revision -> right revision -> idempotent ----------
wait_slot_free
DB=$(run_and_wait_draft "probe-revise-1" "今天卧推 85kg 5组x5 打卡")
DBID=$(echo "$DB" | jget id)
REV_BODY=$(echo "$DB" | node -e 'let s="";process.stdin.on("data",c=>s+=c).on("end",()=>{const d=JSON.parse(s);const p={...d.payload};p.exercise="杠铃卧推（已纠错）";p.warmup_summary="递增至 65kg";console.log(JSON.stringify({payload:p}))})')
http POST "/api/drafts/$DBID/revise" "$REV_BODY" >/dev/null
check P6a "revise -> revision 2" 2 "$(body | jget draft.revision)"
check P6b "revise returns updated diff" true "$([[ $(body | jget draft.diff) != "<absent>" ]] && echo true || echo false)"
W=$(http POST "/api/drafts/$DBID/confirm" '{"revision":1}')
check P6c "confirm with stale-seen revision -> 409" 409 "$W"
check P6d "error_code draft_modified (01 1.4 修订检查)" draft_modified "$(body | jget error_code)"
OK=$(http POST "/api/drafts/$DBID/confirm" '{"revision":2}')
CV1=$(body | jget context_version); SUM1=$(body | jget summary); NC1=$(body | jget newly_committed)
check P6e "confirm with seen revision -> committed" committed "$(body | jget status)"
check P6f "first commit newly_committed=true" true "$NC1"
IDEM=$(http POST "/api/drafts/$DBID/confirm" '{"revision":2}')
check P6g "repeat confirm -> newly_committed=false" false "$(body | jget newly_committed)"
check P6h "repeat confirm returns original context_version" "$CV1" "$(body | jget context_version)"
check P6i "repeat confirm returns original summary" "$SUM1" "$(body | jget summary)"

# ---------- 7. stale -> recalc -> reconfirm ----------
wait_slot_free
DD=$(run_and_wait_draft "probe-stale-1" "今天划船 60kg 4组x10 打卡")
DDID=$(echo "$DD" | jget id); DDBASE=$(echo "$DD" | jget base_business_version)
wait_slot_free
DE=$(run_and_wait_draft "probe-bump-1" "今天深蹲 100kg 3组x5 打卡")
DEID=$(echo "$DE" | jget id)
http POST "/api/drafts/$DEID/confirm" '{"revision":1}' >/dev/null
CV2=$(body | jget context_version)
check P7a "context_version advanced by other confirm" true "$([[ "$CV2" -gt "$CV1" ]] && echo true || echo "false($CV1->$CV2)")"
ST=$(http POST "/api/drafts/$DDID/confirm" '{"revision":1}')
check P7b "confirm stale draft -> 409" 409 "$ST"
check P7c "error_code draft_stale (01 1.6 基线冲突)" draft_stale "$(body | jget error_code)"
RC=$(http POST "/api/drafts/$DDID/recalc" '{}')
NEWID=$(body | jget new_draft.id)
check P7d "recalc -> new draft revision 1" 1 "$(body | jget new_draft.revision)"
check P7e "recalc new draft base = current context_version" "$CV2" "$(body | jget new_draft.base_business_version)"
check P7f "recalc old draft marked stale" stale "$(body | jget old_draft.status)"
check P7g "recalc returns draft_vs_draft_diff" true "$([[ $(body | jget draft_vs_draft_diff) != "<absent>" && $(body | jget draft_vs_draft_diff) != "[]" ]] && echo true || echo "false($(body | jget draft_vs_draft_diff)")"
RC2=$(http POST "/api/drafts/$NEWID/confirm" '{"revision":1}')
check P7h "confirm recalculated draft -> committed" committed "$(body | jget status)"

# ---------- 8. session draft status query ----------
wait_slot_free
DR=$("${CURL[@]}" "$BASE/api/sessions/$SID/drafts")
check P8a "GET /api/sessions/:id/drafts returns array" true "$(echo "$DR" | node -e 'let s="";process.stdin.on("data",c=>s+=c).on("end",()=>{const a=JSON.parse(s);console.log(Array.isArray(a)?true:false)})')"
check P8b "session draft statuses include discarded+committed+stale" true "$(echo "$DR" | node -e 'let s="";process.stdin.on("data",c=>s+=c).on("end",()=>{const a=JSON.parse(s).map(d=>d.status);const ok=["discarded","committed","stale"].every(x=>a.includes(x));console.log(ok?true:JSON.stringify(a))})')"

# ---------- 9. dev status snapshot + SSE event evidence ----------
DS=$("${CURL[@]}" "$BASE/api/dev/status")
check P9a "dev status lists runs with saved_text_chars" true "$(echo "$DS" | jget runs.0.saved_text_chars >/dev/null; echo "$([[ $(echo "$DS" | jget runs.0.saved_text_chars) != '<absent>' ]] && echo true || echo false)")"
sleep 1
kill "$sse_pid" 2>/dev/null; sse_pid=""
log "-- SSE observed event names/counts (curl -N GET /api/events, single client) --"
grep -a '^event: ' "$SSE_LOG" | sort | uniq -c | tee -a "$OUT"
log "-- SSE sample lines (first 12) --"
head -12 "$SSE_LOG" | tee -a "$OUT"
check P9b "SSE stream delivered run.started/delta/draft.proposed/run.completed" true "$(grep -aq 'event: run.started' "$SSE_LOG" && grep -aq 'event: message.delta' "$SSE_LOG" && grep -aq 'event: draft.proposed' "$SSE_LOG" && grep -aq 'event: run.completed' "$SSE_LOG" && echo true || echo false)"

# ---------- summary ----------
log "== RESULT: PASS=$PASS FAIL=$FAIL TOTAL=$TOTAL | elapsed=$(( $(date +%s) - START ))s | deadline_ok=$([[ $(date +%s) -le $DEADLINE ]] && echo yes || echo NO) =="
exit 0
