"""Application use cases, independent of FastAPI and PDF rendering."""

import hashlib
import json
import tempfile
import time
import zipfile
from typing import Any, BinaryIO
from uuid import uuid4

from pydantic import ValidationError

from certificate_forge.config import Settings
from certificate_forge.domain import TERMINAL_JOB_STATUSES, DomainError
from certificate_forge.repository import JobRepository
from certificate_forge.schemas import JobCreate, JobSummary, RecipientInput, RecipientResult
from certificate_forge.storage import LocalCertificateStorage


class CertificateJobService:
    def __init__(
        self, repository: JobRepository, storage: LocalCertificateStorage, settings: Settings
    ):
        self.repository = repository
        self.storage = storage
        self.settings = settings

    def create(self, request: JobCreate, idempotency_key: str | None) -> tuple[JobSummary, bool]:
        job_id = str(uuid4())
        recipients = [
            self._prepare_recipient(job_id, i, raw) for i, raw in enumerate(request.recipients)
        ]
        canonical = json.dumps(
            request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        all_invalid = all(recipient["status"] == "INVALID" for recipient in recipients)
        now = time.time()
        job = {
            "id": job_id,
            "idempotency_key": idempotency_key,
            "payload_hash": hashlib.sha256(canonical.encode()).hexdigest(),
            "title": request.title,
            "issuer": request.issuer,
            "issued_on": request.issued_on.isoformat(),
            "status": "FAILED" if all_invalid else "QUEUED",
            "total": len(recipients),
            "created_at": now,
            "updated_at": now,
            "completed_at": now if all_invalid else None,
        }
        saved_id, replayed = self.repository.create(job, recipients)
        return self.summary(saved_id), replayed

    @staticmethod
    def _prepare_recipient(job_id: str, index: int, raw: Any) -> dict[str, Any]:
        result = {
            "id": str(uuid4()),
            "job_id": job_id,
            "position": index,
            "name": None,
            "email": None,
            "status": "PENDING",
            "error_code": None,
            "error_message": None,
        }
        try:
            recipient = RecipientInput.model_validate(raw)
            result.update(name=recipient.name, email=str(recipient.email))
        except ValidationError as error:
            # Persist only validation reasons, never arbitrary raw input or exception internals.
            details = error.errors(include_url=False, include_input=False, include_context=False)
            messages = [
                f"{'.'.join(map(str, item['loc'])) or 'recipient'}: {item['msg']}"
                for item in details[:5]
            ]
            result.update(
                status="INVALID", error_code="INVALID_RECIPIENT", error_message="; ".join(messages)
            )
        return result

    def summary(self, job_id: str) -> JobSummary:
        data = self.repository.summary(job_id)
        finished = data["succeeded"] + data["failed"] + data["invalid"]
        return JobSummary(
            **{key: data[key] for key in JobSummary.model_fields if key in data},
            progress_percent=round(100 * finished / data["total"], 2),
            status_url=f"/api/jobs/{job_id}",
            recipients_url=f"/api/jobs/{job_id}/recipients",
            download_url=(
                f"/api/jobs/{job_id}/download"
                if data["succeeded"] and data["status"] in TERMINAL_JOB_STATUSES
                else None
            ),
        )

    @staticmethod
    def recipient_result(row: dict[str, Any]) -> RecipientResult:
        return RecipientResult(
            id=row["id"],
            index=row["position"],
            name=row["name"],
            email=row["email"],
            status=row["status"],
            attempts=row["attempts"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            sha256=row["sha256"],
            download_url=(
                f"/api/jobs/{row['job_id']}/certificates/{row['id']}"
                if row["status"] == "SUCCEEDED"
                else None
            ),
        )

    def retry_failed(self, job_id: str) -> JobSummary:
        self.repository.retry_failed(job_id, self.settings.max_attempts)
        return self.summary(job_id)

    def archive(self, job_id: str) -> BinaryIO:
        summary = self.summary(job_id)
        if summary.status not in TERMINAL_JOB_STATUSES:
            raise DomainError(
                "JOB_BUSY", "Wait until the job finishes before downloading a ZIP.", 409
            )
        rows, _ = self.repository.recipients(job_id, limit=1000)
        if not any(row["status"] == "SUCCEEDED" for row in rows):
            raise DomainError("NO_CERTIFICATES", "This job has no generated certificates.", 409)
        # Large archives spill to disk rather than consuming unbounded API memory.
        # Ownership transfers to the streaming HTTP response, which closes it on completion.
        archive = tempfile.SpooledTemporaryFile(  # noqa: SIM115
            max_size=8 * 1024 * 1024, mode="w+b"
        )
        try:
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as output:
                # PDFs already use compression; recompressing them adds CPU latency for little gain.
                for row in rows:
                    if row["status"] == "SUCCEEDED":
                        output.write(
                            self.storage.available_path(row["artifact_path"]), f"{row['id']}.pdf"
                        )
                manifest = {
                    "job_id": job_id,
                    "recipients": [
                        self.recipient_result(row).model_dump(mode="json") for row in rows
                    ],
                }
                output.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
            archive.seek(0)
            return archive
        except BaseException:
            archive.close()
            raise
