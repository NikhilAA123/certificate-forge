"""Local artifact storage with atomic publication and server-generated paths."""

import hashlib
import os
import tempfile
from pathlib import Path

from certificate_forge.domain import DomainError


class LocalCertificateStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(
        self, job_id: str, recipient_id: str, lease_token: str, content: bytes
    ) -> tuple[str, str]:
        relative = f"{job_id}/{recipient_id}-{lease_token}.pdf"
        destination = self.resolve(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # A new lease gets a new filename, so a stale worker cannot overwrite its successor.
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, suffix=".tmp", delete=False
            ) as f:
                temporary = f.name
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        return relative, hashlib.sha256(content).hexdigest()

    def resolve(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise DomainError("INVALID_ARTIFACT_PATH", "Invalid artifact path.", 500)
        return path

    def available_path(self, relative: str) -> Path:
        path = self.resolve(relative)
        if not path.is_file():
            raise DomainError(
                "ARTIFACT_UNAVAILABLE", "Certificate storage is temporarily unavailable.", 503
            )
        return path

    def discard(self, relative: str) -> None:
        self.resolve(relative).unlink(missing_ok=True)
