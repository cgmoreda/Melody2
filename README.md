# Melody2 Discord Bot

Melody2 is a Discord bot for Codeforces communities. It provides:
- Codeforces account verification and rating-role updates
- Contest reminder channels with restart-safe dedupe
- Voice logging (solo sessions + watchdog checks)
- Gym management, tags, rating votes, and GALD trainee tracking
- Coach secretary waiting-room routing
- Per-guild command/text configuration

## Requirements
- Python 3.11+
- PostgreSQL

## Environment Variables
Set these in `.env`:

- `DISCORD_TOKEN` (required): Discord bot token
- `DATABASE_URL` (required): PostgreSQL connection URL
- `CF_REMINDER_POLL_SECONDS` (optional, default `300`): reminder poll interval, clamped to `300..600`
- `CACHE_TTL_SECONDS` (optional, default `60`): Codeforces response cache TTL
- `REQUEST_TIMEOUT_SECONDS` (optional, default `20`): per-request timeout for Codeforces API calls
- `CF_MAX_RETRIES` (optional, default `3`): retry count for Codeforces API calls

## Install
```bash
pip install -r requirements.txt
```

## Run
```bash
python main.py
```

## Commands
Core:
- `!help [command]`
- `!ping`

Verification and Codeforces:
- `!verify <handle>`
- `!confirm`
- `!updaterating` (alias: `!update`)
- `!whois <handle>`
- `!stats <handle>`
- `!roundchanges` (alias: `!lastround`)

Reminders:
- `!reminder enable [#channel]`
- `!reminder disable [#channel]`
- `!reminder status [#channel]`
- `!reminder next`

Config:
- `!config show`
- `!config keys`
- `!config set <key> <int>`
- `!config reset [key|all]`
- `!config text show`
- `!config text keys`
- `!config text set <key> <value>`
- `!config text reset [key|all]`

Coach secretary:
- `!coach setup @CoachUser "Waiting Room" "Coach Room"`
- `!coach reset`
- `!coach config`

Voice logging:
- `!voicehours` (alias: `!solohours`)
- `!voicehours last <x> <hour|day|week|month>`
- `!voicehours tahzeeq <x> [last <x> <hour|day|week|month>]`
- `!voicehours me [last <x> <hour|day|week|month>]`
- `!voicehours user <@member> [last <x> <hour|day|week|month>]`
- `!voicehours role <@role> [last <x> <hour|day|week|month>]`
- `!voicehours roles [last <x> <hour|day|week|month>]`
- `!voicehours top [limit] [last <x> <hour|day|week|month>]`

Gym:
- `!gym` (interactive panel)
- `!gald [contest_id] [teams] [force]`

Text config keys:
- `training_role_substring` (default: `training arc`)
- `coach_role_substring` (default: `coach`)

## Operational Notes
- Verification pending state is persisted in DB and expires after 15 minutes. A restart does not drop pending verification requests.
- Reminder dedupe is persisted in DB (`sent_reminders`) and guarded in-memory for fast repeats in-process. Startup cleanup removes old dedupe rows (retention window: 120 days).
- Voice startup reconciliation closes stale open solo sessions left from downtime and recreates missing sessions for currently active solo members.
- Solo watchdog tasks are race-safe: replacing a task for the same member does not let an old task clear the new map entry.
- Gym participation uses caching: default freshness 1 hour, or 10 minutes when `force` is used.
- High-volume outputs are chunked/truncated to stay within Discord limits (message `2000`, embed description `4096`, field value `1024`).
- Codeforces API failures are typed (`timeout`, `network`, `http`, `non_ok`, `parse`) and surfaced with endpoint/status context.

## Database Migrations / Upgrades
- Schema versioning is tracked in table `schema_version` (single row, id `1`).
- Migrations are applied automatically during `UserRepository.init()` in order.
- Current schema version is `3`.
- Migration highlights:
  - v1: base schema creation
  - v2: lookup/performance indexes
  - v3: integrity constraints and cleanup
- v3 adds:
  - case-insensitive unique handle-per-guild index on `verified_users`
  - gym child-table foreign keys to `gym_contests (guild_id, contest_id)` with `ON DELETE CASCADE`
  - pre-constraint cleanup for duplicate handles and orphan gym child rows
- Upgrades are in-place on startup; no manual SQL steps are normally required.

## Tests
Run:
```bash
python -m pytest -q
```

Current tests cover verification lifecycle, reminder dedupe persistence, voice watchdog/reconciliation behavior, gym rating logic, output size guards, CF error mapping, and migration upgrade paths.
