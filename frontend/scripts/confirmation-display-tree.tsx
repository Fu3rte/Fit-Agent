// 确认展示检查的渲染树夹具：器械目录走应用级查询缓存，夹具在渲染前把目录落到缓存并同源完成服务端渲染。
// 提供者层级与应用根一致：查询缓存与提示词上下文均由上层提供。
import { type ComponentProps } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClientProvider } from "@tanstack/react-query";
import { EXERCISE_EQUIPMENT_QUERY_KEY, queryClient } from "@/lib/query";
import { TooltipProvider } from "@/components/ui/tooltip";
import ChatTranscript from "@/features/chat/components/ChatTranscript";

export function primeEquipment(): void {
  queryClient.setQueryData(EXERCISE_EQUIPMENT_QUERY_KEY, {});
}

export function renderTranscript(
  props: ComponentProps<typeof ChatTranscript>,
): string {
  return renderToStaticMarkup(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <ChatTranscript {...props} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}
