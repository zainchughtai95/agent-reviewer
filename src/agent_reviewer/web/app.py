from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from agent_reviewer.config import get_settings
from agent_reviewer.models import FindingStatus
from agent_reviewer.pr_creator import create_pull_request_for_approved
from agent_reviewer.store import FindingStore

TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)


def create_app() -> FastAPI:
    app = FastAPI(title="Agent Reviewer")
    settings = get_settings()

    def store() -> FindingStore:
        return FindingStore(settings.findings_db())

    @app.get("/", response_class=HTMLResponse)
    def queue(request: Request, job_id: str | None = None, status: str | None = None):
        parsed_status = FindingStatus(status) if status else None
        findings = store().list_findings(job_id=job_id, status=parsed_status)
        jobs = store().list_jobs()
        counts = {
            "pending": len(store().list_findings(status=FindingStatus.PENDING)),
            "approved": len(store().list_findings(status=FindingStatus.APPROVED)),
            "rejected": len(store().list_findings(status=FindingStatus.REJECTED)),
            "applied": len(store().list_findings(status=FindingStatus.APPLIED)),
        }
        return TEMPLATES.TemplateResponse(
            request,
            "queue.html",
            {
                "findings": findings,
                "jobs": jobs,
                "counts": counts,
                "current_job": job_id,
                "current_status": status or "all",
            },
        )

    @app.get("/findings/{finding_id}", response_class=HTMLResponse)
    def detail(request: Request, finding_id: str):
        finding = store().get_finding(finding_id)
        if not finding:
            raise HTTPException(status_code=404, detail="Finding not found")
        return TEMPLATES.TemplateResponse(
            request,
            "finding.html",
            {"finding": finding},
        )

    @app.post("/findings/{finding_id}/review")
    def review(
        finding_id: str,
        action: str = Form(...),
        notes: str = Form(""),
        proposed_snippet: str = Form(""),
    ):
        db = store()
        finding = db.get_finding(finding_id)
        if not finding:
            raise HTTPException(status_code=404, detail="Finding not found")
        finding.reviewer_notes = notes
        if proposed_snippet.strip():
            finding.proposed_snippet = proposed_snippet
        if action == "approve":
            finding.status = FindingStatus.APPROVED
        elif action == "reject":
            finding.status = FindingStatus.REJECTED
        else:
            raise HTTPException(status_code=400, detail="Unknown action")
        db.update_finding(finding)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/jobs/{job_id}/create-pr")
    def create_pr(job_id: str):
        db = store()
        job = db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.source != "github":
            raise HTTPException(
                status_code=400,
                detail="Local scans cannot open a GitHub PR. Re-scan the GitHub repo.",
            )
        pull = create_pull_request_for_approved(job.repo, settings=settings, job_id=job_id)
        return RedirectResponse(url=pull["html_url"], status_code=303)

    return app


def serve_app(host: str, port: int) -> None:
    uvicorn.run(
        "agent_reviewer.web.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=False,
    )
