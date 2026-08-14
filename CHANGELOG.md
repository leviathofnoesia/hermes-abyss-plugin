# Changelog

All notable changes to the Abyss observability plugin.

## [2.0.0] — 2026-08

### Added — "wave" plugin-interface expansion

- Wave telemetry surfaces: `/wave/events`, `/wave/streams`, `/wave/api`,
  `/wave/subagents`, `/wave/approvals`, `/wave/commands`, `/wave/platform`,
  `/wave/skills`, `/wave/summary`, `/wave/emit`.
- Event-bus and streaming-hook recording (`on_stream_start/delta/end`,
  `pre/post_api_request`, `api_request_error`, `subagent_start/stop`,
  `pre/post_approval_*`, `gateway_platform_event`, `pre_command`,
  `on_skill_lifecycle`, `on_session_reset/finalize`).
- Redaction registry + local mask pass on stored wave payloads.
- `wave_register()` entrypoint, auto-wired from `_init`; slash surface
  `/abyss wave [...]`.

### Fixed — August 2026 hardening

- **Clean 400s on malformed params** — client-supplied parameter errors now
  return a clean HTTP 400 (`_BadRequest`) instead of degrading into a 500.
- **Secret masking in wave payloads** — secret-looking values (tokens,
  passwords, API keys, authorization headers) are masked before storage and
  redacted from embedded signal/incident context.
- **Signal-classifier false-positive elimination** — bare exit codes are
  downgraded to warnings (no diagnostic value on their own); the classifier
  no longer raises spurious signals for them.
- **Exit-code classification** — tool-failure exit codes are captured and
  classified explicitly in activity details.
- **Doctor-clean 21 hooks** — all 21 hook registrations (6 core + 15 wave) are
  verified clean across the doctor self-diagnostic run.

### Changed

- Status-bar chip now reflects live open-signal count (not the old LLM-count).
- Slash help updated with the full wave surface.

## [1.0.0] — earlier

- Initial Abyss release: activity feed, calendar, global search, trace,
  brain graph, signals & incidents, doctor, webhooks, slash commands.
