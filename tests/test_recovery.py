from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from certificate_forge.app import create_app
from certificate_forge.schemas import JobCreate


def expire_lease(repository, job_id):
    with repository.database.connection() as connection:
        connection.execute("UPDATE jobs SET lease_expires_at = 0 WHERE id = ?", (job_id,))


def test_job_survives_application_restart(system, payload):
    job_id = system.client.post("/api/jobs", json=payload).json()["id"]
    with TestClient(create_app(system.settings)) as restarted:
        assert restarted.get(f"/api/jobs/{job_id}").json()["status"] == "QUEUED"
        system.worker.run_once()
        assert restarted.get(f"/api/jobs/{job_id}").json()["status"] == "COMPLETED"


def test_crashed_worker_is_recovered_and_old_lease_is_fenced(system, payload):
    job_id = system.client.post("/api/jobs", json=payload).json()["id"]
    repository = system.worker.repository
    old_claim = repository.claim(30, 3)
    recipient = repository.next_recipient(old_claim)
    assert repository.claim(30, 3) is None
    expire_lease(repository, job_id)
    new_claim = repository.claim(30, 3)
    assert new_claim.lease_token != old_claim.lease_token
    assert repository.next_recipient(old_claim) is None
    assert not repository.complete_recipient(
        old_claim, recipient["id"], error_code="STALE", error_message="Should not persist"
    )
    assert not repository.finish(old_claim)
    assert not repository.extend_lease(old_claim, 30)
    expire_lease(repository, job_id)
    system.worker.run_once()
    result = system.client.get(f"/api/jobs/{job_id}").json()
    assert result["status"] == "COMPLETED"
    rows = system.client.get(f"/api/jobs/{job_id}/recipients").json()["items"]
    assert rows[0]["attempts"] == 2
    assert all(row["error_code"] is None for row in rows)


def test_repeated_worker_crashes_have_bounded_attempts(system, payload):
    payload["recipients"] = payload["recipients"][:1]
    job_id = system.client.post("/api/jobs", json=payload).json()["id"]
    repository = system.worker.repository
    for _ in range(3):
        claim = repository.claim(30, 3)
        assert repository.next_recipient(claim)
        expire_lease(repository, job_id)
    system.worker.run_once()
    row = system.client.get(f"/api/jobs/{job_id}/recipients").json()["items"][0]
    assert row["status"] == "FAILED"
    assert row["attempts"] == 3
    assert row["error_code"] == "ATTEMPTS_EXHAUSTED"


def test_simultaneous_idempotent_submissions_create_one_job(system, payload):
    service = system.client.app.state.service
    request = JobCreate.model_validate(payload)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: service.create(request, "same-key"), range(8)))
    assert len({job.id for job, _ in results}) == 1
    assert sum(not replayed for _, replayed in results) == 1
    assert system.worker.run_once()
    assert not system.worker.run_once()


def test_two_workers_cannot_claim_the_same_live_job(system, payload):
    system.client.post("/api/jobs", json=payload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: system.worker.repository.claim(30, 3), range(2)))
    assert sum(claim is not None for claim in claims) == 1


def test_new_lease_artifacts_do_not_overwrite_old_attempt(system):
    storage = system.worker.storage
    old, _ = storage.save("job", "recipient", "old-token", b"old")
    new, _ = storage.save("job", "recipient", "new-token", b"new")
    assert old != new
    assert storage.available_path(old).read_bytes() == b"old"
    assert storage.available_path(new).read_bytes() == b"new"
