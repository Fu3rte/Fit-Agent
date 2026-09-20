import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  deleteProvider,
  getProvider,
  putProvider,
  testProvider,
} from "@/lib/api";
import type {
  ProviderApiWire,
  ProviderStructuredOutputWire,
  ProviderWriteBody,
} from "@/lib/contract";

/** 客户端 transport 的中文标签（value 与后端 APIS 同集合） */
const API_LABELS: Record<ProviderApiWire, string> = {
  openai_compatible: "OpenAI 兼容（ChatOpenAI）",
  anthropic_messages: "Anthropic Messages（ChatAnthropic）",
};

const API_VALUES = Object.keys(API_LABELS) as ProviderApiWire[];

/** 结构化输出机制的中文标签（value 与后端 STRUCTURED_OUTPUTS 同集合） */
const STRUCTURED_OUTPUT_LABELS: Record<
  ProviderStructuredOutputWire,
  string
> = {
  json_schema: "JSON Schema",
  function_calling_strict: "Function Calling（strict）",
};

/** 各 transport 允许的结构化输出机制：与后端 STRUCTURED_OUTPUTS_BY_API 同集合 */
const STRUCTURED_OUTPUTS_BY_API: Record<
  ProviderApiWire,
  ProviderStructuredOutputWire[]
> = {
  openai_compatible: ["json_schema", "function_calling_strict"],
  anthropic_messages: ["json_schema"],
};

const DEFAULT_API: ProviderApiWire = "openai_compatible";
const DEFAULT_STRUCTURED_OUTPUT: ProviderStructuredOutputWire = "json_schema";

const STRUCTURED_OUTPUT_VALUES = Object.keys(
  STRUCTURED_OUTPUT_LABELS,
) as ProviderStructuredOutputWire[];

/** 线上两个协议字段可能缺字段或取未知值（旧后端响应）：只放行已知集合，其余回落服务端默认 */
const asApi = (value: unknown): ProviderApiWire =>
  API_VALUES.includes(value as ProviderApiWire)
    ? (value as ProviderApiWire)
    : DEFAULT_API;

const asStructuredOutput = (value: unknown): ProviderStructuredOutputWire =>
  STRUCTURED_OUTPUT_VALUES.includes(value as ProviderStructuredOutputWire)
    ? (value as ProviderStructuredOutputWire)
    : DEFAULT_STRUCTURED_OUTPUT;

/** 模型配置弹层：入口在侧栏底部，承载 Base URL、模型名、transport、结构化输出与 API Key 的读写 */
export function ProviderDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const provider = useQuery({ queryKey: ["provider"], queryFn: getProvider });

  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [api, setApi] = useState<ProviderApiWire>(DEFAULT_API);
  const [structuredOutput, setStructuredOutput] =
    useState<ProviderStructuredOutputWire>(DEFAULT_STRUCTURED_OUTPUT);

  // 只在服务端字段真的变化时回填：后台 refetch 与保存后的 setQueryData 不得清掉已输入的 Key
  useEffect(() => {
    if (!provider.data) return;
    setBaseUrl(provider.data.base_url);
    setModel(provider.data.model);
    setApi(asApi(provider.data.api));
    setStructuredOutput(asStructuredOutput(provider.data.structured_output));
  }, [
    provider.data?.base_url,
    provider.data?.model,
    provider.data?.api,
    provider.data?.structured_output,
  ]);

  // 换 transport 时把超出该 transport 能力的机制收回默认，避免提交后端必然拒绝的组合
  const allowedStructuredOutputs = STRUCTURED_OUTPUTS_BY_API[api];
  const effectiveStructuredOutput = allowedStructuredOutputs.includes(
    structuredOutput,
  )
    ? structuredOutput
    : DEFAULT_STRUCTURED_OUTPUT;

  const save = useMutation({
    mutationFn: (body: ProviderWriteBody) => putProvider(body),
    onSuccess: (data) => {
      queryClient.setQueryData(["provider"], data);
      toast.success("模型配置已保存");
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "模型配置保存失败"),
  });

  const clear = useMutation({
    mutationFn: () => deleteProvider(),
    onSuccess: (data) => {
      queryClient.setQueryData(["provider"], data);
      setApiKeyInput("");
      toast.success("凭据已清除");
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "凭据清除失败"),
  });

  const test = useMutation({
    mutationFn: (body: ProviderWriteBody) => testProvider(body),
    onSuccess: (data) => {
      if (data.ok) toast.success(`${data.message}（${data.latency_ms} ms）`);
      else toast.error(data.message);
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "模型测试失败"),
  });

  const hasApiKey = provider.data?.has_api_key ?? false;

  const formBody = (): ProviderWriteBody => ({
    api_key: apiKeyInput,
    base_url: baseUrl.trim(),
    model: model.trim(),
    api,
    structured_output: effectiveStructuredOutput,
  });

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        aria-describedby={undefined}
        className="w-[min(34rem,calc(100vw-2rem))] max-w-none gap-4 p-5"
      >
        <div className="min-w-0 pr-10">
          <DialogTitle className="font-medium tracking-tight">
            模型配置
          </DialogTitle>
          <p className="text-xs text-muted-foreground">
            LLM Provider 的 Base URL、模型名、客户端 transport、结构化输出方式与
            API Key；保存时 Api Key 留空则沿用已存的 Key，其余字段整份覆盖。
          </p>
        </div>

        {provider.isLoading ? (
          <p className="text-sm text-muted-foreground">加载模型配置…</p>
        ) : provider.isError ? (
          <p className="text-sm text-destructive">
            加载失败：{provider.error.message}，请关闭后重试。
          </p>
        ) : (
          <>
            <div className="space-y-2">
              <label
                htmlFor="provider-base-url"
                className="text-sm font-medium"
              >
                Base URL
              </label>
              <Input
                id="provider-base-url"
                value={baseUrl}
                placeholder="例：https://api.example.com/v1"
                onChange={(event) => setBaseUrl(event.target.value)}
              />
            </div>

            <div className="space-y-2">
              <label htmlFor="provider-api" className="text-sm font-medium">
                客户端 transport
              </label>
              <Select
                value={api}
                onValueChange={(value) => setApi(value as ProviderApiWire)}
              >
                <SelectTrigger
                  id="provider-api"
                  className="w-full"
                  aria-label="客户端 transport"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {API_VALUES.map((value) => (
                    <SelectItem key={value} value={value}>
                      {API_LABELS[value]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <label
                htmlFor="provider-structured-output"
                className="text-sm font-medium"
              >
                结构化输出方式
              </label>
              <Select
                value={effectiveStructuredOutput}
                onValueChange={(value) =>
                  setStructuredOutput(value as ProviderStructuredOutputWire)
                }
              >
                <SelectTrigger
                  id="provider-structured-output"
                  className="w-full"
                  aria-label="结构化输出方式"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {allowedStructuredOutputs.map((value) => (
                    <SelectItem key={value} value={value}>
                      {STRUCTURED_OUTPUT_LABELS[value]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <label htmlFor="provider-model" className="text-sm font-medium">
                模型名
              </label>
              <Input
                id="provider-model"
                value={model}
                placeholder="例：gpt-4o-mini"
                onChange={(event) => setModel(event.target.value)}
              />
            </div>

            <div className="space-y-2">
              <div className="flex items-center gap-3">
                <label
                  htmlFor="provider-api-key"
                  className="text-sm font-medium"
                >
                  API Key
                </label>
                <Badge variant={hasApiKey ? "default" : "secondary"}>
                  {hasApiKey ? "已配置" : "未配置"}
                </Badge>
              </div>
              <Input
                id="provider-api-key"
                type="text"
                value={apiKeyInput}
                placeholder="留空沿用已存的 Key"
                onChange={(event) => setApiKeyInput(event.target.value)}
              />
            </div>

            <div className="flex flex-wrap items-center justify-end gap-3">
              <Button
                variant="secondary"
                onClick={() => test.mutate(formBody())}
                disabled={test.isPending}
              >
                测试模型
              </Button>
              <Button
                onClick={() => save.mutate(formBody())}
                disabled={save.isPending}
              >
                保存配置
              </Button>
              <Button
                variant="destructive"
                onClick={() => clear.mutate()}
                disabled={clear.isPending}
              >
                清除凭据
              </Button>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
