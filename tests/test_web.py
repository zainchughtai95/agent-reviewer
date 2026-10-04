from fastapi.testclient import TestClient

from agent_reviewer.config import Settings
from agent_reviewer.models import Finding, FindingStatus, Language, ReviewJob, Severity
from agent_reviewer.store import FindingStore
from agent_reviewer.web.app import create_app


def test_review_queue_and_approve(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "findings.sqlite3"
    store = FindingStore(db_path)
    job = ReviewJob(repo="acme/demo", ref="main", source="github", findings_count=1)
    finding = Finding(
        repo="acme/demo",
        ref="main",
        file_path="jobs.py",
        language=Language.SPARK,
        rule_id="spark.collect",
        title="collect on driver",
        start_line=4,
        end_line=6,
        snippet="rows = orders.collect()",
        reasoning="This materializes the full dataset on the driver.",
        impact="Driver OOM",
        proposed_fix="Use take() or write out instead of collect().",
        proposed_snippet="rows = orders.limit(100).collect()",
        severity=Severity.CRITICAL,
    )
    store.save_job(job)
    store.save_findings(job.id, [finding])

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr("agent_reviewer.web.app.get_settings", lambda: settings)

    client = TestClient(create_app())
    home = client.get("/")
    assert home.status_code == 200
    assert "collect on driver" in home.text

    detail = client.get(f"/findings/{finding.id}")
    assert detail.status_code == 200
    assert "Driver OOM" in detail.text

    response = client.post(
        f"/findings/{finding.id}/review",
        data={"action": "approve", "notes": "looks right", "proposed_snippet": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    updated = store.get_finding(finding.id)
    assert updated is not None
    assert updated.status is FindingStatus.APPROVED
    assert updated.reviewer_notes == "looks right"
