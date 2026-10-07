import { QueryClient } from "@tanstack/react-query";

/** 应用级查询缓存（§11.9）：页面之外的事件处理同样需要使业务查询失效 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, retry: 1 },
  },
});
