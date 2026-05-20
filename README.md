# cita-tie — Automated TIE Appointment Booker

Monitors the Spanish ICP+ appointment system and auto-books when a slot opens.
Built for Barcelona (Toma de Huellas / TIE renewal).

## How it works

1. **Smart scheduling** — Aggressive polling during known release windows (midnight, 8-10 AM, noon, 2 PM, 8 PM). Light polling between.
2. **Auto-CAPTCHA** — Solves reCAPTCHA v3 and image CAPTCHAs via CapMonster Cloud.
3. **Auto-booking** — Fills forms, selects best date, confirms appointment.
4. **ntfy notifications** — Push alerts for: slot detected, booking confirmed, errors, SMS code needed.
5. **SMS code handling** — HTTP form for manual SMS code entry when required.

## Deployment

Follows the standard GitOps pattern: GitHub Actions → GHCR → Portainer stack.

### 1. Secrets (on host)

```bash
sudo mkdir -p /srv/cita-tie/secrets /srv/cita-tie/data
sudo cp secrets.example/* /srv/cita-tie/secrets/
```

Then edit each file in `/srv/cita-tie/secrets/` — replace `CHANGE_ME` placeholders with real values.

| File | What to fill in |
|------|----------------|
| `profile.json` | name (as on NIE), doc_type, doc_value, phone, email, country (Spanish, all caps) |
| `capmonster-api-key` | API key from https://capmonster.cloud |
| `ntfy.json` | Already pre-filled with your ntfy URL |
| `sms-webhook-token` | Optional — leave as-is if not using webhook.site |

Templates are in `secrets.example/` in this repo.

### 2. Data directory

```bash
mkdir -p /srv/cita-tie/data
```

### 3. Portainer stack

Create stack from this repo's `docker-compose.yml` via Portainer GitOps.

## Configuration (environment variables — non-secret)

Set in the compose file or Portainer stack:

| Variable | Default | Description |
|----------|---------|-------------|
| `OFFICES` | `BARCELONA,BADALONA,...` | Comma-separated office list |
| `OPERATION_CODE` | `TOMA_HUELLAS` | Procedure type |
| `MIN_DATE` / `MAX_DATE` | *(none)* | Date filter (dd/mm/yyyy) |
| `MIN_TIME` / `MAX_TIME` | *(none)* | Time filter (HH:MM) |
| `SMS_CODE_PORT` | `8080` | Port for SMS code HTTP form |

## SMS Verification

When SMS is needed, you'll get an urgent ntfy push. Open `http://<host>:8085/` and enter the code. 5 minute window.

## Release Windows (community-observed, Europe/Madrid)

| Window | Time | Polling |
|--------|------|---------|
| Midnight | 00:00 - 01:30 | ~15s |
| Morning | 08:00 - 10:00 | ~15s |
| Noon | 12:00 - 13:00 | ~15s |
| Afternoon | 14:00 - 15:00 | ~15s |
| Evening | 20:00 - 21:00 | ~15s |
| Off-peak | Other | ~3 min |

## Based on

Vendored from [cita-bot/cita-bot](https://github.com/cita-bot/cita-bot) (AGPL-3.0), modified for headless Docker, CapMonster Cloud CAPTCHA solving, file-based secrets, ntfy notifications, smart scheduling, and HTTP-based SMS code entry.
