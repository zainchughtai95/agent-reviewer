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
    rules = {item.rule_id for item in scan_text("orders.sql", content)}
    assert "sql.select_star" in rules
    assert "sql.cross_join" in rules
    assert "sql.leading_wildcard_like" in rules


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
