from pathlib import Path
from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AutodevSettings(BaseSettings):
    # Model configuration: loads from ENV or .env
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # API Keys & Credentials
    gemini_api_key: Optional[str] = Field(None, validation_alias="GEMINI_API_KEY")
    gemini_model: str = Field("gemini-3.5-flash", validation_alias="GEMINI_MODEL")
    github_token: Optional[str] = Field(None, validation_alias="GITHUB_TOKEN")

    # Agent / Execution Settings
    max_retries: int = Field(
        3, description="Max retry attempts for debugging/testing phase"
    )
    interactive: bool = Field(
        False, description="Pause for human approval at key execution points"
    )
    max_loop_iterations: int = Field(
        50, description="Hard cap on total ReAct loop iterations per solve"
    )
    max_steps_per_mode: int = Field(
        20, description="Max agent loop steps allowed per mode before force-failing"
    )
    max_tool_stall_warnings: int = Field(
        3,
        description="Max times to warn agent about repeated identical tool calls before failing",
    )
    tool_repeat_threshold: int = Field(
        3,
        description="Number of consecutive identical tool call batches before triggering a stall warning",
    )

    # Sandbox / Security Settings
    sandbox_timeout: int = Field(
        300, description="Max execution time for sandboxed commands (seconds)"
    )
    sandbox_memory_limit: str = Field(
        "512m", description="Docker memory limit for sandbox container"
    )
    allow_local_shell: bool = Field(
        False, description="Allow shell execution on host when Docker is unavailable"
    )

    # Observability Settings
    log_dir: Path = Field(
        default_factory=lambda: Path("./logs"),
        description="Directory to store persistent logs",
    )
    log_level: str = Field(
        "INFO",
        description="Global logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )

    @property
    def is_github_configured(self) -> bool:
        return self.github_token is not None and self.github_token != ""

    @property
    def validated_log_level(self) -> str:
        """Returns log_level validated against standard Python logging levels."""
        valid_levels = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
        level = self.log_level.upper()
        if level not in valid_levels:
            return "INFO"
        return level
