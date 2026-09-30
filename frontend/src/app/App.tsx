import { useLayoutEffect, useRef, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { Toaster, useSonner } from "sonner";
import { Moon, Plus, Sun } from "lucide-react";
import { cn } from "@/lib/utils";
import { getTheme, setTheme, type Theme } from "@/lib/theme";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Sidebar, SidebarContent, SidebarFooter, SidebarGroup, SidebarGroupContent, SidebarHeader, SidebarMenu, SidebarMenuButton, SidebarMenuItem, SidebarProvider, SidebarTrigger, useSidebar } from "@/components/ui/sidebar";
import ChatPage from "@/features/chat/ChatPage";

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
  return <div ref={layer} popover="manual" className="pointer-events-none fixed inset-0 m-0 size-auto max-h-none max-w-none overflow-visible border-0 bg-transparent p-0 **:data-sonner-toaster:pointer-events-auto"><Toaster position="top-center" richColors /></div>;
}

function ThemeToggle() {
  const [theme, setThemeState] = useState<Theme>(getTheme());
  return <SidebarMenuButton onClick={() => {
    const next = theme === "light" ? "dark" : "light";
    setTheme(next);
    setThemeState(next);
  }} className="cursor-pointer">{theme === "light" ? <Moon aria-hidden /> : <Sun aria-hidden />}<span>{theme === "light" ? "切换暗色" : "切换浅色"}</span></SidebarMenuButton>;
}

function SidebarToggle() {
  const { state } = useSidebar();
  return <div className={cn("absolute top-8 z-20 transition-[left] duration-200 ease-linear", state === "collapsed" ? "left-6" : "left-[calc(var(--sidebar-width)-3.5rem)]")}><SidebarTrigger className="cursor-pointer" /></div>;
}

export default function App() {
  const [sessionKey, setSessionKey] = useState(0);
  return (
    <TooltipProvider>
      <GlobalToaster />
      <SidebarProvider className="relative h-screen">
        <Sidebar>
          <SidebarHeader className="px-6 pt-8 pb-6"><h1 className="font-display text-2xl font-light tracking-tight">Fit-Agent</h1><p className="text-xs text-muted-foreground">临时对话 · 模型由服务端环境配置</p></SidebarHeader>
          <SidebarContent><SidebarGroup className="px-3"><SidebarGroupContent><SidebarMenu><SidebarMenuItem><SidebarMenuButton onClick={() => setSessionKey((key) => key + 1)} className="cursor-pointer"><Plus aria-hidden /><span>新建会话</span></SidebarMenuButton></SidebarMenuItem></SidebarMenu></SidebarGroupContent></SidebarGroup></SidebarContent>
          <SidebarFooter className="px-3 pb-4"><ThemeToggle /></SidebarFooter>
        </Sidebar>
        <main className="relative min-h-0 flex-1 overflow-hidden"><div className="h-full"><Routes><Route path="/" element={<ChatPage key={sessionKey} />} /><Route path="*" element={<Navigate to="/" replace />} /></Routes></div></main>
        <SidebarToggle />
      </SidebarProvider>
    </TooltipProvider>
  );
}
