import { useState } from "react";
import {
  NavLink,
  Route,
  Routes,
  useNavigate,
  useSearchParams,
} from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Toaster } from "sonner";
import {
  ChartLine,
  Dumbbell,
  MessageSquare,
  Moon,
  Plus,
  Settings,
  Sun,
  UserRound,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { createSession, getSessions } from "@/lib/api";
import { getTheme, setTheme, type Theme } from "@/lib/theme";
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
  sidebarMenuButtonVariants,
  useSidebar,
} from "@/components/ui/sidebar";
import ChatPage from "@/features/chat/ChatPage";
import ProfilePage from "@/features/profile/ProfilePage";
import RecordsPage from "@/features/records/RecordsPage";
import ReviewPage from "@/features/review/ReviewPage";
import SettingsPage from "@/features/settings/SettingsPage";

const nav = [
  { to: "/", label: "对话", icon: MessageSquare, end: true },
  { to: "/profile", label: "档案与限制", icon: UserRound, end: false },
  { to: "/records", label: "训练记录", icon: Dumbbell, end: false },
  { to: "/review", label: "统计与复盘", icon: ChartLine, end: false },
  { to: "/settings", label: "设置", icon: Settings, end: false },
];

/** 主题切换（C3B）：浅色默认，暗色第二主题 */
function ThemeToggle() {
  const [theme, setThemeState] = useState<Theme>(getTheme());
  const toggle = () => {
    const next: Theme = theme === "light" ? "dark" : "light";
    setTheme(next);
    setThemeState(next);
  };
  return (
    <SidebarMenuButton onClick={toggle} className="cursor-pointer">
      {theme === "light" ? (
        <Moon className="size-4" aria-hidden />
      ) : (
        <Sun className="size-4" aria-hidden />
      )}
      <span>{theme === "light" ? "切换暗色" : "切换浅色"}</span>
    </SidebarMenuButton>
  );
}

/** 历史会话列表（B2A）+ 新建会话；当前会话按 /?s= 高亮 */
function SessionNav() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [params] = useSearchParams();
  const currentSessionId = params.get("s");
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: getSessions });
  const create = useMutation({
    mutationFn: () => createSession(),
    onSuccess: (session) => {
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
      navigate(`/?s=${session.id}`);
    },
  });

  return (
    <SidebarGroup className="px-3">
      <SidebarGroupContent>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              onClick={() => create.mutate()}
              disabled={create.isPending}
              className="font-medium cursor-pointer"
            >
              <Plus aria-hidden />
              <span>新对话</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
        <div className="mt-1 max-h-56 overflow-y-auto scrollbar-none [&::-webkit-scrollbar]:hidden">
          <SidebarMenu>
            {sessions.data?.map((s) => (
              <SidebarMenuItem key={s.id}>
                <NavLink
                  to={`/?s=${s.id}`}
                  title={s.title}
                  data-active={currentSessionId === s.id}
                  className={cn(
                    sidebarMenuButtonVariants(),
                    "data-[active=true]:bg-sidebar-accent data-[active=true]:font-medium data-[active=true]:text-sidebar-accent-foreground",
                  )}
                >
                  <span>{s.title}</span>
                </NavLink>
              </SidebarMenuItem>
            ))}
          </SidebarMenu>
        </div>
      </SidebarGroupContent>
    </SidebarGroup>
  );
}

/** 折叠按钮：常驻在侧栏品牌行右侧；折叠时横向滑到主区左上角（与侧栏内同一高度） */
function SidebarToggle() {
  const { state } = useSidebar();
  return (
    <div
      className={cn(
        "absolute top-8 z-20 transition-[left] duration-200 ease-linear",
        state === "collapsed" ? "left-6" : "left-50",
      )}
    >
      <SidebarTrigger className="cursor-pointer"/>
    </div>
  );
}

export default function App() {
  return (
    <>
      {/* 全局反馈 toast（A5），全应用仅此一处 */}
      <Toaster position="top-center" richColors />

      <SidebarProvider className="relative h-screen">
        {/* 侧边栏（PLAN-FRONTEND B2 结构） */}
        <Sidebar collapsible="offcanvas">
          <SidebarHeader className="px-6 pt-8 pb-6">
            <h1 className="font-display text-2xl font-light tracking-tight">
              Fit-Agent
            </h1>
            <p className="text-xs text-muted-foreground">
              健身计划 · 打卡 · 复盘
            </p>
          </SidebarHeader>

          <SidebarContent>
            <SessionNav />

            <div className="mx-3 h-px bg-sidebar-border" />

            <SidebarGroup className="px-3">
              <SidebarGroupContent>
                <SidebarMenu>
                  {nav.map(({ to, label, icon: Icon, end }) => (
                    <SidebarMenuItem key={to}>
                      <NavLink
                        to={to}
                        end={end}
                        className={({ isActive }) =>
                          cn(
                            sidebarMenuButtonVariants(),
                            isActive &&
                              "bg-sidebar-accent font-medium text-sidebar-accent-foreground",
                          )
                        }
                      >
                        <Icon aria-hidden />
                        <span>{label}</span>
                      </NavLink>
                    </SidebarMenuItem>
                  ))}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          </SidebarContent>

          <SidebarFooter className="px-3 pb-4">
            <ThemeToggle />
          </SidebarFooter>
        </Sidebar>

        {/* 主区 */}
        <main className="relative flex-1 overflow-y-auto">
          <div className="h-full">
            <Routes>
              <Route path="/" element={<ChatPage />} />
              <Route path="/profile" element={<ProfilePage />} />
              <Route path="/records" element={<RecordsPage />} />
              <Route path="/review" element={<ReviewPage />} />
              <Route path="/settings" element={<SettingsPage />} />
            </Routes>
          </div>
        </main>

        <SidebarToggle />
      </SidebarProvider>
    </>
  );
}
