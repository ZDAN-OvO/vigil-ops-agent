"""新项目配置，配置文件位置不依赖进程工作目录。"""

from pathlib import Path
from typing import Literal

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

    @property
    def runtime_path(self) -> Path:
        return (PROJECT_ROOT / self.runtime_dir).resolve()
