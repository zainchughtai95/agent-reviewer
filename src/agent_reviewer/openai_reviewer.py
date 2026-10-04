from __future__ import annotations

import json

from openai import OpenAI

from agent_reviewer.config import Settings
from agent_reviewer.models import Candidate, Finding, Language, Severity


SYSTEM_PROMPT = """You are a senior data engineer reviewing production code for
inefficiency. You only comment on real performance, cost, or scalability problems
in SQL, Python, or Spark/PySpark.

Return a JSON object with:
- is_real_issue: boolean. False if the heuristic is a false positive or the snippet is fine.
- title: short finding title
- severity: one of critical, high, medium, low, info
- confidence: number from 0 to 1
- reasoning: 2-4 sentences explaining WHY this is inefficient, in plain language
- impact: who/what it hurts (driver OOM, full table scan, shuffle, CPU, dollars)
- proposed_fix: concrete rewrite guidance a human can review
- proposed_snippet: a drop-in replacement for the provided snippet. Keep surrounding
  context. Do not invent APIs. If you cannot safely rewrite it, repeat the original snippet.

Severity guide:
- critical: likely outage, OOM, or full production table scan at scale
- high: large shuffle/scan or driver collect on a real dataset
- medium: clearly wasteful but maybe acceptable on small data
- low: style-level inefficiency
- info: worth knowing, not worth changing yet
"""


class OpenAIReviewer:
    def __init__(self, settings: Settings):
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        self.settings = settings
        self.client = OpenAI(api_key=settings.openai_api_key)

    def review_candidate(
        self, candidate: Candidate, repo: str, ref: str
    ) -> Finding | None:
        user_prompt = (
            f"Repository: {repo}@{ref}\n"
            f"File: {candidate.file_path}\n"
            f"Language: {candidate.language.value}\n"
            f"Heuristic: {candidate.rule_id} ({candidate.title})\n"
            f"Hint: {candidate.hint}\n"
            f"Lines: {candidate.start_line}-{candidate.end_line}\n\n"
            "Snippet:\n"
            f"```\n{candidate.snippet}\n```\n"
        )
        response = self.client.chat.completions.create(
            model=self.settings.openai_model,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = response.choices[0].message.content or "{}"
        data = json.loads(raw)
        if not data.get("is_real_issue", True):
            return None

        severity = _parse_severity(data.get("severity", "medium"))
        proposed = data.get("proposed_snippet") or candidate.snippet
        return Finding(
            repo=repo,
            ref=ref,
            file_path=candidate.file_path,
            language=Language(candidate.language),
            rule_id=candidate.rule_id,
            title=str(data.get("title") or candidate.title),
            start_line=candidate.start_line,
            end_line=candidate.end_line,
            snippet=candidate.snippet,
            reasoning=str(data.get("reasoning") or candidate.hint),
            impact=str(data.get("impact") or "Performance"),
            proposed_fix=str(data.get("proposed_fix") or ""),
            proposed_snippet=proposed,
            severity=severity,
            confidence=float(data.get("confidence") or 0.5),
        )


def _parse_severity(value: str) -> Severity:
    normalized = str(value).strip().lower()
    mapping = {item.value: item for item in Severity}
    return mapping.get(normalized, Severity.MEDIUM)
