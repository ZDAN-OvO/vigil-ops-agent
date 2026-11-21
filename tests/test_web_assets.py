"""在没有 PostgreSQL 时仍核对普通 wheel 的网页和 Python 资源。"""

import asyncio
from zipfile import ZipFile

import httpx
import pytest

from aiops import cli, web_api
from aiops.config import PROJECT_ROOT, Settings
from aiops.web_assets import check_web_assets


def test_wheel_contains_frontend_entry_assets_skills_and_migrations(built_wheel, tmp_path):
    with ZipFile(built_wheel) as archive:
        resources = set(archive.namelist())
        assert sum(name.startswith("aiops/_builtin_skills/") for name in resources) >= 5
        assert "aiops/migrations/0013_action_safety.sql" in resources
        assert any(name.endswith(".dist-info/entry_points.txt") for name in resources)
        static = {name for name in resources if name.startswith("aiops/static/")}
        for name in static:
            archive.extract(name, tmp_path)

    source = PROJECT_ROOT / "web/dist"
    files = {path.relative_to(source) for path in source.rglob("*") if path.is_file()}
    assert static == {f"aiops/static/{path.as_posix()}" for path in files}
    installed = tmp_path / "aiops/static"
    check_web_assets(installed)
    for path in files:
        assert (installed / path).read_bytes() == (source / path).read_bytes()


def test_missing_web_assets_explain_source_build(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(web_api, "STATIC_DIR", tmp_path / "missing")
    with pytest.raises(ValueError, match="make web-build"):
        web_api.require_web_assets()
    assert cli.main(["serve"]) == 1
    assert "make web-build" in capsys.readouterr().err

    async def run():
        settings = Settings(_env_file=None)
        app = web_api.build_web_app(
            settings, sessions=None, safety=None, auth=web_api.BrowserAuth(interactive=False)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=settings.web_origin
        ) as client:
            assert (await client.get("/")).status_code == 503
            for path in ("/static/assets/missing.js", "/api/missing"):
                assert (await client.get(path)).status_code == 404

    asyncio.run(run())


def test_build_assets_reject_incomplete_output(tmp_path):
    (tmp_path / "index.html").write_text(
        '<script src="/static/assets/index.js"></script>'
        '<link rel="stylesheet" href="/static/assets/index.css">',
        encoding="utf-8",
    )
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/index.js").write_text("/* fixture */", encoding="utf-8")
    with pytest.raises(ValueError, match="构建资源无效或缺失"):
        check_web_assets(tmp_path)
