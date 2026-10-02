import { useEffect, useRef, useState } from "react";
import ChatComposer from "./components/ChatComposer";
import ChatTranscript from "./components/ChatTranscript";
import { runReActStream, submitSteering } from "@/lib/api";
import { acceptSteering, applyReActEvent, canSubmitChatInput, finishReActRound, markUnknownSteering, ReActHttpError, type ReActRound } from "./utils/reactAgent";

export default function ChatPage() {
  const [sessionId] = useState(() => crypto.randomUUID());
  const [rounds, setRounds] = useState<ReActRound[]>([]);
  const records = useRef<ReActRound[]>([]);
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState(true);
  const [error, setError] = useState<string>();
  const alive = useRef(true);
  const active = useRef<{ controller: AbortController; id: string; run_id?: string } | null>(null);
  const steering = useRef<AbortController | null>(null);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      active.current?.controller.abort();
      active.current = null;
      steering.current?.abort();
      steering.current = null;
    };
  }, []);

  const update = (id: string, change: (round: ReActRound) => ReActRound) => {
    records.current = records.current.map((round) => round.id === id ? change(round) : round);
    setRounds(records.current);
  };

  const stop = () => {
    const run = active.current;
    if (!run) return;
    active.current = null;
    run.controller.abort();
    update(run.id, (round) => finishReActRound(round, "cancelled"));
    setBusy(false);
    setReady(true);
  };

  const send = (request: string): Promise<boolean> => {
    if (!canSubmitChatInput(request, records.current.flatMap((round) => round.unknown_steering ?? [])) || steering.current) return Promise.resolve(false);
    setError(undefined);
    const run = active.current;
    if (run) {
      if (!run.run_id) return Promise.resolve(false);
      const controller = new AbortController();
      steering.current = controller;
      return submitSteering(run.run_id, { session_id: sessionId, message: request }, controller.signal).then(
        (accepted) => {
          if (!alive.current || steering.current !== controller) return false;
          update(run.id, (round) => acceptSteering(round, accepted, request));
          steering.current = null;
          return true;
        },
        (failure: unknown) => {
          if (!alive.current || steering.current !== controller) return false;
          steering.current = null;
          if (failure instanceof ReActHttpError) setError(failure.message);
          else {
            update(run.id, (round) => markUnknownSteering(round, request));
            setError("Steering 提交结果未知。");
          }
          return false;
        },
      );
    }
    const current = { controller: new AbortController(), id: crypto.randomUUID(), run_id: undefined as string | undefined };
    active.current = current;
    records.current = [...records.current, { id: current.id, entries: [{ kind: "user", id: current.id, request }], status: "running" }];
    setRounds(records.current);
    setBusy(true);
    setReady(false);
    return new Promise<boolean>((resolve) => {
      void runReActStream({ session_id: sessionId, request }, (event) => {
        if (!alive.current || active.current !== current) return;
        update(current.id, (round) => applyReActEvent(round, event));
        if (event.event === "done" || event.event === "error") {
          active.current = null;
          setBusy(false);
          setReady(true);
        }
      }, current.controller.signal, (runId) => {
        if (!alive.current || active.current !== current) return;
        current.run_id = runId;
        update(current.id, (round) => ({ ...round, run_id: runId }));
        setReady(true);
        resolve(true);
      }).then(
        () => resolve(false),
        (failure: unknown) => {
          resolve(false);
          if (!alive.current || active.current !== current) return;
          const message = failure instanceof Error ? failure.message : "连接失败。";
          update(current.id, (round) => finishReActRound(round, "failed", message));
          active.current = null;
          setBusy(false);
          setReady(true);
        },
      );
    });
  };

  return (
    <div className="relative flex h-full w-full flex-col overflow-hidden">
      <ChatTranscript rounds={rounds} />
      <ChatComposer busy={busy} ready={ready} error={error} unknownRequests={rounds.flatMap((round) => round.unknown_steering ?? [])} onSend={send} onStop={stop} />
    </div>
  );
}
