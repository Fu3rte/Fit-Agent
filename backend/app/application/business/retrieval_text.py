import json
import unicodedata
from pathlib import Path

from app.domain.business.models import CatalogExercise

# 统一检索文本编码规则版本：T5 构建索引时随构建记录写入，变更字段顺序、
# 分隔符或规范化时递增，保证文档与查询可复现同一文本。
ENCODING_RULE_VERSION = "retrieval-text-2026-10-14.2"

# 冻结的字段顺序；空字段跳过，不产生占位符。
FIELD_ORDER = (
    "name",
    "aliases",
    "english_name",
    "equipment",
    "body_part",
    "target",
    "muscle_group",
    "steps_zh",
    "steps_en",
)
SEGMENT_SEPARATOR = "\n"

MAPPING_FIELDS = ("equipment", "body_part", "target", "muscle_group")

BACKEND_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MAPPINGS_PATH = BACKEND_ROOT / "dataset" / "action-field-mappings.json"
# 索引只使用已审阅内容：默认读取发布语料，候选语料仅作审计与候选评估来源。
DEFAULT_SUPPLEMENTS_PATH = (
    BACKEND_ROOT / "dataset" / "action-supplements.published.json"
)
CANDIDATE_SUPPLEMENTS_PATH = (
    BACKEND_ROOT / "dataset" / "action-supplements.candidates.json"
)


def normalize(text: str) -> str:
    # 文档与查询共用：NFKC 兼容归一、空白折叠为单空格、英文大小写折叠。
    # casefold 对中日韩字符为空操作，中文别名语义不变。
    collapsed = " ".join(unicodedata.normalize("NFKC", text).split())
    return collapsed.casefold()


def load_mappings(path: Path = DEFAULT_MAPPINGS_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_supplements(path: Path = DEFAULT_SUPPLEMENTS_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def supplement_index(document: dict) -> dict[str, dict]:
    return {entry["id"]: entry for entry in document["entries"]}


def mapped_field(mappings: dict, field: str, canonical: str) -> str:
    value = mappings["fields"][field].get(canonical)
    if value is None:
        raise ValueError(f"字段 {field} 缺少 canonical 映射: {canonical}")
    return f"{canonical} {value['zh']}"


def query_canonical(mappings: dict, field: str, term: str) -> set[str]:
    # 中文查询词到 canonical 的规范化；命中 canonical、zh 或词表任一项即返回。
    # 返回全部命中的 canonical 集合：同义词组（如 斜方肌 → trapezius/traps）与
    # 跨 canonical 共享词（如 器械 → leverage machine/sled machine）不会丢失。
    # 消费方拿到集合后自行选择过滤方式。
    target = normalize(term)
    hits: set[str] = set()
    for canonical, value in mappings["fields"][field].items():
        candidates = (canonical, value["zh"], *value["terms"])
        if any(normalize(item) == target for item in candidates):
            hits.add(canonical)
    return hits


def approved_aliases(supplement: dict | None) -> list[str]:
    if supplement is None:
        return []
    return [
        alias["text"]
        for alias in supplement["aliases"]
        if alias["review_status"] == "approved"
    ]


def approved_english_name(supplement: dict | None) -> str:
    # 英文名称与别名执行同一审阅门禁：仅 approved 进入统一文本。
    if supplement is None:
        return ""
    entry = supplement["english_name"]
    return entry["text"] if entry["review_status"] == "approved" else ""


def build_unified_text(
    exercise: CatalogExercise,
    supplement: dict | None,
    mappings: dict,
    *,
    include_unreviewed: bool = False,
) -> str:
    if include_unreviewed and supplement is not None:
        aliases = [alias["text"] for alias in supplement["aliases"]]
        english_name = supplement["english_name"]["text"]
    else:
        aliases = approved_aliases(supplement)
        english_name = approved_english_name(supplement)
    segments = {
        "name": exercise.name,
        "aliases": " ".join(sorted(aliases)),
        "english_name": english_name,
        "equipment": mapped_field(mappings, "equipment", exercise.equipment),
        "body_part": mapped_field(mappings, "body_part", exercise.body_part),
        "target": mapped_field(mappings, "target", exercise.target),
        "muscle_group": mapped_field(mappings, "muscle_group", exercise.muscle_group),
        "steps_zh": " ".join(exercise.steps.zh),
        "steps_en": " ".join(exercise.steps.en),
    }
    parts = [normalize(segments[field]) for field in FIELD_ORDER if segments[field]]
    return SEGMENT_SEPARATOR.join(part for part in parts if part)


def provenance(mappings: dict, document: dict) -> dict:
    # T5 复现同一文本所需的版本与哈希记录。
    return {
        "encoding_rule_version": ENCODING_RULE_VERSION,
        "field_order": list(FIELD_ORDER),
        "segment_separator": SEGMENT_SEPARATOR,
        "catalog_sha256": mappings["catalog"]["sha256"],
        "catalog_count": mappings["catalog"]["count"],
        "mappings_version": mappings["version"],
        "supplements_version": document["version"],
        "review_status": document["review_status"],
        "entry_count": len(document["entries"]),
    }
