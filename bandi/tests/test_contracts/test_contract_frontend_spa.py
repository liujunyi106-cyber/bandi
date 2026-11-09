"""契约：FastAPI 单端口托管前端 dist 时，静态资源与 SPA 深链可访问，保留前缀不回落 HTML。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bandi.api.app import _is_spa_asset_path, _resolve_frontend_dist, _SPAStaticFiles


def _build_dist(root: Path) -> Path:
    dist = root / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return dist


def test_spa_asset_path_excludes_api_ws_and_probes() -> None:
    assert _is_spa_asset_path("/")
    assert _is_spa_asset_path("/work")
    assert _is_spa_asset_path("/assets/app.js")
    assert not _is_spa_asset_path("/api/health")
    assert not _is_spa_asset_path("/ws/company")
    assert not _is_spa_asset_path("/health")
    assert not _is_spa_asset_path("/openapi.json")


def test_spa_static_files_serves_assets_and_deep_link(tmp_path: Path) -> None:
    dist = _build_dist(tmp_path)
    app = FastAPI()
    app.mount("/", _SPAStaticFiles(directory=str(dist), html=True), name="frontend-spa")
    client = TestClient(app)

    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/work").text == "<html>spa</html>"
    assert client.get("/").text == "<html>spa</html>"


def test_spa_static_files_keeps_reserved_prefixes_404(tmp_path: Path) -> None:
    dist = _build_dist(tmp_path)
    app = FastAPI()
    app.mount("/", _SPAStaticFiles(directory=str(dist), html=True), name="frontend-spa")
    client = TestClient(app)

    for path in ("/api/missing", "/ws/missing", "/health/missing", "/docs/missing"):
        response = client.get(path)
        assert response.status_code == 404
        assert response.text != "<html>spa</html>"


def test_resolve_frontend_dist_prefers_explicit_override(tmp_path: Path, monkeypatch) -> None:
    dist = _build_dist(tmp_path)
    monkeypatch.setenv("BANDI_FRONTEND_DIST", str(dist))
    assert _resolve_frontend_dist() == dist
