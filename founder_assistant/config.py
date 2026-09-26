"""Runtime configuration, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")))
    timezone: str = field(default_factory=lambda: _env("TIMEZONE", "Asia/Ho_Chi_Minh"))
    daily_report_time: str = field(default_factory=lambda: _env("DAILY_REPORT_TIME", "21:30"))

    claude_model: str = field(default_factory=lambda: _env("CLAUDE_MODEL", "claude-opus-5"))

    zalo_app_id: str = field(default_factory=lambda: _env("ZALO_APP_ID"))
    zalo_oa_secret_key: str = field(default_factory=lambda: _env("ZALO_OA_SECRET_KEY"))
    zalo_app_secret: str = field(default_factory=lambda: _env("ZALO_APP_SECRET"))
    zalo_access_token: str = field(default_factory=lambda: _env("ZALO_ACCESS_TOKEN"))
    zalo_refresh_token: str = field(default_factory=lambda: _env("ZALO_REFRESH_TOKEN"))
    founder_zalo_user_id: str = field(default_factory=lambda: _env("FOUNDER_ZALO_USER_ID"))

    stt_url: str = field(default_factory=lambda: _env("STT_URL"))
    stt_api_key: str = field(default_factory=lambda: _env("STT_API_KEY"))
    stt_model: str = field(default_factory=lambda: _env("STT_MODEL", "whisper-1"))

    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL"))
    report_link_secret: str = field(default_factory=lambda: _env("REPORT_LINK_SECRET", "change-me"))

    # Validation thresholds
    price_alert_pct: float = 20.0          # |change| >= this -> "tăng/giảm quá mạnh"
    amount_tolerance_vnd: float = 1000.0   # rounding slack for qty x price vs amount
    amount_tolerance_pct: float = 0.5
    max_backdate_days: int = 45

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "founder.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.media_dir, self.reports_dir):
            d.mkdir(parents=True, exist_ok=True)
