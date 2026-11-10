"""新项目配置，配置文件位置不依赖进程工作目录。"""

from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
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

    # 现有网关的 OpenAI 兼容端点，见决策 D25。模型 ID 必填，实际返回模型单独记录。
    model_base_url: str = "http://127.0.0.1:3425"
    model_name: str | None = None
    model_api_key: SecretStr | None = None
    model_timeout_seconds: float = 120.0
    model_max_retries: int = 4

    # 调查预算同样是临时默认值，等实验后冻结。
    # MS03 起工具预算放宽到 16：入口告警需要排除入口自身、检查依赖并读取日志。
    investigation_budget_tool_calls: int = 16
    investigation_budget_model_calls: int = 8
    investigation_budget_seconds: int = 480
    investigation_max_observation_chars: int = 8000

    # 告警入口。token 未配置时服务拒绝启动，避免裸奔的 webhook。
    cluster: str = "vigil-lab"
    namespace: str = "vigil-shop"
    webhook_token: SecretStr | None = None
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8081
    dedup_minutes: int = 10

    # 受控执行的时间窗，同样是临时默认值。
    action_rollout_timeout_seconds: float = 180.0
    action_verify_seconds: int = 90
    action_sample_seconds: int = 15
    # 审批有效期：跨进程等待超过该时长后批准仍不执行，需重新提案。
    action_approval_ttl_seconds: int = 900

    @property
    def runtime_path(self) -> Path:
        return (PROJECT_ROOT / self.runtime_dir).resolve()
