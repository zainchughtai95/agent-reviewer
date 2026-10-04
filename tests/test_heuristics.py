from pathlib import Path

from agent_reviewer.heuristics import classify_path, scan_text
from agent_reviewer.models import Language
from agent_reviewer.pr_creator import apply_snippet
from agent_reviewer.models import Finding, Severity


EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "inefficient"


def test_classifies_pyspark_as_spark() -> None:
    content = (EXAMPLES / "jobs.py").read_text()
    assert classify_path("jobs.py", content) is Language.SPARK


def test_sql_heuristics_catch_select_star_and_cross_join() -> None:
    content = (EXAMPLES / "orders.sql").read_text()
    found = {item.rule_id: item.start_line for item in scan_text("orders.sql", content)}
    assert found["sql.select_star"] == 3
    assert found["sql.cross_join"] == 5
    assert found["sql.leading_wildcard_like"] == 8


def test_python_heuristics_catch_iterrows() -> None:
    content = (EXAMPLES / "rollup.py").read_text()
    rules = {item.rule_id for item in scan_text("rollup.py", content)}
    assert "py.pandas_iterrows" in rules
    assert "py.pandas_apply" in rules


def test_spark_heuristics_catch_collect_and_udf() -> None:
    content = (EXAMPLES / "jobs.py").read_text()
    rules = {item.rule_id for item in scan_text("jobs.py", content)}
    assert "spark.collect" in rules
    assert "spark.to_pandas" in rules
    assert "spark.python_udf" in rules
    assert "spark.coalesce_one" in rules


def test_sql_ignores_comments_and_functions_on_literals() -> None:
    content = """
-- SELECT * FROM hidden
SELECT id
FROM events
WHERE status = LOWER('OPEN')
ORDER BY id
LIMIT 10;
"""
    rules = {item.rule_id for item in scan_text("events.sql", content)}
    assert "sql.select_star" not in rules
    assert "sql.function_on_column" not in rules
    assert "sql.order_without_limit" not in rules


def test_sql_flags_function_on_column_and_order_without_limit() -> None:
    content = (EXAMPLES / "orders.sql").read_text()
    rules = {item.rule_id for item in scan_text("orders.sql", content)}
    assert "sql.function_on_column" in rules
    assert "sql.not_in_subquery" in rules
    assert "sql.order_without_limit" in rules


def test_apply_on_unrelated_object_is_ignored() -> None:
    content = """
class Box:
    def apply(self, fn):
        return fn(1)

def run(box: Box, paths):
    box.apply(lambda value: value)
    for path in paths:
        for line in path:
            pass
"""
    rules = {item.rule_id for item in scan_text("safe.py", content)}
    assert "py.pandas_apply" not in rules
    assert "py.nested_same_iterable" not in rules


def test_embedded_sql_string_is_parsed() -> None:
    content = '''
def load():
    return """
    SELECT *
    FROM warehouse.events
    """
'''
    rules = {item.rule_id for item in scan_text("query.py", content)}
    assert "sql.select_star" in rules


def test_apply_snippet_replaces_first_match() -> None:
    original = "df.collect()\nprint(1)\n"
    finding = Finding(
        repo="acme/demo",
        ref="main",
        file_path="jobs.py",
        language=Language.SPARK,
        rule_id="spark.collect",
        title="collect",
        start_line=1,
        end_line=1,
        snippet="df.collect()",
        reasoning="driver oom",
        impact="driver",
        proposed_fix="use limit",
        proposed_snippet="df.limit(10).collect()",
        severity=Severity.HIGH,
    )
    assert "limit(10)" in apply_snippet(original, finding)
