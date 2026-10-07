import { useQuery } from "@tanstack/react-query";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { getProfile } from "@/lib/api";
import ProfileFields from "./ProfileFields";

/**
 * 个人画像页（§11.9、PRODUCT.md §3.2）：只展示 GET /api/profile 的已保存画像；
 * 建档与修改在对话中完整展示并经自然语言确认后保存，保存落定使本查询失效重取。读取失败保留已有展示。
 */
export default function ProfilePage() {
  const profile = useQuery({
    queryKey: ["profile"],
    queryFn: ({ signal }) => getProfile(signal),
  });

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto w-full max-w-4xl px-6 py-10">
        {profile.isError && (
          <p role="alert" className="mb-4 text-sm text-destructive">
            {profile.error.message}
          </p>
        )}
        {profile.data !== undefined && (
          <Card>
            <CardHeader>
              <CardTitle>个人画像</CardTitle>
              <CardDescription>
                {profile.data.version === null
                  ? "尚未建档"
                  : `版本 ${profile.data.version}`}
              </CardDescription>
            </CardHeader>
            {profile.data.content !== null && (
              <CardContent>
                <ProfileFields content={profile.data.content} />
              </CardContent>
            )}
          </Card>
        )}
      </div>
    </div>
  );
}
