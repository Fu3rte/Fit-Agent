"""F6-01b：Provider 设置 HTTP 面（10.3；拼写冻结见 stage6-transport-freeze.md §1.4）。

覆盖：

1. 查询只回安全投影：``has_api_key``、Provider 标识与只读配置展示（协议／Base URL／模型 id／
   部署位置）；**不含**完整 Key、不含掩码、不含数据目录。
2. 录入／替换成功后查询与 ``/healthz`` 一致为 true；删除后 false，重复删除仍 false（幂等）。
3. 形状错误与空／纯空白 Key 一律 400 ``invalid_request``，且错误详情不回显 Key 内容。
4. Key 明文不进入任何响应；同数据目录跨重启后 ``has_api_key`` 保持一致。
5. 回环边界沿用既有中间件：非回环 Host 403。

全程离线：``tmp_path`` 临时库与假 Key；不发真实模型请求、不联网。
"""

from pathlib import Path

from fastapi.testclient import TestClient

from api.app import create_app
from runtime.models import QWEN37_FLASH

#: 与产品投影同一来源（不再写死历史 Provider 标识）。

BASE_URL = "http://127.0.0.1"
#: 辨识度明确的假 Key：出现在任何响应里即视为泄漏（不读取真实凭据）
FAKE_KEY = "sk-f6-01b-fake-key-not-a-real-credential"


def test_query_put_and_delete_roundtrip_without_key_leak(tmp_path: Path) -> None:
    app = create_app(tmp_path / "data")
    with TestClient(app, base_url=BASE_URL) as client:
        initial = client.get("/api/provider")
        assert initial.status_code == 200
        body = initial.json()
        assert body["provider"] == QWEN37_FLASH.provider
        assert body["has_api_key"] is False
        assert body["protocol"] == "openai-compatible"
        assert body["base_url"].startswith("http")
        assert body["model"]["name"]
        assert body["model"]["deployment"] == "cloud"
        assert "data_dir" not in body

        saved = client.put("/api/provider/api-key", json={"api_key": FAKE_KEY})
        assert saved.status_code == 200
        assert saved.json() == {"provider": QWEN37_FLASH.provider, "has_api_key": True}

        after = client.get("/api/provider")
        assert after.json()["has_api_key"] is True
        healthz = client.get("/healthz")
        assert healthz.json()["provider_has_api_key"] is True
        # 查询与健康检查都不回显 Key（含掩码）
        assert FAKE_KEY not in after.text
        assert FAKE_KEY not in healthz.text

        removed = client.delete("/api/provider/api-key")
        assert removed.status_code == 200
        assert removed.json() == {
            "provider": QWEN37_FLASH.provider,
            "has_api_key": False,
        }
        assert client.get("/api/provider").json()["has_api_key"] is False
        # 幂等：未配置再删一次仍是同一投影
        repeated = client.delete("/api/provider/api-key")
        assert repeated.status_code == 200
        assert repeated.json() == {
            "provider": QWEN37_FLASH.provider,
            "has_api_key": False,
        }


def test_put_rejects_bad_shape_and_empty_key_without_echoing_key(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path / "data")
    with TestClient(app, base_url=BASE_URL) as client:
        rejected = (
            {},  # 缺 api_key
            {"api_key": FAKE_KEY, "provider": QWEN37_FLASH.provider},  # 未登记字段
            {"api_key": 123},  # 非字符串
            {"api_key": ""},  # 空
            {"api_key": "   "},  # 纯空白
        )
        for body in rejected:
            response = client.put("/api/provider/api-key", json=body)
            assert response.status_code == 400, body
            assert response.json()["error_code"] == "invalid_request", body
            # 形状错误也不回显 Key 内容（含列表/字典里的 Key）
            assert FAKE_KEY not in response.text, body
            assert client.get("/api/provider").json()["has_api_key"] is False


def test_provider_status_survives_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    with TestClient(create_app(data_dir), base_url=BASE_URL) as client:
        client.put("/api/provider/api-key", json={"api_key": FAKE_KEY})
        assert client.get("/api/provider").json()["has_api_key"] is True
    # 同数据目录重启（同库）：Key 已落库且只经 has_api_key 体现
    with TestClient(create_app(data_dir), base_url=BASE_URL) as client:
        assert client.get("/api/provider").json()["has_api_key"] is True
        client.delete("/api/provider/api-key")
    with TestClient(create_app(data_dir), base_url=BASE_URL) as client:
        assert client.get("/api/provider").json()["has_api_key"] is False


def test_provider_route_respects_loopback_boundaries(tmp_path: Path) -> None:
    """10.1：Provider 路由同样只在回环 Host／Origin 下可用。"""
    app = create_app(tmp_path / "data")
    with TestClient(app, base_url="http://evil.example") as client:
        assert client.get("/api/provider").status_code == 403
    with TestClient(app, base_url=BASE_URL) as client:
        hostile = client.get("/api/provider", headers={"origin": "http://evil.example"})
        assert hostile.status_code == 403
        assert client.get("/api/provider").status_code == 200


def test_provider_label_and_credential_slot_follow_the_frozen_catalog_spec(
    tmp_path: Path,
) -> None:
    """Provider 标识与凭据槽位同取冻结模型目录（2026-09-13 复审 P2 的处置）。

    默认 Harness（无 ``harness.toml``）的模型是 Stage 6 目录项（owner env ``MODEL_NAME``
    实际值）：公开 ``provider`` 字段、``base_url``、模型 id 与**凭据槽位**必须同源，
    不得遗留写死的历史 Provider 标识。
    """
    data_dir = tmp_path / "data"
    with TestClient(create_app(data_dir), base_url=BASE_URL) as client:
        body = client.get("/api/provider").json()
        assert body["provider"] == QWEN37_FLASH.provider == "aliyun-bailian"
        assert body["base_url"] == QWEN37_FLASH.base_url
        assert body["model"]["name"] == QWEN37_FLASH.model_id
        client.put("/api/provider/api-key", json={"api_key": FAKE_KEY})
    # 凭据确实落在该槽位上：同目录重启后仍可经 has_api_key 读到
    with TestClient(create_app(data_dir), base_url=BASE_URL) as client:
        assert client.get("/api/provider").json()["has_api_key"] is True


def test_configured_model_selects_its_own_credential_slot(tmp_path: Path) -> None:
    """换模型即换槽位：配了 DeepSeek 目录项时不再复用 Stage 6 槽位的 Key（反之亦然）。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "harness.toml").write_text(
        'model = "deepseek-flash"\n', encoding="utf-8"
    )
    with TestClient(create_app(data_dir), base_url=BASE_URL) as client:
        body = client.get("/api/provider").json()
        assert body["provider"] == "deepseek"
        assert body["base_url"] == "https://api.deepseek.com"
        assert body["model"]["name"] == "deepseek-flash"
        assert body["has_api_key"] is False  # 槽位不同：不误用另一端点的 Key
        client.put("/api/provider/api-key", json={"api_key": FAKE_KEY})
    with TestClient(create_app(data_dir), base_url=BASE_URL) as client:
        assert client.get("/api/provider").json()["has_api_key"] is True
