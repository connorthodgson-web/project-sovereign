"""Application configuration loaded from environment variables."""

from pathlib import Path

from pydantic import PrivateAttr
from pydantic_settings import BaseSettings, SettingsConfigDict


_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    """Runtime settings for Project Sovereign."""

    app_name: str = "Project Sovereign"
    environment: str = "development"
    api_prefix: str = ""
    workspace_root: str = str(Path.cwd() / "workspace")
    cors_allowed_origins: str = ""

    openrouter_api_key: str | None = None
    openrouter_model: str = "openai/gpt-4o"
    openrouter_model_tier1: str = "google/gemini-2.0-flash-001"
    openrouter_model_tier2: str = "openai/gpt-4o"
    openrouter_model_tier3: str = "openai/gpt-5.5"
    model_routing_enabled: bool = True
    model_tier_1: str | None = None
    model_tier_2: str | None = None
    model_tier_3: str | None = None
    model_default_tier: int = 2
    model_escalation_enabled: bool = True
    frontier_model_provider: str | None = None
    frontier_model_api_key: str | None = None
    frontier_model_name: str | None = None
    openai_enabled: bool = False
    openai_api_key: str | None = None
    openai_model_tier: int | None = None
    openai_model_tier_3: str | None = None
    escalation_enabled: bool = True
    openai_agents_enabled: bool = False
    openai_agents_api_key: str | None = None
    manus_enabled: bool = False
    manus_api_key: str | None = None
    codex_cli_enabled: bool = False
    codex_cli_command: str = "codex"
    codex_cli_workspace_root: str | None = None
    codex_cli_timeout_seconds: int = 900
    codex_cli_auto_mode: bool = False
    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    supabase_service_role_key: str | None = None
    semantic_retrieval_enabled: bool = False
    retrieval_backend: str | None = None
    retrieval_url: str | None = None
    retrieval_api_key: str | None = None
    embeddings_model: str | None = None
    search_enabled: bool = False
    web_search_enabled: bool = False
    search_provider: str | None = None
    search_api_key: str | None = None
    search_timeout_seconds: int = 30
    gemini_search_model: str = "google/gemini-2.5-flash"
    memory_provider: str = "local"
    chroma_path: str | None = None
    chroma_collection_name: str = "sovereign_memory"
    chroma_max_distance: float = 1.35
    memory_backend: str = "local"
    zep_api_key: str | None = None
    zep_base_url: str | None = None
    zep_user_id: str = "sovereign-default-user"
    zep_thread_id: str = "sovereign-default-thread"
    slack_bot_token: str | None = None
    slack_app_token: str | None = None
    slack_signing_secret: str | None = None
    slack_client_id: str | None = None
    slack_client_secret: str | None = None
    slack_enabled: bool = True
    telegram_bot_token: str | None = None
    messaging_enabled: bool = False
    messaging_provider: str | None = None
    messaging_api_key: str | None = None
    messaging_default_channel: str | None = None
    voice_api_key: str | None = None
    voice_enabled: bool = False
    call_provider: str | None = None
    call_api_key: str | None = None
    browser_use_enabled: bool = False
    browser_use_api_key: str | None = None
    browser_backend_mode: str = "auto"
    browser_enabled: bool = False
    browser_headless: bool = True
    browser_visible: bool = False
    browser_show_window: bool = False
    browser_base_url: str | None = None
    browser_save_screenshots: str = "on_failure"
    browser_worker_mode: str | None = None
    browser_worker_url: str | None = None
    browser_worker_shared_secret: str | None = None
    email_enabled: bool = False
    email_provider: str | None = None
    email_api_key: str | None = None
    email_from_address: str | None = None
    gmail_enabled: bool = False
    gmail_credentials_path: str = "secrets/gmail_credentials.json"
    gmail_token_path: str = "secrets/gmail_token.json"
    gmail_scopes: str = "https://mail.google.com/"
    calendar_enabled: bool = False
    calendar_provider: str | None = None
    calendar_client_id: str | None = None
    calendar_client_secret: str | None = None
    calendar_refresh_token: str | None = None
    calendar_id: str = "primary"
    google_calendar_enabled: bool = False
    google_calendar_credentials_path: str = "secrets/credentials.json"
    google_calendar_token_path: str = "secrets/token.json"
    google_calendar_scopes: str = "https://www.googleapis.com/auth/calendar"
    google_tasks_enabled: bool = False
    google_tasks_credentials_path: str = "secrets/google_tasks_credentials.json"
    google_tasks_token_path: str = "secrets/google_tasks_token.json"
    google_tasks_scopes: str = "https://www.googleapis.com/auth/tasks"
    google_tasks_list_study: str | None = None
    google_tasks_list_id: str = "@default"
    reminders_enabled: bool = False
    scheduler_backend: str | None = None
    scheduler_timezone: str = "America/New_York"
    openclaw_enabled: bool = False
    openclaw_base_url: str | None = None
    openclaw_api_key: str | None = None
    _loaded_from_env_file: str = PrivateAttr(default=".env")

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def require(self, *field_names: str) -> None:
        """Raise a clear error when required settings for a feature are missing."""
        missing = [field_name.upper() for field_name in field_names if not getattr(self, field_name)]
        if missing:
            source = self._loaded_from_env_file
            joined = ", ".join(missing)
            raise RuntimeError(
                f"Missing required environment variables for this operation: {joined}. "
                f"Set them in {source} or the process environment."
            )

    def require_openrouter(self) -> None:
        """Validate settings required for model-provider access."""
        self.require("openrouter_api_key")

    def require_supabase(self, *, service_role: bool = False) -> None:
        """Validate settings required for Supabase-backed features."""
        required_fields = ["supabase_url", "supabase_anon_key"]
        if service_role:
            required_fields.append("supabase_service_role_key")
        self.require(*required_fields)

    def require_slack_bot(self) -> None:
        """Validate settings required for Slack bot/event handling."""
        self.require("slack_bot_token", "slack_signing_secret")

    def require_slack_socket_mode(self) -> None:
        """Validate settings required for Slack Socket Mode runtime."""
        self.require("slack_bot_token", "slack_app_token")

    def require_slack_oauth(self) -> None:
        """Validate settings required for Slack OAuth flows."""
        self.require("slack_client_id", "slack_client_secret", "slack_signing_secret")

    def require_telegram(self) -> None:
        """Validate settings required for Telegram features."""
        self.require("telegram_bot_token")

    def require_voice(self) -> None:
        """Validate settings required for voice features."""
        self.require("voice_api_key")

    def require_browser_use(self) -> None:
        """Validate settings required for browser automation features."""
        self.require("browser_use_api_key")

    def is_configured(self, *field_names: str) -> bool:
        """Return whether all requested fields are populated."""

        return all(bool(getattr(self, field_name)) for field_name in field_names)

    def configured_fields(self, *field_names: str) -> list[str]:
        """Return populated field names from the provided set."""

        return [field_name for field_name in field_names if getattr(self, field_name)]


settings = Settings()
