import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { createServer } from 'vite';

const directory = readFileSync(path.resolve(import.meta.dirname, '../../backend/temp/profile-joint/latest-evidence.txt'), 'utf8');
const wire = JSON.parse(readFileSync(path.join(directory, 'contract-wire.json'), 'utf8'));
const server = await createServer({ appType: 'custom', logLevel: 'silent', server: { middlewareMode: true } });
const { applyReActEvent, profileSaved, createReActParser } = await server.ssrLoadModule('/src/features/chat/utils/reactAgent.ts');
const { historyToRounds, mergeHistoryRounds } = await server.ssrLoadModule('/src/features/chat/utils/sessionHistory.ts');
await server.close();
let displays = 0;
let refreshes = 0;
const live = [];
for (const turn of wire.turns) {
  const runId = turn.events[0].data.run_id;
  let round = { id: runId, run_id: runId, request_entry_id: turn.request_entry_id, entries: [], status: 'running' };
  const parser = createReActParser(event => {
    if (profileSaved(event, round)) refreshes += 1;
    round = applyReActEvent(round, event);
  }, runId);
  for (const event of turn.events) parser.feed(`event: ${event.event}\ndata: ${JSON.stringify(event.data)}\n\n`);
  assert.equal(round.status, 'completed');
  const tools = new Map(turn.events.filter(e=>e.event==='tool_start').map(e=>[e.data.tool_call_id,e.data.name]));
  const proposals = turn.events.filter(e=>e.event==='tool_result' && tools.get(e.data.tool_call_id)==='prepare_profile_update' && !e.data.is_error);
  const rendered = round.entries.filter(e=>e.kind==='tool' && e.profile!==undefined);
  assert.equal(rendered.length, proposals.length);
  for (const proposal of proposals) {
    const entry = rendered.find(e=>e.entry_id===proposal.data.entry_id);
    assert.ok(entry);
    assert.deepEqual(entry.profile, JSON.parse(proposal.data.content).payload);
  }
  displays += rendered.length;
  if (turn.session_id === wire.history.session.session_id) live.push(round);
}
const historical = historyToRounds(wire.history);
const restored = historical.flatMap(r=>r.entries).filter(e=>e.kind==='tool' && e.profile!==undefined);
const proposals = wire.history.entries.filter(e=>e.message.role==='toolResult' && e.message.tool_name==='prepare_profile_update' && !e.message.is_error);
assert.equal(restored.length, proposals.length);
for (const proposal of proposals) assert.deepEqual(restored.find(e=>e.entry_id===proposal.entry_id).profile, JSON.parse(proposal.message.content).payload);
const merged = mergeHistoryRounds(live, historical);
assert.equal(merged.flatMap(r=>r.entries).filter(e=>e.kind==='tool' && e.profile!==undefined).length, restored.length);
assert.ok(displays >= 6 && refreshes >= 5);
assert.equal(wire.profile.version, 4);
console.log(JSON.stringify({status:'passed',source:directory,live_profile_displays:displays,saved_refresh_signals:refreshes,restored_profile_displays:restored.length,profile_version:wire.profile.version}));
