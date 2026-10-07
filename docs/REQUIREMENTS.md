# Assignment coverage

| Assignment requirement | Implementation | Evidence |
|---|---|---|
| Python backend and relational database | FastAPI with SQLite | `pyproject.toml`, `database.py` |
| Accept one bulk generation request | `POST /api/jobs`, maximum 1,000 recipients | Full workflow and envelope validation tests |
| Validate recipient data | Independent Pydantic validation per recipient | Parameterized invalid-recipient tests |
| Generate a certificate for every valid recipient | One ReportLab PDF template | Tests parse actual PDFs and check name, title, issuer, and ID |
| Track status and progress | Job and recipient states persisted in SQLite | Workflow, partial-success, and restart tests |
| Retrieve generated certificates | Individual PDF and ZIP endpoints | PDF hash, content, and ZIP-manifest assertions |
| Isolate an individual failure | Worker commits each recipient's outcome separately | Injected renderer failure with successful sibling |
| Explain processing choice | Separate worker with database queue and leases | README reliability and latency sections |
| Document setup and tests | Cross-platform commands and exact dependency constraints | README and `requirements.lock` |
| Document example API usage | PowerShell, curl, example JSON, interactive OpenAPI | `examples/job.json`, `/docs` |
| Explain implementation decisions | Focused classes, dependency injection, explicit tradeoffs | README architecture and limitations |

Extras are scoped to the bulk workflow: idempotency, retrying failed certificates, ZIP manifests, pagination, optional API-key protection, worker readiness checks, request timing, and a reproducible benchmark. They do not require extra infrastructure to run locally.

## Five-minute review

1. Install and start the service using the README.
2. Open `/docs`, submit `examples/job.json`, and inspect the job result.
3. Verify that two certificates succeed and the invalid email is reported separately.
4. Download the ZIP and inspect its PDFs and `manifest.json`.
5. Run `pytest`; inspect the injected failure/retry and expired-lease tests.

For an interview, trace a request through `app.py`, `services.py`, and `repository.py`, then trace generation through `worker.py`, `rendering.py`, and `storage.py`. Be able to explain or modify each step; tooling assistance is allowed by the assignment, but understanding the submitted implementation is required.
