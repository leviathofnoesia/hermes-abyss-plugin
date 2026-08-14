# Contributing

Thanks for helping with Abyss. This is a small, focused codebase — the backend
is a flat Python module pair and the frontend is a single-file desktop plugin.
Please keep it that way.

## Layout

```
__init__.py            Backend core: activity store, signals/incidents,
                       doctor, slash commands, REST dispatcher (flat — the
                       test scripts import it as a sibling module).
abyss_wave.py          Wave expansion: event bus, streams, API telemetry,
                       subagents, approvals, platform, commands, skills.
dashboard/             FastAPI router mount for the desktop app.
desktop/               Single-file React UI (plugin.js) + design docs.
```

## Development workflow

1. **Backend** — edit `__init__.py` / `abyss_wave.py` / `dashboard/`, then:

   ```sh
   pip install -r requirements.txt
   python test_plugin.py          # core backend suite
   python test_wave.py            # wave expansion suite
   python dashboard/test_api.py   # dashboard API suite
   ```

   The tests are **script-style** — run them with `python test_*.py`.
   Do not run them through `pytest` (it false-fails on intentional
   `SystemExit` paths).

2. **Frontend** — edit `desktop/plugin.js`, then:

   ```sh
   node --check desktop/plugin.js
   ```

   The UI is a single self-contained ESM file loaded via Blob URL. It may
   import only `@hermes/plugin-sdk`, `react`, and `react/jsx-runtime` — no
   relative imports, no build step. `DESIGN.md` is the design contract:
   theme variables only (no hex/rgb literals), compiled-Tailwind classes only,
   `ctx.rest()` returns parsed JSON (never call `.json()` on it).

3. **Tests** — add coverage alongside any backend change. A change that
   touches signals, incidents, or wave payloads must extend the corresponding
   test script.

## Pull requests

- Branch from `main`: `feat/...`, `fix/...`, `docs/...`, `chore/...`.
- Keep the flat backend layout and the single-file frontend intact.
- Every PR must pass the CI workflow (three Python suites + `node --check`).
- Update `CHANGELOG.md` under `[Unreleased]`-style entries in the current
  version block.
- Screenshots: if a change is visible, attach before/after captures to the PR
  description.

## Reporting issues

Include: Hermes version, platform, the failing command or view, and the
relevant log excerpt. If a signal was a false positive, include the activity
entry that triggered it — classifier tuning needs real examples.
