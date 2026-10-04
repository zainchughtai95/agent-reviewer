from __future__ import annotations

import re
from pathlib import Path

from agent_reviewer.models import Candidate, Language

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

SQL_RULES = [
    (
        "sql.select_star",
        r"\bSELECT\s+\*",
        "SELECT *",
        "Selecting every column increases I/O, network, and memory use.",
    ),
    (
        "sql.leading_wildcard_like",
        r"\bLIKE\s+'%",
        "Leading-wildcard LIKE",
        "LIKE '%value' typically cannot use a B-tree index.",
    ),
    (
        "sql.function_on_column",
        r"\bWHERE\s+\w+\([^)]+\)\s*=",
        "Function on filtered column",
        "Wrapping a column in a function often disables index use.",
    ),
    (
        "sql.not_in_subquery",
        r"\bNOT\s+IN\s*\(",
        "NOT IN subquery",
        "NOT IN is slow and NULLs can produce surprising empty results.",
    ),
    (
        "sql.distinct_band_aid",
        r"\bSELECT\s+DISTINCT\b",
        "SELECT DISTINCT",
        "DISTINCT often hides a bad join and adds a full sort/hash.",
    ),
    (
        "sql.cross_join",
        r"\bCROSS\s+JOIN\b",
        "CROSS JOIN",
        "Cartesian products explode row counts unless they are intentional.",
    ),
    (
        "sql.correlated_subquery",
        r"\bWHERE\s+\w+\s+IN\s*\(\s*SELECT\b",
        "Correlated / nested IN subquery",
        "Nested subqueries in filters often re-execute per outer row.",
    ),
    (
        "sql.order_without_limit",
        r"\bORDER\s+BY\b(?![^;]{0,200}\bLIMIT\b)",
        "ORDER BY without LIMIT",
        "Sorting a large result set without LIMIT is expensive.",
    ),
]

PYTHON_RULES = [
    (
        "py.pandas_iterrows",
        r"\.iterrows\s*\(",
        "pandas iterrows()",
        "Row-wise pandas iteration is much slower than vectorized ops.",
    ),
    (
        "py.pandas_apply",
        r"\.apply\s*\(",
        "pandas apply()",
        "apply() is often a hidden Python loop; prefer vectorized methods.",
    ),
    (
        "py.concat_in_loop",
        r"for\s+.+:\s*\n(?:.*\n){0,8}.*\+=\s*['\"]",
        "String concatenation in a loop",
        "Repeated += on strings copies the whole string each time.",
    ),
    (
        "py.nested_for",
        r"for\s+.+:\s*\n(?:[ \t]+.*\n)*[ \t]+for\s+.+:",
        "Nested for-loops",
        "Nested loops are often O(n^2); look for joins, maps, or vectorization.",
    ),
    (
        "py.read_all",
        r"\.read\(\)\s*$",
        "Read entire file into memory",
        "Loading whole files can blow memory; stream or chunk if possible.",
    ),
    (
        "py.global_list_append_loop",
        r"for\s+.+:\s*\n(?:.*\n){0,12}.*\.append\(",
        "Row-by-row list building",
        "Appending in a Python loop may be replaceable with a comprehension or bulk API.",
    ),
    (
        "py.time_sleep",
        r"time\.sleep\s*\(",
        "time.sleep in application code",
        "Sleep is a common stand-in for backoff/polling and stalls workers.",
    ),
]

SPARK_RULES = [
    (
        "spark.collect",
        r"\.collect\s*\(",
        "DataFrame.collect()",
        "collect() pulls the full dataset to the driver and can OOM.",
    ),
    (
        "spark.to_pandas",
        r"\.toPandas\s*\(",
        "toPandas()",
        "toPandas() materializes the whole DataFrame on the driver.",
    ),
    (
        "spark.count_in_loop",
        r"for\s+.+:\s*\n(?:.*\n){0,10}.*\.count\s*\(",
        "count() inside a loop",
        "Each count() is a Spark job; repeated counts are very expensive.",
    ),
    (
        "spark.python_udf",
        r"\b(?:udf|pandas_udf)\s*\(",
        "Python UDF",
        "Python UDFs break Catalyst optimization and add serialization cost.",
    ),
    (
        "spark.rdd_map",
        r"\.rdd\.(?:map|flatMap|foreach)\s*\(",
        "RDD map instead of DataFrame API",
        "RDDs skip Catalyst and Tungsten optimizations.",
    ),
    (
        "spark.coalesce_one",
        r"\.coalesce\s*\(\s*1\s*\)",
        "coalesce(1)",
        "Writing through a single partition serializes the whole job.",
    ),
    (
        "spark.cartesian",
        r"\.crossJoin\s*\(|\.cartesian\s*\(",
        "Cartesian Spark join",
        "crossJoin/cartesian can explode partitions and shuffle size.",
    ),
    (
        "spark.repartition_no_key",
        r"\.repartition\s*\(\s*\d+\s*\)",
        "repartition by count only",
        "Hash-repartitioning without a key can add a full shuffle.",
    ),
    (
        "spark.take_all",
        r"\.take\s*\(\s*\d{4,}\s*\)",
        "Large take()",
        "take() of thousands of rows still concentrates data on the driver.",
    ),
]


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


def _line_span(text: str, start: int, end: int) -> tuple[int, int]:
    start_line = text.count("\n", 0, start) + 1
    end_line = text.count("\n", 0, end) + 1
    return start_line, end_line


def _context(text: str, start: int, end: int, radius: int = 4) -> tuple[str, int, int]:
    lines = text.splitlines()
    start_line, end_line = _line_span(text, start, end)
    lo = max(0, start_line - 1 - radius)
    hi = min(len(lines), end_line + radius)
    snippet = "\n".join(lines[lo:hi])
    return snippet, lo + 1, hi


def scan_text(file_path: str, content: str) -> list[Candidate]:
    language = classify_path(file_path, content)
    if language is None:
        return []

    if language is Language.SQL:
        rules = SQL_RULES
    elif language is Language.SPARK:
        rules = SPARK_RULES + PYTHON_RULES
    else:
        rules = PYTHON_RULES

    flags = re.IGNORECASE | re.MULTILINE
    found: list[Candidate] = []
    seen: set[tuple[str, int]] = set()

    for rule_id, pattern, title, hint in rules:
        try:
            matches = list(re.finditer(pattern, content, flags))
        except re.error:
            continue
        for match in matches[:8]:
            snippet, start_line, end_line = _context(
                content, match.start(), match.end()
            )
            key = (rule_id, start_line)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                Candidate(
                    file_path=file_path,
                    language=language,
                    rule_id=rule_id,
                    title=title,
                    start_line=start_line,
                    end_line=end_line,
                    snippet=snippet.strip("\n"),
                    hint=hint,
                )
            )
    return found
