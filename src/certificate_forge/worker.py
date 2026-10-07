"""Durable polling worker. Run separately with python -m certificate_forge.worker."""

import logging
import signal
import sqlite3
import threading
import time
from uuid import uuid4

from certificate_forge.config import Settings
from certificate_forge.database import Database
from certificate_forge.domain import CertificateData, ClaimedJob
from certificate_forge.rendering import (
    CertificateRenderer,
    PdfCertificateRenderer,
    UnsupportedTextError,
)
from certificate_forge.repository import JobRepository
from certificate_forge.storage import LocalCertificateStorage

logger = logging.getLogger(__name__)


class CertificateWorker:
    def __init__(
        self,
        repository: JobRepository,
        renderer: CertificateRenderer,
        storage: LocalCertificateStorage,
        settings: Settings,
    ):
        self.repository = repository
        self.renderer = renderer
        self.storage = storage
        self.settings = settings
        self.id = str(uuid4())
        self.stop_event = threading.Event()

    def run_once(self) -> bool:
        self.repository.heartbeat(self.id)
        job = self.repository.claim(self.settings.lease_seconds, self.settings.max_attempts)
        if job is None:
            return False
        started = time.perf_counter()
        lease_stopped = threading.Event()
        keeper = threading.Thread(target=self._keep_lease, args=(job, lease_stopped), daemon=True)
        keeper.start()
        try:
            while not self.stop_event.is_set() and not lease_stopped.is_set():
                recipient = self.repository.next_recipient(job)
                if recipient is None:
                    break
                self._generate(job, recipient)
            self.repository.finish(job)
            logger.info(
                "job_processed job_id=%s duration_ms=%.2f",
                job.id,
                (time.perf_counter() - started) * 1000,
            )
        finally:
            lease_stopped.set()
            keeper.join(timeout=6)
        return True

    def _keep_lease(self, job: ClaimedJob, stopped: threading.Event) -> None:
        interval = min(5.0, self.settings.lease_seconds / 3)
        while not stopped.wait(interval):
            try:
                if not self.repository.extend_lease(job, self.settings.lease_seconds):
                    stopped.set()
                    return
                self.repository.heartbeat(self.id)
            except sqlite3.Error:
                logger.exception("lease_heartbeat_failed job_id=%s", job.id)
                stopped.set()

    def _generate(self, job: ClaimedJob, recipient: dict) -> None:
        artifact_path = None
        try:
            content = self.renderer.render(
                CertificateData(
                    certificate_id=recipient["id"],
                    recipient_name=recipient["name"],
                    title=job.title,
                    issuer=job.issuer,
                    issued_on=job.issued_on,
                )
            )
            artifact_path, digest = self.storage.save(
                job.id, recipient["id"], job.lease_token, content
            )
        except Exception as error:
            logger.exception(
                "certificate_failed job_id=%s recipient_id=%s", job.id, recipient["id"]
            )
            unsupported = isinstance(error, UnsupportedTextError)
            self.repository.complete_recipient(
                job,
                recipient["id"],
                error_code="UNSUPPORTED_TEXT" if unsupported else "GENERATION_FAILED",
                error_message=(
                    "Text contains characters unsupported by the bundled font."
                    if unsupported
                    else "Certificate generation failed; retry is available."
                ),
            )
            return
        # DB failures propagate to the polling loop; the lease allows recovery, rather than
        # incorrectly treating persistence failure as a permanent rendering failure.
        accepted = self.repository.complete_recipient(
            job, recipient["id"], artifact_path=artifact_path, sha256=digest
        )
        if not accepted:
            self.storage.discard(artifact_path)

    def run_forever(self) -> None:
        logger.info("worker_started worker_id=%s", self.id)
        while not self.stop_event.is_set():
            try:
                had_job = self.run_once()
            except Exception:
                logger.exception("worker_iteration_failed")
                had_job = False
            if not had_job:
                self.stop_event.wait(self.settings.poll_seconds)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    database = Database(settings)
    database.initialize()
    worker = CertificateWorker(
        JobRepository(database),
        PdfCertificateRenderer(),
        LocalCertificateStorage(settings.certificate_dir),
        settings,
    )
    for event in (signal.SIGINT, signal.SIGTERM):
        signal.signal(event, lambda *_: worker.stop_event.set())
    try:
        worker.run_forever()
    finally:
        database.close()


if __name__ == "__main__":
    main()
