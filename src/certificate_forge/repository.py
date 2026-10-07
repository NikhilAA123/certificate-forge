"""SQL persistence and atomic queue transitions; PDF work never holds a transaction."""

import time
from typing import Any
from uuid import uuid4

from certificate_forge.database import Database
from certificate_forge.domain import ClaimedJob, DomainError


class JobRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, job: dict[str, Any], recipients: list[dict[str, Any]]) -> tuple[str, bool]:
        with self.database.transaction() as connection:
            if job["idempotency_key"]:
                existing = connection.execute(
                    "SELECT id, payload_hash FROM jobs WHERE idempotency_key = ?",
                    (job["idempotency_key"],),
                ).fetchone()
                if existing:
                    if existing["payload_hash"] != job["payload_hash"]:
                        raise DomainError(
                            "IDEMPOTENCY_CONFLICT",
                            "This Idempotency-Key was already used with a different request.",
                            409,
                        )
                    return existing["id"], True
            connection.execute(
                """INSERT INTO jobs
                (id, idempotency_key, payload_hash, title, issuer, issued_on, status,
                 total, created_at, updated_at, completed_at)
                VALUES (:id, :idempotency_key, :payload_hash, :title, :issuer, :issued_on,
                        :status, :total, :created_at, :updated_at, :completed_at)""",
                job,
            )
            connection.executemany(
                """INSERT INTO recipients
                (id, job_id, position, name, email, status, error_code, error_message)
                VALUES (:id, :job_id, :position, :name, :email, :status,
                        :error_code, :error_message)""",
                recipients,
            )
        return job["id"], False

    def summary(self, job_id: str) -> dict[str, Any]:
        with self.database.connection() as connection:
            # One statement yields a consistent job-and-counts snapshot.
            row = connection.execute(
                """SELECT j.*,
                SUM(r.status = 'PENDING') AS pending,
                SUM(r.status = 'PROCESSING') AS processing,
                SUM(r.status = 'SUCCEEDED') AS succeeded,
                SUM(r.status = 'FAILED') AS failed,
                SUM(r.status = 'INVALID') AS invalid
                FROM jobs j JOIN recipients r ON r.job_id = j.id
                WHERE j.id = ? GROUP BY j.id""",
                (job_id,),
            ).fetchone()
        if row is None:
            raise DomainError("JOB_NOT_FOUND", "Job not found.", 404)
        return dict(row)

    def recipients(
        self, job_id: str, limit: int = 100, offset: int = 0, status: str | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        condition = "job_id = ?"
        params: list[Any] = [job_id]
        if status is not None:
            condition += " AND status = ?"
            params.append(status)
        with self.database.connection() as connection:
            # Read transaction keeps the count and page consistent while the worker writes.
            connection.execute("BEGIN")
            if not connection.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone():
                raise DomainError("JOB_NOT_FOUND", "Job not found.", 404)
            count = connection.execute(
                f"SELECT COUNT(*) FROM recipients WHERE {condition}", params
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM recipients WHERE {condition} ORDER BY position LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [dict(row) for row in rows], count

    def certificate(self, job_id: str, recipient_id: str) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM recipients WHERE id = ? AND job_id = ?",
                (recipient_id, job_id),
            ).fetchone()
        if row is None:
            raise DomainError("CERTIFICATE_NOT_FOUND", "Certificate not found.", 404)
        if row["status"] != "SUCCEEDED":
            raise DomainError("CERTIFICATE_NOT_READY", "This certificate is not available.", 409)
        return dict(row)

    def claim(self, lease_seconds: float, max_attempts: int) -> ClaimedJob | None:
        now = time.time()
        with self.database.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM jobs WHERE status = 'QUEUED'
                OR (status = 'PROCESSING' AND lease_expires_at <= ?)
                ORDER BY created_at LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            token = str(uuid4())
            connection.execute(
                """UPDATE jobs SET status = 'PROCESSING', lease_token = ?,
                lease_expires_at = ?, updated_at = ? WHERE id = ?""",
                (token, now + lease_seconds, now, row["id"]),
            )
            # A dead worker may have started a recipient without committing its result.
            connection.execute(
                """UPDATE recipients SET status = 'PENDING'
                WHERE job_id = ? AND status = 'PROCESSING'""",
                (row["id"],),
            )
            connection.execute(
                """UPDATE recipients SET status = 'FAILED', error_code = 'ATTEMPTS_EXHAUSTED',
                error_message = 'The processing attempt limit was reached after interruption.'
                WHERE job_id = ? AND status = 'PENDING' AND attempts >= ?""",
                (row["id"], max_attempts),
            )
        return ClaimedJob(row["id"], token, row["title"], row["issuer"], row["issued_on"])

    def extend_lease(self, job: ClaimedJob, lease_seconds: float) -> bool:
        now = time.time()
        with self.database.connection() as connection:
            result = connection.execute(
                """UPDATE jobs SET lease_expires_at = ?
                WHERE id = ? AND lease_token = ? AND status = 'PROCESSING'
                AND lease_expires_at > ?""",
                (now + lease_seconds, job.id, job.lease_token, now),
            )
        return result.rowcount == 1

    @staticmethod
    def _owns_job(connection: Any, job: ClaimedJob) -> bool:
        return (
            connection.execute(
                """SELECT 1 FROM jobs WHERE id = ? AND lease_token = ?
            AND status = 'PROCESSING' AND lease_expires_at > ?""",
                (job.id, job.lease_token, time.time()),
            ).fetchone()
            is not None
        )

    def next_recipient(self, job: ClaimedJob) -> dict[str, Any] | None:
        with self.database.transaction() as connection:
            if not self._owns_job(connection, job):
                return None
            row = connection.execute(
                """SELECT * FROM recipients WHERE job_id = ? AND status = 'PENDING'
                ORDER BY position LIMIT 1""",
                (job.id,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """UPDATE recipients SET status = 'PROCESSING', attempts = attempts + 1
                WHERE id = ?""",
                (row["id"],),
            )
        return dict(row)

    def complete_recipient(
        self,
        job: ClaimedJob,
        recipient_id: str,
        *,
        artifact_path: str | None = None,
        sha256: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        with self.database.transaction() as connection:
            if not self._owns_job(connection, job):
                return False
            result = connection.execute(
                """UPDATE recipients SET status = ?, artifact_path = ?, sha256 = ?,
                error_code = ?, error_message = ?
                WHERE id = ? AND job_id = ? AND status = 'PROCESSING'""",
                (
                    "FAILED" if error_code else "SUCCEEDED",
                    artifact_path,
                    sha256,
                    error_code,
                    error_message,
                    recipient_id,
                    job.id,
                ),
            )
            connection.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (time.time(), job.id))
        return result.rowcount == 1

    def finish(self, job: ClaimedJob) -> bool:
        with self.database.transaction() as connection:
            if not self._owns_job(connection, job):
                return False
            counts = dict(
                connection.execute(
                    "SELECT status, COUNT(*) FROM recipients WHERE job_id = ? GROUP BY status",
                    (job.id,),
                ).fetchall()
            )
            if counts.get("PENDING", 0) or counts.get("PROCESSING", 0):
                return False
            status = "COMPLETED"
            if counts.get("FAILED", 0) or counts.get("INVALID", 0):
                status = "PARTIALLY_COMPLETED" if counts.get("SUCCEEDED", 0) else "FAILED"
            now = time.time()
            connection.execute(
                """UPDATE jobs SET status = ?, completed_at = ?, updated_at = ?,
                lease_token = NULL, lease_expires_at = NULL WHERE id = ?""",
                (status, now, now, job.id),
            )
        return True

    def retry_failed(self, job_id: str, max_attempts: int) -> None:
        with self.database.transaction() as connection:
            job = connection.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise DomainError("JOB_NOT_FOUND", "Job not found.", 404)
            if job["status"] in ("QUEUED", "PROCESSING"):
                raise DomainError("JOB_BUSY", "Wait until the job finishes before retrying.", 409)
            result = connection.execute(
                """UPDATE recipients SET status = 'PENDING', error_code = NULL,
                error_message = NULL WHERE job_id = ? AND status = 'FAILED' AND attempts < ?""",
                (job_id, max_attempts),
            )
            if result.rowcount == 0:
                raise DomainError(
                    "NOTHING_TO_RETRY",
                    "No retryable failures. Invalid input must be corrected in a new job.",
                    409,
                )
            connection.execute(
                """UPDATE jobs SET status = 'QUEUED', completed_at = NULL, updated_at = ?,
                lease_token = NULL, lease_expires_at = NULL WHERE id = ?""",
                (time.time(), job_id),
            )

    def heartbeat(self, worker_id: str) -> None:
        with self.database.connection() as connection:
            now = time.time()
            connection.execute(
                """INSERT INTO worker_heartbeats VALUES (?, ?)
                ON CONFLICT(id) DO UPDATE SET last_seen_at = excluded.last_seen_at""",
                (worker_id, now),
            )
            connection.execute(
                "DELETE FROM worker_heartbeats WHERE last_seen_at < ?", (now - 86400,)
            )

    def worker_available(self, stale_after: float = 15) -> bool:
        with self.database.connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM worker_heartbeats WHERE last_seen_at > ? LIMIT 1",
                    (time.time() - stale_after,),
                ).fetchone()
                is not None
            )
