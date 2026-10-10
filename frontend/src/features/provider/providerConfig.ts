import { deleteProvider, getProvider, putProvider } from "@/lib/api";
import type {
  ModelApi,
  ProviderStatusWire,
  ProviderWriteBody,
} from "@/lib/contract";
import { PROVIDER_QUERY_KEY, queryClient } from "@/lib/query";

export const NO_VALID_MODEL_CONFIG = "未配置可用模型，无法发起新的运行。";

// 每次挂载重新读取生效配置。
export const providerQueryOptions = () => ({
  queryKey: PROVIDER_QUERY_KEY,
  queryFn: ({ signal }: { signal: AbortSignal }) => getProvider(signal),
  staleTime: 0,
});

export interface ProviderFormState {
  api: ModelApi | null;
  base_url: string;
  model: string;
  api_key: string;
  provider: string;
}

export function providerFormState(
  status: ProviderStatusWire | undefined,
): ProviderFormState {
  return {
    api: status?.api ?? null,
    base_url: status?.base_url ?? "",
    model: status?.model ?? "",
    api_key: status?.api_key ?? "",
    provider: status?.provider ?? "",
  };
}

export function hasValidProviderConfig(
  status: ProviderStatusWire | undefined,
): boolean {
  return (
    status !== undefined &&
    status.api !== null &&
    status.base_url !== null &&
    status.model !== null &&
    status.api_key !== null &&
    status.provider !== null
  );
}

// 在途运行使用受理时的配置快照，可继续接收 Steering。
export function providerAllowsInput(
  status: ProviderStatusWire | undefined,
  running: boolean,
): boolean {
  return running || hasValidProviderConfig(status);
}

// 变更前后取消读取，防止旧响应或期间新发起的读取覆盖变更结果。
async function mutateProvider(
  request: () => Promise<ProviderStatusWire>,
): Promise<ProviderStatusWire> {
  await queryClient.cancelQueries(
    { queryKey: PROVIDER_QUERY_KEY },
    { revert: false },
  );
  const status = await request();
  await queryClient.cancelQueries(
    { queryKey: PROVIDER_QUERY_KEY },
    { revert: false },
  );
  queryClient.setQueryData(PROVIDER_QUERY_KEY, status);
  return status;
}

export const saveProviderConfig = (body: ProviderWriteBody) =>
  mutateProvider(() => putProvider(body));

export const clearProviderConfig = () => mutateProvider(() => deleteProvider());
