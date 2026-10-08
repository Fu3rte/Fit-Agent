import { QueryClient } from "@tanstack/react-query";

/** 应用级查询缓存（§11.9）：页面之外的事件处理同样需要使业务查询失效 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, retry: 1 },
  },
});

/** 训练记录查询前缀（workout-http-sse-contract §2、§3）：列表与详情共用，保存落定后整体失效 */
export const WORKOUT_QUERY_KEY = ["workout"] as const;
