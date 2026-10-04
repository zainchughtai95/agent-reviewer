"""Dispatch each file to SQLGlot or Python's AST.

SQL files are parsed with SQLGlot. Python and PySpark files are parsed with
the stdlib `ast` module. SQL string literals inside Python, and string literals
inside Scala, are parsed again with SQLGlot. The parsers do not execute code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agent_reviewer.models import Candidate, Language
from agent_reviewer.python_scan import embedded_sql_strings, find_python_issues
from agent_reviewer.sql_scan import (
    SPARK_DIALECTS,
    SQL_DIALECTS,
    extract_scala_strings,
    find_sql_issues,
    looks_like_sql,
)

SKIP_DIR_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".tox",
    "site-packages",
}

SQL_SUFFIXES = {".sql"}
PYTHON_SUFFIXES = {".py"}
SPARK_SUFFIXES = {".scala"}


@dataclass
class FileScan:
    path: str
    engine: str
    candidates: list[Candidate] = field(default_factory=list)
    detail: str = ""


def classify_path(path: str, content: str = "") -> Language | None:
    suffix = Path(path).suffix.lower()
    lowered = content.lower()
    if suffix in SQL_SUFFIXES:
        return Language.SQL
    if suffix in SPARK_SUFFIXES:
        return Language.SPARK
    if suffix in PYTHON_SUFFIXES:
        spark_markers = (
            "pyspark",
            "spark.sql",
            "spark.table",
            "spark.read",
            "from pyspark",
            "import pyspark",
        )
        if any(marker in lowered for marker in spark_markers):
            return Language.SPARK
        return Language.PYTHON
    return None


def should_skip_path(path: str) -> bool:
    parts = set(Path(path).parts)
    return bool(parts & SKIP_DIR_PARTS)


def is_supported_path(path: str) -> bool:
    if should_skip_path(path):
        return False
    suffix = Path(path).suffix.lower()
    return suffix in SQL_SUFFIXES | PYTHON_SUFFIXES | SPARK_SUFFIXES


def scan_text(file_path: str, content: str) -> list[Candidate]:
    return scan_source(file_path, content).candidates


def scan_source(file_path: str, content: str) -> FileScan:
    language = classify_path(file_path, content)
    if language is None:
        return FileScan(file_path, "skipped")

    suffix = Path(file_path).suffix.lower()
    if suffix in SQL_SUFFIXES:
        candidates, detail = find_sql_issues(
            content,
            file_path=file_path,
            file_text=content,
            dialects=SQL_DIALECTS,
        )
        return FileScan(file_path, "SQLGlot", candidates, detail)

    if suffix in SPARK_SUFFIXES:
        candidates: list[Candidate] = []
        errors: list[str] = []
        for line, text in extract_scala_strings(content):
            if not looks_like_sql(text):
                continue
            found, detail = find_sql_issues(
                text,
                file_path=file_path,
                file_text=content,
                start_line=line,
                dialects=SPARK_DIALECTS,
            )
            candidates.extend(found)
            if detail:
                errors.append(f"line {line}: {detail}")
        detail = errors[0] if errors and not candidates else ""
        return FileScan(file_path, "SQLGlot", _dedupe(candidates), detail)

    candidates, detail = find_python_issues(file_path, content, language)
    embedded, embedded_errors = _embedded_sql(file_path, content, language)
    engine = "Python AST + SQLGlot" if embedded else "Python AST"
    if detail and not candidates and not embedded:
        return FileScan(file_path, engine, [], detail)
    extra = f" Embedded SQL: {embedded_errors[0]}" if embedded_errors and not embedded else ""
    return FileScan(file_path, engine, _dedupe(candidates + embedded), extra.strip())


def _embedded_sql(
    file_path: str, content: str, language: Language
) -> tuple[list[Candidate], list[str]]:
    dialects = SPARK_DIALECTS if language is Language.SPARK else SQL_DIALECTS
    found: list[Candidate] = []
    errors: list[str] = []
    for line, text in embedded_sql_strings(content):
        if not looks_like_sql(text):
            continue
        candidates, detail = find_sql_issues(
            text,
            file_path=file_path,
            file_text=content,
            start_line=line,
            dialects=dialects,
        )
        found.extend(candidates)
        if detail:
            errors.append(detail)
    return found, errors


def _dedupe(candidates: list[Candidate]) -> list[Candidate]:
    seen: set[tuple[str, int, str]] = set()
    unique: list[Candidate] = []
    for candidate in candidates:
        key = (candidate.rule_id, candidate.start_line, candidate.file_path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique
