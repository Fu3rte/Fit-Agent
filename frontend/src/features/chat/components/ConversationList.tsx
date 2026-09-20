import { matchPath, NavLink, useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import { toast } from "sonner";
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarMenu,
  SidebarMenuItem,
  sidebarMenuButtonVariants,
} from "@/components/ui/sidebar";
import { cn } from "@/lib/utils";
import { deleteConversation, listConversations } from "@/lib/api";

/** 会话路由参数名与 App.tsx 的 ``/chat/:chatId`` 路由同源 */
const CHAT_PATH = "/chat/:chatId";

/** 会话导航项视觉（与 App.tsx 主导航项同一口径） */
const conversationLinkClass = ({ isActive }: { isActive: boolean }) =>
  cn(
    sidebarMenuButtonVariants(),
    isActive && "bg-sidebar-accent font-medium text-sidebar-accent-foreground",
  );

/**
 * 侧栏会话历史：常驻显示，点会话名进入 ``/chat/:chatId``；删除当前会话后回到根路径的新会话状态。
 *
 * 列表顺序沿用服务端返回的 ``updated_at`` 降序，不在前端重排。
 */
export default function ConversationList() {
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const currentChatId = matchPath(CHAT_PATH, location.pathname)?.params.chatId;

  const conversations = useQuery({
    queryKey: ["conversations"],
    queryFn: listConversations,
  });

  const remove = useMutation({
    mutationFn: deleteConversation,
    onSuccess: async (_deleted, conversationId) => {
      queryClient.removeQueries({ queryKey: ["conversation", conversationId] });
      toast.success("会话已删除");
      await queryClient.invalidateQueries({ queryKey: ["conversations"] });
      if (conversationId === currentChatId) navigate("/");
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "删除会话失败"),
  });

  return (
    <SidebarGroup className="px-3">
      <SidebarGroupContent>
        <SidebarMenu>
          {(conversations.data?.conversations ?? []).map((conversation) => (
            <SidebarMenuItem key={conversation.id}>
              <NavLink
                to={`/chat/${conversation.id}`}
                className={conversationLinkClass}
              >
                <span className="truncate">{conversation.title}</span>
              </NavLink>
              <button
                type="button"
                aria-label={`删除会话 ${conversation.title}`}
                onClick={() => remove.mutate(conversation.id)}
                className="absolute top-1/2 right-1 -translate-y-1/2 cursor-pointer rounded p-1 text-muted-foreground opacity-0 transition-opacity group-hover/menu-item:opacity-100 focus-visible:opacity-100"
              >
                <X aria-hidden className="size-4" />
              </button>
            </SidebarMenuItem>
          ))}
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  );
}
