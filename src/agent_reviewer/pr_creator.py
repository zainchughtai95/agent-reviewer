from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from agent_reviewer.config import Settings
from agent_reviewer.github_client import GitHubClient, parse_repo
from agent_reviewer.models import Finding, FindingStatus
from agent_reviewer.store import FindingStore


def apply_snippet(original: str, finding: Finding) -> str:
    """Replace the first occurrence of the reviewed snippet with the proposed rewrite."""
    if finding.snippet in original:
        return original.replace(finding.snippet, finding.proposed_snippet, 1)

    lines = original.splitlines()
    start = max(finding.start_line - 1, 0)
    end = min(finding.end_line, len(lines))
    rewritten = (
        lines[:start]
        + finding.proposed_snippet.splitlines()
        + lines[end:]
    )
    return "\n".join(rewritten) + ("\n" if original.endswith("\n") else "")


def create_pull_request_for_approved(
    repo: str,
    *,
    settings: Settings | None = None,
    job_id: str | None = None,
    base: str | None = None,
) -> dict:
    settings = settings or Settings()
    store = FindingStore(settings.findings_db())
    findings = [
        finding
        for finding in store.list_findings(job_id=job_id, repo=repo)
        if finding.status is FindingStatus.APPROVED
    ]
    if not findings:
        raise RuntimeError("No approved findings to apply. Review them in the UI first.")

    owner, name = parse_repo(repo)
    github = GitHubClient(settings)
    repo_meta = github.get_repo(owner, name)
    base_branch = base or findings[0].ref or repo_meta.get("default_branch") or "main"
    base_sha = github.get_ref_sha(owner, name, base_branch)
    commit = github.get_commit(owner, name, base_sha)
    base_tree = commit["tree"]["sha"]

    grouped: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        grouped[finding.file_path].append(finding)

    tree_entries: list[dict] = []
    for path, file_findings in grouped.items():
        content = github.get_file_text(owner, name, path, base_sha)
        for finding in sorted(file_findings, key=lambda item: item.start_line, reverse=True):
            content = apply_snippet(content, finding)
        blob_sha = github.create_blob(owner, name, content)
        tree_entries.append(
            {
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha,
            }
        )

    tree_sha = github.create_tree(owner, name, base_tree, tree_entries)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch = f"agent-reviewer/optimizations-{stamp}"
    message = "Apply approved inefficiency fixes from agent-reviewer"
    commit_sha = github.create_commit(
        owner,
        name,
        message=message,
        tree=tree_sha,
        parents=[base_sha],
    )
    github.create_ref(owner, name, branch, commit_sha)

    body = _pr_body(findings)
    pull = github.create_pull_request(
        owner,
        name,
        title="Performance fixes from agent-reviewer",
        body=body,
        head=branch,
        base=base_branch,
    )

    for finding in findings:
        finding.status = FindingStatus.APPLIED
        store.update_finding(finding)

    return pull


def _pr_body(findings: list[Finding]) -> str:
    lines = [
        "This pull request was opened **after a human approved** the findings below.",
        "",
        "The agent scanned SQL, Python, and Spark files, asked OpenAI for reasoning and",
        "severity, and only applied snippets that a reviewer signed off on.",
        "",
        "| Severity | File | Finding |",
        "| --- | --- | --- |",
    ]
    for finding in findings:
        lines.append(
            f"| {finding.severity.value} | `{finding.file_path}:{finding.start_line}` | {finding.title} |"
        )
    lines.extend(["", "## Reasoning", ""])
    for finding in findings:
        lines.extend(
            [
                f"### {finding.title} (`{finding.file_path}`)",
                "",
                f"- Severity: `{finding.severity.value}` (confidence {finding.confidence:.2f})",
                f"- Impact: {finding.impact}",
                f"- Why: {finding.reasoning}",
                f"- Fix: {finding.proposed_fix}",
                "",
            ]
        )
    lines.append("Please still treat this as a suggested patch and run tests before merge.")
    return "\n".join(lines)
