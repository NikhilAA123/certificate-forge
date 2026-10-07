"""Explicit configuration shared by the API and worker processes."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    api_key: str | None = None
    poll_seconds: float = 0.25
    lease_seconds: float = 30.0
    max_attempts: int = 3
    max_body_bytes: int = 2_000_000

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.getenv("CERTIFICATE_DATA_DIR", "data")).resolve(),
            api_key=os.getenv("CERTIFICATE_API_KEY") or None,
        )

    @property
    def database_path(self) -> Path:
        return self.data_dir / "certificates.sqlite3"

    @property
    def certificate_dir(self) -> Path:
        return self.data_dir / "certificates"
