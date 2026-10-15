import math
import os
import sys
from contextlib import closing

from pymilvus import (
    AnnSearchRequest,
    DataType,
    Function,
    FunctionType,
    MilvusClient,
    WeightedRanker,
)

# 连接约定：本机 Standalone，默认无鉴权；T5/T6 复用同一环境变量名。
URI = os.environ.get("FIT_AGENT_MILVUS_URI", "http://127.0.0.1:19530")
TOKEN = os.environ.get("FIT_AGENT_MILVUS_TOKEN") or None
TIMEOUT = float(os.environ.get("FIT_AGENT_MILVUS_TIMEOUT", "10"))
EXPECTED_SERVER_VERSION = os.environ.get("FIT_AGENT_MILVUS_EXPECTED_VERSION", "3.0.2")

# 探针集合固定归属 T3，禁止通过环境变量指向任意集合。
PROBE_COLLECTION = "fit_agent_t3_probe"
BASIC_COLLECTION = "fit_agent_t3_probe_basic"
COLLECTION_ENV_KEY = "FIT_AGENT_MILVUS_TEST_COLLECTION"

# 归属校验先于连接与任何数据库操作，覆盖 run/persistence/cleanup/all/默认全部入口。
env_collection = os.environ.get(COLLECTION_ENV_KEY)
if env_collection is not None and env_collection != PROBE_COLLECTION:
    raise SystemExit(
        f"非法 {COLLECTION_ENV_KEY}={env_collection!r}："
        f"本脚本仅允许固定探针集合名 {PROBE_COLLECTION!r}，"
        f"请将其设为 {PROBE_COLLECTION!r} 或不设置该环境变量"
    )

DENSE_DIM = 1024
ANALYZER_PARAMS = {"type": "chinese"}
BM25_QUERY = "哑铃"
DENSE_QUERY_UNIT = 0


def unit_vector(index: int) -> list[float]:
    # 数学上已知的非零向量，仅用于验证 COSINE 与融合语义，非百炼输出。
    vector = [0.0] * DENSE_DIM
    vector[index] = 1.0
    return vector


# 数据库能力测试输入：id 保留前导零，器械/部位用于过滤，unit 指定正交单位向量。
RECORDS = [
    {"id": "0001", "text": "杠铃 卧推 chest barbell", "equipment": "barbell", "body_part": "chest", "unit": 0},
    {"id": "0002", "text": "哑铃 卧推 chest dumbbell", "equipment": "dumbbell", "body_part": "chest", "unit": 1},
    {"id": "0003", "text": "引体向上 back bodyweight", "equipment": "bodyweight", "body_part": "back", "unit": 2},
    {"id": "0004", "text": "哑铃 划船 back dumbbell", "equipment": "dumbbell", "body_part": "back", "unit": 3},
]


def connect() -> MilvusClient:
    print(f"连接 {URI} token={'有' if TOKEN else '无'} timeout={TIMEOUT}s")
    return MilvusClient(uri=URI, token=TOKEN, timeout=TIMEOUT)


def expect_server_version(client: MilvusClient) -> None:
    version = client.get_server_version()
    print(f"服务端版本: {version}")
    assert version.lstrip("v").startswith(EXPECTED_SERVER_VERSION), version


def check_basic_ops(client: MilvusClient) -> None:
    if client.has_collection(BASIC_COLLECTION):
        client.drop_collection(BASIC_COLLECTION)
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field(field_name="id", datatype=DataType.VARCHAR, max_length=32, is_primary=True)
    schema.add_field(field_name="vec", datatype=DataType.FLOAT_VECTOR, dim=4)
    index_params = client.prepare_index_params()
    index_params.add_index(field_name="vec", index_type="FLAT", metric_type="COSINE")
    client.create_collection(collection_name=BASIC_COLLECTION, schema=schema, index_params=index_params)
    client.insert(collection_name=BASIC_COLLECTION, data=[{"id": "0001", "vec": [1.0, 0.0, 0.0, 0.0]}])
    client.flush(collection_name=BASIC_COLLECTION)
    client.load_collection(collection_name=BASIC_COLLECTION)
    rows = client.query(
        collection_name=BASIC_COLLECTION,
        filter='id == "0001"',
        output_fields=["id"],
        consistency_level="Strong",
    )
    assert [row["id"] for row in rows] == ["0001"], rows
    client.drop_collection(BASIC_COLLECTION)
    print("基础 collection 建删、插入与主键查询通过")


def check_analyzer(client: MilvusClient) -> None:
    # 覆盖中文名称、中文描述、英文及中英混合；仅验证配置可用并记录真实 token。
    samples = [
        "杠铃卧推",
        "坐姿哑铃弯举，锻炼肱二头肌",
        "barbell bench press",
        "Dumbbell 哑铃卧推 3/4",
    ]
    for sample in samples:
        tokens = client.run_analyzer(sample, ANALYZER_PARAMS)
        assert tokens, sample
        print(f"analyzer {sample!r} -> {tokens}")


def build_probe_schema(client: MilvusClient):
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field(field_name="id", datatype=DataType.VARCHAR, max_length=64, is_primary=True)
    schema.add_field(
        field_name="text",
        datatype=DataType.VARCHAR,
        max_length=2048,
        enable_analyzer=True,
        analyzer_params=ANALYZER_PARAMS,
    )
    schema.add_field(field_name="sparse", datatype=DataType.SPARSE_FLOAT_VECTOR)
    schema.add_field(field_name="dense", datatype=DataType.FLOAT_VECTOR, dim=DENSE_DIM)
    schema.add_field(field_name="equipment", datatype=DataType.VARCHAR, max_length=64)
    schema.add_field(field_name="body_part", datatype=DataType.VARCHAR, max_length=64)
    schema.add_function(
        Function(
            name="text_bm25_emb",
            function_type=FunctionType.BM25,
            input_field_names=["text"],
            output_field_names=["sparse"],
        )
    )
    return schema


def build_probe_index_params(client: MilvusClient):
    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="sparse",
        index_type="SPARSE_INVERTED_INDEX",
        metric_type="BM25",
        params={"inverted_index_algo": "DAAT_MAXSCORE", "bm25_k1": 1.2, "bm25_b": 0.75},
    )
    index_params.add_index(field_name="dense", index_type="AUTOINDEX", metric_type="COSINE")
    return index_params


def create_probe_collection(client: MilvusClient) -> None:
    if client.has_collection(PROBE_COLLECTION):
        client.drop_collection(PROBE_COLLECTION)
    client.create_collection(
        collection_name=PROBE_COLLECTION,
        schema=build_probe_schema(client),
        index_params=build_probe_index_params(client),
    )
    client.insert(
        collection_name=PROBE_COLLECTION,
        data=[
            {
                "id": row["id"],
                "text": row["text"],
                "dense": unit_vector(row["unit"]),
                "equipment": row["equipment"],
                "body_part": row["body_part"],
            }
            for row in RECORDS
        ],
    )
    client.flush(collection_name=PROBE_COLLECTION)
    client.load_collection(collection_name=PROBE_COLLECTION)
    print(f"测试 collection（T3 归属）: {PROBE_COLLECTION}")


def single_search(client: MilvusClient, *, data, anns_field: str, limit: int, expr: str = ""):
    return client.search(
        collection_name=PROBE_COLLECTION,
        data=data,
        anns_field=anns_field,
        filter=expr,
        limit=limit,
        output_fields=["id", "text", "equipment", "body_part"],
        consistency_level="Strong",
    )[0]


def hybrid_search(client: MilvusClient, reqs, weights, limit: int):
    # 请求顺序固定 [BM25, dense]，权重 [α, 1−α]，内置 arctan 归一化。
    return client.hybrid_search(
        collection_name=PROBE_COLLECTION,
        reqs=reqs,
        ranker=WeightedRanker(*weights),
        limit=limit,
        output_fields=["id", "text", "equipment", "body_part"],
        consistency_level="Strong",
    )[0]


def scored(hits) -> list[tuple[str, float]]:
    return [(hit["id"], round(hit["distance"], 6)) for hit in hits]


def check_bm25(client: MilvusClient) -> None:
    hits = single_search(client, data=[BM25_QUERY], anns_field="sparse", limit=10)
    print(f"BM25 {BM25_QUERY!r}: {scored(hits)}")
    assert {hit["id"] for hit in hits} == {"0002", "0004"}, hits
    assert all(hit["id"].startswith("0") for hit in hits), hits
    assert all(hit["distance"] > 0 for hit in hits), hits


def check_dense(client: MilvusClient) -> None:
    hits = single_search(client, data=[unit_vector(DENSE_QUERY_UNIT)], anns_field="dense", limit=10)
    print(f"COSINE dense e{DENSE_QUERY_UNIT}: {scored(hits)}")
    assert hits[0]["id"] == "0001", hits
    assert math.isclose(hits[0]["distance"], 1.0, abs_tol=1e-5), hits[0]
    assert {hit["id"] for hit in hits} == {"0001", "0002", "0003", "0004"}, hits
    assert all(math.isclose(hit["distance"], 0.0, abs_tol=1e-5) for hit in hits[1:]), hits


def check_hybrid_weights(client: MilvusClient) -> None:
    def bm25_req(expr: str = ""):
        return AnnSearchRequest(data=[BM25_QUERY], anns_field="sparse", param={}, limit=10, expr=expr)

    def dense_req(expr: str = ""):
        return AnnSearchRequest(data=[unit_vector(DENSE_QUERY_UNIT)], anns_field="dense", param={}, limit=10, expr=expr)

    sparse_only = hybrid_search(client, [bm25_req(), dense_req()], (1.0, 0.0), 10)
    print(f"hybrid weights=[1.0,0.0]: {scored(sparse_only)}")
    assert sparse_only[0]["id"] in {"0002", "0004"}, sparse_only
    sparse_ids = [hit["id"] for hit in sparse_only]
    assert sparse_ids.index("0002") < sparse_ids.index("0001"), sparse_ids

    dense_only = hybrid_search(client, [bm25_req(), dense_req()], (0.0, 1.0), 10)
    print(f"hybrid weights=[0.0,1.0]: {scored(dense_only)}")
    assert dense_only[0]["id"] == "0001", dense_only

    blended = hybrid_search(client, [bm25_req(), dense_req()], (0.5, 0.5), 10)
    print(f"hybrid weights=[0.5,0.5]: {scored(blended)}")
    blended_ids = {hit["id"] for hit in blended}
    assert "0001" in blended_ids, blended_ids
    assert blended_ids & {"0002", "0004"}, blended_ids


def check_filter(client: MilvusClient) -> None:
    expr = 'equipment == "dumbbell" and id not in ["0002"]'
    hits = hybrid_search(
        client,
        [
            AnnSearchRequest(data=[BM25_QUERY], anns_field="sparse", param={}, limit=10, expr=expr),
            AnnSearchRequest(data=[unit_vector(1)], anns_field="dense", param={}, limit=10, expr=expr),
        ],
        (0.5, 0.5),
        10,
    )
    print(f"过滤 {expr!r}: {scored(hits)}")
    assert hits, hits
    assert all(hit["entity"]["equipment"] == "dumbbell" for hit in hits), hits
    assert all(hit["id"] != "0002" for hit in hits), hits
    assert {hit["id"] for hit in hits} == {"0004"}, hits

    combined = 'body_part == "back" and equipment == "dumbbell"'
    combined_hits = hybrid_search(
        client,
        [
            AnnSearchRequest(data=[BM25_QUERY], anns_field="sparse", param={}, limit=10, expr=combined),
            AnnSearchRequest(data=[unit_vector(3)], anns_field="dense", param={}, limit=10, expr=combined),
        ],
        (0.5, 0.5),
        10,
    )
    print(f"过滤 {combined!r}: {scored(combined_hits)}")
    assert {hit["id"] for hit in combined_hits} == {"0004"}, combined_hits

    empty_expr = 'equipment == "kettlebell"'
    empty_hits = hybrid_search(
        client,
        [
            AnnSearchRequest(data=[BM25_QUERY], anns_field="sparse", param={}, limit=10, expr=empty_expr),
            AnnSearchRequest(data=[unit_vector(0)], anns_field="dense", param={}, limit=10, expr=empty_expr),
        ],
        (0.5, 0.5),
        10,
    )
    print(f"过滤 {empty_expr!r}: {scored(empty_hits)}")
    assert empty_hits == [], empty_hits


def check_capability(client: MilvusClient) -> None:
    create_probe_collection(client)
    check_bm25(client)
    check_dense(client)
    check_hybrid_weights(client)
    check_filter(client)


def check_persistence(client: MilvusClient) -> None:
    assert client.has_collection(PROBE_COLLECTION), f"缺失测试 collection {PROBE_COLLECTION}"
    client.load_collection(PROBE_COLLECTION)
    rows = client.query(
        collection_name=PROBE_COLLECTION,
        filter='id in ["0001", "0002", "0003", "0004"]',
        output_fields=["id", "text", "equipment", "body_part"],
        consistency_level="Strong",
    )
    by_id = {row["id"]: row for row in rows}
    print(f"重启后主键与文本: {[(key, by_id[key]['text']) for key in sorted(by_id)]}")
    assert set(by_id) == {"0001", "0002", "0003", "0004"}, by_id
    for record in RECORDS:
        row = by_id[record["id"]]
        assert row["text"] == record["text"], row
        assert row["equipment"] == record["equipment"], row
        assert row["body_part"] == record["body_part"], row
    bm25_ids = {hit["id"] for hit in single_search(client, data=[BM25_QUERY], anns_field="sparse", limit=10)}
    assert bm25_ids == {"0002", "0004"}, bm25_ids
    dense_hits = single_search(client, data=[unit_vector(DENSE_QUERY_UNIT)], anns_field="dense", limit=10)
    assert dense_hits[0]["id"] == "0001", dense_hits
    print("重启后 collection、主键、文本与查询结果保持可用")


def cleanup(client: MilvusClient) -> None:
    # 仅删除 T3 固定归属的精确名称，禁止前缀匹配扩散到其他集合。
    targets = [name for name in (PROBE_COLLECTION, BASIC_COLLECTION) if client.has_collection(name)]
    for name in targets:
        client.drop_collection(name)
    print(f"已删除测试 collection: {targets}")


def run_capability_suite(client: MilvusClient) -> None:
    expect_server_version(client)
    check_basic_ops(client)
    check_analyzer(client)
    check_capability(client)


def main(argv: list[str]) -> None:
    commands = argv or ["all"]
    with closing(connect()) as milvus:
        for command in commands:
            if command == "run":
                run_capability_suite(milvus)
            elif command == "persistence":
                check_persistence(milvus)
            elif command == "cleanup":
                cleanup(milvus)
            elif command == "all":
                run_capability_suite(milvus)
                cleanup(milvus)
            else:
                raise SystemExit(f"未知子命令: {command}")


if __name__ == "__main__":
    main(sys.argv[1:])
