import hashlib
import json
from io import BytesIO
from uuid import uuid4
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from certificate_forge.app import create_app
from certificate_forge.config import Settings
from certificate_forge.rendering import PdfCertificateRenderer


def create_job(system, payload):
    response = system.client.post("/api/jobs", json=payload)
    assert response.status_code == 202, response.text
    return response.json()["id"]


def test_full_workflow_persists_job_renders_pdf_and_downloads_zip(system, payload):
    response = system.client.post("/api/jobs", json=payload)
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "QUEUED"
    assert job["pending"] == 2
    assert job["progress_percent"] == 0
    assert response.headers["location"] == job["status_url"]
    assert response.headers["server-timing"].startswith("app;dur=")
    assert response.headers["x-request-id"]
    assert not list(system.settings.certificate_dir.rglob("*.pdf"))
    assert system.worker.run_once()
    result = system.client.get(job["status_url"]).json()
    assert result["status"] == "COMPLETED"
    assert result["succeeded"] == 2
    assert result["progress_percent"] == 100
    assert result["completed_at"]
    recipients = system.client.get(job["recipients_url"]).json()["items"]
    for recipient in recipients:
        response = system.client.get(recipient["download_url"])
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert hashlib.sha256(response.content).hexdigest() == recipient["sha256"]
        pdf = PdfReader(BytesIO(response.content))
        assert len(pdf.pages) == 1
        text = pdf.pages[0].extract_text()
        assert recipient["name"] in text
        assert payload["title"] in text
        assert payload["issuer"] in text
        assert recipient["id"] in text
    response = system.client.get(result["download_url"])
    assert response.status_code == 200
    with ZipFile(BytesIO(response.content)) as archive:
        assert len(archive.namelist()) == 3
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["job_id"] == job["id"]
        assert len(manifest["recipients"]) == 2
        assert all(item["status"] == "SUCCEEDED" for item in manifest["recipients"])


@pytest.mark.parametrize(
    "invalid",
    [
        {"name": "", "email": "a@example.com"},
        {"name": "Alice", "email": "not-an-email"},
        {"name": "Alice", "email": "a@example.com", "unexpected": True},
        {"name": "  ", "email": "a@example.com"},
        {"name": "Alice\x00", "email": "a@example.com"},
        {"name": 42, "email": "a@example.com"},
        "not an object",
        None,
    ],
)
def test_invalid_recipient_does_not_reject_valid_recipients(system, payload, invalid):
    payload["recipients"].append(invalid)
    job_id = create_job(system, payload)
    initial = system.client.get(f"/api/jobs/{job_id}").json()
    assert initial["invalid"] == 1
    assert initial["pending"] == 2
    system.worker.run_once()
    result = system.client.get(f"/api/jobs/{job_id}").json()
    assert result["status"] == "PARTIALLY_COMPLETED"
    assert result["succeeded"] == 2
    assert result["invalid"] == 1
    assert result["progress_percent"] == 100
    page = system.client.get(f"/api/jobs/{job_id}/recipients?status=INVALID").json()
    assert page["total"] == 1
    assert page["items"][0]["index"] == 2
    assert page["items"][0]["error_code"] == "INVALID_RECIPIENT"


@pytest.mark.parametrize(
    "field,value",
    [
        ("recipients", []),
        ("recipients", [{}] * 1001),
        ("issued_on", "not-a-date"),
        ("title", ""),
        ("issuer", "\n"),
    ],
)
def test_invalid_job_envelope_returns_422(system, payload, field, value):
    payload[field] = value
    assert system.client.post("/api/jobs", json=payload).status_code == 422


def test_all_invalid_job_is_terminal_without_a_worker(system, payload):
    payload["recipients"] = [None, {}]
    job_id = create_job(system, payload)
    job = system.client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "FAILED"
    assert job["invalid"] == 2
    assert job["progress_percent"] == 100
    assert not system.worker.run_once()
    assert system.client.get(f"/api/jobs/{job_id}/download").status_code == 409
    assert system.client.post(f"/api/jobs/{job_id}/retry").status_code == 409


class FailMayaOnce:
    def __init__(self):
        self.failed = False
        self.delegate = PdfCertificateRenderer()

    def render(self, certificate):
        if certificate.recipient_name == "Maya Rao" and not self.failed:
            self.failed = True
            raise RuntimeError("Simulated renderer failure")
        return self.delegate.render(certificate)


def test_failure_is_isolated_and_retry_preserves_successful_certificate(system, payload):
    system.worker.renderer = FailMayaOnce()
    job_id = create_job(system, payload)
    assert system.client.post(f"/api/jobs/{job_id}/retry").status_code == 409
    system.worker.run_once()
    job = system.client.get(f"/api/jobs/{job_id}").json()
    assert (job["status"], job["succeeded"], job["failed"]) == ("PARTIALLY_COMPLETED", 1, 1)
    before = system.client.get(f"/api/jobs/{job_id}/recipients").json()["items"]
    assert before[1]["error_code"] == "GENERATION_FAILED"
    assert "Simulated" not in before[1]["error_message"]
    assert system.client.post(f"/api/jobs/{job_id}/retry").status_code == 202
    system.worker.run_once()
    after = system.client.get(f"/api/jobs/{job_id}/recipients").json()["items"]
    assert after[0]["sha256"] == before[0]["sha256"]
    assert after[0]["attempts"] == 1
    assert after[1]["attempts"] == 2
    assert after[1]["error_code"] is None
    assert system.client.get(f"/api/jobs/{job_id}").json()["status"] == "COMPLETED"


def test_retry_attempt_limit(system, payload):
    class AlwaysFails:
        def render(self, certificate):
            raise RuntimeError("Failure")

    system.worker.renderer = AlwaysFails()
    job_id = create_job(system, payload)
    for attempt in range(3):
        if attempt:
            assert system.client.post(f"/api/jobs/{job_id}/retry").status_code == 202
        system.worker.run_once()
    assert system.client.post(f"/api/jobs/{job_id}/retry").status_code == 409
    rows = system.client.get(f"/api/jobs/{job_id}/recipients").json()["items"]
    assert all(row["attempts"] == 3 for row in rows)


def test_idempotency_replay_and_conflicting_payload(system, payload):
    headers = {"Idempotency-Key": "submission-001"}
    first = system.client.post("/api/jobs", json=payload, headers=headers)
    second = system.client.post("/api/jobs", json=payload, headers=headers)
    assert second.status_code == 200
    assert second.headers["idempotency-replayed"] == "true"
    assert second.json()["id"] == first.json()["id"]
    payload["title"] = "Different course"
    conflict = system.client.post("/api/jobs", json=payload, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_pagination_and_status_filter(system, payload):
    job_id = create_job(system, payload)
    page = system.client.get(f"/api/jobs/{job_id}/recipients?limit=1&offset=1").json()
    assert page["total"] == 2
    assert len(page["items"]) == 1
    assert page["items"][0]["name"] == "Maya Rao"
    assert system.client.get(f"/api/jobs/{job_id}/recipients?limit=201").status_code == 422


def test_missing_jobs_and_unready_downloads(system, payload):
    assert system.client.get(f"/api/jobs/{uuid4()}").status_code == 404
    assert system.client.get("/api/jobs/not-a-uuid").status_code == 422
    job_id = create_job(system, payload)
    row = system.client.get(f"/api/jobs/{job_id}/recipients").json()["items"][0]
    assert system.client.get(f"/api/jobs/{job_id}/certificates/{row['id']}").status_code == 409
    assert system.client.get(f"/api/jobs/{job_id}/download").status_code == 409


def test_api_key_protects_job_endpoints(tmp_path, payload):
    with TestClient(create_app(Settings(data_dir=tmp_path, api_key="test-secret"))) as client:
        assert client.get("/health/live").status_code == 200
        assert client.post("/api/jobs", json=payload).status_code == 401
        assert (
            client.post("/api/jobs", json=payload, headers={"X-API-Key": "wrong"}).status_code
            == 401
        )
        response = client.post("/api/jobs", json=payload, headers={"X-API-Key": "test-secret"})
        assert response.status_code == 202
        job_id = response.json()["id"]
        for suffix in ("", "/recipients", "/download", f"/certificates/{uuid4()}"):
            assert client.get(f"/api/jobs/{job_id}{suffix}").status_code == 401
        assert client.post(f"/api/jobs/{job_id}/retry").status_code == 401


def test_request_body_limit_also_applies_without_content_length(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, max_body_bytes=64))) as client:
        response = client.post("/api/jobs", content=iter([b"x" * 40, b"y" * 40]))
        assert response.status_code == 413


def test_health_reports_missing_worker_then_live_worker(system):
    assert system.client.get("/health/ready").status_code == 503
    system.worker.run_once()
    assert system.client.get("/health/ready").status_code == 200


def test_lost_pdf_has_explicit_storage_error(system, payload):
    job_id = create_job(system, payload)
    system.worker.run_once()
    recipient = system.client.get(f"/api/jobs/{job_id}/recipients").json()["items"][0]
    record = system.worker.repository.certificate(job_id, recipient["id"])
    system.worker.storage.discard(record["artifact_path"])
    response = system.client.get(recipient["download_url"])
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ARTIFACT_UNAVAILABLE"


def test_text_escaping_and_unsupported_font_are_explicit(system, payload):
    payload["recipients"] = [
        {"name": "Ana <Engineer> & José", "email": "ana@example.com"},
        {"name": "Engineer 🦄", "email": "other@example.com"},
    ]
    job_id = create_job(system, payload)
    system.worker.run_once()
    rows = system.client.get(f"/api/jobs/{job_id}/recipients").json()["items"]
    pdf = system.client.get(rows[0]["download_url"])
    assert "Ana <Engineer> & José" in PdfReader(BytesIO(pdf.content)).pages[0].extract_text()
    assert rows[1]["error_code"] == "UNSUPPORTED_TEXT"


def test_openapi_and_interactive_docs_are_available(system):
    assert system.client.get("/docs").status_code == 200
    schema = system.client.get("/openapi.json").json()
    assert "/api/jobs/{job_id}/retry" in schema["paths"]
    assert schema["components"]["securitySchemes"]["APIKeyHeader"]["name"] == "X-API-Key"
