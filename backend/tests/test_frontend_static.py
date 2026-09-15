"""F6-01c：前端构建产物静态托管与 SPA 回退（10.1；冻结口径见 stage6-transport-freeze.md §5）。

覆盖：

1. ``dist`` 存在时 ``/`` 与前端路由路径回退 ``index.html``，真实产物文件（``assets/*``）按
   文件类型返回；``/api/*`` 不被静态路由吞掉（未匹配的 API 路径仍是 404，不是 HTML）。
2. ``dist`` 缺失（未构建／未打包）时非 ``/api`` 路径 404，且 ``/api`` 与 ``/healthz`` 照常工作
   （后端不依赖前端产物）。

全程离线：``tmp_path`` 临时库与临时 ``dist`` 目录，不读仓库真实构建产物。
"""

from pathlib import Path

from fastapi.testclient import TestClient

from api.app import _dist_file, create_app

BASE_URL = "http://127.0.0.1"
INDEX_HTML = '<!doctype html><html><body><div id="root"></div></body></html>'


def _write_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (dist / "assets" / "app.js").write_text(
        "console.log('fit-agent');", encoding="utf-8"
    )
    return dist


def test_serves_build_output_and_falls_back_to_index(tmp_path: Path) -> None:
    dist = _write_dist(tmp_path)
    app = create_app(tmp_path / "data", frontend_dist=dist)
    with TestClient(app, base_url=BASE_URL) as client:
        root = client.get("/")
        assert root.status_code == 200
        assert root.text == INDEX_HTML
        # SPA 回退：前端路由（无对应文件）返回同一份 index.html
        spa = client.get("/profile")
        assert spa.status_code == 200
        assert spa.text == INDEX_HTML
        # 真实产物文件按自身内容与类型返回
        asset = client.get("/assets/app.js")
        assert asset.status_code == 200
        assert asset.text == "console.log('fit-agent');"
        assert "javascript" in asset.headers["content-type"]

        # /api 与 /healthz 不被静态路由抢走
        assert client.get("/healthz").json()["status"] == "ok"
        assert client.get("/api/profile").status_code == 200
        unknown_api = client.get("/api/no-such-endpoint")
        assert unknown_api.status_code == 404
        assert unknown_api.headers["content-type"].startswith("application/json")


def test_missing_dist_keeps_api_working_and_non_api_404(tmp_path: Path) -> None:
    """未构建前端时后端照常服务：只有非 ``/api`` 路径 404（不假装已交付前端）。"""
    app = create_app(tmp_path / "data", frontend_dist=tmp_path / "no-such-dist")
    with TestClient(app, base_url=BASE_URL) as client:
        assert client.get("/").status_code == 404
        assert client.get("/profile").status_code == 404
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/profile").status_code == 200


def test_static_route_never_escapes_dist(tmp_path: Path) -> None:
    """``../`` 逃逸不在 dist 内解析：逃逸请求按未命中处理，不读 dist 之外的文件。"""
    dist = _write_dist(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("not part of the build", encoding="utf-8")

    assert _dist_file(dist, "assets/app.js") == (dist / "assets" / "app.js")
    assert _dist_file(dist, "../outside.txt") is None
    assert _dist_file(dist, "assets/../../outside.txt") is None
    assert _dist_file(dist, "missing.js") is None

    app = create_app(tmp_path / "data", frontend_dist=dist)
    with TestClient(app, base_url=BASE_URL) as client:
        # 裸 `..` 会被 HTTP 客户端发送前归一化（到不了服务端校验，断言恒真）；
        # 用百分号编码的点段把 `assets/../outside.txt` 真正送进路由：服务端自己的
        # `_dist_file` 拒绝越界，按未命中回退 index.html，不读 dist 之外文件。
        escaped = client.get("/assets/%2e%2e/outside.txt")
        assert escaped.status_code == 200
        assert escaped.text == INDEX_HTML
        assert "not part of the build" not in escaped.text
