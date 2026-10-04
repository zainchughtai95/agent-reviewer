from __future__ import annotations

from pathlib import Path

from agent_reviewer.config import Settings
from agent_reviewer.github_client import GitHubClient, parse_repo
from agent_reviewer.heuristics import is_supported_path, scan_text, should_skip_path
from agent_reviewer.models import Candidate, Finding, ReviewJob, Severity
from agent_reviewer.openai_reviewer import OpenAIReviewer
from agent_reviewer.store import FindingStore


def scan_local_tree(root: Path) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        if should_skip_path(rel) or not is_supported_path(rel):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        files.append((rel, content))
    return files


def collect_candidates(files: list[tuple[str, str]]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for path, content in files:
        candidates.extend(scan_text(path, content))
    return candidates


def run_scan(
    *,
    repo: str,
    ref: str,
    settings: Settings | None = None,
    local_path: Path | None = None,
    skip_llm: bool = False,
) -> tuple[ReviewJob, list[Finding]]:
    settings = settings or Settings()
    store = FindingStore(settings.findings_db())

    if local_path:
        files = scan_local_tree(local_path)
        source = "local"
        job_repo = repo or f"local/{local_path.name}"
    else:
        owner, name = parse_repo(repo)
        github = GitHubClient(settings)
        meta = github.get_repo(owner, name)
        ref = ref or meta.get("default_branch") or "main"
        sha = github.get_ref_sha(owner, name, ref)
        fetched = github.fetch_supported_files(owner, name, sha)
        files = [(item.path, item.content) for item in fetched]
        source = "github"
        job_repo = f"{owner}/{name}"

    candidates = collect_candidates(files)
    findings: list[Finding] = []

    if skip_llm:
        for candidate in candidates:
            findings.append(
                Finding(
                    repo=job_repo,
                    ref=ref,
                    file_path=candidate.file_path,
                    language=candidate.language,
                    rule_id=candidate.rule_id,
                    title=candidate.title,
                    start_line=candidate.start_line,
                    end_line=candidate.end_line,
                    snippet=candidate.snippet,
                    reasoning=candidate.hint,
                    impact="Heuristic-only; run without --skip-llm for OpenAI reasoning.",
                    proposed_fix="Enable OpenAI review to generate a concrete rewrite.",
                    proposed_snippet=candidate.snippet,
                    severity=Severity.MEDIUM,
                    confidence=0.35,
                )
            )
    else:
        reviewer = OpenAIReviewer(settings)
        for candidate in candidates:
            finding = reviewer.review_candidate(candidate, job_repo, ref)
            if finding:
                findings.append(finding)

    job = ReviewJob(
        repo=job_repo,
        ref=ref,
        source=source,  # type: ignore[arg-type]
        files_scanned=len(files),
        candidates_found=len(candidates),
        findings_count=len(findings),
    )
    store.save_job(job)
    store.save_findings(job.id, findings)
    return job, findings
