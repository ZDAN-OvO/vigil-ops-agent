"""新项目配置，配置文件位置不依赖进程工作目录。"""

import os
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_CHECKOUT = (
    Path(__file__).resolve().parents[1].name == "src"
    and (PROJECT_ROOT / "pyproject.toml").is_file()
)
_CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "vigil"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT if _SOURCE_CHECKOUT else _CONFIG_DIR) / ".env",
        env_prefix="VIGIL_",
        extra="ignore",
    )

    environment: Literal["local", "test"] = "local"
    runtime_dir: Path = Path("runtime")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    database_url: str | None = None

    # 只读工具边界。数值是先跑通链路的临时默认值，尚未按实验冻结。
    prometheus_url: str = "http://127.0.0.1:9090"
    kubeconfig: Path | None = None
    kube_context: str | None = None
    tool_timeout_seconds: float = 10.0
    tool_max_points: int = 2000
    tool_max_series: int = 200
    tool_max_items: int = 100

    # 现有网关的 OpenAI 兼容端点，见 docs/adr/0010-model-gateway.md。
    # 模型 ID 必填，实际返回模型单独记录。
    model_base_url: str = "http://127.0.0.1:3425"
    model_name: str | None = None
    model_api_key: SecretStr | None = None
    model_timeout_seconds: float = 120.0
    model_max_retries: int = 4

    # 调查预算按 2026-11-10 的实测定值（16 次运行，见 docs/adr/0011-investigation-budget.md）：
    # 成功运行的工具调用最多 21、模型调用最多 7、墙钟最多 453 秒，
    # 各留约三成余量后取整。改动数值要先重测，不能因为某次失败就调。
    investigation_budget_tool_calls: int = 28
    investigation_budget_model_calls: int = 20
    investigation_budget_seconds: int = 600
    investigation_budget_action_seconds: int = 450
    # 动作开始前为“至少一次反馈模型调用”预留的调查预算。
    # 取当前单次模型超时的一半以内：这是给反馈预留的下限，不是单次超时上限。
    investigation_action_model_reserve_seconds: float = 60.0
    investigation_max_observation_chars: int = 8000
    # 仓库内置 Skill；只允许本地目录，远程分发留到后续阶段。
    skills_dir: Path | None = None

    # 告警入口。token 未配置时服务拒绝启动，避免裸奔的 webhook。
    cluster: str = "vigil-lab"
    namespace: str = "vigil-shop"
    webhook_token: SecretStr | None = None
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8081
    dedup_minutes: int = 10

    # 受控执行的时间窗，同样是临时默认值。
    action_rollout_timeout_seconds: float = 180.0
    action_verify_seconds: int = 135
    action_sample_seconds: int = 15
    # 审批有效期：跨进程等待超过该时长后批准仍不执行，需重新提案。
    action_approval_ttl_seconds: int = 900

    # 可选追踪：默认关闭，业务正确性不依赖追踪平台。
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "vigil-aiops"
    langfuse_enabled: bool = False
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    @property
    def runtime_path(self) -> Path:
        root = (
            PROJECT_ROOT
            if _SOURCE_CHECKOUT
            else Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "vigil"
        )
        return (root / self.runtime_dir).resolve()

    @property
    def skills_path(self) -> Path:
        if self.skills_dir is not None:
            return ((PROJECT_ROOT if _SOURCE_CHECKOUT else Path.cwd()) / self.skills_dir).resolve()
        if _SOURCE_CHECKOUT:
            return PROJECT_ROOT / "skills"
        # Python 安装 wheel 时会把包资源解包到文件系统，Skill 读取器可直接使用该目录。
        return Path(str(files("aiops").joinpath("_builtin_skills")))
