import { NavLink, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarMenu,
  SidebarMenuItem,
  sidebarMenuButtonVariants,
} from "@/components/ui/sidebar";
import { cn } from "@/lib/utils";
import { listSessions } from "@/lib/api";

/** 会话导航项视觉（与侧栏其它导航项同一口径） */
const sessionLinkClass = ({ isActive }: { isActive: boolean }) =>
  cn(
    sidebarMenuButtonVariants(),
    isActive && "bg-sidebar-accent font-medium text-sidebar-accent-foreground",
  );

/**
 * 侧栏会话列表（session-history-contract §2）：常驻显示全量会话，点击打开 ``/sessions/{id}``。
 * 路由变化时按路径重取以纳入刚创建或更新的会话；读取失败保留已有列表，不清空展示。
 */
export default function SessionList() {
  const location = useLocation();
  const sessions = useQuery({
    queryKey: ["sessions", location.pathname],
    queryFn: ({ signal }) => listSessions(signal),
    placeholderData: (previous) => previous,
  });

  return (
    <SidebarGroup className="px-3">
      <SidebarGroupContent>
        <SidebarMenu>
          {(sessions.data?.sessions ?? []).map((session) => (
            <SidebarMenuItem key={session.session_id}>
              <NavLink to={`/sessions/${session.session_id}`} className={sessionLinkClass}>
                <span className="truncate">{session.title}</span>
              </NavLink>
            </SidebarMenuItem>
          ))}
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  );
}
