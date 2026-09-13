import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Cpu, FolderOpen, KeyRound } from "lucide-react";
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
import { deleteApiKey, getProvider, putApiKey } from "@/lib/api";

/** /settings 设置：Provider 与 Key 管理、当前模型标识、数据目录 */
export default function SettingsPage() {
  const queryClient = useQueryClient();
  const provider = useQuery({ queryKey: ["provider"], queryFn: getProvider });

  const [keyInput, setKeyInput] = useState("");
  const [saving, setSaving] = useState(false);
  /* 删除二次确认：两段式按钮，无弹窗组件 */
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const hasKey = provider.data?.has_api_key ?? false;

  const saveKey = async () => {
    const value = keyInput.trim();
    if (!value) {
      toast.error("请先输入 API Key");
      return;
    }
    setSaving(true);
    try {
      await putApiKey(value);
      toast.success("Key 已保存");
      setKeyInput("");
      await queryClient.invalidateQueries({ queryKey: ["provider"] });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const removeKey = async () => {
    if (!confirmingDelete) {
      setConfirmingDelete(true);
      return;
    }
    setSaving(true);
    try {
      await deleteApiKey();
      toast.success("Key 已删除");
      setConfirmingDelete(false);
      setKeyInput("");
      await queryClient.invalidateQueries({ queryKey: ["provider"] });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "删除失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mx-auto w-full max-w-3xl px-6">
      <header className="pt-10 pb-6">
        <h2 className="font-display text-3xl font-light tracking-tight">
          设置
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Provider 配置与 Key 管理、当前模型、数据目录。
        </p>
      </header>

      <div className="flex flex-col gap-4 pb-10">
        {/* Provider 卡：界面任何位置不出现明文 Key，仅 has_api_key 徽章 */}
        <Card>
          <CardHeader className="flex-row items-start justify-between gap-4">
            <div className="flex flex-col gap-1.5">
              <CardTitle className="flex items-center gap-2">
                <KeyRound className="size-4 text-muted-foreground" />
                模型 Provider
              </CardTitle>
              <CardDescription>
                {provider.data?.protocol ?? "—"} ·{" "}
                {provider.data?.base_url ?? "—"}
              </CardDescription>
            </div>
            <Badge variant={hasKey ? "default" : "secondary"}>
              {hasKey ? "已配置" : "未配置"}
            </Badge>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <div className="flex items-center gap-2">
              <Input
                type="password"
                value={keyInput}
                onChange={(e) => setKeyInput(e.target.value)}
                placeholder={
                  hasKey
                    ? "输入新 Key 以替换（不回显）"
                    : "录入 API Key（保存后不回显）"
                }
                autoComplete="off"
                className="flex-1"
                onKeyDown={(e) => {
                  if (e.key === "Enter") void saveKey();
                }}
              />
              <Button onClick={() => void saveKey()} disabled={saving}>
                {hasKey ? "替换 Key" : "保存 Key"}
              </Button>
              {hasKey && (
                <Button
                  variant={confirmingDelete ? "destructive" : "outline"}
                  onClick={() => void removeKey()}
                  onBlur={() => setConfirmingDelete(false)}
                  disabled={saving}
                >
                  {confirmingDelete ? "确认删除？" : "删除"}
                </Button>
              )}
            </div>
            <p className="text-xs text-muted-foreground">
              Key 保存在本地数据库，仅经 has_api_key
              徽章体现，不进入界面或日志。
            </p>
          </CardContent>
        </Card>

        {/* 当前模型卡：明确展示本地 / 云端（PRD 5.1） */}
        <Card>
          <CardHeader className="flex-row items-start justify-between gap-4">
            <div className="flex flex-col gap-1.5">
              <CardTitle className="flex items-center gap-2">
                <Cpu className="size-4 text-muted-foreground" />
                当前模型
              </CardTitle>
              <CardDescription>
                {provider.data?.model.name ?? "—"}
              </CardDescription>
            </div>
            <Badge
              variant={
                provider.data?.model.deployment === "cloud"
                  ? "secondary"
                  : "outline"
              }
            >
              {provider.data?.model.deployment === "cloud" ? "云端" : "本地"}
            </Badge>
          </CardHeader>
          {provider.data?.model.deployment === "cloud" && (
            <CardContent>
              <p className="text-xs text-muted-foreground">
                使用云端模型时，训练与健康相关文本将发送至服务端。
              </p>
            </CardContent>
          )}
        </Card>

        {/* 数据目录卡：freeze 契约不投影 data_dir（F6-02d），只给指引文案，不编造路径 */}
        <Card>
          <CardHeader>
            <div className="flex flex-col gap-1.5">
              <CardTitle className="flex items-center gap-2">
                <FolderOpen className="size-4 text-muted-foreground" />
                数据目录
              </CardTitle>
              <CardDescription>见数据目录配置</CardDescription>
            </div>
          </CardHeader>
        </Card>
      </div>
    </div>
  );
}
