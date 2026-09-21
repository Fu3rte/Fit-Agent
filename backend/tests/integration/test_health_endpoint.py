# 健康探针集成测试：``/healthz`` 的结果全部来自组合根装配的 ``services.health``。
# 依据：架构决议 §5.1（app/api/app.py 承载 /healthz）与传输层不持有 Database 的边界。

from pathlib import Path

import httpx

from app.bootstrap import create_app


async def test_healthz_reports_the_probe_values(tmp_path: Path) -> None:
    """真实 lifespan 启动后：四个字段全部就位，连接状态为 open，无 Provider 配置即 False。"""
    data_dir = tmp_path / "data"
    app = create_app(data_dir=data_dir, frontend_dist=tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "open",
        "business_timezone": str(app.state.business_timezone),
        "provider_has_api_key": False,
    }
