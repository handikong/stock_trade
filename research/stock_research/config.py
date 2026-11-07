from pathlib import Path
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ROOT / ".env"), extra="ignore")
    database_url: str = ""
    postgres_host: str = ""
    postgres_user: str = "researcher"
    postgres_db: str = "stock_research"
    postgres_password: SecretStr = SecretStr("")
    data_dir: Path = ROOT / "data"
    sec_user_agent: str = ""
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_model: str = "deepseek-flash"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_thinking: bool = False
    feishu_webhook_url: SecretStr = SecretStr("")
    feishu_signing_secret: SecretStr = SecretStr("")
    feishu_timeout_seconds: int = Field(15, ge=3, le=60)
    feishu_max_messages_per_run: int = Field(20, ge=1, le=100)
    history_years: int = Field(3, ge=1, le=15)
    market_cache_hours: int = Field(12, ge=1, le=168)
    # 仅控制一致预期、日程、修订等动态资料；已保存财报/业绩附件不设TTL。
    disclosure_cache_hours: int = Field(24, ge=1, le=720)
    financial_report_count: int = Field(2, ge=1, le=20)
    research_display_timezone: str = "Asia/Shanghai"
    external_briefs_policy_file: Path = ROOT / "config" / "external-briefs.json"
    yahoo_cooldown_seconds: int = Field(120, ge=30, le=86400)
    fmp_api_key: SecretStr = SecretStr("")
    material_link_limit: int = Field(3, ge=1, le=10)
    valuation_policy_file: Path = ROOT / "config" / "valuation-policy.json"
    # 手工资料永久保存；可选复核间隔，未配置时不按固定天数自动判旧。
    manual_valuation_review_days: int | None = Field(default=None, ge=1, le=3650)
    automatic_pe_policy_file: Path = ROOT / "config" / "automatic-pe-policy.json"
    manual_input_policy_file: Path = ROOT / "config" / "manual-input-policy.json"
    post_close_schedule_file: Path = ROOT / "config" / "post-close-schedule.json"
    market_brief_slope_sessions: int = Field(5, ge=1, le=20)
    market_brief_volume_high: float = Field(1.25, gt=1, le=5)
    market_brief_volume_low: float = Field(0.80, gt=0, lt=1)
    market_brief_level_buffer_atr: float = Field(0.15, gt=0, le=1)
    portfolio_policy_file: Path = ROOT / "config" / "portfolio-policy.json"
    us_universe_policy_file: Path = ROOT / "config" / "us-universe-policy.json"
    technical_strength_file: Path = ROOT / "config" / "technical-strength.json"
    fundamental_technical_policy_file: Path = ROOT / "config" / "fundamental-technical-policy.json"
    trend_policy_file: Path = ROOT / "config" / "trend-policy.json"
    trend_history_policy_file: Path = ROOT / "config" / "trend-short-history.json"
    post_close_delay_minutes: int = Field(30, ge=0, le=240)
    pivot_left: int = Field(3, ge=1, le=30)
    pivot_right: int = Field(3, ge=1, le=30)
    atr_window: int = Field(14, ge=2, le=100)
    zone_tolerance_atr: float = Field(0.8, gt=0, le=5)
    zone_lookback: int = Field(252, ge=30, le=1500)
    ma_windows: str = "20,50,200"
    ai_chunk_chars: int = Field(18000, ge=4000, le=60000)
    ai_max_chunks: int = Field(16, ge=1, le=100)
    ai_output_tokens: int = Field(5000, ge=1000, le=32000)
    ai_merge_output_tokens: int = Field(10000, ge=1000, le=32000)
    ai_validation_repairs: int = Field(1, ge=0, le=1)
    ai_fact_limit: int = Field(200, ge=20, le=3000)

    @field_validator("research_display_timezone")
    @classmethod
    def valid_research_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("RESEARCH_DISPLAY_TIMEZONE需要有效时区，例如Asia/Shanghai或UTC") from None
        return value

    @property
    def storage(self) -> Path:
        return self.data_dir.expanduser().resolve()

    @property
    def database(self) -> str:
        if self.database_url:
            return self.database_url
        if self.postgres_host:
            return URL.create("postgresql+psycopg",username=self.postgres_user,
                password=self.postgres_password.get_secret_value(),host=self.postgres_host,
                database=self.postgres_db).render_as_string(hide_password=False)
        return f"sqlite:///{self.storage / 'research.db'}"

    @property
    def averages(self) -> list[int]:
        values = sorted(set(int(v.strip()) for v in self.ma_windows.split(",")))
        if not values or any(v < 2 or v > 1000 for v in values):
            raise ValueError("均线窗口必须介于2与1000之间")
        return values


settings = Settings()
