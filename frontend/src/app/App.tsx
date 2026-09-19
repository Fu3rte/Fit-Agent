import { useLayoutEffect, useRef, useState } from "react";
import {
  Navigate,
  NavLink,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { Toaster, useSonner } from "sonner";
import {
  ClipboardList,
  LayoutDashboard,
  MessageSquare,
  Moon,
  Settings,
  Sun,
} from "lucide-react";
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
  sidebarMenuButtonVariants,
  useSidebar,
} from "@/components/ui/sidebar";
import DashboardPage from "@/features/dashboard/DashboardPage";
import PlansPage from "@/features/plans/PlansPage";
import ChatPage from "@/features/chat/ChatPage";
import { ProviderDialog } from "@/features/provider/ProviderDialog";

/** 主导航：对话、数据看板、训练计划；模型配置由侧栏底部弹层承载 */
const nav = [
  { to: "/chat", label: "对话", icon: MessageSquare, end: false },
  { to: "/dashboard", label: "数据看板", icon: LayoutDashboard, end: false },
  { to: "/plans", label: "训练计划", icon: ClipboardList, end: false },
];

/** 侧栏导航项视觉 */
const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  cn(
    sidebarMenuButtonVariants(),
    isActive && "bg-sidebar-accent font-medium text-sidebar-accent-foreground",
  );

/**
 * 全局 toast 容器（A5）：用 ``popover="manual"`` 把 sonner 一起送进 top layer。
 *
 * 原生 ``<dialog>`` 的 ``showModal()`` 会把弹层与遮罩放进 top layer，普通流里的 z-index 再高也压不过它；
 * top layer 内部按加入顺序叠放，因此每次 toast 集合变化都重新入栈，保证 toast 盖在已打开的弹层之上。
 */
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
      className="pointer-events-none fixed inset-0 m-0 size-auto max-h-none max-w-none overflow-visible border-0 bg-transparent p-0 [&_[data-sonner-toaster]]:pointer-events-auto"
    >
      <Toaster position="top-center" richColors />
    </div>
  );
}

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
      {theme === "light" ? <Moon aria-hidden /> : <Sun aria-hidden />}
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
        // 展开时贴住品牌行内容右缘（侧栏 px-6 ＋ 按钮 size-8 = 3.5rem），跟随 --sidebar-width
        state === "collapsed"
          ? "left-6"
          : "left-[calc(var(--sidebar-width)-3.5rem)]",
      )}
    >
      <SidebarTrigger className="cursor-pointer" />
    </div>
  );
}

export default function App() {
  const [providerOpen, setProviderOpen] = useState(false);
  const location = useLocation();
  /** 对话页自管滚动（消息列滚、输入框固定），主区锁高度避免再冒出整页滚动条 */
  const mainOverflow =
    location.pathname === "/chat" || location.pathname === "/"
      ? "overflow-hidden"
      : "overflow-y-auto";

  return (
    <TooltipProvider>
      {/* 全局反馈 toast（A5），全应用仅此一处 */}
      <GlobalToaster />

      <SidebarProvider className="relative h-screen">
        {/* 侧边栏（PLAN-FRONTEND B2 结构） */}
        <Sidebar>
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
                      <NavLink to={to} end={end} className={navLinkClass}>
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
            <SidebarMenuButton
              onClick={() => setProviderOpen(true)}
              className="cursor-pointer"
            >
              <Settings aria-hidden />
              <span>模型配置</span>
            </SidebarMenuButton>
            <ThemeToggle />
          </SidebarFooter>
        </Sidebar>

        <main className={`relative min-h-0 flex-1 ${mainOverflow}`}>
          <div className="h-full">
            <Routes>
              <Route path="/" element={<Navigate to="/chat" replace />} />
              <Route path="/chat" element={<ChatPage />} />
              <Route path="/dashboard" element={<DashboardPage />} />
              <Route path="/plans" element={<PlansPage />} />
            </Routes>
          </div>
        </main>

        {providerOpen && (
          <ProviderDialog onClose={() => setProviderOpen(false)} />
        )}

        <SidebarToggle />
      </SidebarProvider>
    </TooltipProvider>
  );
}
