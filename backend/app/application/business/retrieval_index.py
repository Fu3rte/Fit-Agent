import asyncio
import hashlib
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

import pymilvus
from jsonschema import Draft202012Validator
from pymilvus import DataType, Function, FunctionType, MilvusClient

from app.application.business import retrieval_text as rt
from app.application.business.catalog import DEFAULT_ACTIONS_PATH, Catalog
from app.application.business.retrieval_embedding import EmbeddingClient

PUBLISHED_ALIAS = "fit_agent_actions_current"
BUILD_PREFIX = "fit_agent_actions_build_"
MANIFEST_VERSION = "action-index-1"
MANIFEST_METADATA_FORMAT = "action-index-schema-metadata-1"
COLLECTION_DESCRIPTION_MAX_BYTES = 1024
MANIFEST_DESCRIPTION_MAX_BYTES = 4096
PUBLISHER = "fit-agent-t5-single-publisher"
DIMENSIONS = 1024
ANALYZER = {"type": "chinese"}
CAPACITIES = {"id": 64, "text": 4096, **dict.fromkeys(rt.MAPPING_FIELDS, 128)}
INDEXES = {
    "sparse": {
        "index_type": "SPARSE_INVERTED_INDEX",
        "metric_type": "BM25",
        "params": {"inverted_index_algo": "DAAT_MAXSCORE", "bm25_k1": 1.2, "bm25_b": 0.75},
    },
    "dense": {"index_type": "AUTOINDEX", "metric_type": "COSINE", "params": {}},
}
DATASET = DEFAULT_ACTIONS_PATH.parent
SOURCE_PATHS = {
    "catalog": DEFAULT_ACTIONS_PATH,
    "published": rt.DEFAULT_SUPPLEMENTS_PATH,
    "mappings": rt.DEFAULT_MAPPINGS_PATH,
    "reviews": DATASET / "action-supplement-reviews.json",
    "candidates": rt.CANDIDATE_SUPPLEMENTS_PATH,
}


class IndexCompatibilityError(ValueError):
    pass


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise IndexCompatibilityError(message)


@dataclass(frozen=True)
class RetrievalCorpus:
    catalog: Catalog
    records: tuple[dict, ...]
    sources: dict
    maxima: dict[str, int]

    @classmethod
    def load(cls) -> "RetrievalCorpus":
        catalog = Catalog.load()
        exercises = catalog.all()
        ids = [item.id for item in exercises]
        require(bool(ids) and len(ids) == len(set(ids)), "目录 ID 数量或唯一性错误")
        require(all(isinstance(item, str) and item for item in ids), "目录 ID 必须为非空字符串")
        paths = SOURCE_PATHS
        hashes = {name: sha256(path) for name, path in paths.items()}
        documents = {
            name: json.loads(path.read_text(encoding="utf-8"))
            for name, path in paths.items() if name != "catalog"
        }
        for name, schema in (
            ("published", "action-supplements.schema.json"),
            ("candidates", "action-supplements.schema.json"),
            ("mappings", "action-field-mappings.schema.json"),
        ):
            Draft202012Validator(
                json.loads((DATASET / schema).read_text(encoding="utf-8"))
            ).validate(documents[name])
        published, mappings, reviews = (documents[name] for name in ("published", "mappings", "reviews"))
        require(reviews["status"] == published["review_status"] == "approved", "语料尚未批准")
        require(bool(reviews["reviewer"]) and bool(reviews["reviewed_at"]), "审阅身份缺失")
        require(reviews["published_sha256"] == hashes["published"], "发布语料批准哈希错误")
        decisions = [
            decision for decision in reviews["decisions"]
            if decision["scope"] == "backend/dataset/action-supplements.candidates.json"
        ]
        require(len(decisions) == 1, "语料批准决定缺失或重复")
        require(decisions[0]["conclusion"] == "approved", "候选语料未批准")
        require(decisions[0]["content_sha256"] == hashes["candidates"], "候选语料批准哈希错误")
        for document in (published, mappings):
            require(document["catalog"]["sha256"] == hashes["catalog"], "语料目录哈希不符")
            require(document["catalog"]["count"] == len(ids), "语料目录数量不符")
        entries = rt.supplement_index(published)
        candidates = rt.supplement_index(documents["candidates"])
        require(len(entries) == len(published["entries"]) == len(ids), "发布 ID 数量或唯一性错误")
        require(set(entries) == set(candidates) == set(ids), "发布语料目录引用不符")
        require(len(candidates) == len(documents["candidates"]["entries"]), "候选 ID 重复")
        for field in rt.MAPPING_FIELDS:
            require(
                set(mappings["fields"][field]) == {getattr(item, field) for item in exercises},
                f"字段映射枚举不符: {field}",
            )
        records = []
        for exercise in exercises:
            entry, candidate = entries[exercise.id], candidates[exercise.id]
            require(entry["english_name"]["review_status"] == "approved", "英文名称未批准")
            require(all(alias["review_status"] == "approved" for alias in entry["aliases"]), "别名未批准")
            require(entry["english_name"]["text"] == candidate["english_name"]["text"], "发布英文内容不符")
            require(
                [alias["text"] for alias in entry["aliases"]] == [alias["text"] for alias in candidate["aliases"]],
                "发布别名内容不符",
            )
            text = rt.build_unified_text(exercise, entry, mappings)
            require(bool(text), "统一文本为空")
            records.append({"id": exercise.id, "text": text, **{field: getattr(exercise, field) for field in rt.MAPPING_FIELDS}})
        maxima = {field: max(len(record[field].encode("utf-8")) for record in records) for field in CAPACITIES}
        for field, maximum in maxima.items():
            require(maximum <= CAPACITIES[field], f"VARCHAR 容量不足: {field}")
        # 原始目录为数组，内容哈希即其源版本。
        sources = {
            name: {"version": documents[name]["version"] if name != "catalog" else "sha256:" + hashes[name], "sha256": hashes[name]}
            for name in paths
        }
        require(all(sha256(path) == hashes[name] for name, path in paths.items()), "载入期间语料发生变化")
        return cls(catalog, tuple(records), sources, maxima)

    def validate_sources(self) -> None:
        require(all(sha256(path) == self.sources[name]["sha256"] for name, path in SOURCE_PATHS.items()), "检索语料快照已变化")

    def compatibility(self, embedding: EmbeddingClient, server_version: str) -> dict:
        require(server_version == "3.0.2" and pymilvus.__version__ == "3.0.2", "Milvus 服务或 SDK 版本不符")
        require(embedding.config.dimensions == DIMENSIONS, "Embedding 维度不符")
        return {
            "manifest_version": MANIFEST_VERSION,
            "count": len(self.records),
            "sources": self.sources,
            "encoding_rule_version": rt.ENCODING_RULE_VERSION,
            "field_order": list(rt.FIELD_ORDER),
            "separator": rt.SEGMENT_SEPARATOR,
            "model": embedding.config.model,
            "dimensions": DIMENSIONS,
            "api_base_url": embedding.config.base_url,
            "analyzer": ANALYZER,
            "capacities": CAPACITIES,
            "indexes": INDEXES,
            "server_version": server_version,
            "sdk_version": pymilvus.__version__,
            "text_sha256": hashlib.sha256(json_text(self.records).encode("utf-8")).hexdigest(),
        }


class MilvusConnection:
    # 同步 Milvus I/O 在线程内执行；连接建立保留有限超时，RPC 单次尝试关闭自动重试，取消等待线程内调用返回，关闭与调用串行。
    def __init__(self, uri: str, token: str, timeout: float) -> None:
        require(isinstance(uri, str) and bool(uri.strip()), "缺少 FIT_AGENT_MILVUS_URI")
        require(not isinstance(timeout, bool) and isinstance(timeout, (int, float)) and math.isfinite(timeout) and timeout > 0, "FIT_AGENT_MILVUS_TIMEOUT 必须为有限正数")
        self.uri, self._token, self.timeout = uri, token, timeout
        self._client: MilvusClient | None = None
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    def from_env(cls) -> "MilvusConnection":
        uri = os.environ.get("FIT_AGENT_MILVUS_URI", "")
        raw_timeout = os.environ.get("FIT_AGENT_MILVUS_TIMEOUT", "")
        require(bool(raw_timeout.strip()), "缺少 FIT_AGENT_MILVUS_TIMEOUT")
        return cls(uri, os.environ.get("FIT_AGENT_MILVUS_TOKEN", ""), float(raw_timeout))

    async def _run(self, function, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        result = task.result()
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def __aenter__(self) -> "MilvusConnection":
        require(self._client is None, "MilvusConnection 已打开")
        self._loop = asyncio.get_running_loop()
        async with self._lock:
            # 创建成功后赋值在线程操作内部完成，取消路径仍可明确关闭。
            def connect():
                self._client = MilvusClient(uri=self.uri, token=self._token, timeout=self.timeout)
            try:
                await self._run(connect)
            except asyncio.CancelledError:
                if self._client is not None:
                    await self._run(self._client.close)
                    self._client = None
                raise
        return self

    async def call(self, method: str, *args, **kwargs):
        require(asyncio.get_running_loop() is self._loop, "MilvusConnection 事件循环不符")
        async with self._lock:
            require(self._client is not None, "MilvusConnection 未打开")
            # 不下传数值 timeout 使 retry_on_rpc_failure 的 retry_timeout=None，改走计数上限；retry_times=0 令首次失败即超时判定成立，retry_on_rate_limit=False 关闭限流重试，RPC 单次尝试后底层错误直接传播。
            return await self._run(getattr(self._client, method), *args, retry_times=0, retry_on_rate_limit=False, **kwargs)

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        require(asyncio.get_running_loop() is self._loop, "MilvusConnection 事件循环不符")
        async with self._lock:
            if self._client is not None:
                try:
                    await self._run(self._client.close)
                finally:
                    self._client = None
                    self._loop = None


def create_schema(manifest: dict):
    # 集合 Schema 元信息：collection description 保存摘要，text 字段 description 保存完整 JSON。
    payload = json_text(manifest)
    payload_bytes = payload.encode("utf-8")
    require(len(payload_bytes) <= MANIFEST_DESCRIPTION_MAX_BYTES, "索引构建清单超出元信息容量")
    header = json_text({
        "format": MANIFEST_METADATA_FORMAT,
        "field": "text",
        "bytes": len(payload_bytes),
        "sha256": hashlib.sha256(payload_bytes).hexdigest(),
    })
    require(len(header.encode("utf-8")) <= COLLECTION_DESCRIPTION_MAX_BYTES, "集合 description 超出容量")
    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False, description=header)
    for field, capacity in CAPACITIES.items():
        options = {"max_length": capacity}
        if field == "id":
            options["is_primary"] = True
        if field == "text":
            options.update(enable_analyzer=True, analyzer_params=ANALYZER, description=payload)
        schema.add_field(field_name=field, datatype=DataType.VARCHAR, **options)
    schema.add_field(field_name="dense", datatype=DataType.FLOAT_VECTOR, dim=DIMENSIONS)
    schema.add_field(field_name="sparse", datatype=DataType.SPARSE_FLOAT_VECTOR)
    schema.add_function(Function(name="text_bm25", input_field_names=["text"], output_field_names=["sparse"], function_type=FunctionType.BM25))
    return schema


def create_indexes():
    indexes = MilvusClient.prepare_index_params()
    for field, options in INDEXES.items():
        indexes.add_index(field_name=field, index_name=field + "_index", **options)
    return indexes


def decode_manifest(description: dict) -> dict:
    header_text = description.get("description")
    require(isinstance(header_text, str) and bool(header_text), "索引构建清单摘要缺失")
    require(len(header_text.encode("utf-8")) <= COLLECTION_DESCRIPTION_MAX_BYTES, "集合 description 超出容量")
    header = json.loads(header_text)
    require(isinstance(header, dict) and header.get("format") == MANIFEST_METADATA_FORMAT and header.get("field") == "text", "索引构建清单元信息格式不符")
    fields = [field for field in description["fields"] if field["name"] == "text"]
    require(len(fields) == 1, "索引构建清单字段缺失或重复")
    payload = fields[0].get("description")
    require(isinstance(payload, str) and bool(payload), "索引构建清单缺失")
    payload_bytes = payload.encode("utf-8")
    require(len(payload_bytes) <= MANIFEST_DESCRIPTION_MAX_BYTES, "索引构建清单超出元信息容量")
    declared_bytes = header.get("bytes")
    require(isinstance(declared_bytes, int) and not isinstance(declared_bytes, bool) and declared_bytes == len(payload_bytes), "索引构建清单字节数不符")
    require(header.get("sha256") == hashlib.sha256(payload_bytes).hexdigest(), "索引构建清单哈希不符")
    manifest = json.loads(payload)
    require(isinstance(manifest, dict), "索引构建清单必须为 JSON 对象")
    return manifest


async def read_manifest(db: MilvusConnection, collection: str) -> dict:
    return decode_manifest(await db.call("describe_collection", collection))


async def validated_manifest(db: MilvusConnection, collection: str, expected: dict) -> dict:
    # 轻量清单校验：回读清单并校验格式、字节数、SHA-256 及兼容与构建身份字段；
    # 供完整索引校验与查询热路径共享，不新增 alias 配置选项。
    manifest = await read_manifest(db, collection)
    require(manifest["compatibility"] == expected, "索引与输入配置版本不兼容")
    require(manifest["collection"] == collection and manifest["publisher"] == PUBLISHER, "索引构建身份不符")
    return manifest


async def validate_index(db: MilvusConnection, collection: str, corpus: RetrievalCorpus, expected: dict) -> dict:
    manifest = await validated_manifest(db, collection, expected)
    description = await db.call("describe_collection", collection)
    fields = {field["name"]: field for field in description["fields"]}
    require(set(fields) == set(CAPACITIES) | {"dense", "sparse"}, "索引物理字段不符")
    require(fields["id"].get("is_primary") is True and not description.get("auto_id"), "索引主键配置不符")
    for field, capacity in CAPACITIES.items():
        require(fields[field]["type"] == DataType.VARCHAR and int(fields[field]["params"]["max_length"]) == capacity, f"索引 VARCHAR 配置不符: {field}")
    text_params = fields["text"]["params"]
    analyzer = text_params.get("analyzer_params")
    if isinstance(analyzer, str):
        analyzer = json.loads(analyzer)
    require(text_params.get("enable_analyzer") in (True, "true", "True") and analyzer == ANALYZER, "索引 analyzer 不符")
    require(fields["dense"]["type"] == DataType.FLOAT_VECTOR and int(fields["dense"]["params"]["dim"]) == DIMENSIONS, "dense 物理维度不符")
    require(fields["sparse"]["type"] == DataType.SPARSE_FLOAT_VECTOR, "sparse 物理类型不符")
    functions = description.get("functions", [])
    require(len(functions) == 1 and functions[0]["name"] == "text_bm25" and functions[0]["type"] == FunctionType.BM25 and functions[0]["input_field_names"] == ["text"] and functions[0]["output_field_names"] == ["sparse"], "BM25 Function 不符")
    names = await db.call("list_indexes", collection)
    require(set(names) == {"dense_index", "sparse_index"}, "检索索引缺失")
    for field, options in INDEXES.items():
        actual = await db.call("describe_index", collection, index_name=field + "_index")
        require(actual["field_name"] == field and actual["index_type"] == options["index_type"] and actual["metric_type"] == options["metric_type"], f"索引类型或度量不符: {field}")
        for key, value in options["params"].items():
            require(str(actual[key]) == str(value), f"索引参数不符: {key}")
    await db.call("load_collection", collection)
    counts = await db.call("query", collection, filter='id != ""', output_fields=["count(*)"], consistency_level="Strong")
    require(counts[0]["count(*)"] == len(corpus.records), "索引数量不符")
    seen = set()
    for start in range(0, len(corpus.records), 100):
        batch = corpus.records[start:start + 100]
        rows = await db.call("get", collection, ids=[record["id"] for record in batch], output_fields=[*CAPACITIES, "dense"], consistency_level="Strong")
        require(len(rows) == len(batch), "索引动作缺失或重复")
        expected_rows = {record["id"]: record for record in batch}
        for row in rows:
            require(row["id"] not in seen and row["id"] in expected_rows, "索引 ID 重复或目录引用不符")
            seen.add(row["id"])
            require(all(row[field] == expected_rows[row["id"]][field] for field in CAPACITIES), "索引动作字段不符")
            vector = row["dense"]
            require(len(vector) == DIMENSIONS and all(math.isfinite(value) for value in vector) and any(value != 0 for value in vector), "索引 dense 向量无效")
    require(seen == corpus.catalog.exercise_ids, "索引目录 ID 集合不符")
    return manifest


async def published_collection(db: MilvusConnection) -> str:
    aliases = (await db.call("list_aliases"))["aliases"]
    require(PUBLISHED_ALIAS in aliases, "正式动作索引尚未发布")
    return (await db.call("describe_alias", PUBLISHED_ALIAS))["collection_name"]


async def build_index(db: MilvusConnection, corpus: RetrievalCorpus, embedding: EmbeddingClient) -> dict:
    # 唯一受控发布入口；运营方串行运行，保留成功发布的既有集合。
    server = await db.call("get_server_version")
    expected = corpus.compatibility(embedding, server)
    collection = BUILD_PREFIX + uuid4().hex
    require(not await db.call("has_collection", collection), "构建候选集合已存在")
    manifest = {"collection": collection, "publisher": PUBLISHER, "build_id": collection[len(BUILD_PREFIX):], "created_at": time.time(), "compatibility": expected, "utf8_maxima": corpus.maxima}
    await db.call("create_collection", collection, schema=create_schema(manifest), index_params=create_indexes(), consistency_level="Strong")
    require(await read_manifest(db, collection) == manifest, "构建清单写入校验失败")
    started = time.monotonic()
    before = embedding.usage
    encoding_seconds = 0.0
    for start in range(0, len(corpus.records), 20):
        batch = corpus.records[start:start + 20]
        encoding_started = time.monotonic()
        vectors = await embedding.encode_batch([record["text"] for record in batch])
        encoding_seconds += time.monotonic() - encoding_started
        rows = [{**record, "dense": vector} for record, vector in zip(batch, vectors, strict=True)]
        result = await db.call("insert", collection, data=rows)
        require(result["insert_count"] == len(rows), "索引写入数量不符")
    await db.call("flush", collection)
    await publish_index(db, collection, corpus, embedding)
    usage = {key: value - asdict(before)[key] for key, value in asdict(embedding.usage).items()}
    return {"manifest": manifest, "encoding_usage": usage, "encoding_seconds": encoding_seconds, "build_seconds": time.monotonic() - started}


async def publish_index(db: MilvusConnection, collection: str, corpus: RetrievalCorpus, embedding: EmbeddingClient) -> dict:
    require(collection.startswith(BUILD_PREFIX) and len(collection) == len(BUILD_PREFIX) + 32, "正式发布对象不是构建候选")
    suffix = collection[len(BUILD_PREFIX):]
    require(all(character in "0123456789abcdef" for character in suffix), "构建候选身份不符")
    server = await db.call("get_server_version")
    expected = corpus.compatibility(embedding, server)
    manifest = await validate_index(db, collection, corpus, expected)
    require(manifest["build_id"] == suffix, "构建发布身份不符")
    probe = corpus.records[0]
    vector = (await db.call("get", collection, ids=[probe["id"]], output_fields=["dense"], consistency_level="Strong"))[0]["dense"]
    from app.application.business.retrieval_search import search_vectors
    for mode in ("bm25", "dense", "hybrid"):
        hits = await search_vectors(db, collection, corpus.catalog, probe["text"], vector, mode=mode, limit=1)
        require(bool(hits), f"发布前真实查询无结果: {mode}")
    # 发布前再次确认源快照，变更不会把新旧文本空间混入正式入口。
    current = RetrievalCorpus.load()
    require(current.compatibility(embedding, server) == expected, "构建期间输入版本发生变化")
    aliases = (await db.call("list_aliases"))["aliases"]
    await db.call("alter_alias" if PUBLISHED_ALIAS in aliases else "create_alias", collection, alias=PUBLISHED_ALIAS)
    require(await published_collection(db) == collection, "正式 alias 发布校验失败")
    return manifest
