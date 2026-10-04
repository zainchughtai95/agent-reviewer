from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agent_reviewer.models import Finding, FindingStatus, ReviewJob


class FindingStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS findings (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )

    def save_job(self, job: ReviewJob) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jobs (id, payload) VALUES (?, ?)",
                (job.id, job.model_dump_json()),
            )

    def save_findings(self, job_id: str, findings: list[Finding]) -> None:
        with self._connect() as conn:
            for finding in findings:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO findings (id, job_id, repo, status, payload)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        finding.id,
                        job_id,
                        finding.repo,
                        finding.status.value,
                        finding.model_dump_json(),
                    ),
                )

    def list_jobs(self) -> list[ReviewJob]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM jobs ORDER BY id DESC"
            ).fetchall()
        return [ReviewJob.model_validate_json(row["payload"]) for row in rows]

    def get_job(self, job_id: str) -> ReviewJob | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return ReviewJob.model_validate_json(row["payload"]) if row else None

    def list_findings(
        self,
        job_id: str | None = None,
        status: FindingStatus | None = None,
        repo: str | None = None,
    ) -> list[Finding]:
        query = "SELECT payload FROM findings WHERE 1=1"
        params: list[object] = []
        if job_id:
            query += " AND job_id = ?"
            params.append(job_id)
        if status:
            query += " AND status = ?"
            params.append(status.value)
        if repo:
            query += " AND repo = ?"
            params.append(repo)
        query += " ORDER BY id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [Finding.model_validate_json(row["payload"]) for row in rows]

    def get_finding(self, finding_id: str) -> Finding | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM findings WHERE id = ?", (finding_id,)
            ).fetchone()
        return Finding.model_validate_json(row["payload"]) if row else None

    def update_finding(self, finding: Finding) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE findings
                SET status = ?, payload = ?
                WHERE id = ?
                """,
                (finding.status.value, finding.model_dump_json(), finding.id),
            )

    def export_json(self, target: Path) -> None:
        payload = {
            "jobs": [job.model_dump() for job in self.list_jobs()],
            "findings": [finding.model_dump() for finding in self.list_findings()],
        }
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
