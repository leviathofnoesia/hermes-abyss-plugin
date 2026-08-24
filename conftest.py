"""Pytest collection shim for Abyss's script-style test files.

The Abyss backend tests (`test_plugin.py`, `test_wave.py`, `dashboard/test_api.py`)
are written as self-executing scripts: they run their assertions at module import
time and call ``sys.exit(1 if FAIL else 0)`` at the bottom. They contain ZERO
``def test_*`` functions, so plain ``pytest`` reports "no tests collected" and --
for files that ``sys.exit(0)`` on success -- crashes during collection with
``INTERNALERROR: SystemExit: 0``.

Running them directly (``python test_plugin.py``) works and is the original
contract. This conftest makes ``python -m pytest <files> -v`` work too, by
collecting each script-style file as a single pytest item that runs the file in
a subprocess and maps its exit code to pass/fail. This is the idiomatic pytest
pattern for script-style tests and requires NO modification to the test files
themselves (which would risk the working 213-pass baseline).

A file is treated as a script-style test if it lives under this plugin tree,
matches ``test_*.py``, and defines no ``test_*`` functions.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def _is_script_style(path: Path) -> bool:
    """True for the plugin's self-executing script-style test files.

    These files run their assertions from a `_run_script()` body (invoked by an
    `if __name__ == "__main__"` guard or a thin test_runner() wrapper) and set
    os.environ["HERMES_PROFILE_HOME"] at module level. Importing them in-process
    during collection pollutes the shared interpreter: each file's temp-dir env
    overwrite races, and the first import fixes __init__.py's module-level DB
    paths -- so later files silently read/write an earlier file's SQLite DB
    (observed: dashboard/test_api.py saw test_plugin.py's pre-seeded activity
    rows and failed its exact-count GET /activity assertion). Always run them
    in a subprocess, even when they define a pytest-compat test_runner().
    """
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return False
    funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    return "_run_script" in funcs or not any(n.startswith("test_") for n in funcs)


class ScriptTestItem(pytest.Item):
    """A single pytest item that runs a script-style test file in a subprocess."""

    def __init__(self, name: str, parent: pytest.Collector, script: Path):
        super().__init__(name, parent)
        self.script = script

    def runtest(self) -> None:
        # Run in isolation so module-level sys.exit() maps to a clean exit code
        # instead of crashing the pytest interpreter via SystemExit propagation.
        proc = subprocess.run(
            [sys.executable, str(self.script)],
            cwd=str(self.script.parent),
            capture_output=True,
            text=True,
            timeout=300,
        )
        # Attach output for reporting on failure.
        self._stdout = proc.stdout
        self._stderr = proc.stderr
        self._rc = proc.returncode
        if proc.returncode != 0:
            raise AssertionError(
                f"{self.script.name} exited {proc.returncode}\n"
                f"--- stdout ---\n{proc.stdout}\n"
                f"--- stderr ---\n{proc.stderr}"
            )

    def repr_failure(self, excinfo, style=None):
        # Surface the script's own [FAIL] lines + tail; the script already prints
        # human-readable failure context that is more useful than a traceback.
        if self._rc != 0:
            tail = "\n".join((self._stdout or "").splitlines()[-30:])
            err = (self._stderr or "").strip()
            return f"{self.script.name} failed (exit {self._rc}):\n{tail}" + (
                f"\n[stderr]\n{err}" if err else ""
            )
        return super().repr_failure(excinfo, style)

    def reportinfo(self):
        return (self.fspath, 0, f"script: {self.script.name}")


class ScriptTestFile(pytest.File):
    """Collects a script-style file as one ScriptTestItem."""

    def collect(self):
        script = Path(self.fspath)
        item = ScriptTestItem.from_parent(
            self, name=script.stem, script=script
        )
        yield item


def pytest_collect_file(parent, file_path):
    """Hook: collect script-style test files (no `def test_*` funcs)."""
    path = Path(file_path)
    # Only act on files whose name matches pytest's test-file convention.
    if not path.name.startswith("test_") or not path.name.endswith(".py"):
        return None
    # Only handle directories relevant to this plugin so we don't hijack
    # unrelated test files elsewhere in the tree that DO use def test_*.
    root = Path(__file__).resolve().parent
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return None
    # Script-style files (self-executing: define `_run_script` or contain no
    # real test_* functions) are collected as a ScriptTestItem for subprocess
    # isolation. Genuine pytest-style files (real test_* functions, no
    # _run_script) are left to normal pytest collection.
    if _is_script_style(path):
        return ScriptTestFile.from_parent(parent, path=path)
    return None


def pytest_collection_modifyitems(config, items):
    """Drop duplicate normal-pytest items for script-style files.

    Script-style files ALSO define a pytest-compat ``test_runner()`` (so direct
    ``python -m pytest <file>`` works without this shim), which makes pytest's
    default collector import and collect them IN-PROCESS in addition to our
    ScriptTestItem. Running the same self-executing script twice (once in a
    clean subprocess, once inside the shared interpreter) doubles runtime and
    re-introduces the module-cache/env pollution this shim exists to prevent.
    Keep only the isolated ScriptTestItem per claimed file.
    """
    script_paths = set()
    for item in items:
        if isinstance(item, ScriptTestItem):
            try:
                script_paths.add(str(Path(str(item.fspath)).resolve()))
            except Exception:
                pass
    if not script_paths:
        return
    kept = []
    for item in items:
        if isinstance(item, ScriptTestItem):
            kept.append(item)
            continue
        try:
            p = str(Path(str(item.fspath)).resolve())
        except Exception:
            p = None
        if p not in script_paths:
            kept.append(item)
    items[:] = kept
