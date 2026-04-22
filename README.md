# Melody2 Discord Bot

Melody2 is a Discord bot for Codeforces communities. It includes:
- Codeforces account verification and role assignment
- Contest reminder channels
- Voice logging and coach secretary features
- Server-level bot configuration (`!config`)
- Live Codeforces rating prediction (approximate)

## Requirements
- Python 3.11+
- PostgreSQL

## Environment Variables
Set these in `.env`:

- `DISCORD_TOKEN` - Discord bot token
- `DATABASE_URL` - PostgreSQL connection URL
- `CF_REMINDER_POLL_SECONDS` - Reminder polling interval (effective clamp: 300-600)

Codeforces API authenticated access (required for prediction standings flow):
- `CF_API_KEY`
- `CF_API_SECRET`

Prediction/runtime tuning:
- `CACHE_TTL_SECONDS` (default `60`)
- `REQUEST_TIMEOUT_SECONDS` (default `20`)
- `CF_MAX_RETRIES` (default `3`)
- `CF_STANDINGS_PAGE_SIZE` (default `500`)
- `DEFAULT_WATCH_INTERVAL_MINUTES` (default `5`)

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
- `!voicehours me [last <x> <hour|day|week|month>]`
- `!voicehours user <@member> [last <x> <hour|day|week|month>]`
- `!voicehours role <@role> [last <x> <hour|day|week|month>]`
- `!voicehours roles [last <x> <hour|day|week|month>]` (team roles only)
- `!voicehours top [limit] [last <x> <hour|day|week|month>]`
- `!config <show|keys|set|reset>`
- `!help [command]`

## Rating Prediction Commands
All commands are available as prefix and slash (hybrid command registration).

Prediction commands use the existing verification flow (`!verify` + `!confirm`) and verified users in the server.

### Prediction
- `!cf-predict <contest_id> [server_only=True] [show_unofficial=False]`
- `!cf-predict-handles <contest_id> <handles>`
  - handles can be comma or space separated
- `!cf-predict-me <contest_id>`

### Watch Mode (Manage Server permission)
- `!cf-watch <contest_id> [interval_minutes=5] [server_only=True] [show_unofficial=False]`
- `!cf-unwatch <contest_id>`

Watch jobs are stored in PostgreSQL and resumed after restart.

### Verification Utility
- `!cf-verify-finished <contest_id>`

Compares predicted deltas against official `contest.ratingChanges` and reports:
- MAE
- max absolute error
- exact matches
- close matches (`<= 10`)

## Approximation Notes
Live prediction is approximate. Official Codeforces deltas may differ because Codeforces uses additional internal adjustments and final rating processing rules.

The implementation is designed for maintainability and can be improved later with:
- tighter correction terms
- more eligibility edge-case handling
- performance optimizations for very large contests

## Database Schema Integration
The bot uses raw SQL initialization in `UserRepository.init()`.
New persisted tables added for prediction feature:
- `linked_accounts`
- `watch_jobs`

No separate migration framework is currently used in this repo.

## Tests
Run:
```bash
pytest
```

Tests include:
- Codeforces signing logic
- standings parsing
- predictor behavior on handcrafted data
- linked account persistence logic
- watch job persistence logic
