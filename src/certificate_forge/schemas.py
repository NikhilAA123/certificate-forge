"""Validate the job envelope separately so one invalid recipient cannot reject a batch."""

import unicodedata
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from certificate_forge.domain import JobStatus, RecipientStatus


def clean_text(value: str) -> str:
    value = unicodedata.normalize("NFC", value.strip())
    if not value or any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("Must contain visible text without control characters")
    return value


class RecipientInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    email: EmailStr

    _clean_name = field_validator("name")(clean_text)


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=160)
    issuer: str = Field(min_length=1, max_length=120)
    issued_on: date
    recipients: list[Any] = Field(min_length=1, max_length=1000)

    _clean_fields = field_validator("title", "issuer")(clean_text)


class JobSummary(BaseModel):
    id: str
    title: str
    issuer: str
    issued_on: date
    status: JobStatus
    total: int
    pending: int
    processing: int
    succeeded: int
    failed: int
    invalid: int
    progress_percent: float
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    status_url: str
    recipients_url: str
    download_url: str | None


class RecipientResult(BaseModel):
    id: str
    index: int
    name: str | None
    email: str | None
    status: RecipientStatus
    attempts: int
    error_code: str | None
    error_message: str | None
    download_url: str | None
    sha256: str | None


class RecipientPage(BaseModel):
    items: list[RecipientResult]
    total: int
    limit: int
    offset: int
