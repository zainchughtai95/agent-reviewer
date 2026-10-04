from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class Language(str, Enum):
    SQL = "sql"
    PYTHON = "python"
    SPARK = "spark"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"


class Candidate(BaseModel):
    file_path: str
    language: Language
    rule_id: str
    title: str
    start_line: int
    end_line: int
    snippet: str
    hint: str


class Finding(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    repo: str
    ref: str
    file_path: str
    language: Language
    rule_id: str
    title: str
    start_line: int
    end_line: int
    snippet: str
    reasoning: str
    impact: str
    proposed_fix: str
    proposed_snippet: str
    severity: Severity
    confidence: float = 0.5
    status: FindingStatus = FindingStatus.PENDING
    reviewer_notes: str = ""
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class ReviewJob(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    repo: str
    ref: str
    source: Literal["github", "local"] = "github"
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    files_scanned: int = 0
    candidates_found: int = 0
    findings_count: int = 0
