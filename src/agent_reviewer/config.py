from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    github_token: str = ""
    github_api_base: str = "https://api.github.com"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    review_host: str = "127.0.0.1"
    review_port: int = 8000
    data_dir: Path = Path("./data")
    max_file_bytes: int = 200_000
    max_files: int = 200

    def findings_db(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / "findings.sqlite3"


def get_settings() -> Settings:
    return Settings()
