"""Application settings loaded from environment variables.

All secrets come from the environment (or a secret manager that populates it).
Nothing in this module should contain a real credential.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../../.env"),
        env_prefix="ATLAS_",
        extra="ignore",
    )

    # Product branding is configurable so the commercial name can change later.
    product_name: str = "ATLAS"
    environment: Environment = Environment.LOCAL
    log_level: str = "INFO"
    service_name: str = "atlas-api"

    database_url: str = "postgresql+asyncpg://atlas:atlas@127.0.0.1:5433/atlas"
    database_pool_size: int = 10
    database_max_overflow: int = 10
    database_statement_timeout_ms: int = 15_000

    redis_url: str = "redis://127.0.0.1:6380/0"

    # Auth
    jwt_secret: SecretStr = SecretStr("change-me-local-only-not-for-production-use-0000")
    jwt_issuer: str = "atlas"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 14
    refresh_cookie_name: str = "atlas_refresh"
    cookie_secure: bool = False
    allow_signup: bool = True

    # Fernet key (urlsafe base64, 32 bytes) used to encrypt integration secrets at rest.
    secrets_encryption_key: SecretStr = SecretStr("")

    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )
    max_request_bytes: int = 10 * 1024 * 1024
    docs_enabled: bool = True

    # Calling safety: real calls are impossible unless explicitly enabled.
    live_calls_enabled: bool = False

    # Voice runtime (internal infrastructure, never exposed to tenants).
    # Server-to-server address ATLAS uses for the runtime API.
    voice_runtime_base_url: str = "http://127.0.0.1:8000"
    # Address browsers use for call audio signaling (behind a reverse proxy in production).
    voice_runtime_public_url: str = "http://localhost:8000"
    # Address the runtime uses to reach ATLAS for call events.
    voice_runtime_callback_url: str = "http://host.docker.internal:8080"
    voice_runtime_timeout_seconds: float = 20.0
    voice_runtime_tenant_email_domain: str = "tenants.atlas.internal"
    # Shared secret for the voice service's platform-wide metrics endpoint (pipeline
    # health panel, ADR 0006). Must equal the runtime's devops secret; empty = panel off.
    voice_runtime_metrics_secret: SecretStr = SecretStr("")
    # Keep the runtime's vendor-hosted trial models. Off: call data only goes to
    # providers a tenant configures explicitly.
    voice_allow_hosted_trial_models: bool = False
    # Origin of the ATLAS web app (browser test calls are restricted to it).
    web_app_origin: str = "http://localhost:5173"
    # Base URL visitors' browsers use to reach this API (the website widget's script and
    # sessions). Empty: the web app origin, which proxies /api to this service.
    public_api_url: str | None = None

    # Knowledge base: documents agents consult during calls. Parsing and chunking run in
    # the voice service's external document-processing service (see docs/knowledge.md).
    knowledge_enabled: bool = True
    # The voice service refuses larger files at processing time, so the limit is enforced
    # here before anything is uploaded.
    knowledge_max_upload_bytes: int = 5 * 1024 * 1024
    knowledge_upload_timeout_seconds: float = 60.0

    rate_limit_auth_per_minute: int = 10
    # Requests per minute allowed for one tenant API key (X-API-Key).
    rate_limit_api_key_per_minute: int = 600

    # Outbound webhooks. Endpoint URLs must be https and resolve to public addresses;
    # private/loopback/link-local/metadata ranges are refused unless this is true
    # (local development against services on the same machine).
    allow_private_webhook_urls: bool = False
    webhook_timeout_seconds: float = 10.0
    webhook_max_attempts: int = 8
    webhook_auto_disable_after_failures: int = 50

    # Actions (HTTP calls an agent makes during a conversation) are executed by the
    # voice runtime, so their URLs go through the same destination policy as webhooks
    # before anything is stored: https only (http in local), public addresses only.
    # Set to true only for local development against services on the same machine.
    allow_private_action_urls: bool = False

    # Self-hosted language model warm-up (docs/local-models.md, "Warming the prompt"):
    # when a call or browser test call starts with a self-hosted model, ATLAS sends the
    # agent's prompt to the model server so the first reply does not wait for it.
    local_llm_warmup_enabled: bool = True
    # Where ATLAS itself reaches the model server, e.g. http://127.0.0.1:11434/v1 when the
    # API runs on the host and the connection's base URL is a Docker-internal name such as
    # http://local-llm:11434/v1. Operator-set, so it may be private; outside local
    # development the network policy requires https unless the host is internal (a Docker
    # service name or a private address, e.g. http://local-llm:11434/v1 for an API container).
    local_llm_warmup_url: str | None = None
    local_llm_warmup_timeout_seconds: float = 30.0
    # A tenant's self-hosted connection URL is only warmed directly when it resolves to a
    # public address, unless this is true (models on your own private network).
    allow_private_model_urls: bool = False

    # Pocket TTS voice cloning (docs/local-models.md, "Voice cloning"): where ATLAS itself
    # reaches the Pocket TTS server to list, clone and delete voices, e.g.
    # http://127.0.0.1:8002/v1 when the API runs on the host and the connection's base URL is
    # the Docker-internal http://local-tts:8000/v1. Operator-set, so it may be private;
    # outside local development the network policy requires https unless the host is
    # internal (a Docker service name or a private address). Unset: the connection's
    # own base URL, which must be public unless ALLOW_PRIVATE_MODEL_URLS is true.
    local_tts_url: str | None = None
    # Only when the Pocket TTS server requires a key (its POCKET_TTS_API_KEY).
    local_tts_api_key: SecretStr = SecretStr("")
    # Computing a voice takes a few seconds on CPU, longer while calls are speaking.
    local_tts_timeout_seconds: float = 120.0
    voice_clone_max_upload_bytes: int = 10 * 1024 * 1024

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str) and not value.startswith("["):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment in (Environment.PRODUCTION, Environment.STAGING)

    def validate_for_runtime(self) -> None:
        """Refuse to boot a non-local deployment with insecure defaults."""
        if not self.is_production:
            return
        problems: list[str] = []
        if self.jwt_secret.get_secret_value().startswith("change-me"):
            problems.append("ATLAS_JWT_SECRET must be set")
        if len(self.jwt_secret.get_secret_value()) < 32:
            problems.append("ATLAS_JWT_SECRET must be at least 32 characters")
        if not self.secrets_encryption_key.get_secret_value():
            problems.append("ATLAS_SECRETS_ENCRYPTION_KEY must be set")
        if not self.cookie_secure:
            problems.append("ATLAS_COOKIE_SECURE must be true")
        if problems:
            raise RuntimeError("Insecure configuration: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
