from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class UISettings(BaseSettings):
    """Environment prefix OCR_UI_. Deliberately separate from the API's Settings: this service must not hold the
    API's pepper, admin token, database or storage credentials. It needs one thing: the collection client's API key."""
    model_config = SettingsConfigDict(env_prefix="OCR_UI_", extra="ignore")

    api_base_url: str = "http://api:8000"
    api_key: SecretStr                                     # no default: the app refuses to start without it
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, gt=0)      # per file; keep equal to or below the API's limit
    max_files_per_upload: int = Field(default=20, ge=1, le=100)
    # Shown under the Submit button. Empty hides it. Keep it consistent with the collection client's training policy.
    feedback_notice: str = "Your corrections may help improve invoice extraction accuracy."
    # Extra browser origins allowed to POST (comma separated), needed only behind a reverse proxy that rewrites Host.
    allowed_origins: str = ""

    @field_validator("api_key")
    @classmethod
    def _key_present(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value().strip()) < 20:
            raise ValueError("OCR_UI_API_KEY must be the collection client's API key")
        return v

    @field_validator("api_base_url")
    @classmethod
    def _base(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("OCR_UI_API_BASE_URL must be an http(s) URL")
        return v.rstrip("/")

    @property
    def origins(self) -> set[str]:
        return {o.strip().rstrip("/") for o in self.allowed_origins.split(",") if o.strip()}
