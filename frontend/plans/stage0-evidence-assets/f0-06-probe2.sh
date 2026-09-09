#!/usr/bin/env bash
# F0-06 bounded refinement probe #2 (same one-shot script family, corrected expectations):
#  - cancel AFTER first streamed delta -> saved_text retained (stage0 §7 step 3)
#  - seed context_version baseline
#  - plan_adjust stale -> recalc -> non-empty draft_vs_draft_diff -> reconfirm
set -u
PORT=5199
BASE="http://127.0.0.1:$PORT"
DEV_LOG=/tmp/f0-06-vite2.log
SSE_LOG=/tmp/f0-06-sse2.log
OUT=/tmp/f0-06-probe2-out.txt
START=$(date +%s); DEADLINE=$((START + 75))
: > "$OUT"; : > "$SSE_LOG"
CURL=(curl -sS --noproxy '*' -m 8)
sse_pid=""

cleanup() {
  [[ -n "$sse_pid" ]] && kill "$sse_pid" 2>/dev/null
  pkill -f "vite --port $PORT" 2>/dev/null
  wait 2>/dev/null
  echo "--- cleanup done at +$(( $(date +%s) - START ))s ---" >> "$OUT"
}
trap cleanup EXIT INT TERM

log() { echo "$*" | tee -a "$OUT"; }
jget() {
  node -e '
let s="";process.stdin.on("data",c=>s+=c).on("end",()=>{
  let o; try { o = JSON.parse(s); } catch { console.log("<PARSE_ERR>"); return; }
  let v=o; for (const k of process.argv[1].split(".")) { if (v==null){v=undefined;break;} v=v[k]; }
  if (v===undefined) return console.log("<absent>");
  console.log(typeof v === "object" ? JSON.stringify(v) : String(v));
});' "$1"
}
http() {
  local m=$1 p=$2 b=${3:-}
  local args=(-o /tmp/f0-06-body2 -w '%{http_code}' -X "$m" "$BASE$p" -H 'Content-Type: application/json')
  [[ -n "$b" ]] && args+=(-d "$b")
  "${CURL[@]}" "${args[@]}"
}
body() { cat /tmp/f0-06-body2; }
PASS=0; FAIL=0; TOTAL=0
check() {
  TOTAL=$((TOTAL+1))
  if [[ "$3" == "$4" ]]; then PASS=$((PASS+1)); log "[$1] PASS  $2 | expected=$3 actual=$4";
  else FAIL=$((FAIL+1)); log "[$1] FAIL  $2 | expected=$3 actual=$4"; fi
}

log "== F0-06 refinement probe (mock only) | $(date -Is) | budget 75s =="
( cd /home/finnian/code/agent/Fit-Agent/frontend && npm run dev -- --port "$PORT" --strictPort --host 127.0.0.1 >"$DEV_LOG" 2>&1 ) &
up=no
for _ in $(seq 1 60); do
  [[ $(date +%s) -gt $DEADLINE ]] && break
  if "${CURL[@]}" -o /dev/null "$BASE/api/provider"; then up=yes; break; fi
  sleep 0.5
done
check R0 "vite dev server reachable on $PORT" yes "$up"
[[ "$up" != yes ]] && { tail -20 "$DEV_LOG" >> "$OUT"; exit 1; }

"${CURL[@]}" -N --max-time 70 "$BASE/api/events" >"$SSE_LOG" 2>/dev/null &
sse_pid=$!
sleep 1

http POST /api/dev/reset '{}' >/dev/null
SEED_CV=$(body | jget context_version)
log "seed context_version (reset baseline) = $SEED_CV  (expected 3 per src/mock/server.ts:363)"
http PUT /api/provider/api-key '{"api_key":"sk-mock-probe-not-a-real-key"}' >/dev/null
SID=$("${CURL[@]}" "$BASE/api/sessions" | jget 0.id)

# ---- cancel AFTER first delta: saved_text must be retained ----
RID=$(http POST /api/runs "{\"session_id\":\"$SID\",\"message\":\"今天卧推 80kg 4组x8 打卡\",\"client_request_id\":\"probe2-cancel-1\"}" | jget run_id)
SAVED=0
for _ in $(seq 1 40); do
  [[ $(date +%s) -gt $DEADLINE ]] && break
  S=$("${CURL[@]}" "$BASE/api/runs/active" | jget run.saved_text)
  if [[ ${#S} -gt 0 && "$S" != "<absent>" ]]; then SAVED=${#S}; break; fi
  sleep 0.25
done
check R1 "first message.delta reached client before cancel (saved_text chars > 0)" true "$([[ $SAVED -gt 0 ]] && echo true || echo "false($SAVED)")"
ST=$("${CURL[@]}" "$BASE/api/runs/active" | jget run.status)
check R2 "run still active when cancelled" true "$([[ "$ST" == pending || "$ST" == running ]] && echo true || echo "false($ST)")"
http POST "/api/runs/$RID/cancel" '{}' >/dev/null
POSTSNAP=$("${CURL[@]}" "$BASE/api/runs/active")
check R3 "after cancel status=cancelled" cancelled "$(echo "$POSTSNAP" | jget run.status)"
SAVED_POST=$(echo "$POSTSNAP" | jget run.saved_text); SAVED_POST=${#SAVED_POST}
check R4 "saved_text retained after cancel (>= chars at cancel)" true "$([[ $SAVED_POST -ge $SAVED && $SAVED_POST -gt 0 ]] && echo true || echo "false(pre=$SAVED,post=$SAVED_POST)")"
check R5 "cancelled run has no draft" 0 "$(echo "$POSTSNAP" | jget run.drafts.length)"

wait_slot_free() {
  for _ in $(seq 1 20); do
    local v; v=$("${CURL[@]}" "$BASE/api/dev/status" | jget execution_slot_run_id)
    [[ "$v" == "null" || "$v" == "<absent>" ]] && return 0
    sleep 0.4
  done
}
run_and_wait_draft() {
  http POST /api/runs "{\"session_id\":\"$SID\",\"message\":\"$2\",\"client_request_id\":\"$1\"}" >/dev/null
  local d
  for _ in $(seq 1 40); do
    [[ $(date +%s) -gt $DEADLINE ]] && break
    d=$("${CURL[@]}" "$BASE/api/runs/active")
    [[ "$(echo "$d" | jget run.status)" == completed ]] && break
    sleep 0.4
  done
  echo "$d" | node -e 'let s="";process.stdin.on("data",c=>s+=c).on("end",()=>{const o=JSON.parse(s);console.log(JSON.stringify(o.run.drafts[0]??null))})'
}

# ---- plan_adjust stale -> recalc (non-empty diff) -> reconfirm ----
wait_slot_free
DP=$(run_and_wait_draft "probe2-plan-1" "最近很累，帮我调整计划")
DPID=$(echo "$DP" | jget id); DPKIND=$(echo "$DP" | jget kind); DPBASE=$(echo "$DP" | jget base_business_version)
check R6 "plan_adjust draft proposed" plan_adjust "$DPKIND"
wait_slot_free
DR=$(run_and_wait_draft "probe2-bump-1" "今天划船 60kg 4组x10 打卡")
DRID=$(echo "$DR" | jget id)
http POST "/api/drafts/$DRID/confirm" '{"revision":1}' >/dev/null
CV2=$(body | jget context_version)
check R7 "context_version advanced past plan draft base" true "$([[ "$CV2" -gt "$DPBASE" ]] && echo true || echo "false(base=$DPBASE,now=$CV2)")"
http POST "/api/drafts/$DPID/confirm" '{"revision":1}' >/dev/null
check R8 "confirm stale plan draft -> draft_stale" draft_stale "$(body | jget error_code)"
http POST "/api/drafts/$DPID/recalc" '{}' >/dev/null
NID=$(body | jget new_draft.id); DIFF=$(body | jget draft_vs_draft_diff)
check R9 "recalc -> new plan draft revision 1" 1 "$(body | jget new_draft.revision)"
check R10 "recalc old draft stale" stale "$(body | jget old_draft.status)"
check R11 "recalc draft_vs_draft_diff non-empty (plan_adjust)" true "$([[ "$DIFF" != "[]" && "$DIFF" != "<absent>" ]] && echo true || echo "false($DIFF)")"
log "draft_vs_draft_diff = $DIFF"
http POST "/api/drafts/$NID/confirm" '{"revision":1}' >/dev/null
check R12 "confirm recalculated plan draft -> committed" committed "$(body | jget status)"

# ---- dev-only failure injection (used by stage0 §7 step 4; owner manual walkthrough) ----
wait_slot_free
RID2=$(http POST /api/runs "{\"session_id\":\"$SID\",\"message\":\"今天深蹲 100kg 3组x5 打卡\",\"client_request_id\":\"probe2-fail-1\"}" | jget run_id)
sleep 1.4
http POST /api/dev/restart '{}' >/dev/null
check R13 "dev restart injection -> affected run failed/interrupted_by_restart" true "$(body | node -e 'let s="";process.stdin.on("data",c=>s+=c).on("end",()=>{const o=JSON.parse(s);const a=(o.affected||[])[0]||{};console.log(a.status==="failed"&&a.error_code==="interrupted_by_restart"?true:JSON.stringify(a))})')"
ACT=$("${CURL[@]}" "$BASE/api/runs/active")
check R14 "runs/active surfaces failed + error_code + saved_text kept" true "$(echo "$ACT" | node -e 'let s="";process.stdin.on("data",c=>s+=c).on("end",()=>{const r=JSON.parse(s).run;console.log(r.status==="failed"&&r.error_code==="interrupted_by_restart"&&typeof r.saved_text==="string"?true:JSON.stringify({s:r.status,e:r.error_code,c:r.saved_text.length}))})')"

# ---- SSE suspend (used by stage0 §7 step 6) ----
http POST /api/dev/events/suspend '{"seconds":50,"heartbeat":false}' >/dev/null
check R15 "dev suspend -> business events dropped, heartbeat suppressed" "完全静默（无业务事件、无 heartbeat）：前端连续 45s 无事件即关闭连接转 GET /api/runs/active 轮询（08 8.7）" "$(body | jget client_effect)"
http POST /api/dev/events/resume '{}' >/dev/null
check R16 "dev resume -> no replay of dropped events" "不补发挂起期间丢弃的事件（08 8.7 无重放）；当前状态经业务接口查询" "$(body | jget note)"

sleep 1; kill "$sse_pid" 2>/dev/null; sse_pid=""
log "-- SSE event names/counts --"
grep -a '^event: ' "$SSE_LOG" | sort | uniq -c | tee -a "$OUT"
log "== RESULT: PASS=$PASS FAIL=$FAIL TOTAL=$TOTAL | elapsed=$(( $(date +%s) - START ))s | deadline_ok=$([[ $(date +%s) -le $DEADLINE ]] && echo yes || echo NO) =="
exit 0
