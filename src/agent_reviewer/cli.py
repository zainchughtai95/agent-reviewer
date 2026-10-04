from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from agent_reviewer.config import get_settings
from agent_reviewer.models import FindingStatus
from agent_reviewer.pipeline import run_scan
from agent_reviewer.pr_creator import create_pull_request_for_approved
from agent_reviewer.store import FindingStore
from agent_reviewer.web.app import serve_app

app = typer.Typer(
    help="Scan GitHub SQL/Python/Spark code, review findings with a human, then open a PR.",
    no_args_is_help=True,
)
console = Console()


@app.command()
def scan(
    repo: str = typer.Argument(..., help="GitHub repo as owner/name"),
    ref: str = typer.Option("main", help="Branch or SHA to scan"),
    local: Path | None = typer.Option(
        None, help="Scan a local directory instead of GitHub"
    ),
    skip_llm: bool = typer.Option(
        False, help="Skip OpenAI and keep parser findings only"
    ),
    quiet: bool = typer.Option(False, "--quiet", help="Print only the summary table"),
) -> None:
    """Find inefficient SQL, Python, and Spark code and store findings for human review."""
    settings = get_settings()
    job, findings = run_scan(
        repo=repo,
        ref=ref,
        settings=settings,
        local_path=local,
        skip_llm=skip_llm,
        reporter=None if quiet else console.print,
    )
    table = Table(title=f"Job {job.id} — {job.repo}@{job.ref}")
    table.add_column("Severity")
    table.add_column("File")
    table.add_column("Finding")
    for finding in findings:
        table.add_row(
            finding.severity.value,
            f"{finding.file_path}:{finding.start_line}",
            finding.title,
        )
    console.print(table)
    console.print(
        f"[green]Scanned {job.files_scanned} files, "
        f"{job.candidates_found} parser findings, "
        f"{job.findings_count} findings ready for review.[/green]"
    )
    console.print("Next: agent-reviewer serve")


@app.command()
def serve(
    host: str | None = typer.Option(None),
    port: int | None = typer.Option(None),
) -> None:
    """Open the human review queue in a browser."""
    settings = get_settings()
    serve_app(host or settings.review_host, port or settings.review_port)


@app.command("create-pr")
def create_pr(
    repo: str = typer.Argument(..., help="GitHub repo as owner/name"),
    job_id: str | None = typer.Option(None, help="Limit to one scan job"),
    base: str | None = typer.Option(None, help="Base branch, defaults to the scanned ref"),
) -> None:
    """Open a GitHub PR for findings a human already approved."""
    pull = create_pull_request_for_approved(repo, job_id=job_id, base=base)
    console.print(f"[green]Opened PR:[/green] {pull.get('html_url')}")


@app.command()
def status(
    job_id: str | None = typer.Option(None),
) -> None:
    """Show stored findings and their review status."""
    store = FindingStore(get_settings().findings_db())
    findings = store.list_findings(job_id=job_id)
    table = Table(title="Findings")
    table.add_column("ID")
    table.add_column("Status")
    table.add_column("Severity")
    table.add_column("Repo")
    table.add_column("Title")
    for finding in findings:
        table.add_row(
            finding.id[:8],
            finding.status.value,
            finding.severity.value,
            finding.repo,
            finding.title,
        )
    console.print(table)
    counts = {status.value: 0 for status in FindingStatus}
    for finding in findings:
        counts[finding.status.value] += 1
    console.print(counts)


if __name__ == "__main__":
    app()
