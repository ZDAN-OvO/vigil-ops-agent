"""普通 wheel 携带已经构建的网页。"""

from pathlib import Path
from runpy import run_path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        if version != "standard":
            return

        root = Path(self.root)
        dist = root / "web" / "dist"
        # 构建隔离环境只安装打包依赖。资源校验只使用标准库。
        check_web_assets = run_path(str(root / "src/aiops/web_assets.py"))["check_web_assets"]
        try:
            check_web_assets(dist)
        except (OSError, ValueError) as exc:
            raise ValueError("普通 wheel 缺少有效的网页产物，请先运行 make web-build") from exc
        build_data["force_include"][str(dist)] = "aiops/static"
