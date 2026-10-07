import { useLayoutEffect, useRef, useState } from "react";
import {
  matchPath,
  NavLink,
  Navigate,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { Toaster, useSonner } from "sonner";
import { Moon, Plus, Sun, UserRound } from "lucide-react";
import { cn } from "@/lib/utils";
import { getTheme, setTheme, type Theme } from "@/lib/theme";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";
import ChatPage from "@/features/chat/ChatPage";
import SessionList, {
  navLinkClass,
} from "@/features/chat/components/SessionList";
import ProfilePage from "@/features/profile/ProfilePage";
import {
  ensureDraft,
  loadChatStore,
  startNewSession,
} from "@/features/chat/utils/reactAgent";

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function GlobalToaster() {
  const layer = useRef<HTMLDivElement>(null);
  const { toasts } = useSonner();
  const ids = toasts.map((item) => item.id).join("|");
  useLayoutEffect(() => {
    const node = layer.current;
    if (node === null) return;
    if (node.matches(":popover-open")) node.hidePopover();
    node.showPopover();
  }, [ids]);
  return (
    <div
      ref={layer}
      popover="manual"
      className="pointer-events-none fixed inset-0 m-0 size-auto max-h-none max-w-none overflow-visible border-0 bg-transparent p-0 **:data-sonner-toaster:pointer-events-auto"
    >
      <Toaster position="top-center" richColors />
    </div>
  );
}

function ThemeToggle() {
  const [theme, setThemeState] = useState<Theme>(getTheme());
  return (
    <SidebarMenuButton
      onClick={() => {
        const next = theme === "light" ? "dark" : "light";
        setTheme(next);
        setThemeState(next);
      }}
      className="cursor-pointer"
    >
      {theme === "light" ? <Moon aria-hidden /> : <Sun aria-hidden />}
      <span>{theme === "light" ? "切换暗色" : "切换浅色"}</span>
    </SidebarMenuButton>
  );
}

function SidebarToggle() {
  const { state } = useSidebar();
  return (
    <div
      className={cn(
        "absolute top-8 z-20 transition-[left] duration-200 ease-linear",
        state === "collapsed"
          ? "left-6"
          : "left-[calc(var(--sidebar-width)-3.5rem)]",
      )}
    >
      <SidebarTrigger className="cursor-pointer" />
    </div>
  );
}


function ChatHost() {
  const location = useLocation();
  const match = matchPath("/sessions/:sessionId", location.pathname);
  const urlSessionId = match?.params.sessionId ?? null;
  if (urlSessionId === null && location.pathname !== "/")
    return <Navigate to="/" replace />;
  const store = loadChatStore();
  if (urlSessionId === null) {
    if (store.selected_session_id !== null)
      return <Navigate to={`/sessions/${store.selected_session_id}`} replace />;
    const draftId = (store.draft ?? ensureDraft().draft!).session_id;
    return <ChatPage key={draftId} sessionId={draftId} isDraft />;
  }
  if (!UUID_PATTERN.test(urlSessionId)) return <Navigate to="/" replace />;
  return (
    <ChatPage key={urlSessionId} sessionId={urlSessionId} isDraft={false} />
  );
}

/** 页面路由（PRODUCT.md §3.5）：``/profile`` 打开个人画像页，其余路径由会话宿主处理 */
function PageHost() {
  const location = useLocation();
  if (location.pathname === "/profile") return <ProfilePage />;
  return <ChatHost />;
}

export default function App() {
  const navigate = useNavigate();
  return (
    <TooltipProvider>
      <GlobalToaster />
      <SidebarProvider className="relative h-screen">
        <Sidebar>
          <SidebarHeader className="px-6 pt-8 pb-6">
            <h1 className="font-display text-2xl font-light tracking-tight">
              Fit-Agent
            </h1>
          </SidebarHeader>
          <SidebarContent>
            <SidebarGroup className="px-3">
              <SidebarGroupContent>
                <SidebarMenu>
                  <SidebarMenuItem>
                    <SidebarMenuButton
                      onClick={() => {
                        startNewSession();
                        navigate("/");
                      }}
                      className="cursor-pointer"
                    >
                      <Plus aria-hidden />
                      <span>新建会话</span>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                  <SidebarMenuItem>
                    <NavLink to="/profile" className={navLinkClass}>
                      <UserRound aria-hidden />
                      <span>个人画像</span>
                    </NavLink>
                  </SidebarMenuItem>
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
            <SessionList />
          </SidebarContent>
          <SidebarFooter className="px-3 pb-4">
            <ThemeToggle />
          </SidebarFooter>
        </Sidebar>
        <main className="relative min-h-0 flex-1 overflow-hidden">
          <div className="h-full">
            <PageHost />
          </div>
        </main>
        <SidebarToggle />
      </SidebarProvider>
    </TooltipProvider>
  );
}
