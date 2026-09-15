import { useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { Toaster } from "sonner";
import { Dumbbell, Moon, Sun, UserRound } from "lucide-react";
import { cn } from "@/lib/utils";
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
import ProfilePage from "@/features/profile/ProfilePage";
import RecordsPage from "@/features/records/RecordsPage";

/** Stage 1 保留页面：画像与训练记录（讨论总结 §7；旧对话/复盘/设置页已随旧路径删除）。 */
const nav = [
  { to: "/profile", label: "用户画像", icon: UserRound, end: false },
  { to: "/records", label: "训练记录", icon: Dumbbell, end: false },
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
      <SidebarTrigger className="cursor-pointer" />
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
            <p className="text-xs text-muted-foreground">健身计划 · 打卡</p>
          </SidebarHeader>

          <SidebarContent>
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
              <Route path="/" element={<RecordsPage />} />
              <Route path="/profile" element={<ProfilePage />} />
              <Route path="/records" element={<RecordsPage />} />
            </Routes>
          </div>
        </main>

        <SidebarToggle />
      </SidebarProvider>
    </>
  );
}
