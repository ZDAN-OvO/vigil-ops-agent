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
    model_timeout_seconds: float = 60.0
    model_max_retries: int = 2

    @property
    def runtime_path(self) -> Path:
        return (PROJECT_ROOT / self.runtime_dir).resolve()
