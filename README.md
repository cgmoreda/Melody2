# Melody2 Discord Bot

Melody2 is a production Discord bot for competitive-programming training communities. It combines Codeforces account verification, contest reminders, training accountability, voice-hour tracking, and coach workflow automation in one server-scoped bot.

The bot uses the `!` command prefix and stores persistent state in PostgreSQL. Database migrations run automatically on startup.

## Features

### Codeforces And AtCoder
- Codeforces handle verification with persisted pending-verification state.
- Rating refresh and Discord role assignment based on Codeforces max rating.
- Live profile, statistics, and latest-round rating-change reports.
- Codeforces and AtCoder contest reminders with restart-safe deduplication.

### Training Operations
- Daily sheet reminders per guild and channel with `@everyone` delivery pings.
- Voice-hour tracking for solo channels and configured tracked-channel keywords.
- Training timesheets and max voice-hour reports with Egypt-day boundaries.
- Trainee accountability reports for minimum voice-hour targets.

### Discord Workflow
- Coach secretary setup for waiting-room routing.
- Dynamic voice channel management.
- Per-guild command limits and text configuration.
- Output chunking and truncation for Discord message/embed limits.

## Requirements

- Python 3.13 or newer
- PostgreSQL
- Discord bot token with these privileged intents enabled:
  - Message Content Intent
  - Server Members Intent
  - Voice States Intent

## Configuration

Create a `.env` file in the repository root:

```env
DISCORD_TOKEN=your-discord-bot-token
DATABASE_URL=postgresql://user:password@host:5432/database
```

Optional environment variables:

| Variable | Default | Description |
| --- | ---: | --- |
| `CF_REMINDER_POLL_SECONDS` | `300` | Contest reminder poll interval. Clamped to `300..600`. |
| `CACHE_TTL_SECONDS` | `60` | Codeforces response cache TTL. |
| `CF_CACHE_MAX_ENTRIES` | `1024` | Maximum in-memory Codeforces response cache entries. |
| `REQUEST_TIMEOUT_SECONDS` | `20` | Per-request Codeforces API timeout. |
| `CF_MAX_RETRIES` | `3` | Codeforces API retry count. |

## Installation

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Linux/macOS:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Running The Bot

```bash
python main.py
```

On startup, Melody2 will:

1. Connect to PostgreSQL.
2. Apply any pending schema migrations.
3. Start contest reminders and daily sheet reminder loops.
4. Load all Discord cogs.
5. Sync the Discord app command tree.

## Command Reference

### Core

| Command | Description |
| --- | --- |
| `!help [command]` | Show bot help or command-specific help. |
| `!ping` | Confirm the bot is responsive. |

### Verification And Codeforces

| Command | Description |
| --- | --- |
| `!verify <handle>` | Start Codeforces account verification. |
| `!confirm` | Confirm verification after setting the generated code on Codeforces. |
| `!updaterating` / `!update` | Refresh linked Codeforces rating and role. |
| `!whois <handle>` | Show a live Codeforces profile summary. |
| `!stats <handle>` | Show contest and submission statistics. |
| `!roundchanges` / `!lastround` | Show latest round rating changes for verified members. |

### Reminders

| Command | Description |
| --- | --- |
| `!reminder enable [#channel]` | Enable contest reminders in a channel. |
| `!reminder disable [#channel]` | Disable contest reminders in a channel. |
| `!reminder status [#channel]` | Show reminder status for a channel. |
| `!reminder next [codeforces\|atcoder]` | Preview upcoming contests. |
| `!dailysheets set <HH:MM UTC> [#channel] [message]` | Configure the daily sheet reminder. |
| `!dailysheets set [#channel] <HH:MM UTC> [message]` | Configure the same reminder with channel before time. |
| `!dailysheets status` | Show the configured daily sheet reminder. |
| `!dailysheets disable` | Disable the daily sheet reminder. |

Aliases for daily sheets: `!dailyreminder`, `!sheets`.

### Guild Configuration

| Command | Description |
| --- | --- |
| `!config show` | Show current numeric and text config values. |
| `!config keys` | List numeric config keys and valid ranges. |
| `!config set <key> <int>` | Set a numeric config value. |
| `!config reset [key\|all]` | Reset one or all numeric config values. |
| `!config text show` | Show text config values. |
| `!config text keys` | List text config keys. |
| `!config text set <key> <value>` | Set a text config value. |
| `!config text reset [key\|all]` | Reset one or all text config values. |

Config keys:

| Key | Default | Description |
| --- | --- | --- |
| `reminder_preview_limit` | `3` | Upcoming contests shown by `!reminder next`. |
| `roundchanges_max_lines` | `30` | Maximum rows shown by `!roundchanges`. |
| `voicehours_max_lines` | `35` | Maximum rows shown by voice-hour reports. |
| `voice_check_interval_seconds` | `900` | Seconds between solo-channel work checks. |
| `voice_confirm_timeout_seconds` | `180` | Seconds a user has to confirm they are still working. |

Text config keys:

| Key | Default | Description |
| --- | --- | --- |
| `training_role_substring` | `training arc` | Substring used to detect trainee roles. |
| `coach_role_substring` | `coach` | Substring used to detect coach roles. |
| `voice_tracked_keywords` | empty | Comma-separated channel keywords that count toward voice hours. |

### Coach Secretary

| Command | Description |
| --- | --- |
| `!coach setup @CoachUser "Waiting Room" "Coach Room"` | Configure coach routing. |
| `!coach reset` | Remove coach routing configuration. |
| `!coach config` | Show current coach routing configuration. |
| `!summon <@user\|@role>` | Coach-only summon to the configured coach voice room; users outside voice are DM'd and can enter the waiting room for automatic transfer within 10 minutes. |

### Voice Hours

Voice time is counted for:

- Channels named like solo channels.
- Channels containing any configured `voice_tracked_keywords`.
- Open tracked sessions, clipped to the requested report window.

Timesheet and max reports use `Africa/Cairo`; a training day starts at 05:00 Egypt time.

| Command | Description |
| --- | --- |
| `!voicehours` / `!solohours` | Show all-time, last-week, and last-month tracked voice summary. |
| `!voicehours last <x> <hour\|day\|week\|month>` | Show tracked voice hours for a window. |
| `!voicehours me [last <x> <unit>]` | Show your own tracked hours and rank. |
| `!voicehours user <@member> [last <x> <unit>]` | Show one member's tracked hours. |
| `!voicehours role <@role> [last <x> <unit>]` | Show tracked hours for one role. |
| `!voicehours roles [last <x> <unit>]` / `!voicehours teams ...` | Show team-role standings. |
| `!voicehours unis [last <x> <unit>]` | Show university-role standings. |
| `!voicehours top [limit] [last <x> <unit>]` | Show top tracked-hour users. |
| `!voicehours last <x> <unit> top <limit>` | Same top report with the window first. |
| `!voicehours tahzeeq <hours> [last <x> <unit>]` | List trainees below a target number of hours. |
| `!voicehours last <x> <unit> tahzeeq <hours>` | Same accountability report with the window first. |

### Voice Timesheets And Max Reports

| Command | Description |
| --- | --- |
| `!voicehours timesheet last <days> days` | Show a daily training-role timesheet. |
| `!timesheet last <days> days` | Same timesheet via the shorter command. |
| `!voicehours max day last <amount> <day\|week\|month>` | Best fixed Egypt-day bucket in the lookback. |
| `!voicehours max week last <amount> <week\|month>` | Best rolling 7-day window in the lookback. |
| `!voicehours max month last <amount> months` | Best rolling 30-day window in the lookback. |
| `!voicehours max range <amount> <hour\|day\|week\|month> last <lookback_amount> <hour\|day\|week\|month>` | Best custom rolling window in the lookback. |
| `!voicehours top <limit> max <day\|week\|month\|range> ...` | Limit max-report rows with `top` before `max`. |
| `!voicehours max <day\|week\|month\|range> ... top <limit>` | Limit max-report rows with `top` after the max query. |

The same `max`, `top <limit> max`, and `max ... top <limit>` forms are available through `!timesheet`.

Limits:

- `timesheet last <days>` is capped at 31 days.
- Max-report lookback is capped at 12 months.
- Month windows are treated as 30 days.

### Voice Tracking Keywords

| Command | Description |
| --- | --- |
| `!voicehours track list` | Show configured tracked-channel keywords. |
| `!voicehours track add <keyword>` | Add a tracked-channel keyword. |
| `!voicehours track remove <keyword>` | Remove a tracked-channel keyword. |

## Operational Behavior

- Pending Codeforces verification expires after 15 minutes and survives restarts.
- Contest reminder dedupe is persisted in PostgreSQL and protected in memory for fast repeats.
- Daily sheet reminders are persisted per guild and sent once per UTC day at or after the configured time.
- Voice startup reconciliation closes stale open tracked sessions and recreates missing sessions for currently active tracked members.
- Solo-channel watchdog tasks are race-safe and clean up correctly when replaced.
- High-volume output is chunked or truncated to stay within Discord limits.
- Codeforces API failures are typed and surfaced with endpoint/status context.

## Database Migrations

Schema versioning is tracked in `schema_version` and applied automatically during startup. No manual SQL is normally required for upgrades.

Current schema version: `7`

| Version | Summary |
| ---: | --- |
| `1` | Base schema creation. |
| `2` | Lookup and performance indexes. |
| `3` | Integrity constraints and duplicate/orphan cleanup. |
| `4` | Reminder platform support and text contest IDs. |
| `5` | Renamed voice session tracking flag to `is_tracked`. |
| `6` | Enforced one open voice session per guild member. |
| `7` | Added daily sheet reminder configuration. |

Migration notes:

- v3 adds a case-insensitive unique handle-per-guild index and referential integrity cleanup.
- v4 expands `sent_reminders` with `platform` and stores `contest_id` as text.
- v5 changes `voice_sessions.is_solo` to `voice_sessions.is_tracked`.
- v6 closes duplicate open voice sessions and adds a partial unique index for open sessions.
- v7 adds one persisted daily sheet reminder per guild.

## Testing

Run the full test suite before deployment:

```bash
python -m pytest -q
```

Coverage includes:

- Codeforces verification lifecycle and pending-state persistence.
- Contest reminder dedupe persistence.
- Daily sheet reminder persistence.
- Voice watchdog, startup reconciliation, dynamic voice rules, timesheets, and max reports.
- Discord output size guards.
- Codeforces error mapping.
- Help metadata and migration upgrade paths.

## Production Checklist

Before deploying:

1. Confirm `.env` contains `DISCORD_TOKEN` and `DATABASE_URL`.
2. Confirm the Discord application has Message Content, Server Members, and Voice States intents enabled.
3. Confirm PostgreSQL is reachable from the host.
4. Run `python -m pytest -q`.
5. Start the bot with `python main.py` or the host process manager.
6. Check logs for successful extension loading, migration completion, reminder startup, and bot readiness.
