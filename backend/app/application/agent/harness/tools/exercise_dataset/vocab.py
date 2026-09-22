# 动作数据集工具的词表与行模型：facet 把中文或英文查询词归一到数据集的规范英文取值，动作行只读地
# 承载数据集字段（数据集字段为准）。这里只维护"闭集 facet 的中英对照 + 检索用的确定性匹配"，开放集
# 的动作名依赖调用侧翻译，不在此收录。与工具同目录，供 store.py / tools.py 使用。

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: 可过滤的 facet 字段；动作名（开放集）与二级肌群（列表）不在可精确过滤的 facet 内。
FACET_FIELDS: tuple[str, ...] = ("body_part", "equipment", "target", "muscle_group")

# 规范英文（数据集里出现的全部取值，即下列字典的键）→ 中文：面向用户的答复展示与中文查询词归一的
# 权威对照。muscle_group 以短形为键，数据集里的长名记为别名。
BODY_PART_ZH: Mapping[str, str] = {
    "back": "背部",
    "cardio": "有氧",
    "chest": "胸部",
    "lower arms": "前臂",
    "lower legs": "小腿部",
    "neck": "颈部",
    "shoulders": "肩部",
    "upper arms": "上臂",
    "upper legs": "大腿",
    "waist": "腰腹",
}
EQUIPMENT_ZH: Mapping[str, str] = {
    "assisted": "助力",
    "band": "弹力带",
    "barbell": "杠铃",
    "body weight": "自重",
    "bosu ball": "波速球",
    "cable": "绳索",
    "dumbbell": "哑铃",
    "elliptical machine": "椭圆机",
    "ez barbell": "EZ 杆",
    "hammer": "锤铃",
    "kettlebell": "壶铃",
    "leverage machine": "器械",
    "medicine ball": "药球",
    "olympic barbell": "奥林匹克杠铃",
    "resistance band": "阻力带",
    "roller": "泡沫轴",
    "rope": "战绳",
    "skierg machine": "SkiErg 划雪机",
    "sled machine": "雪橇机",
    "smith machine": "史密斯机",
    "stability ball": "稳定球",
    "stationary bike": "动感单车",
    "stepmill machine": "登山机",
    "tire": "轮胎",
    "trap bar": "六角杠铃",
    "upper body ergometer": "上肢测功仪",
    "weighted": "负重",
    "wheel roller": "健腹轮",
}
TARGET_ZH: Mapping[str, str] = {
    "abductors": "外展肌群",
    "abs": "腹肌",
    "adductors": "内收肌群",
    "biceps": "肱二头肌",
    "calves": "小腿",
    "cardiovascular system": "心肺系统",
    "delts": "三角肌",
    "forearms": "前臂",
    "glutes": "臀肌",
    "hamstrings": "腘绳肌",
    "lats": "背阔肌",
    "levator scapulae": "肩胛提肌",
    "pectorals": "胸大肌",
    "quads": "股四头肌",
    "serratus anterior": "前锯肌",
    "spine": "脊柱",
    "traps": "斜方肌",
    "triceps": "肱三头肌",
    "upper back": "上背部",
}
MUSCLE_GROUP_ZH: Mapping[str, str] = {
    "abdominals": "腹部",
    "ankle stabilizers": "踝稳定肌",
    "ankles": "踝关节",
    "biceps": "肱二头肌",
    "calves": "小腿",
    "chest": "胸部",
    "core": "核心",
    "deltoids": "三角肌",
    "forearms": "前臂",
    "glutes": "臀肌",
    "hamstrings": "腘绳肌",
    "hands": "手部",
    "hip flexors": "髋屈肌",
    "lats": "背阔肌",
    "lower back": "下背部",
    "obliques": "腹斜肌",
    "quadriceps": "股四头肌",
    "rhomboids": "菱形肌",
    "rotator cuff": "肩袖",
    "shoulders": "肩部",
    "soleus": "比目鱼肌",
    "traps": "斜方肌",
    "triceps": "肱三头肌",
    "upper back": "上背部",
    "wrist extensors": "腕伸肌",
    "wrist flexors": "腕屈肌",
    "wrists": "腕关节",
}

#: 各 facet 的规范英文取值：中英对照表的键即为数据集出现过的全部规范取值。
BODY_PART_VALUES: frozenset[str] = frozenset(BODY_PART_ZH)
EQUIPMENT_VALUES: frozenset[str] = frozenset(EQUIPMENT_ZH)
TARGET_VALUES: frozenset[str] = frozenset(TARGET_ZH)
MUSCLE_GROUP_VALUES: frozenset[str] = frozenset(MUSCLE_GROUP_ZH)

# 数据集里出现、但折叠到规范码的长名：命中规范码时这些行一并召回。
MUSCLE_GROUP_ALIASES: Mapping[str, str] = {
    "latissimus dorsi": "lats",
    "trapezius": "traps",
}

_ZH: Mapping[str, Mapping[str, str]] = {
    "body_part": BODY_PART_ZH,
    "equipment": EQUIPMENT_ZH,
    "target": TARGET_ZH,
    "muscle_group": MUSCLE_GROUP_ZH,
}


class UnknownFacetValue(ValueError):
    """facet 取值无法归一到数据集规范英文：既非规范英文也非对照中文。"""


def _normalizer(zh: Mapping[str, str], aliases: Mapping[str, str]) -> dict[str, str]:
    """构造一个 facet 的 输入（小写）→ 规范英文 表：规范英文自映射、长名折叠、中文反查。"""
    table = {en: en for en in zh}
    table.update(aliases)
    table.update({cn: en for en, cn in zh.items()})
    return {key.casefold(): value for key, value in table.items()}


_NORMALIZERS: Mapping[str, dict[str, str]] = {
    "body_part": _normalizer(BODY_PART_ZH, {}),
    "equipment": _normalizer(EQUIPMENT_ZH, {}),
    "target": _normalizer(TARGET_ZH, {}),
    "muscle_group": _normalizer(MUSCLE_GROUP_ZH, MUSCLE_GROUP_ALIASES),
}


def normalize_facet(field: str, value: str) -> str:
    """把中文或英文 facet 取值归一为数据集的规范英文；无法归一时抛错，交回调用侧修正参数。"""
    canonical = _NORMALIZERS[field].get(value.strip().casefold())
    if canonical is None:
        raise UnknownFacetValue(
            f"{field} 取值 {value!r} 不在词表内；"
            f"可用中文：{'、'.join(sorted(_ZH[field].values()))}"
        )
    return canonical


def equivalent_values(canonical: str) -> frozenset[str]:
    """规范英文对应的数据集原始取值集合：折叠的长名与规范码一并召回。"""
    return frozenset({canonical}) | {
        alias for alias, canon in MUSCLE_GROUP_ALIASES.items() if canon == canonical
    }


def facet_label(field: str, canonical: str) -> str:
    """规范英文对应的中文标签；用于向用户展示检索结果。"""
    return _ZH[field][canonical]


def facet_options_zh(field: str) -> tuple[str, ...]:
    """一个 facet 的全部中文可选值（升序）：供工具参数描述列举，避免模型猜词表。"""
    return tuple(sorted(_ZH[field].values()))


@dataclass(frozen=True, slots=True)
class DatasetExercise:
    """动作数据集的一行：字段以数据集为准，动作名为英文，指导语与分步中英双全。"""

    id: str
    name: str
    category: str
    body_part: str
    equipment: str
    target: str
    muscle_group: str
    secondary_muscles: tuple[str, ...]
    instructions_zh: str
    instructions_en: str
    steps_zh: tuple[str, ...]
    steps_en: tuple[str, ...]

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "DatasetExercise":
        """把数据集 JSON 记录映射为领域行；缺字段即报错，不静默补默认。"""
        instructions = row["instructions"]
        steps = row["steps"]
        return cls(
            id=str(row["id"]),
            name=str(row["name"]),
            category=str(row["category"]),
            body_part=str(row["body_part"]),
            equipment=str(row["equipment"]),
            target=str(row["target"]),
            muscle_group=str(row["muscle_group"]),
            secondary_muscles=tuple(str(m) for m in row["secondary_muscles"]),
            instructions_zh=str(instructions["zh"]),
            instructions_en=str(instructions["en"]),
            steps_zh=tuple(str(s) for s in steps["zh"]),
            steps_en=tuple(str(s) for s in steps["en"]),
        )

    def matches_text(self, needle: str) -> bool:
        """确定性文本匹配：对英文动作名做子串命中（大小写无关）。"""
        return needle.casefold() in self.name.casefold()

    def search_view(self) -> dict[str, Any]:
        """紧凑视图：身份、动作名与中英 facet 标签，够模型挑选后再取详情。"""
        return {
            "id": self.id,
            "name": self.name,
            "body_part": self.body_part,
            "body_part_zh": facet_label("body_part", self.body_part),
            "equipment": self.equipment,
            "equipment_zh": facet_label("equipment", self.equipment),
            "target": self.target,
            "target_zh": facet_label("target", self.target),
            "muscle_group": self.muscle_group,
            "muscle_group_zh": facet_label("muscle_group", self.muscle_group),
        }

    def detail_view(self) -> dict[str, Any]:
        """详情视图：紧凑视图的全部字段 ＋ 二级肌群 ＋ 中英指导语与分步。"""
        return {
            **self.search_view(),
            "secondary_muscles": list(self.secondary_muscles),
            "instructions": {"zh": self.instructions_zh, "en": self.instructions_en},
            "steps": {"zh": list(self.steps_zh), "en": list(self.steps_en)},
        }
