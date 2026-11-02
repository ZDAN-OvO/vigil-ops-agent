"""CLI 入口；W0 仅提供不访问外部服务的实现状态查询。"""

import json
from enum import StrEnum

import typer

from aiops import __version__

app = typer.Typer(
    help="Vigil AIOps：故障调查与受控修复（当前为 W0 骨架）。",
    no_args_is_help=True,
    add_completion=False,
)


class OutputFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


@app.callback()
def main() -> None:
    """业务命令按阶段接入，帮助输出不初始化数据库或集群。"""


@app.command()
def status(output: OutputFormat = OutputFormat.TEXT) -> None:
    """查看当前实现范围；不是集群健康检查。"""
    payload = {
        "schema_version": "1",
        "version": __version__,
        "stage": "W0",
        "capabilities": {"cli": True, "investigation": False, "remediation": False},
        "external_services_checked": False,
    }
    if output == OutputFormat.JSON:
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        typer.echo(f"Vigil AIOps {__version__} · W0：CLI 骨架可用，业务能力尚未实现。")


if __name__ == "__main__":
    app()
