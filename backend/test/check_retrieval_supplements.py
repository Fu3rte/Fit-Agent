import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from jsonschema import Draft202012Validator

from app.application.business import retrieval_text as rt
from app.application.business.catalog import DEFAULT_ACTIONS_PATH, Catalog

DATASET = Path(__file__).resolve().parents[1] / "dataset"

SUPPLEMENTS = DATASET / "action-supplements.candidates.json"
SUPPLEMENTS_SCHEMA = DATASET / "action-supplements.schema.json"
MAPPINGS = DATASET / "action-field-mappings.json"
MAPPINGS_SCHEMA = DATASET / "action-field-mappings.schema.json"
QUERIES = DATASET / "action-retrieval-queries.json"
QUERIES_SCHEMA = DATASET / "action-retrieval-queries.schema.json"
REVIEWS = DATASET / "action-supplement-reviews.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_schema(document: dict, schema_path: Path) -> None:
    Draft202012Validator(load(schema_path)).validate(document)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_catalog(catalog: Catalog) -> dict:
    records = load(DEFAULT_ACTIONS_PATH)
    sha = hashlib.sha256(DEFAULT_ACTIONS_PATH.read_bytes()).hexdigest()
    ids = [record["id"] for record in records]
    assert len(ids) == len(set(ids)) == len(catalog) == 1321, "目录数量或唯一 ID 不符"
    assert all(len(i) == 4 and i.isdigit() for i in ids), "ID 必须为 4 位零填充字符串"
    by_field = {
        field: Counter(record[field] for record in records)
        for field in rt.MAPPING_FIELDS
    }
    return {"sha256": sha, "ids": set(ids), "records": records, "by_field": by_field}


def check_supplements(document: dict, catalog_ids: set[str]) -> None:
    validate_schema(document, SUPPLEMENTS_SCHEMA)
    entries = document["entries"]
    ids = [entry["id"] for entry in entries]
    assert len(ids) == len(set(ids)), "补充条目 ID 重复"
    assert set(ids) == catalog_ids, "补充条目未覆盖目录全部 ID"
    assert document["catalog"]["count"] == len(catalog_ids)
    assert document["review_status"] == "candidate", "当前交付应为候选状态"
    assert document["upstream"]["license"] == "MIT"

    alias_texts = Counter()
    families = set()
    with_alias = 0
    for entry in entries:
        assert entry["english_name"]["text"].strip(), f"{entry['id']} 英文名称为空"
        assert entry["english_name"]["source"].startswith("upstream:"), "英文名称需记录上游来源"
        assert entry["english_name"]["review_status"] == "candidate"
        if entry["aliases"]:
            with_alias += 1
        for alias in entry["aliases"]:
            alias_texts[alias["text"]] += 1
            assert alias["text"].strip() == alias["text"], f"{entry['id']} 别名首尾空白"
            assert alias["review_status"] == "candidate"
            assert alias["source"].startswith("subagent:")
            if alias["family"] is not None:
                families.add(alias["family"])
    one_to_many = sum(1 for count in alias_texts.values() if count > 1)
    print(
        f"补充数据：动作 {len(ids)}，有俗称动作 {with_alias}，无补充动作 {len(ids) - with_alias}，"
        f"别名 {sum(alias_texts.values())}，家族 {len(families)}，一对多别名 {one_to_many}"
    )


def check_mappings(document: dict, catalog: dict) -> None:
    validate_schema(document, MAPPINGS_SCHEMA)
    assert document["catalog"]["sha256"] == catalog["sha256"], "映射文件目录哈希不一致"
    synonym: dict[tuple[str, str], int] = {}
    for index, group in enumerate(document.get("synonym_groups", [])):
        field, *canonicals = group
        for canonical in canonicals:
            assert canonical in document["fields"][field], f"同义词组引用未知值 {field}.{canonical}"
            synonym[(field, canonical)] = index
    for field in rt.MAPPING_FIELDS:
        mapped = document["fields"][field]
        assert set(mapped) == set(catalog["by_field"][field]), f"{field} 映射与目录不一致"
        grouped: dict[str, list[str]] = defaultdict(list)
        for canonical, value in mapped.items():
            assert value["zh"].strip(), f"{field}.{canonical} 中文为空"
            for term in (canonical, value["zh"], *value["terms"]):
                grouped[rt.normalize(term)].append(canonical)
        for norm, canonicals in grouped.items():
            distinct = sorted(set(canonicals))
            if len(distinct) <= 1:
                continue
            families = {synonym.get((field, canonical)) for canonical in distinct}
            assert None not in families and len(families) == 1, (
                f"{field} 未声明的规范化碰撞 {norm!r}: {distinct}"
            )
    print("字段映射：四字段 canonical 全覆盖，规范化碰撞仅限已声明同义词组")


def check_queries(document: dict, catalog: dict) -> None:
    validate_schema(document, QUERIES_SCHEMA)
    queries = document["queries"]
    catalog_ids = catalog["ids"]
    equipment_by_id = {record["id"]: record["equipment"] for record in catalog["records"]}
    ids = [item["id"] for item in queries]
    assert len(ids) == len(set(ids)), "查询 ID 重复"
    assert len(queries) >= 120, "查询数量不足 120"
    assert all(item["label_status"] == document["label_status"] for item in queries), (
        "查询标注状态必须与文件级一致"
    )
    counts = Counter(item["intent"] for item in queries)
    for item in queries:
        label_ids = [label["id"] for label in item["labels"]]
        assert len(label_ids) == len(set(label_ids)), f"{item['id']} 标注 ID 重复"
        for label_id in label_ids:
            assert label_id in catalog_ids, f"{item['id']} 标注引用目录外 ID {label_id}"
        if item["expect_empty"]:
            assert not item["labels"], f"{item['id']} 无答案查询不应有相关标注"
        # 正相关标注必须满足查询声明的有效硬约束：显式器械筛选非空且期望非空时，
        # 每条正相关 ID 的目录器械必须与筛选一致，避免遵守过滤的系统被扣分。
        equipment_filter = item["filters"]["equipment"]
        if equipment_filter is not None and not item["expect_empty"]:
            for label in item["labels"]:
                actual = equipment_by_id[label["id"]]
                assert actual == equipment_filter, (
                    f"{item['id']} 正相关 {label['id']} 目录器械 {actual!r} 不满足筛选 {equipment_filter!r}"
                )
    family_split = defaultdict(set)
    for item in queries:
        family_split[item["family"]].add(item["split"])
    leaked = {family: splits for family, splits in family_split.items() if len(splits) > 1}
    assert not leaked, f"family 跨集泄漏: {leaked}"
    print(
        f"评测查询：{len(queries)} 条，开发集 {sum(1 for i in queries if i['split'] == 'dev')}，"
        f"留出集 {sum(1 for i in queries if i['split'] == 'holdout')}，家族 {len(family_split)}，"
        f"标注 {sum(len(i['labels']) for i in queries)}，类别 {dict(sorted(counts.items()))}，"
        f"正相关硬约束（器械筛选）全部满足"
    )


def check_reviews(document: dict, candidates: dict, catalog_ids: set[str]) -> None:
    published_path = DATASET / Path(document["published_file"]).name
    repo_root = DATASET.parents[1]
    # 批准账本绑定具体内容哈希：scope 文件被改动后旧批准记录不可继承。
    for decision in document["decisions"]:
        assert decision["conclusion"] in {"approved", "rejected"}
        assert decision["reviewer"] and decision["scope"]
        assert isinstance(decision["reviewed_at"], int) and decision["reviewed_at"] > 0
        scope_path = repo_root / decision["scope"]
        assert scope_path.exists(), f"批准范围文件缺失: {decision['scope']}"
        assert decision["content_sha256"] == file_sha256(scope_path), (
            f"批准决定与当前文件内容哈希不一致: {decision['scope']}"
        )
    if document["status"] == "approved":
        assert document["reviewer"] and document["reviewed_at"], "通过审阅必须记录审阅人与时间"
        assert document["decisions"], "通过审阅必须包含逐条结论"
        assert published_path.exists(), "通过审阅后必须生成发布语料"
        assert document["published_sha256"] == file_sha256(published_path), (
            "发布语料内容哈希与账本绑定不一致"
        )
        published = load(published_path)
        validate_schema(published, SUPPLEMENTS_SCHEMA)
        assert published["review_status"] == "approved"
        published_entries = published["entries"]
        published_ids = [entry["id"] for entry in published_entries]
        assert len(published_ids) == len(set(published_ids)), "发布语料 ID 重复"
        assert set(published_ids) == catalog_ids, "发布语料未覆盖目录全部 ID"
        assert len(published_entries) == len(catalog_ids), "发布语料数量与目录不符"
        candidates_by_id = {entry["id"]: entry for entry in candidates["entries"]}
        for published_entry in published_entries:
            candidate_entry = candidates_by_id[published_entry["id"]]
            assert published_entry["english_name"]["review_status"] == "approved"
            assert published_entry["english_name"]["text"] == candidate_entry["english_name"]["text"]
            assert [alias["text"] for alias in published_entry["aliases"]] == [
                alias["text"] for alias in candidate_entry["aliases"]
            ]
            assert all(alias["review_status"] == "approved" for alias in published_entry["aliases"])
        print(
            f"人工审阅账本：approved，发布语料 {len(published_entries)} 条，"
            f"候选内容一致，账本与发布语料哈希绑定一致"
        )
    else:
        assert document["reviewer"] is None and document["reviewed_at"] is None, "不得编造审阅人或时间"
        assert not published_path.exists(), "发布语料在人工审阅通过前不得生成"
        print("人工审阅账本：pending，无审阅结论，发布语料未生成")


def check() -> None:
    catalog = Catalog.load()
    info = check_catalog(catalog)
    supplements = load(SUPPLEMENTS)
    check_supplements(supplements, info["ids"])
    check_mappings(load(MAPPINGS), info)
    check_queries(load(QUERIES), info)
    check_reviews(load(REVIEWS), supplements, info["ids"])

    mappings = rt.load_mappings()
    entries = rt.supplement_index(rt.load_supplements())
    texts = [
        rt.build_unified_text(exercise, entries.get(exercise.id), mappings)
        for exercise in catalog.all()
    ]
    assert len(texts) == len(catalog)
    assert all(text for text in texts), "统一文本不得为空"
    duplicate_texts = len(texts) - len(set(texts))
    print(f"统一文本：{len(texts)} 条，最大字符数 {max(len(t) for t in texts)}，相同文本 {duplicate_texts} 组")
    print("检索数据校验通过")


if __name__ == "__main__":
    check()
