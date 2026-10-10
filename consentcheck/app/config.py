import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    model: str = ""
    database: Path = ROOT / "outbox.db"

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model and not self.api_key.startswith("your_")
                    and not self.model.startswith("your_"))


def load_settings() -> Settings:
    # Resolve .env relative to the application, independent of terminal directory.
    values = dotenv_values(ROOT / ".env")
    return Settings(
        api_key=(os.getenv("GEMINI_API_KEY", values.get("GEMINI_API_KEY") or "")).strip(),
        model=(os.getenv("GEMINI_MODEL", values.get("GEMINI_MODEL") or "")).strip(),
    )
