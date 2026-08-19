"""Guard: no except-handler in production code may swallow silently.

Every handler in core/, scripts/, app_logging/, models/, config/ must either
re-raise, or call a logging method (debug/info/warning/error/exception/critical
— attribute or bare name), or carry a `# swallow:intentional` comment inside
the handler body (which is a whitelist for exotic AST shapes only — those sites
must STILL log).

Added 2026-08-18 after Smit's directive: zero silent swallows, everything
grep-able via the [SWALLOWED] sentinel.
"""
import ast
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
ROOTS = ["core", "scripts", "app_logging", "models", "config"]
LOG_FUNCS = {"debug", "info", "warning", "error", "exception", "critical"}
INTENTIONAL = "swallow:intentional"


def _call_name(node):
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _handler_has_raise(handler):
    for stmt in handler.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Raise):
                return True
    return False


def _handler_logs(handler):
    for stmt in handler.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and _call_name(node) in LOG_FUNCS:
                return True
    return False


def _handler_intentional(handler, lines):
    start = handler.lineno - 1
    end = handler.end_lineno
    for i in range(start, min(end, len(lines))):
        if INTENTIONAL in lines[i]:
            return True
    return False


def _violations():
    bad = []
    for root in ROOTS:
        for dirpath, _, files in os.walk(REPO / root):
            if "__pycache__" in dirpath:
                continue
            for fn in sorted(files):
                if not fn.endswith(".py"):
                    continue
                path = Path(dirpath) / fn
                src = path.read_text()
                lines = src.splitlines()
                tree = ast.parse(src)
                for node in ast.walk(tree):
                    if not isinstance(node, ast.ExceptHandler):
                        continue
                    if _handler_has_raise(node):
                        continue
                    logs = _handler_logs(node)
                    intentional = _handler_intentional(node, lines)
                    if logs:
                        continue  # intentional comment or not, logging is enough
                    if intentional:
                        bad.append(f"{path.relative_to(REPO)}:{node.lineno} marked intentional but does not log")
                        continue
                    bad.append(f"{path.relative_to(REPO)}:{node.lineno} silent except-handler (no log, no raise)")
    return bad


def test_no_silent_exception_swallows():
    bad = _violations()
    assert not bad, "Silent except-handlers found (add a [SWALLOWED] log line or re-raise):\n" + "\n".join(bad)
