"""Find inefficient SQL by walking a SQLGlot tree.

SQLGlot parses the statement, so comments, string literals, and the difference
between `LOWER(column)` and `LOWER('literal')` stay intact. Parse failures are
reported and never turned into guesses.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import traverse_scope

from agent_reviewer.models import Candidate, Language

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_MAX_PER_RULE = 8

SQL_DIALECTS: tuple[str | None, ...] = (None, "spark", "hive", "postgres", "bigquery")
SPARK_DIALECTS: tuple[str | None, ...] = ("spark", "hive", None)

_HINTS = {
    "sql.select_star": "Selecting every column increases I/O, network, and memory use.",
    "sql.leading_wildcard_like": "LIKE '%value' typically cannot use a B-tree index.",
    "sql.function_on_column": "Wrapping a filtered column in a function often disables index use.",
    "sql.not_in_subquery": "NOT IN (SELECT ...) is slow, and NULLs can make the predicate match nothing.",
    "sql.distinct": "DISTINCT often hides a bad join and adds a full sort or hash.",
    "sql.cross_join": "A CROSS JOIN builds a cartesian product.",
    "sql.in_subquery": "IN (SELECT ...) can be rewritten as a join or semi-join.",
    "sql.correlated_subquery": "This subquery reads columns from an outer query and can run once per outer row.",
    "sql.order_without_limit": "Sorting a full result with no LIMIT is expensive.",
}

_PREDICATES = (exp.EQ, exp.NEQ, exp.GT, exp.LT, exp.GTE, exp.LTE, exp.Like, exp.ILike)


def find_sql_issues(
    sql: str,
    *,
    file_path: str,
    file_text: str,
    start_line: int = 1,
    dialects: tuple[str | None, ...] = SQL_DIALECTS,
) -> tuple[list[Candidate], str]:
    """Parse `sql` and return candidates plus a human-readable error, if any."""
    statements, error = _parse(sql, dialects)
    if error:
        return [], error

    found: list[Candidate] = []
    seen: set[tuple[str, int]] = set()
    counts: dict[str, int] = {}

    for statement in statements:
        for rule_id, title, node in _issues(statement):
            start, end = _source_span(node)
            line = start_line + start - 1
            end_line = start_line + end - 1
            key = (rule_id, line)
            if key in seen or counts.get(rule_id, 0) >= _MAX_PER_RULE:
                continue
            seen.add(key)
            counts[rule_id] = counts.get(rule_id, 0) + 1
            snippet, _, _ = snippet_around(file_text, line, end_line)
            found.append(
                Candidate(
                    file_path=file_path,
                    language=Language.SQL,
                    rule_id=rule_id,
                    title=title,
                    start_line=line,
                    end_line=end_line,
                    snippet=snippet,
                    hint=_HINTS[rule_id],
                )
            )
    return found, ""


def extract_scala_strings(source: str) -> list[tuple[int, str]]:
    """Return (start line, text) for Scala string literals. This is a lexer, not a rule."""
    found: list[tuple[int, str]] = []
    index = 0
    line = 1
    length = len(source)
    while index < length:
        if source[index] == "\n":
            line += 1
            index += 1
            continue
        if source.startswith('"""', index):
            end = source.find('"""', index + 3)
            if end == -1:
                break
            text = source[index + 3 : end]
            found.append((line, text))
            line += text.count("\n")
            index = end + 3
            continue
        if source[index] == '"':
            start_line = line
            text, index, line, closed = _read_scala_string(source, index + 1, line)
            if closed:
                found.append((start_line, text))
            continue
        index += 1
    return found


def _read_scala_string(source: str, index: int, line: int) -> tuple[str, int, int, bool]:
    chars: list[str] = []
    length = len(source)
    while index < length:
        char = source[index]
        if char == "\\" and index + 1 < length:
            chars.append(source[index + 1])
            if source[index + 1] == "\n":
                line += 1
            index += 2
            continue
        if char == '"':
            return "".join(chars), index + 1, line, True
        if char == "\n":
            line += 1
        chars.append(char)
        index += 1
    return "".join(chars), index, line, False


def looks_like_sql(text: str) -> bool:
    stripped = text.strip().lstrip("(").strip()
    if len(stripped) < 20 or not stripped:
        return False
    head = stripped.split(None, 1)[0].lower()
    return head in {"select", "with", "insert", "update", "delete", "merge"}


def snippet_around(content: str, start_line: int, end_line: int, radius: int = 3) -> tuple[str, int, int]:
    lines = content.splitlines()
    lo = max(0, start_line - 1 - radius)
    hi = min(len(lines), max(end_line, start_line) + radius)
    return "\n".join(lines[lo:hi]), lo + 1, hi


def _parse(
    sql: str, dialects: tuple[str | None, ...]
) -> tuple[list[exp.Expression], str]:
    last_error = "SQLGlot could not parse this SQL"
    saw_success = False
    for dialect in dialects:
        try:
            parsed = sqlglot.parse(sql, read=dialect) if dialect else sqlglot.parse(sql)
        except ParseError as exc:
            last_error = _clean_error(exc)
            continue
        saw_success = True
        statements = [item for item in parsed if item is not None]
        if statements:
            return statements, ""
    if saw_success:
        return [], ""
    return [], last_error


def _issues(statement: exp.Expression) -> list[tuple[str, str, exp.Expression]]:
    found: list[tuple[str, str, exp.Expression]] = []
    for star in statement.find_all(exp.Star):
        if _is_projection_star(star):
            found.append(("sql.select_star", "SELECT *", star))

    for like in statement.find_all(exp.Like, exp.ILike):
        pattern = _unwrap(like.expression)
        if (
            isinstance(pattern, exp.Literal)
            and pattern.args.get("is_string")
            and str(pattern.this).startswith("%")
        ):
            found.append(("sql.leading_wildcard_like", "Leading-wildcard LIKE", like))

    for predicate in statement.find_all(*_PREDICATES):
        if not _in_filter(predicate):
            continue
        for side in (predicate.this, predicate.expression):
            side = _unwrap(side)
            if isinstance(side, exp.AggFunc) or not isinstance(side, exp.Func):
                continue
            if any(isinstance(node, exp.Column) for node in side.find_all(exp.Column)):
                found.append(("sql.function_on_column", "Function on filtered column", side))
                break

    for node in statement.find_all(exp.Not):
        inner = node.this
        if isinstance(inner, exp.In) and isinstance(inner.args.get("query"), exp.Subquery):
            found.append(("sql.not_in_subquery", "NOT IN subquery", node))

    for node in statement.find_all(exp.In):
        if isinstance(node.parent, exp.Not):
            continue
        if isinstance(node.args.get("query"), exp.Subquery):
            found.append(("sql.in_subquery", "IN subquery", node))

    for select in statement.find_all(exp.Select):
        if select.args.get("distinct"):
            distinct = select.args.get("distinct")
            marker = distinct if isinstance(distinct, exp.Expression) else select
            found.append(("sql.distinct", "SELECT DISTINCT", marker))
        order = select.args.get("order")
        if order and not select.args.get("limit"):
            found.append(("sql.order_without_limit", "ORDER BY without LIMIT", order))

    for join in statement.find_all(exp.Join):
        if str(join.args.get("kind") or "").upper() == "CROSS":
            found.append(("sql.cross_join", "CROSS JOIN", join))

    try:
        scopes = list(traverse_scope(statement))
    except Exception:
        scopes = []
    for scope in scopes:
        if not getattr(scope, "is_correlated_subquery", False):
            continue
        qualified = [column for column in scope.external_columns if column.table]
        if not qualified:
            continue
        found.append(
            ("sql.correlated_subquery", "Correlated subquery", scope.expression)
        )
    return found


def _is_projection_star(star: exp.Star) -> bool:
    parent = star.parent
    if isinstance(parent, exp.Select):
        return True
    return isinstance(parent, exp.Column) and isinstance(parent.parent, exp.Select)


def _unwrap(node: exp.Expression | None) -> exp.Expression | None:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _in_filter(node: exp.Expression) -> bool:
    parent = node.parent
    while parent is not None:
        if isinstance(parent, (exp.Where, exp.Having, exp.Join)):
            return True
        if isinstance(parent, (exp.Select, exp.Union, exp.Subquery)):
            return False
        parent = parent.parent
    return False


def _source_span(node: exp.Expression) -> tuple[int, int]:
    """Use SQLGlot's token positions. Many parent nodes have no line of their own."""
    lines: list[int] = []
    for child in node.walk():
        line = (child.meta or {}).get("line")
        if line:
            lines.append(int(line))
    if not lines:
        parent = node.parent
        while parent is not None:
            line = (parent.meta or {}).get("line")
            if line:
                lines.append(int(line))
                break
            parent = parent.parent
    if not lines:
        return 1, 1
    return min(lines), max(lines)


def _clean_error(exc: BaseException) -> str:
    text = _ANSI.sub("", str(exc)).strip().splitlines()
    return text[0][:240] if text else "SQL parse error"
