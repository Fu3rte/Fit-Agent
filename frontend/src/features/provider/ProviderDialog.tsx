import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
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
import { testProvider } from "@/lib/api";
import { parseProviderWriteBody } from "@/lib/business";
import type { ModelApi, ProviderWriteBody } from "@/lib/contract";
import {
  clearProviderConfig,
  providerFormState,
  providerQueryOptions,
  saveProviderConfig,
} from "./providerConfig";

const API_LABELS: Record<ModelApi, string> = {
  "openai-completions": "OpenAI Completions",
  "anthropic-messages": "Anthropic Messages",
};

const API_VALUES = Object.keys(API_LABELS) as ModelApi[];

export function ProviderDialog({ onClose }: { onClose: () => void }) {
  const provider = useQuery(providerQueryOptions());

  const [form, setForm] = useState(() => providerFormState(provider.data));
  const patch = (change: Partial<typeof form>) =>
    setForm((current) => ({ ...current, ...change }));

  // 按字段值变化回填，避免相同数据的刷新重置表单。
  useEffect(() => {
    if (provider.data === undefined) return;
    setForm(providerFormState(provider.data));
  }, [
    provider.data?.api,
    provider.data?.base_url,
    provider.data?.model,
    provider.data?.api_key,
    provider.data?.provider,
  ]);

  const save = useMutation({
    mutationFn: saveProviderConfig,
    onSuccess: () => toast.success("模型配置已保存"),
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "模型配置保存失败"),
  });

  const clear = useMutation({
    mutationFn: clearProviderConfig,
    onSuccess: () => {
      patch({ api: null, base_url: "", model: "", provider: "", api_key: "" });
      toast.success("配置已清除");
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "配置清除失败"),
  });

  const test = useMutation({
    mutationFn: testProvider,
    onSuccess: (result) => {
      const detail = `${result.message}（${result.latency_ms} ms）`;
      if (result.ok) toast.success(detail);
      else toast.error(detail);
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "模型测试失败"),
  });

  const busy = save.isPending || clear.isPending || test.isPending;

  const formBody = (): ProviderWriteBody | null => {
    try {
      return parseProviderWriteBody({
        api: form.api,
        base_url: form.base_url.trim(),
        model: form.model.trim(),
        api_key: form.api_key,
        provider: form.provider,
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "模型配置校验失败");
      return null;
    }
  };

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
              <label htmlFor="provider-api" className="text-sm font-medium">
                协议
              </label>
              <Select
                value={form.api ?? undefined}
                onValueChange={(value) => patch({ api: value as ModelApi })}
              >
                <SelectTrigger
                  id="provider-api"
                  className="w-full"
                  aria-label="协议"
                >
                  <SelectValue placeholder="选择协议" />
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
                htmlFor="provider-base-url"
                className="text-sm font-medium"
              >
                Base URL
              </label>
              <Input
                id="provider-base-url"
                value={form.base_url}
                onChange={(event) => patch({ base_url: event.target.value })}
              />
            </div>

            <div className="space-y-2">
              <label htmlFor="provider-model" className="text-sm font-medium">
                模型 ID
              </label>
              <Input
                id="provider-model"
                value={form.model}
                onChange={(event) => patch({ model: event.target.value })}
              />
            </div>

            <div className="space-y-2">
              <label
                htmlFor="provider-provider"
                className="text-sm font-medium"
              >
                服务标识（provider）
              </label>
              <Input
                id="provider-provider"
                value={form.provider}
                onChange={(event) => patch({ provider: event.target.value })}
              />
            </div>

            <div className="space-y-2">
              <label
                htmlFor="provider-api-key"
                className="text-sm font-medium"
              >
                API Key
              </label>
              <Input
                id="provider-api-key"
                type="text"
                value={form.api_key}
                onChange={(event) => patch({ api_key: event.target.value })}
              />
            </div>

            <div className="flex flex-wrap items-center justify-end gap-3">
              <Button
                variant="secondary"
                onClick={() => {
                  const body = formBody();
                  if (body !== null) test.mutate(body);
                }}
                disabled={busy}
              >
                测试模型
              </Button>
              <Button
                onClick={() => {
                  const body = formBody();
                  if (body !== null) save.mutate(body);
                }}
                disabled={busy}
              >
                保存配置
              </Button>
              <Button
                variant="destructive"
                onClick={() => clear.mutate()}
                disabled={busy}
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
