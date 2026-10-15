import argparse
import asyncio
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from pymilvus import MilvusException

from app.application.business import retrieval_index as ri
from app.application.business import retrieval_text as rt
from app.application.business.retrieval_config import (
    EmbeddingConfig,
    MissingRetrievalConfig,
)
from app.application.business.retrieval_embedding import EmbeddingClient
from app.application.business.retrieval_index import (
    BUILD_PREFIX,
    PUBLISHED_ALIAS,
    IndexCompatibilityError,
    MilvusConnection,
    RetrievalCorpus,
    decode_manifest,
    json_text,
    published_collection,
    validated_manifest,
)
from app.application.business.retrieval_search import (
    HybridRetriever,
    SearchConditions,
    search_vectors,
)

# P2 验证：轻量查询路径与实例内身份缓存。全程不调用在线 Embedding，不建删集合或 alias，
# 不替换 SDK 返回；仅统计真实 MilvusConnection.call 的次数。

EVIDENCE = Path(__file__).resolve().parents[2] / "tmp" / "action-retrieval-t5"


def save(name: str, data: dict) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(data, ensure_ascii=False), flush=True)


class CallCounter:
    # 只统计真实调用次数；调用原样透传到 MilvusConnection.call，不改写返回。
    def __init__(self, db: MilvusConnection) -> None:
        self._original = db.call
        self.counts: dict[str, int] = {}
        db.call = self._call

    async def _call(self, method: str, *args, **kwargs):
        self.counts[method] = self.counts.get(method, 0) + 1
        return await self._original(method, *args, **kwargs)

    def reset(self) -> None:
        self.counts = {}

    def snapshot(self) -> dict:
        return dict(sorted(self.counts.items()))


def manifest_sha256(fingerprint: str) -> str:
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def full_get_calls(corpus: RetrievalCorpus) -> int:
    return (len(corpus.records) + 99) // 100


def full_reads(counts: dict) -> dict:
    # 全量回读的直接证据：get / query(count) / load_collection。
    return {key: counts.get(key, 0) for key in ("get", "query", "load_collection")}


async def validation() -> int:
    async with MilvusConnection.from_env() as db:
        corpus = RetrievalCorpus.load()
        embedding = EmbeddingClient.from_env()
        counter = CallCounter(db)
        expected_gets = full_get_calls(corpus)

        collections_before = sorted(await db.call("list_collections"))
        aliases_before = await db.call("list_aliases")
        binding_before = await db.call("describe_alias", PUBLISHED_ALIAS)
        published = await published_collection(db)

        # A 启动完整验证成功；同一集合下一次校验的全目录 get 为 0。
        counter.reset()
        retriever = await HybridRetriever.open(db, corpus, embedding)
        startup = counter.snapshot()
        assert isinstance(retriever, HybridRetriever)
        assert retriever._validated is not None and retriever._validated[0] == published
        assert startup.get("get", 0) == expected_gets, startup
        counter.reset()
        reused = await retriever._validate_published()
        reuse = counter.snapshot()
        assert reused == published
        assert full_reads(reuse) == {"get": 0, "query": 0, "load_collection": 0}, reuse
        save(
            "p2-validation-startup.json",
            {
                "published_collection": published,
                "startup": startup,
                "startup_get_calls": startup.get("get", 0),
                "startup_manifest_sha256": manifest_sha256(retriever._validated[1]),
                "reuse": reuse,
                "reuse_full_directory_get_calls": reuse.get("get", 0),
                "embedding_api_requests": 0,
            },
        )

        # B 并发校验同一身份，仅一次完整验证。
        counter.reset()
        concurrent_retriever = HybridRetriever(db, corpus, embedding)
        await asyncio.gather(
            concurrent_retriever._ensure_collection(published),
            concurrent_retriever._ensure_collection(published),
        )
        concurrent = counter.snapshot()
        assert concurrent.get("get", 0) == expected_gets, concurrent
        save(
            "p2-validation-concurrent.json",
            {
                "concurrent": concurrent,
                "full_directory_get_calls": concurrent.get("get", 0),
                "expected_single_full_validation_gets": expected_gets,
                "embedding_api_requests": 0,
            },
        )

        # C 重建实例后首次验证重新执行完整校验。
        counter.reset()
        rebuilt = HybridRetriever(db, corpus, embedding)
        await rebuilt._ensure_collection(published)
        rebuild = counter.snapshot()
        assert rebuild.get("get", 0) == expected_gets, rebuild
        save(
            "p2-validation-rebuild.json",
            {
                "rebuilt_instance_full_directory_get_calls": rebuild.get("get", 0),
                "expected_full_directory_get_calls": expected_gets,
                "embedding_api_requests": 0,
            },
        )

        # D 私有单集合入口读取两个实际保留的真实构建集合；身份变化触发完整校验。
        build_collections = [
            name for name in collections_before if name.startswith(BUILD_PREFIX)
        ]
        server = await db.call("get_server_version")
        expected = corpus.compatibility(embedding, server)
        compatible: list[str] = []
        incompatible: list[str] = []
        for name in build_collections:
            try:
                await validated_manifest(db, name, expected)
                compatible.append(name)
            except IndexCompatibilityError:
                incompatible.append(name)
        if len(compatible) < 2:
            save(
                "p2-validation-blocked.json",
                {
                    "build_collections": build_collections,
                    "compatible": compatible,
                    "incompatible": incompatible,
                    "reason": "无第二份与当前输入兼容的保留构建集合",
                },
            )
            return 2
        first_collection, second_collection = compatible[0], compatible[1]
        counter.reset()
        identity_retriever = HybridRetriever(db, corpus, embedding)
        await identity_retriever._ensure_collection(first_collection)
        first_pass = counter.snapshot()
        counter.reset()
        await identity_retriever._ensure_collection(second_collection)
        second_pass = counter.snapshot()
        counter.reset()
        await identity_retriever._ensure_collection(second_collection)
        third_pass = counter.snapshot()
        assert first_pass.get("get", 0) == expected_gets, first_pass
        assert second_pass.get("get", 0) == expected_gets, second_pass
        assert full_reads(third_pass) == {"get": 0, "query": 0, "load_collection": 0}, third_pass
        assert identity_retriever._validated is not None
        assert identity_retriever._validated[0] == second_collection
        save(
            "p2-validation-identity.json",
            {
                "build_collections": build_collections,
                "compatible": compatible,
                "incompatible": incompatible,
                "first_collection": first_collection,
                "second_collection": second_collection,
                "first_pass_get_calls": first_pass.get("get", 0),
                "second_pass_get_calls": second_pass.get("get", 0),
                "third_pass": third_pass,
                "third_pass_full_directory_get_calls": third_pass.get("get", 0),
                "cached_collection": identity_retriever._validated[0],
                "alias_unchanged": (await db.call("describe_alias", PUBLISHED_ALIAS)) == binding_before,
                "embedding_api_requests": 0,
            },
        )

        # F 失败与取消不记录未知身份；资源仍可关闭。
        counter.reset()
        failing = HybridRetriever(db, corpus, embedding)
        missing = "fit_agent_p2_missing_" + uuid4().hex
        try:
            await failing._ensure_collection(missing)
        except MilvusException as error:
            failure_error = type(error).__name__
        else:
            raise AssertionError("缺失集合未明确失败")
        assert failing._validated is None

        cancelling = HybridRetriever(db, corpus, embedding)
        task = asyncio.create_task(cancelling._ensure_collection(second_collection))
        for _ in range(8):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            cancel_outcome = "CancelledError"
        else:
            raise AssertionError("校验取消未传播")
        assert cancelling._validated is None
        assert await db.call("get_server_version") == "3.0.2"
        save(
            "p2-validation-failure.json",
            {
                "missing_collection_error": failure_error,
                "missing_identity_recorded": failing._validated is not None,
                "cancel_outcome": cancel_outcome,
                "cancelled_identity_recorded": cancelling._validated is not None,
                "connection_usable_after_cancel": True,
                "embedding_api_requests": 0,
            },
        )

        collections_after = sorted(await db.call("list_collections"))
        aliases_after = await db.call("list_aliases")
        binding_after = await db.call("describe_alias", PUBLISHED_ALIAS)
        assert collections_after == collections_before
        assert aliases_after == aliases_before
        assert binding_after == binding_before
        save(
            "p2-validation-state.json",
            {
                "collections": collections_after,
                "aliases": aliases_after,
                "binding": binding_after,
                "collections_aliases_binding_unchanged": True,
                "embedding_api_requests": 0,
            },
        )
    return 0


async def config_checks() -> int:
    async with MilvusConnection.from_env() as db:
        corpus = RetrievalCorpus.load()
        embedding = EmbeddingClient.from_env()
        server = await db.call("get_server_version")
        collection = await published_collection(db)
        expected = corpus.compatibility(embedding, server)

        # 正确期待与真实清单一致（轻量校验通过）。
        await validated_manifest(db, collection, expected)
        correct_expected = True

        # 本地非法模型配置（不发 API 请求）。
        wrong_model_config = embedding.config.model_copy(
            update={"model": "local-wrong-model-p2"}
        )
        wrong_model_expected = corpus.compatibility(
            EmbeddingClient(wrong_model_config), server
        )
        try:
            await validated_manifest(db, collection, wrong_model_expected)
        except IndexCompatibilityError as error:
            wrong_model_error = str(error)
        else:
            raise AssertionError("错误模型期待配置未失败")
        assert wrong_model_error == "索引与输入配置版本不兼容"

        # 非法维度在本地配置构造阶段即失败，不触达服务。
        try:
            EmbeddingConfig(
                api_key="local-probe",
                base_url=embedding.config.base_url,
                model=embedding.config.model,
                dimensions=512,
            )
        except ValueError as error:
            dimension_error = type(error).__name__
        else:
            raise AssertionError("非法维度配置被接受")

        # 篡改期待维度属于本地配置输入错误，不宣称服务端返回非法数据。
        tampered_expected = {**expected, "dimensions": 512}
        try:
            await validated_manifest(db, collection, tampered_expected)
        except IndexCompatibilityError as error:
            tampered_error = str(error)
        else:
            raise AssertionError("篡改维度期待配置未失败")
        assert tampered_error == "索引与输入配置版本不兼容"

        # 损坏清单仅测试原清单校验函数，不替换 SDK 返回。
        snapshot = await db.call("describe_collection", collection)
        rejected: list[str] = []
        for label in ("missing_header", "changed_size", "changed_payload", "oversized_header"):
            invalid = deepcopy(snapshot)
            text_field = next(f for f in invalid["fields"] if f["name"] == "text")
            if label == "missing_header":
                invalid["description"] = ""
            elif label == "changed_size":
                header = json.loads(invalid["description"])
                header["bytes"] += 1
                invalid["description"] = json_text(header)
            elif label == "changed_payload":
                text_field["description"] = text_field["description"][:-1] + "0"
            else:
                invalid["description"] = "a" * 1025
            try:
                decode_manifest(invalid)
            except IndexCompatibilityError:
                rejected.append(label)
            else:
                raise AssertionError("损坏清单被接受: " + label)

        save(
            "p2-config.json",
            {
                "correct_expected_passed": correct_expected,
                "wrong_model_error": wrong_model_error,
                "dimension_config_error": dimension_error,
                "tampered_dimension_error": tampered_error,
                "corrupt_manifest_rejected": rejected,
                "corrupt_manifest_tested_function": "decode_manifest",
                "embedding_client_entered": False,
                "embedding_api_requests": 0,
                "note": "以上均为本地配置输入校验，未宣称服务端返回非法数据",
            },
        )
    return 0


async def search_checks() -> int:
    async with MilvusConnection.from_env() as db:
        corpus = RetrievalCorpus.load()
        collection = await published_collection(db)
        rows = await db.call(
            "get",
            collection,
            ids=["0001"],
            output_fields=["id", "text", "dense", *rt.MAPPING_FIELDS],
            consistency_level="Strong",
        )
        assert len(rows) == 1 and rows[0]["id"] == "0001"
        vector = rows[0]["dense"]
        assert len(vector) == ri.DIMENSIONS
        exercise = corpus.catalog.get("0001")
        modes = {}
        for mode in ("bm25", "dense", "hybrid"):
            conditions = (
                SearchConditions(equipment=exercise.equipment, excluded_ids=("0001",))
                if mode == "hybrid"
                else SearchConditions()
            )
            results = await search_vectors(
                db,
                collection,
                corpus.catalog,
                exercise.name,
                vector,
                mode=mode,
                conditions=conditions,
                limit=5,
            )
            assert results
            assert all(
                isinstance(item.id, str)
                and corpus.catalog.get(item.id) is not None
                and conditions.matches(corpus.catalog.get(item.id))
                for item in results
            )
            scores = [item.score for item in results]
            assert scores == sorted(scores, reverse=True)
            modes[mode] = {
                "filter": conditions.expression(corpus.catalog),
                "ids": [item.id for item in results],
                "scores": scores,
                "hard_constraints_verified": True,
            }
        save(
            "p2-search.json",
            {
                "vector_source_id": "0001",
                "collection": collection,
                "modes": modes,
                "ordering_and_filter_verified": True,
                "embedding_client_created": False,
                "embedding_api_requests": 0,
            },
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("validation", "config", "search"))
    mode = parser.parse_args().mode
    try:
        if mode == "validation":
            return asyncio.run(validation())
        if mode == "config":
            return asyncio.run(config_checks())
        return asyncio.run(search_checks())
    except MissingRetrievalConfig as error:
        print("BLOCKED 缺少必需配置: " + ", ".join(error.variables))
        return 2


if __name__ == "__main__":
    sys.exit(main())
