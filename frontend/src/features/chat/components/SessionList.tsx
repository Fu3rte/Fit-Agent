import { useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import {
  useMutation,
  useMutationState,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { toast } from "sonner";
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarMenu,
  SidebarMenuItem,
  sidebarMenuButtonVariants,
} from "@/components/ui/sidebar";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { deleteSession, listSessions } from "@/lib/api";
import type { SessionListWire, SessionWire } from "@/lib/contract";
import { forgetSession } from "@/features/chat/utils/reactAgent";

/** 侧栏导航项视觉（会话列表与页面导航共用） */
export const navLinkClass = cn(
  sidebarMenuButtonVariants(),
  "min-w-0 aria-[current=page]:bg-sidebar-accent aria-[current=page]:font-medium aria-[current=page]:text-sidebar-accent-foreground",
);

/**
 * 侧栏会话列表（session-history-contract §2）：常驻显示全量会话，点击打开 ``/sessions/{id}``。
 * 全部路由共用会话列表缓存，创建、执行及删除后的失效重取纳入业务更新；读取失败保留已有列表。
 * 删除（session-delete-contract §1、§5）：点击删除图标先经确认弹窗（正文仅会话标题），确认后才发请求；
 * 服务端确认成功后清理全部路径的列表缓存、本地选择与账本，仅在回调时当前页面就是目标会话才回到空白页；
 * 失败或结果未知保留展示与账本，可再次点击重试。
 */
export default function SessionList() {
  const navigate = useNavigate();
  const queries = useQueryClient();
  const [confirming, setConfirming] = useState<SessionWire | null>(null);
  const sessions = useQuery({
    queryKey: ["sessions"],
    queryFn: ({ signal }) => listSessions(signal),
  });

  const pending = useMutationState({
    filters: { mutationKey: ["delete-session"], status: "pending" },
    select: (mutation) => mutation.state.variables as string,
  });
  const remove = useMutation({
    mutationKey: ["delete-session"],
    mutationFn: deleteSession,
    onSuccess: async (_result, sessionId) => {
      // revert:false 保留已有列表展示，只取消在途请求，使迟到的旧列表无法写回缓存
      await queries.cancelQueries(
        { queryKey: ["sessions"] },
        { revert: false },
      );
      forgetSession(sessionId);
      queries.setQueriesData<SessionListWire>(
        { queryKey: ["sessions"] },
        (current) =>
          current && {
            sessions: current.sessions.filter(
              (item) => item.session_id !== sessionId,
            ),
          },
      );
      void queries.invalidateQueries({ queryKey: ["sessions"] });
      if (window.location.pathname === `/sessions/${sessionId}`) navigate("/");
    },
    onError: (failure) =>
      toast.error(
        failure instanceof Error ? failure.message : "删除会话结果未知。",
      ),
  });

  return (
    <SidebarGroup className="px-3">
      <SidebarGroupContent>
        <SidebarMenu>
          {(sessions.data?.sessions ?? []).map((session) => (
            <SidebarMenuItem key={session.session_id}>
              <Tooltip>
                <TooltipTrigger asChild>
                  <NavLink
                    to={`/sessions/${session.session_id}`}
                    className={cn(navLinkClass, "pr-8")}
                  >
                    <span className="min-w-0 flex-1 truncate">
                      {session.title}
                    </span>
                  </NavLink>
                </TooltipTrigger>
                <TooltipContent
                  side="top"
                  className="max-w-xs whitespace-pre-wrap wrap-break-word"
                >
                  {session.title}
                </TooltipContent>
              </Tooltip>
              <button
                type="button"
                aria-label={`删除会话 ${session.title}`}
                disabled={pending.includes(session.session_id)}
                onClick={() => setConfirming(session)}
                className="pointer-coarse:opacity-100 absolute top-1/2 right-0 grid size-8 -translate-y-1/2 cursor-pointer place-items-center rounded text-muted-foreground opacity-0 transition-opacity hover:bg-muted hover:text-foreground focus-visible:opacity-100 group-hover/menu-item:opacity-100"
              >
                <Trash2 aria-hidden className="size-4" />
              </button>
            </SidebarMenuItem>
          ))}
        </SidebarMenu>
      </SidebarGroupContent>
      {confirming !== null && (
        <Dialog open onOpenChange={(open) => !open && setConfirming(null)}>
          <DialogContent
            aria-describedby={undefined}
            className="w-[min(26rem,calc(100vw-2rem))]"
          >
            <DialogTitle className="pr-10 font-medium tracking-tight">
              {confirming.title}
            </DialogTitle>
            <div className="flex justify-end gap-2">
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setConfirming(null)}
              >
                取消
              </Button>
              <Button
                variant="destructive"
                size="sm"
                onClick={() => {
                  remove.mutate(confirming.session_id);
                  setConfirming(null);
                }}
              >
                删除
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      )}
    </SidebarGroup>
  );
}
