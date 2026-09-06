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
    <button
      type="button"
      onClick={toggle}
      className="flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent/60 hover:text-sidebar-foreground"
    >
      {theme === "light" ? (
        <Moon className="size-4" aria-hidden />
      ) : (
        <Sun className="size-4" aria-hidden />
      )}
      {theme === "light" ? "切换暗色" : "切换浅色"}
    </button>
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
    <div className="flex flex-col gap-1 px-3">
      <button
        type="button"
        onClick={() => create.mutate()}
        disabled={create.isPending}
        className="flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors hover:bg-sidebar-accent/60 disabled:opacity-50"
      >
        <Plus className="size-4" aria-hidden />
        新对话
      </button>
      <div className="mt-1 max-h-56 overflow-y-auto">
        {sessions.data?.map((s) => (
          <button
            key={s.id}
            type="button"
            onClick={() => navigate(`/?s=${s.id}`)}
            className={cn(
              "flex w-full flex-col items-start rounded-lg px-3 py-1.5 text-left text-sm transition-colors",
              currentSessionId === s.id
                ? "bg-sidebar-accent text-sidebar-accent-foreground"
                : "text-muted-foreground hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
            )}
            title={s.title}
          >
            <span className="w-full truncate">{s.title}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

export default function App() {
  return (
    <>
      {/* 全局反馈 toast（A5），全应用仅此一处 */}
      <Toaster position="top-center" richColors />

      <div className="flex h-screen">
        {/* 侧边栏（PLAN-FRONTEND B2 结构） */}
        <aside className="flex w-60 shrink-0 flex-col border-r border-sidebar-border bg-sidebar">
          <div className="px-6 pt-8 pb-6">
            <h1 className="font-display text-2xl font-light tracking-tight">
              Fit-Agent
            </h1>
            <p className="mt-1 text-xs text-muted-foreground">
              健身计划 · 打卡 · 复盘
            </p>
          </div>

          <SessionNav />

          <nav className="mt-4 flex flex-col gap-1 border-t border-sidebar-border px-3 pt-4">
            {nav.map(({ to, label, icon: Icon, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                className={({ isActive }) =>
                  cn(
                    "flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors",
                    isActive
                      ? "bg-sidebar-accent text-sidebar-accent-foreground"
                      : "text-muted-foreground hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
                  )
                }
              >
                <Icon className="size-4" aria-hidden />
                {label}
              </NavLink>
            ))}
          </nav>

          <div className="mt-auto flex flex-col gap-2 px-3 pb-4">
            <ThemeToggle />
            <div className="px-3 pb-2 text-xs text-muted-foreground">
              本地部署 · 单用户
            </div>
          </div>
        </aside>

        {/* 主区 + 氛围渐变球（仅装饰） */}
        <main className="relative flex-1 overflow-y-auto">
          <div
            aria-hidden
            className="pointer-events-none absolute inset-0 overflow-hidden"
          >
            <div className="bg-orb-mint absolute -top-32 -right-24 size-96 opacity-50 blur-3xl" />
            <div className="bg-orb-peach absolute top-1/3 -left-32 size-96 opacity-40 blur-3xl" />
            <div className="bg-orb-lavender absolute -bottom-40 right-1/4 size-96 opacity-40 blur-3xl" />
          </div>
          <div className="relative min-h-full">
            <Routes>
              <Route path="/" element={<ChatPage />} />
              <Route path="/profile" element={<ProfilePage />} />
              <Route path="/records" element={<RecordsPage />} />
              <Route path="/review" element={<ReviewPage />} />
              <Route path="/settings" element={<SettingsPage />} />
            </Routes>
          </div>
        </main>
      </div>
    </>
  );
}
