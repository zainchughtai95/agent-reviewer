from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agent_reviewer.config import Settings
from agent_reviewer.github_client import GitHubClient, parse_repo
from agent_reviewer.heuristics import is_supported_path, scan_source, should_skip_path
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


Progress = Callable[[str], None]


def collect_candidates(files: list[tuple[str, str]]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for path, content in files:
        candidates.extend(scan_source(path, content).candidates)
    return candidates


def _from_candidate(candidate: Candidate, repo: str, ref: str) -> Finding:
    return Finding(
        repo=repo,
        ref=ref,
        file_path=candidate.file_path,
        language=candidate.language,
        rule_id=candidate.rule_id,
        title=candidate.title,
        start_line=candidate.start_line,
        end_line=candidate.end_line,
        snippet=candidate.snippet,
        reasoning=candidate.hint,
        impact="Parser finding. Re-run without --skip-llm for an OpenAI explanation.",
        proposed_fix="Enable OpenAI review to generate a concrete rewrite.",
        proposed_snippet=candidate.snippet,
        severity=Severity.MEDIUM,
        confidence=0.55,
    )


def run_scan(
    *,
    repo: str,
    ref: str,
    settings: Settings | None = None,
    local_path: Path | None = None,
    skip_llm: bool = False,
    reporter: Progress | None = None,
) -> tuple[ReviewJob, list[Finding]]:
    settings = settings or Settings()
    store = FindingStore(settings.findings_db())

    def emit(message: str) -> None:
        if reporter:
            reporter(message)

    if local_path:
        emit(f"Scanning local tree {local_path}")
        files = scan_local_tree(local_path)
        source = "local"
        job_repo = repo or f"local/{local_path.name}"
    else:
        owner, name = parse_repo(repo)
        emit(f"Connecting to GitHub {owner}/{name}")
        github = GitHubClient(settings)
        meta = github.get_repo(owner, name)
        ref = ref or meta.get("default_branch") or "main"
        emit(f"Reading tree {owner}/{name}@{ref}")
        sha = github.get_ref_sha(owner, name, ref)
        fetched = github.fetch_supported_files(owner, name, sha)
        files = [(item.path, item.content) for item in fetched]
        source = "github"
        job_repo = f"{owner}/{name}"
        emit(f"Downloaded {len(files)} SQL, Python, and Spark files")

    candidates: list[Candidate] = []
    total = len(files)
    for index, (path, content) in enumerate(files, start=1):
        result = scan_source(path, content)
        emit(f"[{index}/{total}] {path}  ({result.engine})")
        if result.detail:
            emit(f"    {result.detail}")
        if not result.candidates and not result.detail:
            emit("    no issues")
        for candidate in result.candidates:
            emit(f"    {candidate.title}  line {candidate.start_line}")
        candidates.extend(result.candidates)

    findings: list[Finding] = []
    if skip_llm:
        emit(f"OpenAI skipped. Storing {len(candidates)} parser findings for review.")
        findings.extend(_from_candidate(candidate, job_repo, ref) for candidate in candidates)
    elif not candidates:
        emit("Nothing to send to OpenAI.")
    else:
        emit(f"Asking OpenAI about {len(candidates)} findings")
        reviewer = OpenAIReviewer(settings)
        for index, candidate in enumerate(candidates, start=1):
            emit(
                f"  [{index}/{len(candidates)}] {candidate.file_path}:{candidate.start_line}  {candidate.title}"
            )
            try:
                finding = reviewer.review_candidate(candidate, job_repo, ref)
            except Exception as exc:
                emit(f"    OpenAI failed ({exc.__class__.__name__}). Keeping the parser finding.")
                finding = _from_candidate(candidate, job_repo, ref)
            if finding is None:
                emit("    dropped as a false positive")
                continue
            emit(f"    kept {finding.severity.value}  {finding.title}")
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
