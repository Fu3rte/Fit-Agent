import { useEffect, useRef, useState } from "react";
import ChatComposer from "./components/ChatComposer";
import ChatTranscript from "./components/ChatTranscript";
import { runReActStream } from "@/lib/api";
import { applyReActEvent, failReActRound, type ReActRound } from "./utils/reactAgent";

export default function ChatPage() {
  const [sessionId] = useState(() => crypto.randomUUID());
  const [rounds, setRounds] = useState<ReActRound[]>([]);
  const [busy, setBusy] = useState(false);
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => {
    active.current?.abort();
    active.current = null;
  }, []);

  const send = (request: string) => {
    if (active.current || !request.trim() || Array.from(request).length > 32000) return;
    const controller = new AbortController();
    active.current = controller;
    const id = crypto.randomUUID();
    setBusy(true);
    setRounds((current) => [...current, { id, request, tools: [], status: "running" }]);
    void runReActStream({ session_id: sessionId, request }, (event) => {
      if (active.current !== controller) return;
      setRounds((current) => current.map((round) => round.id === id ? applyReActEvent(round, event) : round));
    }, controller.signal).then(
      () => {
        if (active.current !== controller) return;
        active.current = null;
        setBusy(false);
      },
      () => {
        if (active.current !== controller) return;
        setRounds((current) => current.map((round) => round.id === id ? failReActRound(round, "连接中断或运行失败，请重新发送。") : round));
        active.current = null;
        setBusy(false);
      },
    );
  };

  return (
    <div className="relative flex h-full w-full flex-col overflow-hidden">
      <ChatTranscript rounds={rounds} />
      <ChatComposer busy={busy} onSend={send} />
    </div>
  );
}
