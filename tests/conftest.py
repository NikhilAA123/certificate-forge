from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from certificate_forge.app import create_app
from certificate_forge.config import Settings
from certificate_forge.rendering import PdfCertificateRenderer
from certificate_forge.worker import CertificateWorker


@dataclass
class TestSystem:
    __test__ = False
    client: TestClient
    worker: CertificateWorker
    settings: Settings


@pytest.fixture
def system(tmp_path):
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        service = app.state.service
        worker = CertificateWorker(
            service.repository, PdfCertificateRenderer(), service.storage, settings
        )
        yield TestSystem(client, worker, settings)


@pytest.fixture
def payload():
    return {
        "title": "Backend Engineering Foundations",
        "issuer": "Engineering Learning Lab",
        "issued_on": "2026-10-07",
        "recipients": [
            {"name": "Aarav Sharma", "email": "aarav@example.com"},
            {"name": "Maya Rao", "email": "maya@example.com"},
        ],
    }
