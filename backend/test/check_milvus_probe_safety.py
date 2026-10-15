import subprocess
import sys
from contextlib import closing

from pymilvus import DataType

from test.check_milvus_environment import connect

# 本次安全验证专属隔离集合：与探针同前缀，用于验证 cleanup 不会前缀误删。
GUARD_COLLECTION = "fit_agent_t3_probe_safety_guard"
GUARD_RECORDS = [
    {"id": "guard-0001", "text": "安全验证记录一 safety guard record one", "vec": [1.0, 0.0, 0.0, 0.0]},
    {"id": "guard-0002", "text": "安全验证记录二 safety guard record two", "vec": [0.0, 1.0, 0.0, 0.0]},
]


def create_guard(client) -> None:
    if client.has_collection(GUARD_COLLECTION):
        raise SystemExit(f"隔离集合 {GUARD_COLLECTION!r} 已存在，禁止覆盖或删除既有对象")
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field(field_name="id", datatype=DataType.VARCHAR, max_length=32, is_primary=True)
    schema.add_field(field_name="text", datatype=DataType.VARCHAR, max_length=256)
    # 服务端要求 schema 含向量字段，占位向量不参与任何检索断言。
    schema.add_field(field_name="vec", datatype=DataType.FLOAT_VECTOR, dim=4)
    index_params = client.prepare_index_params()
    index_params.add_index(field_name="vec", index_type="FLAT", metric_type="COSINE")
    client.create_collection(collection_name=GUARD_COLLECTION, schema=schema, index_params=index_params)
    client.insert(collection_name=GUARD_COLLECTION, data=GUARD_RECORDS)
    client.flush(collection_name=GUARD_COLLECTION)
    print(f"隔离集合已创建并写入 {len(GUARD_RECORDS)} 条真实记录: {GUARD_COLLECTION}")


def run_cleanup_entry() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "test.check_milvus_environment", "cleanup"],
        capture_output=True,
        text=True,
    )
    print(result.stdout, end="")
    print(result.stderr, end="")
    assert result.returncode == 0, f"cleanup 入口退出码 {result.returncode}"


def verify_guard(client) -> None:
    assert client.has_collection(GUARD_COLLECTION), f"隔离集合 {GUARD_COLLECTION} 在 cleanup 后缺失"
    rows = client.query(
        collection_name=GUARD_COLLECTION,
        filter='id in ["guard-0001", "guard-0002"]',
        output_fields=["id", "text"],
        consistency_level="Strong",
    )
    by_id = {row["id"]: row["text"] for row in rows}
    assert by_id == {record["id"]: record["text"] for record in GUARD_RECORDS}, by_id
    print(f"cleanup 后隔离集合与记录均保留: {sorted(by_id)}")


def main() -> None:
    with closing(connect()) as client:
        create_guard(client)
    run_cleanup_entry()
    with closing(connect()) as client:
        verify_guard(client)
        client.drop_collection(GUARD_COLLECTION)
        assert not client.has_collection(GUARD_COLLECTION)
    print(f"本次安全验证结束，仅删除本次创建的隔离集合: {GUARD_COLLECTION}")


if __name__ == "__main__":
    main()
