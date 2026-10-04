from pathlib import Path

from agent_reviewer.config import Settings
from agent_reviewer.pipeline import run_scan


def test_local_scan_without_llm(tmp_path: Path) -> None:
    examples = Path(__file__).resolve().parents[1] / "examples" / "inefficient"
    settings = Settings(
        data_dir=tmp_path,
        openai_api_key="",
        github_token="",
    )
    job, findings = run_scan(
        repo="local/examples",
        ref="local",
        settings=settings,
        local_path=examples,
        skip_llm=True,
    )
    assert job.files_scanned >= 3
    assert job.candidates_found == len(findings) > 0
    files = {item.file_path for item in findings}
    assert "orders.sql" in files
    assert "rollup.py" in files
    assert "jobs.py" in files
