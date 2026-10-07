import { useEffect, useMemo, useSyncExternalStore } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import ChatComposer from "./components/ChatComposer";
import ChatTranscript from "./components/ChatTranscript";
import { Button } from "@/components/ui/button";
import { unknownSteeringRequests } from "./utils/reactAgent";
import { sessionRuns } from "./utils/sessionRunManager";

/**
 * 会话页面（session-history-contract §5.1）：URL 身份由宿主传入，运行生命周期由路由之外的
 * 应用级会话运行管理层持有。本页只订阅目标会话的快照并调用管理层操作，卸载仅解除订阅，
 * 站内导航、新建会话与进入个人画像均保持原执行流与运行状态。
 */
export default function ChatPage({
  sessionId,
  isDraft,
}: {
  sessionId: string;
  isDraft: boolean;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const run = sessionRuns.open(sessionId, isDraft);
  const view = useSyncExternalStore(run.subscribe, run.snapshot);

  useEffect(() => {
    run.mount(isDraft);
  }, [run, isDraft]);

  // 草稿创建成功后打开对应会话 URL；页面未挂载时由首页按当前选择恢复（§5.1）
  useEffect(() => {
    if (
      isDraft &&
      view.persistent &&
      location.pathname !== `/sessions/${sessionId}`
    )
      navigate(`/sessions/${sessionId}`);
  }, [isDraft, view.persistent, location.pathname, navigate, sessionId]);

  const unknownRequests = useMemo(
    () => unknownSteeringRequests(view.operations),
    [view.operations],
  );

  return (
    <div className="relative flex h-full w-full flex-col overflow-hidden">
      <ChatTranscript
        rounds={view.rounds}
        onRetry={run.retry}
        onWithdraw={run.withdraw}
        retrying={view.retrying}
        canEdit={view.ready && !view.busy}
        onEdit={run.editMessage}
        onRegenerate={run.regenerateMessage}
      />
      {view.history === "failed" && (
        <div className="mx-auto flex w-full max-w-4xl items-center gap-2 px-6 pt-2">
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={run.reload}
          >
            重新加载历史
          </Button>
        </div>
      )}
      <ChatComposer
        busy={view.busy}
        ready={view.ready}
        error={view.error}
        unknownRequests={unknownRequests}
        onSend={run.send}
        onStop={run.stop}
      />
    </div>
  );
}
