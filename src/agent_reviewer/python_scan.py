"""Find inefficient Python and PySpark by walking the standard-library AST.

Nothing in the file is executed. Names are tracked only from imports and
assignments in the same scope, which keeps pandas `.apply` from matching every
object that happens to have a method of that name.
"""

from __future__ import annotations

import ast

from agent_reviewer.models import Candidate, Language
from agent_reviewer.sql_scan import snippet_around

_MAX_PER_RULE = 8

_HINTS = {
    "py.pandas_iterrows": "Row-wise pandas iteration is much slower than a vectorized operation.",
    "py.pandas_itertuples": "itertuples() still walks rows in Python. Prefer a vectorized operation.",
    "py.pandas_apply": "apply() on a pandas object is often a Python loop in disguise.",
    "py.string_concat_loop": "Repeated += on a string copies the whole string on every iteration.",
    "py.nested_same_iterable": "Looping over the same collection twice is quadratic.",
    "py.read_all": "Reading a whole file into memory can be replaced with streaming or chunks.",
    "py.time_sleep": "time.sleep stalls the worker. Prefer an event, a queue, or bounded backoff.",
    "spark.collect": "collect() pulls the dataset to the driver and can run it out of memory.",
    "spark.to_pandas": "toPandas() materializes the DataFrame on the driver.",
    "spark.count_in_loop": "Each DataFrame.count() is a Spark job. Repeated counts are expensive.",
    "spark.python_udf": "A Python UDF blocks Catalyst optimization and adds serialization.",
    "spark.rdd_action": "RDD map/flatMap/foreach skips Catalyst and Tungsten.",
    "spark.coalesce_one": "coalesce(1) forces the write through a single partition.",
    "spark.cartesian": "crossJoin or cartesian explodes the number of rows and the shuffle.",
    "spark.repartition_no_key": "repartition(n) with no key still shuffles the whole dataset.",
    "spark.large_take": "take() of thousands of rows concentrates data on the driver.",
}

_TITLES = {
    "py.pandas_iterrows": "pandas iterrows()",
    "py.pandas_itertuples": "pandas itertuples()",
    "py.pandas_apply": "pandas apply()",
    "py.string_concat_loop": "String concatenation in a loop",
    "py.nested_same_iterable": "Nested loop over the same collection",
    "py.read_all": "Read entire file into memory",
    "py.time_sleep": "time.sleep",
    "spark.collect": "DataFrame.collect()",
    "spark.to_pandas": "toPandas()",
    "spark.count_in_loop": "count() inside a loop",
    "spark.python_udf": "Python UDF",
    "spark.rdd_action": "RDD map instead of DataFrame API",
    "spark.coalesce_one": "coalesce(1)",
    "spark.cartesian": "Cartesian Spark join",
    "spark.repartition_no_key": "repartition by count only",
    "spark.large_take": "Large take()",
}

_PANDAS_SOURCES = {
    "read_csv",
    "read_parquet",
    "read_sql",
    "read_sql_query",
    "read_excel",
    "read_json",
    "read_feather",
    "DataFrame",
    "Series",
    "concat",
    "merge",
    "pivot_table",
    "get_dummies",
    "crosstab",
}
_UDF_NAMES = {"udf", "pandas_udf"}
_RDD_ACTIONS = {"map", "flatMap", "foreach", "mapPartitions"}


class PythonScanner(ast.NodeVisitor):
    def __init__(self, file_path: str, source: str, language: Language):
        self.file_path = file_path
        self.source = source
        self.language = language
        self.candidates: list[Candidate] = []
        self.modules: dict[str, str] = {}
        self.resolved: dict[str, str] = {}
        self.pandas_names: set[str] = set()
        self.spark_names: set[str] = set()
        self.string_names: set[str] = set()
        self.file_handles: set[str] = set()
        self.loop_depth = 0
        self._seen: set[tuple[str, int]] = set()
        self._counts: dict[str, int] = {}

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name
            self.modules[local] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            local = alias.asname or alias.name
            qualified = f"{module}.{alias.name}" if module else alias.name
            self.resolved[local] = qualified
            self.modules[local] = qualified
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            if self._is_udf(decorator):
                self._emit(decorator, "spark.python_udf")
        saved = self._snapshot()
        for arg in [*node.args.args, *node.args.kwonlyargs, *node.args.posonlyargs]:
            if arg.arg == "spark" or _annotation_mentions(arg.annotation, "SparkSession"):
                self.spark_names.add(arg.arg)
        for statement in node.body:
            self.visit(statement)
        self._restore(saved)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._rebind(target.id, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        if isinstance(node.target, ast.Name) and node.value is not None:
            self._rebind(node.target.id, node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        if (
            self.loop_depth
            and isinstance(node.op, ast.Add)
            and isinstance(node.target, ast.Name)
            and node.target.id in self.string_names
        ):
            self._emit(node, "py.string_concat_loop")

    def visit_For(self, node: ast.For) -> None:
        self._flag_same_iterable(node)
        self.loop_depth += 1
        self.generic_visit(node)
        self.loop_depth -= 1

    visit_AsyncFor = visit_For

    def visit_While(self, node: ast.While) -> None:
        self.loop_depth += 1
        self.generic_visit(node)
        self.loop_depth -= 1

    def visit_With(self, node: ast.With) -> None:
        added: list[str] = []
        for item in node.items:
            if isinstance(item.optional_vars, ast.Name) and _is_open_call(item.context_expr):
                self.file_handles.add(item.optional_vars.id)
                added.append(item.optional_vars.id)
        self.generic_visit(node)
        for name in added:
            self.file_handles.discard(name)

    def visit_Call(self, node: ast.Call) -> None:
        self._inspect_call(node)
        self.generic_visit(node)

    def _inspect_call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name):
            qualified = self.resolved.get(func.id, "")
            if qualified.endswith(".udf") or qualified.endswith(".pandas_udf") or (
                self.language is Language.SPARK and func.id in _UDF_NAMES
            ):
                self._emit(node, "spark.python_udf")
            if qualified == "time.sleep":
                self._emit(node, "py.time_sleep")
            return

        if not isinstance(func, ast.Attribute):
            return

        attr = func.attr
        receiver = func.value
        if attr == "iterrows":
            self._emit(node, "py.pandas_iterrows")
        elif attr == "itertuples":
            self._emit(node, "py.pandas_itertuples")
        elif attr == "apply" and self._pandas_rooted(receiver):
            self._emit(node, "py.pandas_apply")
        elif attr in {"read_text", "read_bytes"}:
            self._emit(node, "py.read_all")
        elif attr == "read" and not node.args and not node.keywords and self._is_file_handle(receiver):
            self._emit(node, "py.read_all")
        elif attr == "sleep" and self._is_time(receiver):
            self._emit(node, "py.time_sleep")
        elif attr == "collect" and not node.args and self._spark_collect(receiver):
            self._emit(node, "spark.collect")
        elif attr == "toPandas":
            self._emit(node, "spark.to_pandas")
        elif (
            attr == "count"
            and not node.args
            and not node.keywords
            and self.loop_depth
            and self._spark_rooted(receiver)
        ):
            self._emit(node, "spark.count_in_loop")
        elif attr == "coalesce" and _const_int(node) == 1 and self._spark_rooted(receiver):
            self._emit(node, "spark.coalesce_one")
        elif attr in {"crossJoin", "cartesian"}:
            self._emit(node, "spark.cartesian")
        elif attr == "repartition" and _single_number(node) and self._spark_rooted(receiver):
            self._emit(node, "spark.repartition_no_key")
        elif attr == "take" and (_const_int(node) or 0) >= 1000 and self._spark_rooted(receiver):
            self._emit(node, "spark.large_take")
        elif attr in _RDD_ACTIONS and isinstance(receiver, ast.Attribute) and receiver.attr == "rdd":
            self._emit(node, "spark.rdd_action")

    def _flag_same_iterable(self, node: ast.For) -> None:
        outer = _iter_key(node.iter)
        if not outer:
            return
        for statement in node.body:
            if isinstance(statement, ast.For):
                inner = _iter_key(statement.iter)
                if inner and inner == outer:
                    self._emit(statement, "py.nested_same_iterable")

    def _emit(self, node: ast.AST, rule_id: str) -> None:
        line = getattr(node, "lineno", None) or 1
        end = getattr(node, "end_lineno", None) or line
        key = (rule_id, line)
        if key in self._seen or self._counts.get(rule_id, 0) >= _MAX_PER_RULE:
            return
        self._seen.add(key)
        self._counts[rule_id] = self._counts.get(rule_id, 0) + 1
        language = Language.SPARK if rule_id.startswith("spark.") else Language.PYTHON
        snippet, _, _ = snippet_around(self.source, line, end)
        self.candidates.append(
            Candidate(
                file_path=self.file_path,
                language=language,
                rule_id=rule_id,
                title=_TITLES[rule_id],
                start_line=line,
                end_line=end,
                snippet=snippet,
                hint=_HINTS[rule_id],
            )
        )

    def _rebind(self, name: str, value: ast.AST) -> None:
        self.pandas_names.discard(name)
        self.spark_names.discard(name)
        self.string_names.discard(name)
        self.file_handles.discard(name)
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            self.string_names.add(name)
        elif self._pandas_rooted(value):
            self.pandas_names.add(name)
        elif self._spark_rooted(value):
            self.spark_names.add(name)
        elif _is_open_call(value):
            self.file_handles.add(name)

    def _pandas_rooted(self, node: ast.AST) -> bool:
        current: ast.AST | None = node
        for _ in range(30):
            if current is None:
                return False
            if isinstance(current, ast.Name):
                return current.id in self.pandas_names or self._pandas_module(current.id)
            if isinstance(current, ast.Attribute):
                if current.attr in _PANDAS_SOURCES and isinstance(current.value, ast.Name):
                    if self._pandas_module(current.value.id):
                        return True
                current = current.value
                continue
            if isinstance(current, ast.Call):
                func = current.func
                if isinstance(func, ast.Name):
                    qualified = self.resolved.get(func.id, "")
                    source = qualified.rsplit(".", 1)[-1]
                    if qualified.startswith("pandas.") and source in _PANDAS_SOURCES:
                        return True
                current = func
                continue
            if isinstance(current, ast.Subscript):
                current = current.value
                continue
            return False
        return False

    def _spark_rooted(self, node: ast.AST) -> bool:
        current: ast.AST | None = node
        for _ in range(30):
            if current is None:
                return False
            if isinstance(current, ast.Name):
                if current.id in self.spark_names or current.id == "SparkSession":
                    return True
                return "pyspark" in self.modules.get(current.id, "") or "pyspark" in self.resolved.get(current.id, "")
            if isinstance(current, ast.Attribute):
                current = current.value
                continue
            if isinstance(current, ast.Call):
                current = current.func
                continue
            if isinstance(current, ast.Subscript):
                current = current.value
                continue
            return False
        return False

    def _spark_collect(self, receiver: ast.AST) -> bool:
        if isinstance(receiver, ast.Name) and receiver.id == "gc":
            return False
        return self._spark_rooted(receiver) or self.language is Language.SPARK

    def _pandas_module(self, name: str) -> bool:
        module = self.modules.get(name, "")
        return module == "pandas" or module.startswith("pandas.")

    def _is_time(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and self.modules.get(node.id) == "time"

    def _is_file_handle(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id in self.file_handles

    def _is_udf(self, node: ast.AST) -> bool:
        target = node.func if isinstance(node, ast.Call) else node
        if isinstance(target, ast.Name):
            qualified = self.resolved.get(target.id, "")
            return qualified.endswith(".udf") or qualified.endswith(".pandas_udf") or (
                self.language is Language.SPARK and target.id in _UDF_NAMES
            )
        if isinstance(target, ast.Attribute) and target.attr in _UDF_NAMES:
            if isinstance(target.value, ast.Name):
                module = self.modules.get(target.value.id, "")
                if "pyspark" in module and "functions" in module:
                    return True
            return self.language is Language.SPARK
        return False

    def _snapshot(self) -> tuple[set[str], set[str], set[str], set[str]]:
        return (
            set(self.spark_names),
            set(self.pandas_names),
            set(self.string_names),
            set(self.file_handles),
        )

    def _restore(self, saved: tuple[set[str], set[str], set[str], set[str]]) -> None:
        (
            self.spark_names,
            self.pandas_names,
            self.string_names,
            self.file_handles,
        ) = saved


def find_python_issues(
    file_path: str, source: str, language: Language
) -> tuple[list[Candidate], str]:
    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as exc:
        location = f"line {exc.lineno}" if exc.lineno else "the file"
        return [], f"Python syntax error at {location}: {exc.msg}"
    scanner = PythonScanner(file_path, source, language)
    scanner.visit(tree)
    return scanner.candidates, ""


def embedded_sql_strings(source: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((node.lineno, node.value))
    return found


def _iter_key(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _iter_key(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _const_int(node: ast.Call) -> int | None:
    if len(node.args) != 1 or node.keywords:
        return None
    arg = node.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, int) and not isinstance(arg.value, bool):
        return arg.value
    return None


def _single_number(node: ast.Call) -> bool:
    value = _const_int(node)
    return value is not None


def _is_open_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "open"
    return isinstance(func, ast.Attribute) and func.attr == "open"


def _annotation_mentions(node: ast.AST | None, name: str) -> bool:
    if node is None:
        return False
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id == name:
            return True
    return False
