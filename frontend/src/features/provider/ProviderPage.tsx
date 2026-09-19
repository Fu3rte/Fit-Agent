/**
 * /provider 模型配置：LLM Provider 连接信息的读取、整份覆盖保存与凭据清除。
 *
 * 契约 ``/api/provider``：GET 不回传 api_key 本体（只给 has_api_key），Key 输入框
 * 永不回填；PUT 整份覆盖三字段（未出现的字段后端写空串），保存始终提交表单当前值，
 * Key 留空即清除已配置凭据；DELETE 清空全部配置并返回空状态。
 * 环境变量 MODEL_* 只在服务端字段为空时回落，非敏感常量提示直接写在页面。
 */
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
import { deleteProvider, getProvider, putProvider } from "@/lib/api";
import type { ProviderWriteBody } from "@/lib/contract";

export default function ProviderPage() {
  const queryClient = useQueryClient();
  const provider = useQuery({ queryKey: ["provider"], queryFn: getProvider });

  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiKeyInput, setApiKeyInput] = useState("");

  const loaded = provider.data ?? null;
  useEffect(() => {
    if (!loaded) return;
    setBaseUrl(loaded.base_url);
    setModel(loaded.model);
    // GET 不含 Key 本体：输入框只在加载与刷新时保持空
    setApiKeyInput("");
  }, [loaded]);

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

  const submit = () => {
    save.mutate({
      api_key: apiKeyInput,
      base_url: baseUrl.trim(),
      model: model.trim(),
    });
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
            <p className="text-xs text-muted-foreground">
              留空保存将清除已配置的 Key；服务端不回传 Key 本体，输入框始终从空开始。
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-3">
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

          <p className="text-xs text-muted-foreground">
            配置落在服务端数据目录 provider.json；字段为空时由环境变量 MODEL_*
            回落。
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Run 边界</CardTitle>
          <CardDescription>服务端非敏感常量，只读展示。</CardDescription>
        </CardHeader>
        <CardContent>
          <ul className="space-y-1.5 text-sm text-muted-foreground">
            <li>单次请求超时：60 秒</li>
            <li>单次 Run 超时：180 秒</li>
            <li>每个 Run 最多请求：5 次</li>
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}
