import argparse
import asyncio
import json
import time
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

from pymilvus import MilvusException

from app.application.business.retrieval_embedding import EmbeddingClient
from app.application.business.retrieval_index import (
    BUILD_PREFIX,
    COLLECTION_DESCRIPTION_MAX_BYTES,
    MANIFEST_DESCRIPTION_MAX_BYTES,
    PUBLISHED_ALIAS,
    PUBLISHER,
    IndexCompatibilityError,
    MilvusConnection,
    RetrievalCorpus,
    build_index,
    create_indexes,
    create_schema,
    decode_manifest,
    json_text,
    publish_index,
    published_collection,
    read_manifest,
    validate_index,
)
from app.application.business.retrieval_search import (
    HybridRetriever,
    SearchConditions,
    search_vectors,
    validate_parameters,
)

EVIDENCE = Path(__file__).resolve().parents[2] / "tmp" / "action-retrieval-t5"
TEST_COLLECTION = "fit_agent_t5_test_incompatible"
METADATA_COLLECTION = "fit_agent_t5_test_metadata"


def save(name: str, data: dict) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False))


def check_inputs(corpus: RetrievalCorpus) -> dict:
    rejected = []
    for name, values in (
        ("rrf_k", [True, None, "60", 0, -1, 60.5, 16384, float("nan"), float("inf")]),
        ("limit", [True, 0, -1, 1.5, "10", 16385]),
        ("bm25_depth", [False, 0, -1, 1.2]),
        ("dense_depth", [False, 0, -1, 1.2]),
    ):
        for value in values:
            kwargs = {"rrf_k": 60, "limit": 10, "bm25_depth": 50, "dense_depth": 50}
            kwargs[name] = value
            try:
                validate_parameters("哑铃", **kwargs)
            except IndexCompatibilityError:
                rejected.append(name + ":" + repr(value))
            else:
                raise AssertionError("非法参数被接受")
    for query in ("", " \t\n", None, 123):
        try:
            validate_parameters(query, 60, 10, 50, 50)
        except IndexCompatibilityError:
            rejected.append("query:" + repr(query))
        else:
            raise AssertionError("非法 query 被接受")
    for field in ("equipment", "body_part", "target", "muscle_group"):
        for value in ('dumbbell" or id != "', "不存在", 1):
            try:
                SearchConditions(**{field: value}).expression(corpus.catalog)
            except IndexCompatibilityError:
                rejected.append(field + ":" + repr(value))
            else:
                raise AssertionError("非法字段被接受")
    for conditions in (
        SearchConditions(available_equipment=("不存在",)),
        SearchConditions(available_equipment="dumbbell"),
        SearchConditions(excluded_ids=("0000",)),
        SearchConditions(excluded_ids=(1,)),
        SearchConditions(excluded_ids=('0001"] or id != "',)),
        SearchConditions(excluded_ids=("0001", "0001")),
    ):
        try:
            conditions.expression(corpus.catalog)
        except IndexCompatibilityError:
            rejected.append(repr(conditions))
        else:
            raise AssertionError("非法条件被接受")
    try:
        SearchConditions(raw_filter='id != ""')
    except TypeError:
        rejected.append("raw_filter")
    else:
        raise AssertionError("raw filter 被接受")
    assert SearchConditions(available_equipment=()).expression(corpus.catalog) == "equipment in []"
    assert len(corpus.records) == len(corpus.catalog.exercise_ids) == 1321
    assert corpus.catalog.get("0001").id == "0001"
    return {"rejected_before_database": rejected, "count": len(corpus.records), "sources": corpus.sources, "utf8_maxima": corpus.maxima}


async def metadata(corpus: RetrievalCorpus) -> None:
    async with EmbeddingClient.from_env() as embedding, MilvusConnection.from_env() as db:
        initial_collections = set(await db.call("list_collections"))
        initial_aliases = await db.call("list_aliases")
        assert METADATA_COLLECTION not in initial_collections, "测试对象已存在，拒绝覆盖"
        owner = uuid4().hex
        manifest = {"collection": METADATA_COLLECTION, "publisher": PUBLISHER, "test_owner": owner, "compatibility": corpus.compatibility(embedding, await db.call("get_server_version")), "utf8_maxima": corpus.maxima}
        schema = create_schema(manifest)
        snapshot = schema.to_dict()
        assert decode_manifest(snapshot) == manifest
        description_bytes = len(snapshot["description"].encode("utf-8"))
        payload_bytes = len(json_text(manifest).encode("utf-8"))
        assert payload_bytes > COLLECTION_DESCRIPTION_MAX_BYTES
        assert description_bytes <= COLLECTION_DESCRIPTION_MAX_BYTES
        assert payload_bytes <= MANIFEST_DESCRIPTION_MAX_BYTES
        rejected = []
        for label in ("missing_header", "missing_payload", "changed_payload", "changed_size", "oversized_header", "oversized_payload"):
            invalid = deepcopy(snapshot)
            text = next(field for field in invalid["fields"] if field["name"] == "text")
            if label == "missing_header":
                invalid["description"] = ""
            elif label == "missing_payload":
                text["description"] = ""
            elif label == "changed_payload":
                text["description"] = text["description"].replace(owner, "0" * len(owner))
            elif label == "changed_size":
                header = json.loads(invalid["description"])
                header["bytes"] += 1
                invalid["description"] = json_text(header)
            elif label == "oversized_header":
                invalid["description"] = "a" * (COLLECTION_DESCRIPTION_MAX_BYTES + 1)
            else:
                text["description"] = "测" * (MANIFEST_DESCRIPTION_MAX_BYTES // 3 + 1)
            try:
                decode_manifest(invalid)
            except IndexCompatibilityError:
                rejected.append(label)
            else:
                raise AssertionError("非法清单元信息被接受")
        boundary = {**manifest, "boundary_note": "测" * 100}
        slack = MANIFEST_DESCRIPTION_MAX_BYTES - len(json_text(boundary).encode("utf-8"))
        assert slack > 0
        boundary["boundary_note"] += "a" * slack
        assert len(json_text(boundary).encode("utf-8")) == MANIFEST_DESCRIPTION_MAX_BYTES
        assert decode_manifest(create_schema(boundary).to_dict()) == boundary
        try:
            create_schema({**boundary, "boundary_note": boundary["boundary_note"] + "a"})
        except IndexCompatibilityError:
            rejected.append("create_over_capacity")
        else:
            raise AssertionError("超容量清单进入建集合路径")
        created = False
        try:
            await db.call("create_collection", METADATA_COLLECTION, schema=schema, index_params=create_indexes(), consistency_level="Strong")
            created = True
            actual = await db.call("describe_collection", METADATA_COLLECTION)
            assert await read_manifest(db, METADATA_COLLECTION) == manifest
            assert decode_manifest(actual) == manifest
            assert actual["description"] == snapshot["description"]
            actual_text = next(field for field in actual["fields"] if field["name"] == "text")
            assert actual_text["description"] == json_text(manifest)
            assert await db.call("list_aliases") == initial_aliases
            assert embedding.usage.requests == 0
            save("metadata-results.json", {"collection": METADATA_COLLECTION, "manifest": manifest, "collection_description_bytes": description_bytes, "manifest_description_bytes": payload_bytes, "collection_description_max_bytes": COLLECTION_DESCRIPTION_MAX_BYTES, "manifest_application_max_bytes": MANIFEST_DESCRIPTION_MAX_BYTES, "rejected_metadata_inputs": rejected, "roundtrip_equal": True, "encoding_requests": 0})
        finally:
            if created:
                actual_manifest = await read_manifest(db, METADATA_COLLECTION)
                assert actual_manifest["test_owner"] == owner
                assert not (await db.call("list_aliases", METADATA_COLLECTION))["aliases"]
                await db.call("drop_collection", METADATA_COLLECTION)
        assert set(await db.call("list_collections")) == initial_collections
        assert await db.call("list_aliases") == initial_aliases


async def build(corpus: RetrievalCorpus) -> None:
    async with EmbeddingClient.from_env() as embedding, MilvusConnection.from_env() as db:
        aliases = (await db.call("list_aliases"))["aliases"]
        if PUBLISHED_ALIAS in aliases:
            retriever = await HybridRetriever.open(db, corpus, embedding)
            collection = await published_collection(db)
            save("build-reuse.json", {"collection": collection, "manifest": await read_manifest(db, collection), "directory_encoding_requests": embedding.usage.requests, "retriever": type(retriever).__name__})
            assert embedding.usage.requests == 0
            return
        try:
            await HybridRetriever.open(db, corpus, embedding)
        except IndexCompatibilityError:
            assert embedding.usage.requests == 0
        else:
            raise AssertionError("未发布索引进入查询")
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        # 一次收费构建闸门；失败保留候选并由协调会话处理，复验不自动重新编码。
        with (EVIDENCE / "directory-build-attempt.json").open("x", encoding="utf-8") as file:
            json.dump({"sources": corpus.sources, "started_at": time.time()}, file)
        started = time.monotonic()
        try:
            report = await build_index(db, corpus, embedding)
        finally:
            save("directory-encoding-usage.json", {"usage": asdict(embedding.usage), "elapsed_seconds": time.monotonic() - started})
        assert report["encoding_usage"]["requests"] == 67
        save("build-result.json", report)


async def check(corpus: RetrievalCorpus, persistence: bool = False) -> None:
    async with EmbeddingClient.from_env() as embedding, MilvusConnection.from_env() as db:
        started = time.monotonic()
        completed = False
        report = {"mode": "persistence" if persistence else "check", "fusion": "rrf", "rrf_k": 60, "bm25_depth": 50, "dense_depth": 50, "limit": 10, "cases": []}
        try:
            retriever = await HybridRetriever.open(db, corpus, embedding)
            assert embedding.usage.requests == 0
            collection = await published_collection(db)
            manifest = await read_manifest(db, collection)
            report.update(collection=collection, manifest=manifest, startup_directory_encoding_requests=embedding.usage.requests)
            if persistence:
                execution = await retriever.search("杠铃卧推")
                assert execution.collection == collection
                report["cases"].append({"query": "杠铃卧推", "hybrid": [asdict(item) for item in execution.candidates]})
                previous = json.loads((EVIDENCE / "retrieval-results.json").read_text(encoding="utf-8"))
                assert report["collection"] == previous["collection"] and manifest == previous["manifest"]
                assert report["cases"][0]["hybrid"] == previous["public_search"]
                assert embedding.usage.requests == 1
                report["directory_encoding_requests"] = 0
                completed = True
                return
            first_vector = None
            for query in ("杠铃卧推", "拉背", "barbell bench press", "dumbbell 哑铃练背", "坐着用哑铃向上推举锻炼肩部"):
                case = {"query": query}
                report["cases"].append(case)
                vector = await embedding.encode_query(query)
                if first_vector is None:
                    first_vector = vector
                for mode in ("bm25", "dense", "hybrid"):
                    results = await search_vectors(db, collection, corpus.catalog, query, vector, mode=mode)
                    case[mode] = [asdict(item) for item in results]
                    if mode in ("dense", "hybrid"):
                        assert results
                case["name_substring_ids"] = [item.id for item in corpus.catalog.search(query)][:10]
            report["filtered"] = []
            for conditions in (
                SearchConditions(equipment="dumbbell"),
                SearchConditions(body_part="back"),
                SearchConditions(equipment="dumbbell", body_part="back"),
                SearchConditions(available_equipment=("body weight", "dumbbell"), body_part="back", excluded_ids=("0001",)),
                SearchConditions(target="lats"),
                SearchConditions(muscle_group="latissimus dorsi"),
                SearchConditions(equipment="dumbbell", body_part="cardio"),
                SearchConditions(available_equipment=()),
                SearchConditions(excluded_ids=tuple(sorted(corpus.catalog.exercise_ids))),
            ):
                catalog_match_count = sum(conditions.matches(item) for item in corpus.catalog.all())
                case = {"conditions": asdict(conditions), "expression": conditions.expression(corpus.catalog), "catalog_match_count": catalog_match_count}
                report["filtered"].append(case)
                for mode in ("bm25", "dense", "hybrid"):
                    results = await search_vectors(db, collection, corpus.catalog, "杠铃卧推", first_vector, conditions=conditions, mode=mode)
                    case[mode] = [asdict(item) for item in results]
                    assert all(conditions.matches(corpus.catalog.get(item.id)) for item in results)
                    if catalog_match_count == 0:
                        assert results == []
            report["rrf_parameters"] = []
            for rrf_k in (1, 60, 16383):
                results = await search_vectors(db, collection, corpus.catalog, "杠铃卧推", first_vector, rrf_k=rrf_k, bm25_depth=30, dense_depth=40, limit=3)
                report["rrf_parameters"].append({"rrf_k": rrf_k, "bm25_depth": 30, "dense_depth": 40, "limit": 3, "results": [asdict(item) for item in results]})
                assert len(results) <= 3
                assert [item.score for item in results] == sorted([item.score for item in results], reverse=True)
            excluded = tuple(sorted(corpus.catalog.exercise_ids - {"0001"}))
            leading_zero = await search_vectors(db, collection, corpus.catalog, "杠铃卧推", first_vector, conditions=SearchConditions(excluded_ids=excluded))
            report["leading_zero"] = [asdict(item) for item in leading_zero]
            assert [item.id for item in leading_zero] == ["0001"]
            before = embedding.usage.requests
            for kwargs in ({"rrf_k": float("nan")}, {"limit": 0}, {"conditions": SearchConditions(equipment='dumbbell" or id != "')}, {"conditions": SearchConditions(excluded_ids=("0000",))}):
                try:
                    await retriever.search("杠铃卧推", **kwargs)
                except IndexCompatibilityError:
                    pass
                else:
                    raise AssertionError("非法公开输入被接受")
            assert embedding.usage.requests == before
            report["invalid_public_query_encoding_requests"] = 0
            report["public_search"] = [asdict(item) for item in (await retriever.search("杠铃卧推")).candidates]
            injection = '哑铃\" or id != \"'
            report["query_injection_text"] = [asdict(item) for item in (await retriever.search(injection, conditions=SearchConditions(equipment="dumbbell"))).candidates]
            assert all(item["equipment"] == "dumbbell" for item in report["query_injection_text"])
            completed = True
        finally:
            report["query_encoding_usage"] = asdict(embedding.usage)
            report["elapsed_seconds"] = time.monotonic() - started
            report["completed"] = completed
            name = "persistence-results.json" if persistence else "retrieval-results.json"
            save(name if completed else "query-failure-results.json", report)


async def failures(corpus: RetrievalCorpus) -> None:
    async with EmbeddingClient.from_env() as embedding, MilvusConnection.from_env() as db:
        old = await published_collection(db)
        expected = corpus.compatibility(embedding, await db.call("get_server_version"))
        await validate_index(db, old, corpus, expected)
        vector = await embedding.encode_query("杠铃卧推")
        before_results = await search_vectors(db, old, corpus.catalog, "杠铃卧推", vector)
        assert not await db.call("has_collection", TEST_COLLECTION), "测试对象已存在，拒绝覆盖"
        owner = uuid4().hex
        manifest = {"collection": TEST_COLLECTION, "publisher": PUBLISHER, "test_owner": owner, "compatibility": {**expected, "encoding_rule_version": "deliberately-incompatible-test-version"}}
        created = False
        failure_collection = None
        try:
            await db.call("create_collection", TEST_COLLECTION, schema=create_schema(manifest), index_params=create_indexes(), consistency_level="Strong")
            created = True
            try:
                await validate_index(db, TEST_COLLECTION, corpus, expected)
            except IndexCompatibilityError as error:
                version_error = str(error)
            else:
                raise AssertionError("不兼容测试索引被接受")
            original_collections = set(await db.call("list_collections"))
            illegal_records = ({**corpus.records[0], "text": ""}, *corpus.records[1:])
            invalid = replace(corpus, records=illegal_records)
            before_usage = embedding.usage.requests
            try:
                await build_index(db, invalid, embedding)
            except ValueError as error:
                build_error = str(error)
            else:
                raise AssertionError("非法构建成功")
            assert embedding.usage.requests == before_usage
            new_collections = set(await db.call("list_collections")) - original_collections
            assert len(new_collections) == 1
            failure_collection = new_collections.pop()
            failed_manifest = await read_manifest(db, failure_collection)
            assert failure_collection.startswith(BUILD_PREFIX)
            assert failed_manifest["publisher"] == PUBLISHER and failed_manifest["collection"] == failure_collection
            assert failed_manifest["build_id"] == failure_collection[len(BUILD_PREFIX):]
            try:
                await publish_index(db, failure_collection, corpus, embedding)
            except IndexCompatibilityError as error:
                incomplete_error = str(error)
            else:
                raise AssertionError("不完整候选被接受")
            try:
                await validate_index(db, failure_collection, invalid, invalid.compatibility(embedding, await db.call("get_server_version")))
            except IndexCompatibilityError as error:
                count_error = str(error)
                assert count_error == "索引数量不符"
            else:
                raise AssertionError("空构建候选数量校验通过")
            assert await published_collection(db) == old
            after_results = await search_vectors(db, old, corpus.catalog, "杠铃卧推", vector)
            assert before_results == after_results
            save("failure-results.json", {"alias": PUBLISHED_ALIAS, "old_collection": old, "current_collection": await published_collection(db), "version_error": version_error, "failed_collection": failure_collection, "build_error": build_error, "incomplete_error": incomplete_error, "count_error": count_error, "failed_build_encoding_requests": 0, "before_results": [asdict(item) for item in before_results], "after_results": [asdict(item) for item in after_results], "cleanup_objects": [TEST_COLLECTION, failure_collection]})
        finally:
            if created:
                actual = await read_manifest(db, TEST_COLLECTION)
                assert actual["test_owner"] == owner and actual["collection"] == TEST_COLLECTION
                assert not (await db.call("list_aliases", TEST_COLLECTION))["aliases"]
                await db.call("drop_collection", TEST_COLLECTION)
            if failure_collection is not None:
                actual = await read_manifest(db, failure_collection)
                assert actual["publisher"] == PUBLISHER and actual["collection"] == failure_collection
                assert await published_collection(db) != failure_collection
                await db.call("drop_collection", failure_collection)
        assert await published_collection(db) == old
        assert not await db.call("has_collection", TEST_COLLECTION)
        save("final-index-state.json", {"collections": await db.call("list_collections"), "aliases": await db.call("list_aliases"), "published_collection": old, "retained_manifest": await read_manifest(db, old)})


async def boundaries(corpus: RetrievalCorpus) -> None:
    async with MilvusConnection.from_env() as db:
        collection = await published_collection(db)
        operation = asyncio.create_task(db.call("query", collection, filter='id != ""', output_fields=["id"], limit=1321, consistency_level="Strong"))
        await asyncio.sleep(0)
        operation.cancel()
        try:
            await operation
        except asyncio.CancelledError:
            cancelled = True
        else:
            raise AssertionError("取消操作未传播")
        assert await db.call("get_server_version") == "3.0.2"
        missing = "fit_agent_t5_test_missing_" + uuid4().hex
        assert not await db.call("has_collection", missing)
        try:
            await db.call("search", missing, data=["哑铃"], anns_field="sparse", limit=1)
        except MilvusException as error:
            infrastructure_error = type(error).__name__
        else:
            raise AssertionError("缺失索引被转换为空结果")
    assert db._client is None
    save("lifecycle-results.json", {"cancelled": cancelled, "next_real_call_succeeded": True, "client_closed": True, "missing_index_error": infrastructure_error, "query_directory_encoding_requests": 0})


async def switch(corpus: RetrievalCorpus) -> None:
    async with EmbeddingClient.from_env() as embedding, MilvusConnection.from_env() as db:
        old = await published_collection(db)
        expected = corpus.compatibility(embedding, await db.call("get_server_version"))
        await validate_index(db, old, corpus, expected)
        old_manifest = await read_manifest(db, old)
        collection = BUILD_PREFIX + uuid4().hex
        assert not await db.call("has_collection", collection)
        manifest = {**old_manifest, "collection": collection, "build_id": collection[len(BUILD_PREFIX):], "created_at": time.time(), "verification_rebuild_from": old}
        await db.call("create_collection", collection, schema=create_schema(manifest), index_params=create_indexes(), consistency_level="Strong")
        # 切换验证复用正式集合中的真实模型向量，完整目录编码请求保持为零。
        for start in range(0, len(corpus.records), 100):
            batch = corpus.records[start:start + 100]
            rows = await db.call("get", old, ids=[record["id"] for record in batch], output_fields=["id", "text", "dense", "equipment", "body_part", "target", "muscle_group"], consistency_level="Strong")
            inserted = await db.call("insert", collection, data=rows)
            assert inserted["insert_count"] == len(batch)
        await db.call("flush", collection)
        assert await published_collection(db) == old
        published_manifest = await publish_index(db, collection, corpus, embedding)
        assert await published_collection(db) == collection
        assert await read_manifest(db, old) == old_manifest
        assert embedding.usage.requests == 0
        save("alias-switch-results.json", {"old_collection": old, "new_collection": collection, "alias": PUBLISHED_ALIAS, "manifest": published_manifest, "old_collection_retained": True, "directory_encoding_requests": 0, "collections": await db.call("list_collections")})


async def service_failure() -> None:
    try:
        async with MilvusConnection.from_env() as db:
            await db.call("get_server_version")
    except MilvusException as error:
        save("service-failure-results.json", {"error_type": type(error).__name__, "empty_candidates_returned": False})
    else:
        raise AssertionError("服务仍可用；此入口必须在本项目 Milvus 停止期间执行")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("inputs", "metadata", "build", "check", "failures", "persistence", "boundaries", "switch", "service_failure"))
    mode = parser.parse_args().mode
    corpus = RetrievalCorpus.load()
    if mode == "inputs":
        save("input-validation.json", check_inputs(corpus))
    elif mode == "metadata":
        asyncio.run(metadata(corpus))
    elif mode == "build":
        asyncio.run(build(corpus))
    elif mode == "failures":
        asyncio.run(failures(corpus))
    elif mode == "boundaries":
        asyncio.run(boundaries(corpus))
    elif mode == "switch":
        asyncio.run(switch(corpus))
    elif mode == "service_failure":
        asyncio.run(service_failure())
    else:
        asyncio.run(check(corpus, persistence=mode == "persistence"))


if __name__ == "__main__":
    main()
