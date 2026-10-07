"""Measure real local HTTP submission latency and end-to-end generation throughput.

Uses isolated temporary data, real API/worker processes, and no mocked PDF generation.
Run: python scripts/benchmark.py --jobs 10 --recipients 100 --concurrency 4
"""

import argparse
import concurrent.futures
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=10)
    parser.add_argument("--recipients", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output", type=Path, default=Path("benchmark-results.json"))
    args = parser.parse_args()
    if args.jobs < 1 or not 1 <= args.recipients <= 1000 or args.concurrency < 1:
        parser.error("Use jobs >= 1, recipients between 1 and 1000, and concurrency >= 1")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="certificate-benchmark-") as temporary:
        env = dict(os.environ, CERTIFICATE_DATA_DIR=temporary)
        env.pop("CERTIFICATE_API_KEY", None)
        log_path = Path(temporary) / "processes.log"
        processes = []
        with log_path.open("w", encoding="utf-8") as log:
            try:
                for command in (
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "certificate_forge.app:create_app",
                        "--factory",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ],
                    [sys.executable, "-m", "certificate_forge.worker"],
                ):
                    processes.append(
                        subprocess.Popen(
                            command,
                            env=env,
                            stdout=log,
                            stderr=log,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                        )
                    )
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{port}", timeout=30, trust_env=False
                ) as client:
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        try:
                            if client.get("/health/ready").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(0.1)
                    else:
                        raise RuntimeError("API and worker did not become ready")
                    payload = {
                        "title": "Backend Engineering Foundations",
                        "issuer": "Engineering Learning Lab",
                        "issued_on": "2026-10-07",
                        "recipients": [
                            {"name": f"Participant {i}", "email": f"person{i}@example.com"}
                            for i in range(args.recipients)
                        ],
                    }

                    def submit(index: int) -> tuple[str, float]:
                        started = time.perf_counter()
                        response = client.post(
                            "/api/jobs", json=payload, headers={"Idempotency-Key": f"bench-{index}"}
                        )
                        duration_ms = (time.perf_counter() - started) * 1000
                        response.raise_for_status()
                        assert response.status_code == 202
                        return response.json()["id"], duration_ms

                    started = time.perf_counter()
                    with concurrent.futures.ThreadPoolExecutor(args.concurrency) as pool:
                        results = list(pool.map(submit, range(args.jobs)))
                    submission_elapsed = time.perf_counter() - started
                    pending = {job_id for job_id, _ in results}
                    status_latencies = []
                    deadline = time.monotonic() + 300
                    while pending and time.monotonic() < deadline:
                        for job_id in list(pending):
                            status_started = time.perf_counter()
                            response = client.get(f"/api/jobs/{job_id}")
                            status_latencies.append((time.perf_counter() - status_started) * 1000)
                            response.raise_for_status()
                            job = response.json()
                            if job["status"] in {"FAILED", "PARTIALLY_COMPLETED"}:
                                raise RuntimeError(f"Unexpected unsuccessful benchmark job: {job}")
                            if job["status"] == "COMPLETED":
                                assert job["succeeded"] == args.recipients
                                pending.remove(job_id)
                        if pending:
                            time.sleep(0.1)
                    if pending:
                        raise TimeoutError("Generation did not complete within five minutes")
                    elapsed = time.perf_counter() - started
                    latencies = [latency for _, latency in results]
                    report = {
                        "environment": {
                            "python": platform.python_version(),
                            "platform": platform.platform(),
                            "cpu_count": os.cpu_count(),
                        },
                        "workload": {
                            "jobs": args.jobs,
                            "recipients_per_job": args.recipients,
                            "concurrency": args.concurrency,
                            "workers": 1,
                        },
                        "submission_ms": {
                            "p50": round(statistics.median(latencies), 2),
                            "p95": round(percentile(latencies, 0.95), 2),
                            "max": round(max(latencies), 2),
                        },
                        "status_ms": {
                            "p50": round(statistics.median(status_latencies), 2),
                            "p95": round(percentile(status_latencies, 0.95), 2),
                        },
                        "submission_elapsed_seconds": round(submission_elapsed, 3),
                        "end_to_end_seconds": round(elapsed, 3),
                        "certificates_per_second": round(args.jobs * args.recipients / elapsed, 2),
                        "completed_certificates": args.jobs * args.recipients,
                        "note": "Localhost measurement; one run, not a production SLA.",
                    }
                    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                    print(json.dumps(report, indent=2))
            except BaseException:
                log.flush()
                print(log_path.read_text(encoding="utf-8")[-8000:], file=sys.stderr)
                raise
            finally:
                for process in processes:
                    process.terminate()
                for process in processes:
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == "__main__":
    main()
