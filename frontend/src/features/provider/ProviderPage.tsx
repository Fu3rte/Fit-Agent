import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { deleteProvider, getProvider, putProvider, testProvider } from "@/lib/api";
import type { ProviderWriteBody } from "@/lib/contract";

export default function ProviderPage() {
  const queryClient = useQueryClient();
  const provider = useQuery({ queryKey: ["provider"], queryFn: getProvider });

  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiKeyInput, setApiKeyInput] = useState("");

  const loaded = provider.data ?? null;
  // 只在服务端字段真的变化时回填：后台 refetch 与保存后的 setQueryData 不得清掉已输入的 Key
  useEffect(() => {
    if (!loaded) return;
    setBaseUrl(loaded.base_url);
    setModel(loaded.model);
  }, [loaded?.base_url, loaded?.model]);

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

  if (provider.isLoading) {
    return <p className="mt-10 text-sm text-muted-foreground">加载模型配置…</p>;
  }
  if (provider.isError) {
    return (
      <p className="mt-10 text-sm text-destructive">
        加载失败：{provider.error.message}，请刷新重试。
      </p>
    );
  }

  const hasApiKey = loaded?.has_api_key ?? false;

  const formBody = (): ProviderWriteBody => ({
    api_key: apiKeyInput,
    base_url: baseUrl.trim(),
    model: model.trim(),
  });

  const submit = () => {
    save.mutate(formBody());
  };

  return (
    <div className="mx-auto max-w-3xl space-y-6 p-6">
      <Card>
        <CardHeader>
          <CardTitle>模型配置</CardTitle>
          <CardDescription>
            LLM Provider 的 Base URL、模型名与 API Key；整份覆盖保存。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="space-y-2">
            <label htmlFor="provider-base-url" className="text-sm font-medium">
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
              <label htmlFor="provider-api-key" className="text-sm font-medium">
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

          <div className="flex flex-wrap justify-end items-center gap-3">
            <Button
              variant="secondary"
              onClick={() => test.mutate(formBody())}
              disabled={test.isPending}
            >
              测试模型
            </Button>
            <Button onClick={submit} disabled={save.isPending}>
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
        </CardContent>
      </Card>
    </div>
  );
}
