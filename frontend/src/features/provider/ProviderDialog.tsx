import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  deleteProvider,
  getProvider,
  putProvider,
  testProvider,
} from "@/lib/api";
import type { ProviderWriteBody } from "@/lib/contract";

/** 模型配置弹层：入口在侧栏底部，承载 Base URL、模型名与 API Key 的整份覆盖读写 */
export function ProviderDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const ref = useRef<HTMLDialogElement>(null);
  const provider = useQuery({ queryKey: ["provider"], queryFn: getProvider });

  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiKeyInput, setApiKeyInput] = useState("");

  useEffect(() => {
    ref.current?.showModal();
  }, []);

  // 只在服务端字段真的变化时回填：后台 refetch 与保存后的 setQueryData 不得清掉已输入的 Key
  useEffect(() => {
    if (!provider.data) return;
    setBaseUrl(provider.data.base_url);
    setModel(provider.data.model);
  }, [provider.data?.base_url, provider.data?.model]);

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
  });

  return (
    <dialog
      ref={ref}
      aria-labelledby="provider-dialog-title"
      onClose={onClose}
      onClick={(event) => {
        if (event.target === ref.current) ref.current.close();
      }}
      className="m-auto w-[min(34rem,calc(100vw-2rem))] rounded-xl border bg-card p-0 text-card-foreground backdrop:bg-black/60"
    >
      <div className="flex flex-col gap-4 p-5">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2
              id="provider-dialog-title"
              className="font-display text-lg font-medium tracking-tight"
            >
              模型配置
            </h2>
            <p className="text-xs text-muted-foreground">
              LLM Provider 的 Base URL、模型名与 API Key；整份覆盖保存。
            </p>
          </div>
          <Button
            variant="outline"
            size="icon"
            className="size-7"
            aria-label="关闭模型配置"
            onClick={() => ref.current?.close()}
          >
            <X className="size-3.5" />
          </Button>
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
                type="password"
                autoComplete="new-password"
                value={apiKeyInput}
                placeholder="输入新 Key 以写入"
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
      </div>
    </dialog>
  );
}
