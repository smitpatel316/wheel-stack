# Python Stdlib Shadowing — `logging/` Dir

## Bug Found 2026-08-02 in ~/options-wheel

Repo had `logging/` directory at root with:
- `logging/__init__.py` (empty)
- `logging/logger_setup.py`
- `logging/strategy_logger.py`

When `PYTHONPATH=.` or repo dir in `sys.path[0]` (common via `python -m` or `uv run`), Python finds `repo/logging/__init__.py` before stdlib `logging`.

## Symptoms

1. `AttributeError: module 'logging' has no attribute 'getLogger'` from `dotenv` import chain:
```
File "config/credentials.py", line 1, from dotenv import load_dotenv
  -> dotenv/main.py imports logging -> gets repo/logging/__init__.py empty -> no getLogger
```

2. In some Python versions `load_dotenv()` with `find_dotenv()` asserts `frame.f_back is not None` fails because logging shim broke stack inspection.

3. `run-strategy` entry point via `setuptools` worked because installed package's import order favored stdlib first? But direct `python3 -c "from config..."` failed.

## Diagnosis

```bash
# In /tmp — stdlib OK
python3 -c "import logging; print(logging.__file__)"
# /usr/.../lib/python3.12/logging/__init__.py

# In repo dir — shadowed
cd ~/options-wheel
python3 -c "import logging; print(logging.__file__)"
# /home/.../options-wheel/logging/__init__.py  ← BUG

python3 -c "import importlib.util; print(importlib.util.find_spec('logging'))"
# origin shows shadow path

# Via venv entrypoint (may still work due to site-packages path order)
source .venv/bin/activate
python3 -c "import logging; print(logging.__file__)"
# could be stdlib or shadow depending on sys.path insertion
```

## Fix

```bash
mv logging app_logging
# update imports
sed -i 's/from logging\./from app_logging./g' scripts/run_strategy.py
# also check any other files: grep -r "from logging\." --include="*.py" . --exclude-dir=.venv
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; true
source .venv/bin/activate
uv pip install -e . -q  # rebuild entry point
# verify
python3 -c "import logging; print(logging.__file__)"  # must be stdlib
python3 -c "from app_logging.logger_setup import setup_logger; print('ok')"
```

Rule: Never name local packages `logging, email, json, os, sys, typing, collections, pathlib, datetime, re, unittest, http, xml, csv, etc.` — full stdlib list ~200 names. Use `app_logging`, `app_email`, etc.

## Why Bundled Skill Patch Was Refused

Attempted to patch bundled `systematic-debugging` skill with this pitfall — Hermes correctly refused: "Refusing background curator patch for bundled skill". That's why this went into new umbrella skill instead. Correct behavior — bundled skills are off-limits to background curators.

Related: This pattern applies to any Pi project, not just options-wheel. Consider adding to `pi-homelab` umbrella if user ever hits similar in other services.

## Prevention for Future Python Projects

Template check in CI:
```bash
# fail if any top-level dir shadows stdlib
python3 - <<'PY'
import sys, pathlib, sysconfig
stdlib = pathlib.Path(sysconfig.get_path('stdlib'))
stdlib_mods = {p.stem for p in stdlib.iterdir()}
local = {p.name for p in pathlib.Path('.').iterdir() if p.is_dir() and (p/'__init__.py').exists()}
overlap = local & stdlib_mods
if overlap:
    print(f"ERROR: local packages shadow stdlib: {overlap}")
    sys.exit(1)
PY
```
