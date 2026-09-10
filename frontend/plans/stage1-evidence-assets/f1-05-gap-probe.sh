#!/usr/bin/env bash
# Stage 1 full-verification supplementary probe: gap scenarios NOT covered by f1-05-probe.sh.
# Same discipline: dedicated 5199, only own PID killed, 180s deadline, no SSE.
set -u
PORT=5199
BASE="http://127.0.0.1:$PORT"
FE=/home/finnian/code/agent/Fit-Agent/frontend
OUT=/tmp/f1-05-gap-probe-out.txt
DEV_LOG=/tmp/f1-05-gap-vite.log
BODYF=/tmp/verify-gap-body.json
START=$(date +%s)
DEADLINE=$((START + 180))
CURL=(curl -sS --noproxy '*' -m 8)
VITE_PID=""
: >"$OUT"
: >"$DEV_LOG"

cleanup() {
  if [[ -n "$VITE_PID" ]]; then
    kill "$VITE_PID" 2>/dev/null
    wait "$VITE_PID" 2>/dev/null
  fi
  echo "--- cleanup at +$(($(date +%s) - START))s (killed only PID ${VITE_PID:-none}) ---" >>"$OUT"
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
http() {
  local m=$1 p=$2 b=${3:-}
  local a=(-o "$BODYF" -w '%{http_code}' -X "$m" "$BASE$p" -H 'Content-Type: application/json')
  [[ -n "$b" ]] && a+=(-d "$b")
  HTTP_CODE=$("${CURL[@]}" "${a[@]}")
  printf '%s' "$HTTP_CODE"
}
body() { cat "$BODYF"; }
has() { [[ "$1" == *"$2"* ]] && echo true || echo false; }

PASS=0
FAIL=0
TOTAL=0
check() {
  TOTAL=$((TOTAL + 1))
  if [[ "$3" == "$4" ]]; then
    PASS=$((PASS + 1))
    log "[$1] PASS  $2 | expected=$3 actual=$4"
  else
    FAIL=$((FAIL + 1))
    log "[$1] FAIL  $2 | expected=$3 actual=$4"
  fi
}

wait_done() {
  local label=$1 max=${2:-60} i=0 st slot
  while [[ $i -lt $max ]]; do
    [[ $(date +%s) -gt $DEADLINE ]] && {
      log "[$label] DEADLINE hit"
      return 1
    }
    LAST_RUN=$("${CURL[@]}" "$BASE/api/runs/active")
    st=$(echo "$LAST_RUN" | jget run.status)
    slot=$("${CURL[@]}" "$BASE/api/dev/status" | jget execution_slot_run_id)
    if [[ "$st" == completed || "$st" == cancelled || "$st" == failed ]] && [[ "$slot" == null || "$slot" == "<absent>" ]]; then return 0; fi
    sleep 0.4
    i=$((i + 1))
  done
  log "[$label] wait_done TIMEOUT (status=$st)"
  return 1
}

send_run() { node -e 'const [sid,msg,cid]=process.argv.slice(1);console.log(JSON.stringify({session_id:sid,message:msg,client_request_id:cid}))' "$2" "$3" "$1"; }
say() { # $1 cid, $2 sid, $3 msg -> sets REPLY
  http POST /api/runs "$(send_run "$1" "$2" "$3")" >/dev/null
  wait_done "$1" 60 || true
  http GET "/api/sessions/$2/messages" >/dev/null
  REPLY=$(body | jlast)
}
new_session() {
  http POST /api/sessions "{\"title\":\"$1\"}" >/dev/null
  echo "$(body | jget id)"
}

log "== Stage1 gap probe | $(date -Is) | port=$PORT | budget=180s =="
cd "$FE" || exit 1
./node_modules/.bin/vite --port "$PORT" --strictPort --host 127.0.0.1 >"$DEV_LOG" 2>&1 &
VITE_PID=$!
log "-- vite PID=$VITE_PID on $PORT --"
up=no
for _ in $(seq 1 60); do
  [[ $(date +%s) -gt $DEADLINE ]] && break
  "${CURL[@]}" -o /dev/null "$BASE/api/provider" && {
    up=yes
    break
  }
  sleep 0.5
done
check C0 "vite reachable" yes "$up"
[[ "$up" != yes ]] && {
  tail -20 "$DEV_LOG" >>"$OUT"
  exit 1
}

http POST /api/dev/reset '{"seed":"empty"}' >/dev/null
http PUT /api/provider/api-key '{"api_key":"sk-mock-gap-not-real"}' >/dev/null

### G1: red flag FIRST, then unlisted symptom -> is the red flag retained? (stage1 F1-02 要求红旗入档)
SID=$(new_session "G1 红旗后被清单外症状")
say g1a "$SID" "我最近胸部异常不适"
check G1a "red flag reply recommends offline eval" true "$([[ "$REPLY" == *线下* && "$REPLY" == *评估* ]] && echo true || echo false)"
check G1b "red flag turn: no draft" 0 "$(echo "$LAST_RUN" | jget run.drafts.length)"
say g1b "$SID" "肩部偶尔发酸"
check G1c "unlisted symptom -> clarifies, no safety verdict" true "$(has "$REPLY" "不会判断它是否安全")"
say g1c "$SID" "目标增肌，有一点基础，每周练3次，每次60分钟，有哑铃，体重75公斤，没有动作限制，没有这些情况"
check G1d "draft generated" 1 "$(echo "$LAST_RUN" | jget run.drafts.length)"
G1FLAGS=$(echo "$LAST_RUN" | jget run.drafts.0.payload.profile.physical_state.red_flags)
check G1e "REQUIREMENT: earlier red flag retained in draft" true "$(has "$G1FLAGS" "胸部异常不适")"
log "G1 observed red_flags in draft = $G1FLAGS"

### G2 control: red flag then explicit '没有这些情况' with NO unlisted symptom in between
SID2=$(new_session "G2 红旗后直接否认")
say g2a "$SID2" "我最近胸部异常不适"
say g2b "$SID2" "没有这些情况"
say g2c "$SID2" "目标增肌，有一点基础，每周练3次，每次60分钟，有哑铃，体重75公斤，没有动作限制"
check G2a "control: red flag retained after explicit none" true "$(echo "$LAST_RUN" | jget run.drafts.0.payload.profile.physical_state.red_flags | grep -q 胸部异常不适 && echo true || echo false)"

### G3: check-in request -> not available in stage 1, no record draft, no plan-precondition claim
SID3=$(new_session "G3 打卡请求")
BEFORE_REC=$(
  http GET /api/records >/dev/null
  body | jget records.length
)
say g3a "$SID3" "今天练了卧推 80kg 5组"
check G3a "check-in reply says not available this stage" true "$(has "$REPLY" "不在本阶段开放")"
check G3b "check-in turn: no draft" 0 "$(echo "$LAST_RUN" | jget run.drafts.length)"
check G3c "check-in reply does not claim plan is prerequisite for records" false "$(has "$REPLY" "必须先建立计划")"
http GET /api/records >/dev/null
check G3d "records unchanged (no record written)" "$BEFORE_REC" "$(body | jget records.length)"

### G4: missing-fact question order, one at a time
SID4=$(new_session "G4 逐项追问")
say g4a "$SID4" "我想增肌"
check G4a "asks experience next" true "$(has "$REPLY" "训练经验")"
say g4b "$SID4" "有一点基础"
check G4b "asks weekly_frequency next" true "$(has "$REPLY" "每周计划训练几次")"
say g4c "$SID4" "每周3次"
check G4c "asks session_minutes next" true "$(has "$REPLY" "多少分钟")"
say g4d "$SID4" "每次60分钟"
check G4d "asks equipment next" true "$(has "$REPLY" "可用器械")"
say g4e "$SID4" "有杠铃"
check G4e "asks body_weight next (required)" true "$(has "$REPLY" "当前体重")"
say g4f "$SID4" "75公斤"
check G4f "asks restrictions next" true "$(has "$REPLY" "动作限制")"
say g4g "$SID4" "没有动作限制"
check G4g "asks physical_state next" true "$(has "$REPLY" "当前身体状态如何")"
check G4h "still no draft before physical_state" 0 "$(echo "$LAST_RUN" | jget run.drafts.length)"
say g4h "$SID4" "没有这些情况"
check G4i "draft after final fact" 1 "$(echo "$LAST_RUN" | jget run.drafts.length)"

### G5: negated red flag is NOT treated as a red flag
SID5=$(new_session "G5 否定红旗")
say g5a "$SID5" "没有胸部异常不适"
check G5a "negated red flag: no offline-eval block" false "$(has "$REPLY" "线下")"
check G5b "negated red flag: no red-flag record" false "$(has "$REPLY" "红旗症状")"

### G6: both restriction granularities land with correct scope
SID6=$(new_session "G6 限制粒度")
say g6a "$SID6" "目标增肌，有一点基础，每周练3次，每次60分钟，有哑铃，体重75公斤，颈后推举肩部不适，深蹲膝部不适，没有这些情况"
D=$(echo "$LAST_RUN" | jget run.drafts.0)
RESTR=$(echo "$D" | jget payload.restrictions)
check G6a "two restrictions" 2 "$(echo "$D" | jget payload.restrictions.length)"
check G6b "specific_action scope present" true "$(echo "$RESTR" | grep -q 'specific_action' && echo true || echo false)"
check G6c "movement_pattern scope present" true "$(echo "$RESTR" | grep -q 'movement_pattern' && echo true || echo false)"
check G6d "no restriction lifecycle/state field (name/scope/note only)" false "$(echo "$RESTR" | grep -qE '"(status|restricted|observed|permanent|temporary)"' && echo true || echo false)"
log "G6 restrictions = $RESTR"

### G8: red flag + unlisted symptom in the SAME turn (positive control)
SID8=$(new_session "G8 同轮红旗+清单外")
say g8a "$SID8" "胸部异常不适，肩部发酸"
say g8b "$SID8" "目标增肌，有一点基础，每周练3次，每次60分钟，有哑铃，体重75公斤，没有动作限制"
check G8a "same-turn: red flag retained in draft" true "$(echo "$LAST_RUN" | jget run.drafts.0.payload.profile.physical_state.red_flags | grep -q 胸部异常不适 && echo true || echo false)"

### G9: unlisted symptom FIRST, then red flag (positive control)
SID9=$(new_session "G9 先清单外后红旗")
say g9a "$SID9" "肩部偶尔发酸"
say g9b "$SID9" "胸部异常不适"
say g9c "$SID9" "目标增肌，有一点基础，每周练3次，每次60分钟，有哑铃，体重75公斤，没有动作限制，没有这些情况"
check G9a "unlisted-then-red-flag: red flag retained in draft" true "$(echo "$LAST_RUN" | jget run.drafts.0.payload.profile.physical_state.red_flags | grep -q 胸部异常不适 && echo true || echo false)"

### G7: unknown equipment is not fabricated as 'none'
SID7=$(new_session "G7 器械未知")
say g7a "$SID7" "目标增肌，有一点基础，每周练3次，每次60分钟，体重75公斤，没有动作限制，没有这些情况"
check G7a "no draft while equipment unknown" 0 "$(echo "$LAST_RUN" | jget run.drafts.length)"
check G7b "asks equipment (not silently 'none')" true "$(has "$REPLY" "可用器械")"

log "== GAP RESULT: PASS=$PASS FAIL=$FAIL TOTAL=$TOTAL | elapsed=$(($(date +%s) - START))s =="
exit 0
