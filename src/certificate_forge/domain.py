"""Domain vocabulary; no HTTP or database dependencies."""

from dataclasses import dataclass
from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED = "FAILED"


class RecipientStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INVALID = "INVALID"


TERMINAL_JOB_STATUSES = {
    JobStatus.COMPLETED,
    JobStatus.PARTIALLY_COMPLETED,
    JobStatus.FAILED,
}


class DomainError(Exception):
    """An expected client-visible error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    lease_token: str
    title: str
    issuer: str
    issued_on: str


@dataclass(frozen=True)
class CertificateData:
    certificate_id: str
    recipient_name: str
    title: str
    issuer: str
    issued_on: str
