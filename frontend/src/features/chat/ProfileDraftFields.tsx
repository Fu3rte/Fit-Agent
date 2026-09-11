import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { Profile, ProfileDraftPayload, Restriction } from "@/lib/contract";
import { ListField, selectClass } from "./draftFields";
import { ProfileFactsFields } from "./ProfileFactsFields";

const RESTRICTION_SCOPES: Array<{
  value: Restriction["scope"];
  label: string;
}> = [
  { value: "specific_action", label: "具体动作" },
  { value: "movement_pattern", label: "动作模式" },
];

/**
 * 草稿卡 3a／3b（读取时派生展示，不新增存储字段）：
 * 3a——有身体情况且本次提议了限制时，给出理由链（本次身体情况集合 → 本次限制集合）；
 * 3b——有身体情况但本次未提议限制时显式展示结论，不静默放过。
 * **集合级，不建立「条件 i → 限制 j」的逐条对应**（2026-09-11 用户拍板）：逐条对应要么靠医学推断
 * （禁止），要么靠新增只读字段（方案排除）；理由链表达的是「本次草稿的上下文」，不是因果论断。
 * 理由链只解释本次提议：不写入 ActionRestriction（不加 reason／source），不进正式档案。
 */
function RestrictionRationale({
  bodyConditions,
  restrictions,
}: {
  bodyConditions?: string[];
  restrictions?: Restriction[];
}) {
  // 无身体情况报告（未收集／明确无）时没有本次提议可解释；限制未收集时不声称结论
  if (bodyConditions === undefined || bodyConditions.length === 0) return null;
  if (restrictions === undefined) return null;
  const reported = bodyConditions.join("、");
  if (restrictions.length === 0)
    return (
      <p className="rounded-md border border-border bg-muted/40 p-2 text-xs text-muted-foreground">
        已记录身体情况，本次未提议动作限制
      </p>
    );
  return (
    <div className="rounded-md border border-border bg-muted/40 p-2 text-xs">
      <p className="text-muted-foreground">
        本次提议理由（集合级，不逐条对应）
      </p>
      <ul className="mt-1 space-y-0.5">
        <li>已记录身体情况：「{reported}」</li>
        <li>
          本次提议限制：
          {restrictions
            .map(
              (r) =>
                `${r.name === "" ? "待填写" : r.name}（${r.scope === "specific_action" ? "具体动作" : "动作模式"}）`,
            )
            .join("、")}
        </li>
      </ul>
    </div>
  );
}

/**
 * 档案草稿结构化卡：PRD §5.2 六类事实 + 必填体重，全部只改待确认草稿（不自动提交）。
 * 身体情况只保存用户报告原文（内联纠错只改待确认草稿）；分类（六类安全症状）在
 * 读取时由安全复核完成，草稿卡不做医学判断、不据此阻断。
 */
export function ProfileDraftFields({
  payload,
  disabled,
  onChange,
}: {
  payload: ProfileDraftPayload;
  disabled: boolean;
  onChange: (payload: ProfileDraftPayload) => void;
}) {
  const [newEquipment, setNewEquipment] = useState("");
  const [newRestriction, setNewRestriction] = useState("");
  const [newBodyCondition, setNewBodyCondition] = useState("");
  const [newRestrictionScope, setNewRestrictionScope] =
    useState<Restriction["scope"]>("specific_action");

  const prof = payload.profile;
  const equipment = prof.equipment;
  const restrictions = payload.restrictions;
  const bodyConditions = prof.body_conditions;

  const patch = (p: Partial<Profile>) =>
    onChange({ ...payload, profile: { ...prof, ...p } });
  const patchRestriction = (i: number, p: Partial<Restriction>) =>
    onChange({
      ...payload,
      restrictions: (restrictions ?? []).map((r, j) =>
        j === i ? { ...r, ...p } : r,
      ),
    });
  const addRestriction = () => {
    const name = newRestriction.trim();
    if (name === "") return;
    onChange({
      ...payload,
      restrictions: [
        ...(restrictions ?? []),
        { name, scope: newRestrictionScope },
      ],
    });
    setNewRestriction("");
  };

  return (
    <div className="mt-3 space-y-2.5 rounded-lg border border-border bg-muted/30 p-3">
      <p className="text-xs font-medium">
        建档事实（八项，均必填；修改后需提交纠错）
      </p>

      <ProfileFactsFields prof={prof} disabled={disabled} patch={patch} />

      <ListField label="可用器械">
        {equipment === undefined ? (
          <p className="text-muted-foreground">尚未收集</p>
        ) : (
          <>
            {equipment.length === 0 && (
              <p className="text-muted-foreground">无器械（用户明确说明）</p>
            )}
            {equipment.map((name, i) => (
              <div key={i} className="flex flex-wrap items-center gap-1.5">
                <Input
                  value={name}
                  disabled={disabled}
                  onChange={(e) =>
                    patch({
                      equipment: equipment.map((n, j) =>
                        j === i ? e.target.value : n,
                      ),
                    })
                  }
                  className="h-7 w-40 text-xs"
                  aria-label={`可用器械 ${i + 1}`}
                />
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 px-2 text-xs"
                  disabled={disabled}
                  onClick={() =>
                    patch({
                      equipment: equipment.filter((_, j) => j !== i),
                    })
                  }
                  aria-label={`删除器械 ${name === "" ? i + 1 : name}`}
                >
                  删除
                </Button>
              </div>
            ))}
            {!disabled && (
              <div className="flex flex-wrap items-center gap-1.5">
                <Input
                  value={newEquipment}
                  onChange={(e) => setNewEquipment(e.target.value)}
                  placeholder="如 杠铃"
                  className="h-7 w-40 text-xs"
                  aria-label="新增器械名称"
                />
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 px-2 text-xs"
                  disabled={newEquipment.trim() === ""}
                  onClick={() => {
                    patch({
                      equipment: [...equipment, newEquipment.trim()],
                    });
                    setNewEquipment("");
                  }}
                  aria-label="添加器械"
                >
                  添加
                </Button>
              </div>
            )}
          </>
        )}
      </ListField>

      <ListField label="动作限制（当前有效，仅两种粒度）">
        {restrictions === undefined ? (
          <p className="text-muted-foreground">尚未收集</p>
        ) : (
          <>
            {restrictions.length === 0 && (
              <p className="text-muted-foreground">无（用户明确说明）</p>
            )}
            {restrictions.map((r, i) => (
              <div key={i} className="flex flex-wrap items-center gap-1.5">
                <Input
                  value={r.name}
                  disabled={disabled}
                  onChange={(e) =>
                    patchRestriction(i, {
                      name: e.target.value,
                    })
                  }
                  className="h-7 w-40 text-xs"
                  aria-label={`限制 ${i + 1} 名称`}
                />
                <select
                  aria-label={`限制 ${i + 1} 粒度`}
                  className={selectClass}
                  value={r.scope}
                  disabled={disabled}
                  onChange={(e) =>
                    patchRestriction(i, {
                      scope: e.target.value as Restriction["scope"],
                    })
                  }
                >
                  {RESTRICTION_SCOPES.map((s) => (
                    <option key={s.value} value={s.value}>
                      {s.label}
                    </option>
                  ))}
                </select>
                {r.note && (
                  <span
                    className="max-w-56 truncate text-muted-foreground"
                    title={r.note}
                  >
                    {r.note}
                  </span>
                )}
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 px-2 text-xs"
                  disabled={disabled}
                  onClick={() =>
                    onChange({
                      ...payload,
                      restrictions: restrictions.filter((_, j) => j !== i),
                    })
                  }
                  aria-label={`删除限制 ${r.name === "" ? i + 1 : r.name}`}
                >
                  删除
                </Button>
              </div>
            ))}
            {!disabled && (
              <div className="flex flex-wrap items-center gap-1.5">
                <Input
                  value={newRestriction}
                  onChange={(e) => setNewRestriction(e.target.value)}
                  placeholder="如 颈后推举"
                  className="h-7 w-40 text-xs"
                  aria-label="新增限制名称"
                />
                <select
                  aria-label="新增限制粒度"
                  className={selectClass}
                  value={newRestrictionScope}
                  onChange={(e) =>
                    setNewRestrictionScope(
                      e.target.value as Restriction["scope"],
                    )
                  }
                >
                  {RESTRICTION_SCOPES.map((s) => (
                    <option key={s.value} value={s.value}>
                      {s.label}
                    </option>
                  ))}
                </select>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 px-2 text-xs"
                  disabled={newRestriction.trim() === ""}
                  onClick={addRestriction}
                  aria-label="添加限制"
                >
                  添加
                </Button>
              </div>
            )}
          </>
        )}
      </ListField>

      <ListField label="身体情况（用户报告原文；可内联纠错）">
        {bodyConditions === undefined ? (
          <p className="text-muted-foreground">尚未收集</p>
        ) : (
          <>
            {bodyConditions.length === 0 && (
              <p className="text-muted-foreground">无（用户明确说明）</p>
            )}
            {bodyConditions.map((c, i) => (
              <div key={i} className="flex flex-wrap items-center gap-1.5">
                <Input
                  value={c}
                  disabled={disabled}
                  onChange={(e) =>
                    patch({
                      body_conditions: bodyConditions.map((n, j) =>
                        j === i ? e.target.value : n,
                      ),
                    })
                  }
                  className="h-7 w-64 text-xs"
                  aria-label={`身体情况 ${i + 1}`}
                />
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 px-2 text-xs"
                  disabled={disabled}
                  onClick={() =>
                    patch({
                      body_conditions: bodyConditions.filter((_, j) => j !== i),
                    })
                  }
                  aria-label={`删除身体情况 ${c === "" ? i + 1 : c}`}
                >
                  删除
                </Button>
              </div>
            ))}
            {!disabled && (
              <div className="flex flex-wrap items-center gap-1.5">
                <Input
                  value={newBodyCondition}
                  onChange={(e) => setNewBodyCondition(e.target.value)}
                  placeholder="如 深蹲时膝盖锐痛"
                  className="h-7 w-64 text-xs"
                  aria-label="新增身体情况"
                />
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7 px-2 text-xs"
                  disabled={newBodyCondition.trim() === ""}
                  onClick={() => {
                    patch({
                      body_conditions: [
                        ...bodyConditions,
                        newBodyCondition.trim(),
                      ],
                    });
                    setNewBodyCondition("");
                  }}
                  aria-label="添加身体情况"
                >
                  添加
                </Button>
              </div>
            )}
          </>
        )}
      </ListField>

      <RestrictionRationale
        bodyConditions={bodyConditions}
        restrictions={restrictions}
      />
    </div>
  );
}
