"""配置和导入边界不能回落到旧项目。"""

import ast
from pathlib import Path

from aiops.config import PROJECT_ROOT, Settings


def test_project_paths_do_not_depend_on_working_directory(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("VIGIL_ENVIRONMENT=test\n")
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None)
    assert settings.environment == "local"
    assert settings.runtime_path == PROJECT_ROOT / "runtime"
    assert Path(Settings.model_config["env_file"]) == PROJECT_ROOT / ".env"
    assert PROJECT_ROOT == Path(__file__).resolve().parents[1]


def test_source_does_not_import_archive():
    for path in (PROJECT_ROOT / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert all(name.split(".")[0] not in {"deprecated", "app"} for name in names), path
