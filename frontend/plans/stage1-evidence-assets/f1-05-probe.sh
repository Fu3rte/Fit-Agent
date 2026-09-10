#!/usr/bin/env bash
# F1-05 bounded mock HTTP probe (Stage 1 建档闭环, plans/stage1.md §7 protocol-level steps).
# Dedicated port 5199 --strictPort; only the exact vite PID started here is killed.
# Does NOT touch the user's 5173 dev server. No SSE (not required by F1-05 task list).
set -u
PORT=5199
BASE="http://127.0.0.1:$PORT"
FE=/home/finnian/code/agent/Fit-Agent/frontend
OUT=/tmp/f1-05-probe-out.txt
DEV_LOG=/tmp/f1-05-vite.log
BODYF=/tmp/f1-05-body.json
START=$(date +%s)
DEADLINE=$((START + 180))
CURL=(curl -sS --noproxy '*' -m 8)
VITE_PID=""
: > "$OUT"; : > "$DEV_LOG"

cleanup() {
  if [[ -n "$VITE_PID" ]]; then
    kill "$VITE_PID" 2>/dev/null
    wait "$VITE_PID" 2>/dev/null
  fi
  echo "--- cleanup at +$(( $(date +%s) - START ))s (killed only PID ${VITE_PID:-none}; 5173 untouched) ---" >> "$OUT"
}
trap cleanup EXIT INT TERM

log() { echo "$*" | tee -a "$OUT"; }

jget() { node -e '
let s="";process.stdin.on("data",c=>s+=c).on("end",()=>{
  let o; try { o = JSON.parse(s); } catch { console.log("<PARSE_ERR>"); return; }
  let v=o; for (const k of process.argv[1].split(".")) { if (v==null){v=undefined;break;} v=v[k]; }
  if (v===undefined) return console.log("<absent>");
  console.log(typeof v === "object" ? JSON.stringify(v) : String(v));
});' "$1"; }

jlast() { node -e '
let s="";process.stdin.on("data",c=>s+=c).on("end",()=>{
  let a; try { a = JSON.parse(s); } catch { console.log("<PARSE_ERR>"); return; }
  console.log(Array.isArray(a) && a.length ? String(a[a.length-1].content ?? "") : "<empty>");
});'; }

HTTP_CODE=""
http() { # method path [body] -> HTTP_CODE + body in $BODYF
  local m=$1 p=$2 b=${3:-}
  local a=(-o "$BODYF" -w '%{http_code}' -X "$m" "$BASE$p" -H 'Content-Type: application/json')
  [[ -n "$b" ]] && a+=(-d "$b")
  HTTP_CODE=$("${CURL[@]}" "${a[@]}")
  printf '%s' "$HTTP_CODE"
}
body() { cat "$BODYF"; }
has() { [[ "$1" == *"$2"* ]] && echo true || echo false; }

PASS=0; FAIL=0; TOTAL=0
check() { # id desc expected actual
  TOTAL=$((TOTAL+1))
  if [[ "$3" == "$4" ]]; then PASS=$((PASS+1)); log "[$1] PASS  $2 | expected=$3 actual=$4"
  else FAIL=$((FAIL+1)); log "[$1] FAIL  $2 | expected=$3 actual=$4"; fi
}

wait_done() { # $1 label, $2 max polls -> sets LAST_RUN
  local label=$1 max=${2:-60} i=0 st slot
  while [[ $i -lt $max ]]; do
    [[ $(date +%s) -gt $DEADLINE ]] && { log "[$label] DEADLINE hit while waiting"; return 1; }
    LAST_RUN=$("${CURL[@]}" "$BASE/api/runs/active")
    st=$(echo "$LAST_RUN" | jget run.status)
    slot=$("${CURL[@]}" "$BASE/api/dev/status" | jget execution_slot_run_id)
    if [[ "$st" == completed || "$st" == cancelled || "$st" == failed ]] && [[ "$slot" == null || "$slot" == "<absent>" ]]; then return 0; fi
    sleep 0.4; i=$((i+1))
  done
  log "[$label] wait_done TIMEOUT (status=$st slot=$slot)"; return 1
}

send_run() { # $1 client_request_id, $2 session_id, $3 message -> body has run_id
  node -e 'const [sid,msg,cid]=process.argv.slice(1);console.log(JSON.stringify({session_id:sid,message:msg,client_request_id:cid}))' "$2" "$3" "$1"
}

log "== F1-05 probe (Stage 1 建档闭环, protocol level) | $(date -Is) | port=$PORT | budget=180s =="

cd "$FE"
./node_modules/.bin/vite --port "$PORT" --strictPort --host 127.0.0.1 >"$DEV_LOG" 2>&1 &
VITE_PID=$!
log "-- started vite PID=$VITE_PID on $PORT (user 5173 untouched) --"

up=no
for _ in $(seq 1 60); do
  [[ $(date +%s) -gt $DEADLINE ]] && break
  "${CURL[@]}" -o /dev/null "$BASE/api/provider" && { up=yes; break; }
  sleep 0.5
done
check C0 "vite dev server reachable on dedicated port $PORT" yes "$up"
[[ "$up" != yes ]] && { tail -20 "$DEV_LOG" >> "$OUT"; exit 1; }

### A. empty reset + empty-state queries
http POST /api/dev/reset '{"seed":"empty"}' >/dev/null
check A1 "reset ok" true "$(body | jget ok)"
check A2 "reset seed=empty" empty "$(body | jget seed)"
check A3 "reset has_profile=false" false "$(body | jget has_profile)"
check A4 "reset drafts=0" 0 "$(body | jget drafts)"
check A5 "empty seed context_version=0" 0 "$(body | jget context_version)"
http GET /api/profile >/dev/null
check A6 "GET /api/profile profile=null (未建档)" null "$(body | jget profile)"
check A7 "GET /api/profile restrictions=[]" 0 "$(body | jget restrictions.length)"
check A8 "GET /api/profile context_version=0" 0 "$(body | jget context_version)"
http GET /api/records >/dev/null
check A9 "GET /api/records empty" 0 "$(body | jget records.length)"
http GET /api/sessions >/dev/null
check A10 "GET /api/sessions empty (bare array)" 0 "$(body | jget length)"

### B. key setup + session
http PUT /api/provider/api-key '{"api_key":"sk-mock-f1-05-not-a-real-key"}' >/dev/null
check B1 "PUT api-key -> has_api_key=true (no plaintext)" true "$(body | jget has_api_key)"
http POST /api/sessions '{"title":"F1-05 建档闭环探针"}' >/dev/null
SID=$(body | jget id)
check B2 "POST /api/sessions returns id" true "$([[ -n "$SID" && "$SID" != "<absent>" ]] && echo true || echo false)"
http GET /api/sessions >/dev/null
check B3 "sessions count=1" 1 "$(body | jget length)"

### T1. multi-turn missing question (only goal given)
S=$(send_run f1-05-t1 "$SID" "我想增肌")
http POST /api/runs "$S" >/dev/null
check T1a "POST /api/runs accepted (200)" 200 "$HTTP_CODE"
wait_done T1 60 || true
check T1b "run completed" completed "$(echo "$LAST_RUN" | jget run.status)"
check T1c "no draft generated while facts missing" 0 "$(echo "$LAST_RUN" | jget run.drafts.length)"
http GET "/api/sessions/$SID/messages" >/dev/null
REPLY1=$(body | jlast)
check T1d "reply asks missing fact (训练经验)" true "$(has "$REPLY1" "训练经验")"
check T1e "reply does not claim a draft" false "$(has "$REPLY1" "生成 1 份档案草稿")"
check T1f "reply does not fabricate default experience" false "$(has "$REPLY1" "初级")"
http GET /api/profile >/dev/null
check T1g "profile still null" null "$(body | jget profile)"

### T2. explicit red flag: professional evaluation, no training advice, no draft
S=$(send_run f1-05-t2 "$SID" "另外我最近胸部异常不适")
http POST /api/runs "$S" >/dev/null
wait_done T2 60 || true
check T2a "run completed" completed "$(echo "$LAST_RUN" | jget run.status)"
check T2b "no draft on red-flag turn" 0 "$(echo "$LAST_RUN" | jget run.drafts.length)"
http GET "/api/sessions/$SID/messages" >/dev/null
REPLY2=$(body | jlast)
check T2c "reply recommends offline professional evaluation" true "$(has "$REPLY2" "线下")"
check T2d "reply states evaluation/assessment" true "$(has "$REPLY2" "评估")"
check T2e "reply records red flag into profile content" true "$(has "$REPLY2" "红旗症状")"
check T2f "reply contains no training prescription markers" false "$([[ "$REPLY2" == *组数* || "$REPLY2" == *RIR* || "$REPLY2" == *RPE* || "$REPLY2" == *计划调整* || "$REPLY2" == *建议你* ]] && echo true || echo false)"
http GET /api/dev/status >/dev/null
check T2g "dev/status drafts=0 after red-flag turn" 0 "$(body | jget drafts.length)"
check T2h "profile still null after red-flag turn" false "$(body | jget has_profile)"

### T3. complete facts -> exactly one structured profile draft
S=$(send_run f1-05-t3 "$SID" "我有一点基础，每周练3次，每次60分钟，有杠铃和哑铃，体重75公斤，颈后推举肩部不适")
http POST /api/runs "$S" >/dev/null
wait_done T3 60 || true
check T3a "run completed" completed "$(echo "$LAST_RUN" | jget run.status)"
check T3b "exactly one draft" 1 "$(echo "$LAST_RUN" | jget run.drafts.length)"
check T3c "draft kind=profile_update" profile_update "$(echo "$LAST_RUN" | jget run.drafts.0.kind)"
check T3d "draft status=pending" pending "$(echo "$LAST_RUN" | jget run.drafts.0.status)"
check T3e "draft revision=1" 1 "$(echo "$LAST_RUN" | jget run.drafts.0.revision)"
check T3f "draft base_business_version=0 (empty seed)" 0 "$(echo "$LAST_RUN" | jget run.drafts.0.base_business_version)"
check T3g "payload goal" "增肌（肌肥大）" "$(echo "$LAST_RUN" | jget run.drafts.0.payload.profile.goal)"
check T3h "payload experience" "初级（有少量训练经验）" "$(echo "$LAST_RUN" | jget run.drafts.0.payload.profile.experience)"
check T3i "payload weekly_frequency=3" 3 "$(echo "$LAST_RUN" | jget run.drafts.0.payload.profile.weekly_frequency)"
check T3j "payload session_minutes=60" 60 "$(echo "$LAST_RUN" | jget run.drafts.0.payload.profile.session_minutes)"
check T3k "payload body_weight_kg=75 (required field)" 75 "$(echo "$LAST_RUN" | jget run.drafts.0.payload.profile.body_weight_kg)"
check T3l "payload equipment collected" true "$(echo "$LAST_RUN" | jget run.drafts.0.payload.profile.equipment | grep -q 杠铃 && echo true || echo false)"
check T3m "payload red flag recorded in profile draft" true "$(echo "$LAST_RUN" | jget run.drafts.0.payload.profile.physical_state.red_flags | grep -q 胸部异常不适 && echo true || echo false)"
check T3n "payload restrictions count=1" 1 "$(echo "$LAST_RUN" | jget run.drafts.0.payload.restrictions.length)"
check T3o "restriction scope=specific_action" specific_action "$(echo "$LAST_RUN" | jget run.drafts.0.payload.restrictions.0.scope)"
DID=$(echo "$LAST_RUN" | jget run.drafts.0.id)
DRAFT0=$(echo "$LAST_RUN" | jget run.drafts.0)
check T3p "server-derived diff non-empty (new profile)" true "$([[ $(echo "$DRAFT0" | jget diff.length) -gt 0 ]] && echo true || echo false)"
check T3q "diff carries weight field row" true "$(has "$DRAFT0" "档案 · 体重")"
check T3r "diff carries restriction row" true "$(has "$DRAFT0" "档案 · 当前有效限制")"

### F. pre-confirm: formal profile unchanged
http GET /api/profile >/dev/null
check F1 "pre-confirm profile still null" null "$(body | jget profile)"
check F2 "pre-confirm restrictions still empty" 0 "$(body | jget restrictions.length)"
check F3 "pre-confirm context_version still 0" 0 "$(body | jget context_version)"

### R. inline revise -> revision+1 + derived diff
REVISE_PAYLOAD='{"profile":{"goal":"增肌（肌肥大）","experience":"初级（有少量训练经验）","weekly_frequency":4,"session_minutes":60,"equipment":["杠铃","哑铃","卧推架"],"body_weight_kg":75,"physical_state":{"red_flags":["胸部异常不适"],"notes":[]}},"restrictions":[{"name":"杠铃颈后推举","scope":"specific_action","note":"肩部不适"},{"name":"深蹲","scope":"movement_pattern","note":"膝部不适"}]}'
REVISE="{\"payload\":$REVISE_PAYLOAD}"
http POST "/api/drafts/$DID/revise" "$REVISE" >/dev/null
REV=$(body | jget draft)
check R1 "revise revision+1 (=2)" 2 "$(echo "$REV" | jget revision)"
check R2 "revise keeps status pending" pending "$(echo "$REV" | jget status)"
check R3 "revise diff re-derived (每周频率 row)" true "$(has "$REV" "档案 · 每周频率")"
check R4 "revise diff shows new frequency value" true "$(has "$REV" "4 次")"
check R5 "revise diff shows added restriction" true "$(has "$REV" "深蹲")"
check R6 "revise payload restrictions count=2" 2 "$(echo "$REV" | jget payload.restrictions.length)"
http GET /api/dev/status >/dev/null
check R7 "revise does not touch formal data (has_profile=false)" false "$(body | jget has_profile)"
check R8 "revise does not advance context_version" 0 "$(body | jget context_version)"

### C. confirm once
http POST "/api/drafts/$DID/confirm" '{"revision":2}' >/dev/null
CONF1=$(body)
check C1 "confirm newly_committed=true" true "$(echo "$CONF1" | jget newly_committed)"
check C2 "confirm status=committed" committed "$(echo "$CONF1" | jget status)"
check C3 "confirm context_version=1" 1 "$(echo "$CONF1" | jget context_version)"
check C4 "confirm summary" "档案与动作限制已写入正式数据" "$(echo "$CONF1" | jget summary)"
http GET /api/profile >/dev/null
PROF=$(body)
check C5 "profile written (non-null)" true "$([[ "$(echo "$PROF" | jget profile)" != null && "$(echo "$PROF" | jget profile)" != "<absent>" ]] && echo true || echo false)"
check C6 "profile weekly_frequency=4" 4 "$(echo "$PROF" | jget profile.weekly_frequency)"
check C7 "profile body_weight_kg=75" 75 "$(echo "$PROF" | jget profile.body_weight_kg)"
check C8 "profile equipment count=3" 3 "$(echo "$PROF" | jget profile.equipment.length)"
check C9 "profile red flag retained" true "$(echo "$PROF" | jget profile.physical_state.red_flags | grep -q 胸部异常不适 && echo true || echo false)"
check C10 "restrictions written count=2" 2 "$(echo "$PROF" | jget restrictions.length)"
check C11 "profile context_version=1" 1 "$(echo "$PROF" | jget context_version)"

### I. repeat confirm -> idempotent original result
http POST "/api/drafts/$DID/confirm" '{"revision":2}' >/dev/null
CONF2=$(body)
check I1 "repeat confirm newly_committed=false" false "$(echo "$CONF2" | jget newly_committed)"
check I2 "repeat confirm returns original context_version=1" 1 "$(echo "$CONF2" | jget context_version)"
check I3 "repeat confirm returns original summary" "档案与动作限制已写入正式数据" "$(echo "$CONF2" | jget summary)"
http GET /api/dev/status >/dev/null
check I4 "repeat confirm does not advance context_version" 1 "$(body | jget context_version)"

### TB. create pending draft B (base=1) that will go stale
S=$(send_run f1-05-tb "$SID" "把每周频率改成4次，加上深蹲膝部不适")
http POST /api/runs "$S" >/dev/null
wait_done TB 60 || true
check TB1 "new pending draft created" 1 "$(echo "$LAST_RUN" | jget run.drafts.length)"
check TB2 "draft B kind=profile_update" profile_update "$(echo "$LAST_RUN" | jget run.drafts.0.kind)"
check TB3 "draft B base_business_version=1" 1 "$(echo "$LAST_RUN" | jget run.drafts.0.base_business_version)"
DIDB=$(echo "$LAST_RUN" | jget run.drafts.0.id)

### SC. second session: full facts -> draft C -> confirm advances context_version to 2
http POST /api/sessions '{"title":"F1-05 第二会话"}' >/dev/null
SID2=$(body | jget id)
S=$(send_run f1-05-tc "$SID2" "目标增肌，有一点基础，每周练4次，每次50分钟，有哑铃，体重70公斤，深蹲膝部不适，没有这些情况")
http POST /api/runs "$S" >/dev/null
wait_done SC 60 || true
check SC1 "session 2 draft created" 1 "$(echo "$LAST_RUN" | jget run.drafts.length)"
check SC2 "session 2 draft kind=profile_update" profile_update "$(echo "$LAST_RUN" | jget run.drafts.0.kind)"
DIDC=$(echo "$LAST_RUN" | jget run.drafts.0.id)
http POST "/api/drafts/$DIDC/confirm" '{"revision":1}' >/dev/null
check SC3 "session 2 confirm newly_committed=true" true "$(body | jget newly_committed)"
check SC4 "context_version advanced to 2" 2 "$(body | jget context_version)"
http GET /api/profile >/dev/null
check SC5 "formal profile now session 2 (weight=70)" 70 "$(body | jget profile.body_weight_kg)"

### ST. stale draft B confirm -> draft_stale, no write
http POST "/api/drafts/$DIDB/confirm" '{"revision":1}' >/dev/null
STB=$(body)
check ST1 "stale confirm HTTP 409" 409 "$HTTP_CODE"
check ST2 "stale error_code=draft_stale" draft_stale "$(echo "$STB" | jget error_code)"
http GET /api/dev/status >/dev/null
check ST3 "stale rejected: context_version still 2" 2 "$(body | jget context_version)"
check ST4 "stale rejected: committed drafts unchanged (D1 + C)" 2 "$(body | jget drafts | grep -o '"status":"committed"' | wc -l | tr -d ' ')"

### TS. S1 run while B pending -> no competing draft, tells user to recalc
S=$(send_run f1-05-ts "$SID" "每周练5次")
http POST /api/runs "$S" >/dev/null
wait_done TS 60 || true
check TS1 "run completed" completed "$(echo "$LAST_RUN" | jget run.status)"
check TS2 "no competing draft while one pending" 0 "$(echo "$LAST_RUN" | jget run.drafts.length)"
http GET "/api/sessions/$SID/messages" >/dev/null
REPLY3=$(body | jlast)
check TS3 "reply tells user draft is stale/recalc" true "$(has "$REPLY3" "过期")"

### RC. recalc B -> new draft on latest context + draft_vs_draft diff
http POST "/api/drafts/$DIDB/recalc" '{}' >/dev/null
REC=$(body)
check RC1 "recalc new_draft revision=1" 1 "$(echo "$REC" | jget new_draft.revision)"
check RC2 "recalc new_draft base_business_version=2" 2 "$(echo "$REC" | jget new_draft.base_business_version)"
check RC3 "recalc old_draft status=stale" stale "$(echo "$REC" | jget old_draft.status)"
check RC4 "recalc draft_vs_draft_diff non-empty" true "$([[ $(echo "$REC" | jget draft_vs_draft_diff.length) -gt 0 ]] && echo true || echo false)"
check RC5 "draft_vs_draft_diff shows frequency change" true "$(has "$REC" "档案 · 每周频率")"
check RC6 "recalc new_draft diff re-derived vs current profile" true "$([[ $(echo "$REC" | jget new_draft.diff.length) -gt 0 ]] && echo true || echo false)"
DIDB2=$(echo "$REC" | jget new_draft.id)

### CB. confirm recalculated draft
http POST "/api/drafts/$DIDB2/confirm" '{"revision":1}' >/dev/null
check CB1 "recalc draft confirm newly_committed=true" true "$(body | jget newly_committed)"
check CB2 "context_version advanced to 3" 3 "$(body | jget context_version)"
http GET /api/profile >/dev/null
PROF3=$(body)
check CB3 "profile updated weekly_frequency=5" 5 "$(echo "$PROF3" | jget profile.weekly_frequency)"
check CB4 "profile body_weight_kg=75 (S1 facts rebuilt)" 75 "$(echo "$PROF3" | jget profile.body_weight_kg)"
check CB5 "restrictions count=2" 2 "$(echo "$PROF3" | jget restrictions.length)"

### TD/DC. discard then reject
S=$(send_run f1-05-td "$SID" "每周练6次")
http POST /api/runs "$S" >/dev/null
wait_done TD 60 || true
check TD1 "new draft for discard created" 1 "$(echo "$LAST_RUN" | jget run.drafts.length)"
DIDD=$(echo "$LAST_RUN" | jget run.drafts.0.id)
http POST "/api/drafts/$DIDD/discard" '{}' >/dev/null
check DC1 "discard status=discarded" discarded "$(body | jget status)"
http POST "/api/drafts/$DIDD/confirm" '{"revision":1}' >/dev/null
DCB=$(body)
check DC0 "confirm discarded HTTP 409" 409 "$HTTP_CODE"
check DC2 "confirm discarded -> error_code invalid_request" invalid_request "$(echo "$DCB" | jget error_code)"
http GET /api/dev/status >/dev/null
check DC3 "discard/reject did not advance context_version" 3 "$(body | jget context_version)"

### Q. query recovery (GET sessions/:id/drafts + runs/active + messages)
http GET "/api/sessions/$SID/drafts" >/dev/null
Q=$(body)
check Q1 "session drafts recoverable (4 drafts)" 4 "$(echo "$Q" | jget length)"
check Q2 "recovered drafts include committed" true "$(has "$Q" '"status":"committed"')"
check Q3 "recovered drafts include stale" true "$(has "$Q" '"status":"stale"')"
check Q4 "recovered drafts include discarded" true "$(has "$Q" '"status":"discarded"')"
http GET /api/runs/active >/dev/null
check Q5 "runs/active exposes latest run draft state" 1 "$(body | jget run.drafts.length)"
http GET "/api/sessions/$SID/messages" >/dev/null
check Q6 "session messages recoverable" true "$([[ $(body | jget length) -ge 10 ]] && echo true || echo false)"

log "== RESULT: PASS=$PASS FAIL=$FAIL TOTAL=$TOTAL | elapsed=$(( $(date +%s) - START ))s =="
exit 0
