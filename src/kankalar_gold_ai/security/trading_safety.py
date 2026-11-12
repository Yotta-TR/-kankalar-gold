"""AST-based checks for broker execution APIs and application route registration."""

import ast
from dataclasses import dataclass
from pathlib import Path

READ_ONLY_MT5_APIS = frozenset(
    {
        "initialize",
        "shutdown",
        "version",
        "terminal_info",
        "symbols_get",
        "symbol_info",
        "symbol_info_tick",
        "copy_rates_range",
        "copy_ticks_range",
        "last_error",
        "TIMEFRAME_M1",
        "TIMEFRAME_M5",
        "TIMEFRAME_M15",
        "TIMEFRAME_H1",
        "TIMEFRAME_H4",
        "TIMEFRAME_D1",
        "COPY_TICKS_ALL",
    }
)

_DYNAMIC_ACCESSORS = frozenset(
    {
        "attrgetter",
        "eval",
        "exec",
        "getattr",
        "getattr_static",
        "import_module",
        "methodcaller",
        "setattr",
        "delattr",
        "find_spec",
        "module_from_spec",
        "exec_module",
        "globals",
        "locals",
        "vars",
        "__getattribute__",
        "__import__",
    }
)
_PROHIBITED_EXECUTION_NAMES = frozenset(
    {"order_send", "place_order", "send_order", "execute_order", "submit_order"}
)
_ROUTE_REGISTRARS = frozenset(
    {
        "add_api_route",
        "add_url_rule",
        "include_router",
        "register_blueprint",
        "route",
    }
)
_HTTP_ROUTE_DECORATORS = frozenset({"delete", "get", "patch", "post", "put", "route"})
_TELEGRAM_HANDLER_REGISTRARS = frozenset(
    {
        "add_handler",
        "callback_query_handler",
        "command",
        "message_handler",
        "on_message",
        "register_command",
        "register_handler",
    }
)
_WEB_FRAMEWORK_TYPES = frozenset(
    {"APIRouter", "APIRoute", "Blueprint", "FastAPI", "Flask", "Route"}
)
_WEB_FRAMEWORK_MODULES = ("fastapi", "flask", "starlette")
_MT5_MODULE = "MetaTrader5"
_TELEGRAM_MODULE = "telegram"
_MT5_ADAPTER_PARTS = ("adapters", "mt5")


@dataclass(frozen=True, slots=True)
class SafetyViolation:
    """A source-level violation with a stable location and explanation."""

    path: str
    line: int
    rule: str
    message: str


class _SafetyVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.violations: list[SafetyViolation] = []
        self.module_aliases: set[str] = set()
        self.sys_aliases: set[str] = set()
        self.dynamic_aliases: set[str] = set()
        self.in_mt5_adapter = Path(path).parts[-3:-1] == _MT5_ADAPTER_PARTS

    def _violate(self, node: ast.AST, rule: str, message: str) -> None:
        self.violations.append(
            SafetyViolation(
                self.path,
                node.lineno if hasattr(node, "lineno") else 1,
                rule,
                message,
            )
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.split(".")[0] == "sys":
                self.sys_aliases.add(alias.asname or "sys")
            if alias.name.split(".")[0] == _TELEGRAM_MODULE:
                self._violate(
                    node,
                    "telegram-handler-boundary",
                    "Telegram client imports are not permitted in Phase 0.1.",
                )
            if alias.name.split(".")[0] in _WEB_FRAMEWORK_MODULES:
                self._violate(
                    node,
                    "web-route-boundary",
                    "Web frameworks are not permitted in Phase 0.1.",
                )
            if alias.name == _MT5_MODULE:
                if not self.in_mt5_adapter:
                    self._violate(
                        node,
                        "mt5-boundary",
                        "MT5 imports are permitted only in adapters/mt5.",
                    )
                self.module_aliases.add(alias.asname or alias.name.split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module == _TELEGRAM_MODULE or module.startswith(f"{_TELEGRAM_MODULE}."):
            self._violate(
                node,
                "telegram-handler-boundary",
                "Telegram client imports are not permitted in Phase 0.1.",
            )
        for alias in node.names:
            if alias.name.casefold() in _PROHIBITED_EXECUTION_NAMES:
                self._violate(
                    node,
                    "execution-api",
                    f"Prohibited imported execution function {alias.name!r}.",
                )
        if module == _MT5_MODULE:
            if not self.in_mt5_adapter:
                self._violate(
                    node,
                    "mt5-boundary",
                    "MT5 imports are permitted only in adapters/mt5.",
                )
            for alias in node.names:
                if alias.name == "*":
                    self._violate(
                        node,
                        "mt5-allowlist",
                        "Wildcard MT5 imports are prohibited.",
                    )
                elif alias.name not in READ_ONLY_MT5_APIS:
                    self._violate(
                        node,
                        "mt5-allowlist",
                        f"MT5 API {alias.name!r} is not in the read-only allowlist.",
                    )

        if module.startswith(_WEB_FRAMEWORK_MODULES) and any(
            alias.name in _WEB_FRAMEWORK_TYPES for alias in node.names
        ):
            self._violate(
                node,
                "web-route-boundary",
                "Web application/router types are not permitted in Phase 0.1.",
            )

        if module in {"builtins", "operator", "inspect"}:
            for alias in node.names:
                if alias.name in _DYNAMIC_ACCESSORS:
                    self.dynamic_aliases.add(alias.asname or alias.name)

        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Name) and node.value.id in self.module_aliases:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.module_aliases.add(target.id)
        if isinstance(node.value, ast.Name) and (
            node.value.id in _DYNAMIC_ACCESSORS or node.value.id in self.dynamic_aliases
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.dynamic_aliases.add(target.id)
        if isinstance(node.value, ast.Attribute) and node.value.attr in _DYNAMIC_ACCESSORS:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.dynamic_aliases.add(target.id)
        if isinstance(node.value, ast.Attribute) and node.value.attr in _HTTP_ROUTE_DECORATORS:
            self._violate(
                node,
                "web-route-boundary",
                "Aliasing an HTTP route method is prohibited in Phase 0.1.",
            )
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.value, ast.Name) and node.value.id in self.module_aliases:
            if isinstance(node.target, ast.Name):
                self.module_aliases.add(node.target.id)
        if isinstance(node.value, ast.Name) and (
            node.value.id in _DYNAMIC_ACCESSORS or node.value.id in self.dynamic_aliases
        ):
            if isinstance(node.target, ast.Name):
                self.dynamic_aliases.add(node.target.id)
        if isinstance(node.value, ast.Attribute) and node.value.attr in _HTTP_ROUTE_DECORATORS:
            self._violate(
                node,
                "web-route-boundary",
                "Aliasing an HTTP route method is prohibited in Phase 0.1.",
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in _PROHIBITED_EXECUTION_NAMES:
            self._violate(
                node,
                "execution-api",
                f"Prohibited execution API name {node.attr!r}.",
            )
        if isinstance(node.value, ast.Name) and node.value.id in self.module_aliases:
            if node.attr not in READ_ONLY_MT5_APIS:
                self._violate(
                    node,
                    "mt5-allowlist",
                    f"MT5 attribute {node.attr!r} is not in the read-only allowlist.",
                )
        elif (
            isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id in self.module_aliases
        ):
            self._violate(
                node,
                "mt5-dynamic-access",
                "Chained attribute access from the MT5 module is prohibited.",
            )
        if isinstance(node.value, ast.Name) and node.value.id in self.sys_aliases:
            if node.attr == "modules":
                self._violate(
                    node,
                    "dynamic-import",
                    "Access to sys.modules is prohibited in the application package.",
                )
        if node.attr in _DYNAMIC_ACCESSORS:
            self._violate(
                node,
                "dynamic-access",
                f"Dynamic access helper {node.attr!r} is prohibited.",
            )

        if node.attr in _ROUTE_REGISTRARS:
            self._violate(
                node,
                "web-route-boundary",
                f"Route registration via {node.attr!r} is prohibited in Phase 0.1.",
            )
        if node.attr in {"delete", "patch", "post", "put"}:
            self._violate(
                node,
                "web-route-boundary",
                f"HTTP route method {node.attr!r} is prohibited in Phase 0.1.",
            )
        if "telegram" in Path(self.path).parts and node.attr in _TELEGRAM_HANDLER_REGISTRARS:
            self._violate(
                node,
                "telegram-handler-boundary",
                "Telegram command/handler registration is prohibited in Phase 0.1.",
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        called_name = _call_name(node.func)
        if called_name in _DYNAMIC_ACCESSORS or called_name in self.dynamic_aliases:
            self._violate(
                node,
                "dynamic-access",
                f"Dynamic access/import helper {called_name!r} is prohibited.",
            )
        if called_name and called_name.casefold() in _PROHIBITED_EXECUTION_NAMES:
            self._violate(
                node,
                "execution-api",
                f"Prohibited execution function {called_name!r}.",
            )
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.startswith("/")
        ):
            self._violate(
                node,
                "web-route-boundary",
                "HTTP GET route registration is prohibited in Phase 0.1.",
            )
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            key = node.slice.value.casefold()
            if key in _PROHIBITED_EXECUTION_NAMES or key in _DYNAMIC_ACCESSORS:
                self._violate(
                    node,
                    "dynamic-access",
                    f"Dynamic lookup of {node.slice.value!r} is prohibited.",
                )
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_route_decorators(node.decorator_list)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_route_decorators(node.decorator_list)
        self.generic_visit(node)

    def _check_route_decorators(self, decorators: list[ast.expr]) -> None:
        for decorator in decorators:
            if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
                if decorator.func.attr in _HTTP_ROUTE_DECORATORS:
                    self._violate(
                        decorator,
                        "web-route-boundary",
                        "HTTP route decorators are prohibited in Phase 0.1.",
                    )
                if (
                    "telegram" in Path(self.path).parts
                    and decorator.func.attr in _TELEGRAM_HANDLER_REGISTRARS
                ):
                    self._violate(
                        decorator,
                        "telegram-handler-boundary",
                        "Telegram command/handler registration is prohibited in Phase 0.1.",
                    )


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def analyze_source(source: str, path: str = "module.py") -> tuple[SafetyViolation, ...]:
    """Analyze source without importing or executing it."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as error:
        return (SafetyViolation(path, error.lineno or 1, "syntax", "Source could not be parsed."),)
    visitor = _SafetyVisitor(path)
    visitor.visit(tree)
    return tuple(visitor.violations)


def scan_package(package_root: str | Path) -> tuple[SafetyViolation, ...]:
    """Scan every Python source file under the application package."""
    root = Path(package_root)
    violations: list[SafetyViolation] = []
    for source_path in sorted(root.rglob("*.py")):
        relative_path = source_path.relative_to(root).as_posix()
        violations.extend(analyze_source(source_path.read_text(encoding="utf-8"), relative_path))
    return tuple(violations)
