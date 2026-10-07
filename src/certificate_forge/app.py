"""Thin HTTP controllers; business rules live in the service and repository."""

import hmac
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Annotated, BinaryIO
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from starlette.background import BackgroundTask

from certificate_forge.config import Settings
from certificate_forge.database import Database
from certificate_forge.domain import DomainError, RecipientStatus
from certificate_forge.middleware import RequestMiddleware
from certificate_forge.repository import JobRepository
from certificate_forge.schemas import JobCreate, JobSummary, RecipientPage
from certificate_forge.services import CertificateJobService
from certificate_forge.storage import LocalCertificateStorage


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings)
    repository = JobRepository(database)
    storage = LocalCertificateStorage(settings.certificate_dir)
    service = CertificateJobService(repository, storage, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.initialize()
        try:
            yield
        finally:
            database.close()

    app = FastAPI(
        title="Certificate Forge",
        version="1.0.0",
        description=(
            "Submit up to 1,000 recipients in one durable job. Poll progress, inspect individual "
            "outcomes, download PDFs or a ZIP, and retry failed generations. "
            "Use Idempotency-Key to safely repeat a submission. "
            "Optional authentication uses X-API-Key when CERTIFICATE_API_KEY is configured."
        ),
        lifespan=lifespan,
    )
    app.state.service = service
    app.state.repository = repository
    app.add_middleware(RequestMiddleware, max_body_bytes=settings.max_body_bytes)

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, error: DomainError):
        return JSONResponse(
            status_code=error.status_code,
            content={"detail": {"code": error.code, "message": str(error)}},
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def database_error_handler(request: Request, error: sqlite3.OperationalError):
        return JSONResponse(
            status_code=503,
            content={
                "detail": {"code": "DATABASE_UNAVAILABLE", "message": "Please retry shortly."}
            },
            headers={"Retry-After": "2"},
        )

    api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def authorize(key: Annotated[str | None, Depends(api_key_header)]) -> None:
        if settings.api_key and (
            key is None or not hmac.compare_digest(key.encode(), settings.api_key.encode())
        ):
            raise DomainError("UNAUTHORIZED", "A valid X-API-Key is required.", 401)

    api = APIRouter(
        prefix="/api/jobs", tags=["Certificate jobs"], dependencies=[Depends(authorize)]
    )

    @api.post("", response_model=JobSummary, status_code=202)
    def create_job(
        payload: JobCreate,
        response: Response,
        idempotency_key: Annotated[
            str | None, Header(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
        ] = None,
    ) -> JobSummary:
        """Persist a batch and return immediately; rendering happens in the worker process."""
        job, replayed = service.create(payload, idempotency_key)
        response.status_code = 200 if replayed else 202
        response.headers["Location"] = job.status_url
        response.headers["Idempotency-Replayed"] = str(replayed).lower()
        response.headers["Retry-After"] = "1"
        return job

    @api.get("/{job_id}", response_model=JobSummary)
    def get_job(job_id: UUID) -> JobSummary:
        """Return counts and terminal progress; invalid recipients count as processed."""
        return service.summary(str(job_id))

    @api.get("/{job_id}/recipients", response_model=RecipientPage)
    def get_recipients(
        job_id: UUID,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        status: RecipientStatus | None = None,
    ) -> RecipientPage:
        rows, total = repository.recipients(str(job_id), limit, offset, status)
        return RecipientPage(
            items=[service.recipient_result(row) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    @api.post("/{job_id}/retry", response_model=JobSummary, status_code=202)
    def retry_job(job_id: UUID) -> JobSummary:
        """Retry only failed generations below the three-attempt limit; preserve successes."""
        return service.retry_failed(str(job_id))

    @api.get("/{job_id}/certificates/{recipient_id}", response_class=FileResponse)
    def download_certificate(job_id: UUID, recipient_id: UUID) -> FileResponse:
        row = repository.certificate(str(job_id), str(recipient_id))
        return FileResponse(
            storage.available_path(row["artifact_path"]),
            media_type="application/pdf",
            filename=f"{recipient_id}.pdf",
            headers={"X-Certificate-SHA256": row["sha256"]},
        )

    @api.get("/{job_id}/download", response_class=StreamingResponse)
    def download_archive(job_id: UUID) -> StreamingResponse:
        """Download successful PDFs plus a JSON manifest of every recipient's result."""
        archive = service.archive(str(job_id))
        return StreamingResponse(
            _read_chunks(archive),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="certificates-{job_id}.zip"'},
            background=BackgroundTask(archive.close),
        )

    @app.get("/health/live", tags=["Health"])
    def live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["Health"])
    def ready(response: Response) -> dict:
        worker_available = repository.worker_available()
        response.status_code = 200 if worker_available else 503
        return {"database": "ok", "worker": "available" if worker_available else "unavailable"}

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/docs")

    app.include_router(api)
    return app


def _read_chunks(stream: BinaryIO) -> Iterator[bytes]:
    try:
        while chunk := stream.read(64 * 1024):
            yield chunk
    finally:
        stream.close()
