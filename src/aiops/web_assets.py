"""校验网页入口及其同源资源。"""

from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

STATIC_PREFIX = "/static/"


class WebAssetsUnavailable(ValueError):
    """启动网页服务所需的资源不可用。"""


class _EntryAssets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []
        self.styles: list[str] = []
        self.preloads: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script":
            self.scripts.append(values.get("src") or "")
        elif tag == "link":
            match values.get("rel"):
                case "stylesheet":
                    self.styles.append(values.get("href") or "")
                case "modulepreload":
                    self.preloads.append(values.get("href") or "")


def check_web_assets(directory: Path) -> None:
    """缺入口、脚本、样式或入口引用的资源时抛出异常。"""
    assets = _EntryAssets()
    assets.feed((directory / "index.html").read_text(encoding="utf-8"))
    if not assets.scripts or not assets.styles:
        raise ValueError("网页入口缺少构建后的脚本或样式")
    for url in assets.scripts + assets.styles + assets.preloads:
        relative = PurePosixPath(url.removeprefix(STATIC_PREFIX))
        asset = directory / relative
        if (
            not url.startswith(STATIC_PREFIX + "assets/")
            or not asset.resolve().is_relative_to(directory.resolve())
            or not asset.is_file()
            or not asset.stat().st_size
        ):
            raise ValueError("网页入口引用的构建资源无效或缺失")
