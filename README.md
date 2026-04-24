# Melody2 Discord Bot

Melody2 is a Discord bot for Codeforces communities. It includes:
- Codeforces account verification and role assignment
- Contest reminder channels
- Voice logging and coach secretary features
- Server-level bot configuration (`!config`)

## Requirements
- Python 3.11+
- PostgreSQL

## Environment Variables
Set these in `.env`:

- `DISCORD_TOKEN` - Discord bot token
- `DATABASE_URL` - PostgreSQL connection URL
- `CF_REMINDER_POLL_SECONDS` - Reminder polling interval (effective clamp: 300-600)

Runtime tuning:
- `CACHE_TTL_SECONDS` (default `60`)
- `REQUEST_TIMEOUT_SECONDS` (default `20`)
- `CF_MAX_RETRIES` (default `3`)

## Install
```bash
pip install -r requirements.txt
```

## Run
```bash
python main.py
```

## Existing Commands
- `!verify <handle>`
- `!confirm`
- `!update`
- `!whois <handle>`
- `!stats <handle>`
- `!roundchanges`
- `!reminder <enable|disable|status|next> [#channel]`
- `!coach <setup|reset|config>`
- `!voicehours`
- `!voicehours last <x> <hour|day|week|month>`
- `!voicehours tahzeeq <x> [last <x> <hour|day|week|month>]` (mentions trainees below target)
- `!voicehours me [last <x> <hour|day|week|month>]`
- `!voicehours user <@member> [last <x> <hour|day|week|month>]`
- `!voicehours role <@role> [last <x> <hour|day|week|month>]`
- `!voicehours roles [last <x> <hour|day|week|month>]` (all roles containing `team`)
- `!voicehours top [limit] [last <x> <hour|day|week|month>]`
- `!gym` (button panel for adding/listing gyms, tags, ratings, reset/delete)
- `!gald [contest_id] [teams] [force]`
  - default checks all `individual` gyms
  - `teams` includes team gyms
  - `force` refreshes stale participation newer than 10m instead of 1h cache
- `!config <show|keys|set|reset|text>`
- `!config text <show|keys|set|reset>`

Text config keys:
- `training_role_substring` (default: `training arc`)
- `coach_role_substring` (default: `coach`)
- `!help [command]`

## Database Schema Integration
The bot uses raw SQL initialization in `UserRepository.init()`.
No separate migration framework is currently used in this repo.

## Tests
Run:
```bash
pytest
```

Automated tests were removed with the `cf-predict` feature cleanup. Add/update tests as new features are introduced.
