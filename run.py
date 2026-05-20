"""
Smart scheduler for TIE appointment booking.
Runs aggressive polling during known release windows, lighter polling otherwise.
Secrets read from /secrets/ (mounted volume). Config from environment variables.
"""
import json
import logging
import os
import random
import sys
import time
from datetime import datetime

from bcncita import CustomerProfile, DocType, Office, OperationType, Province, try_cita

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger("scheduler")

SECRETS_DIR = os.environ.get("SECRETS_DIR", "/secrets")

# Community-observed release windows (Europe/Madrid local time)
# Format: (start_hour, start_min, end_hour, end_min)
HOT_WINDOWS = [
    (0, 0, 1, 30),    # 00:00-01:30 — midnight release
    (8, 0, 10, 0),    # 08:00-10:00 — morning release (most common)
    (12, 0, 13, 0),   # 12:00-13:00 — noon release
    (14, 0, 15, 0),   # 14:00-15:00 — afternoon cancellations
    (20, 0, 21, 0),   # 20:00-21:00 — evening release
]

HOT_CYCLES = 30        # attempts per hot-window run (~15 min at 30s each)
HOT_SLEEP = 15         # seconds between hot-window runs
COLD_CYCLES = 5        # attempts per cold-window run
COLD_SLEEP = 180       # seconds between cold-window runs (3 min)

# Office name → enum mapping
OFFICE_MAP = {name: member for name, member in Office.__members__.items()}


def read_secret(filename: str) -> str:
    """Read a secret from a file in SECRETS_DIR."""
    path = os.path.join(SECRETS_DIR, filename)
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def is_hot_window() -> bool:
    now = datetime.now()
    current_minutes = now.hour * 60 + now.minute
    for sh, sm, eh, em in HOT_WINDOWS:
        start = sh * 60 + sm
        end = eh * 60 + em
        if start <= current_minutes < end:
            return True
    return False


def next_hot_window_seconds() -> int:
    """Seconds until the next hot window starts."""
    now = datetime.now()
    current_minutes = now.hour * 60 + now.minute
    best = None
    for sh, sm, _eh, _em in HOT_WINDOWS:
        start = sh * 60 + sm
        diff = start - current_minutes
        if diff < 0:
            diff += 1440  # wrap to next day
        if diff == 0:
            continue
        if best is None or diff < best:
            best = diff
    return (best or 60) * 60


def parse_offices(offices_str: str) -> list:
    """Parse comma-separated office names."""
    if not offices_str:
        return []
    offices = []
    for name in offices_str.split(","):
        name = name.strip().upper()
        if name in OFFICE_MAP:
            offices.append(OFFICE_MAP[name])
        else:
            log.warning(f"Unknown office: {name}. Available: {', '.join(OFFICE_MAP.keys())}")
    return offices


def build_profile() -> CustomerProfile:
    """Build CustomerProfile from secrets files + environment config."""
    # Secrets from files
    profile_path = os.path.join(SECRETS_DIR, "profile.json")
    try:
        with open(profile_path) as f:
            profile_data = json.load(f)
    except FileNotFoundError:
        log.error(f"Missing {profile_path} — create it with: name, doc_type, doc_value, phone, email, country")
        sys.exit(1)

    required_fields = ["name", "doc_type", "doc_value", "phone", "email", "country"]
    missing = [k for k in required_fields if not profile_data.get(k)]
    if missing:
        log.error(f"Missing fields in profile.json: {', '.join(missing)}")
        sys.exit(1)

    capmonster_key = read_secret("capmonster-api-key")
    ntfy_config_raw = read_secret("ntfy.json")

    doc_type = {"nie": DocType.NIE, "dni": DocType.DNI, "passport": DocType.PASSPORT}.get(
        profile_data["doc_type"].lower(), DocType.NIE
    )

    # Non-secret config from environment
    op_code_str = os.environ.get("OPERATION_CODE", "TOMA_HUELLAS").upper()
    op_code = getattr(OperationType, op_code_str, OperationType.TOMA_HUELLAS)
    offices = parse_offices(os.environ.get("OFFICES", ""))

    profile = CustomerProfile(
        name=profile_data["name"],
        doc_type=doc_type,
        doc_value=profile_data["doc_value"],
        phone=profile_data["phone"],
        email=profile_data["email"],
        province=Province.BARCELONA,
        operation_code=op_code,
        country=profile_data["country"],
        offices=offices,
        auto_office=True,
        auto_captcha=bool(capmonster_key),
        capmonster_api_key=capmonster_key or None,
        save_artifacts=True,
        sms_webhook_token=read_secret("sms-webhook-token") or None,
        min_date=os.environ.get("MIN_DATE"),
        max_date=os.environ.get("MAX_DATE"),
        min_time=os.environ.get("MIN_TIME"),
        max_time=os.environ.get("MAX_TIME"),
        sms_code_port=int(os.environ.get("SMS_CODE_PORT", "8080")),
    )

    log.info(f"Profile: {profile.name} ({profile.doc_type.value})")
    log.info(f"Operation: {op_code_str}")
    log.info(f"Offices: {[o.name for o in offices] if offices else 'auto-select'}")
    log.info(f"CapMonster: {'enabled' if profile.auto_captcha else 'DISABLED'}")
    log.info(f"Date range: {profile.min_date or 'any'} - {profile.max_date or 'any'}")
    return profile, ntfy_config_raw


def _ntfy(title, message, ntfy_config_raw, priority="default", tags=""):
    """Send ntfy push notification."""
    if not ntfy_config_raw:
        return
    try:
        import requests as req
        config = json.loads(ntfy_config_raw) if isinstance(ntfy_config_raw, str) else ntfy_config_raw
        url = config.get("url", "")
        topic = config.get("topic", "")
        if not url or not topic:
            return
        req.post(
            f"{url}/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=10,
        )
    except Exception:
        pass


def main():
    profile, ntfy_config = build_profile()

    _ntfy(
        "TIE Checker Started",
        f"Monitoring for {profile.operation_code.name} appointments in Barcelona",
        ntfy_config,
        priority="default",
        tags="mag,robot_face",
    )

    run_count = 0
    while True:
        hot = is_hot_window()
        cycles = HOT_CYCLES if hot else COLD_CYCLES
        sleep_time = HOT_SLEEP if hot else COLD_SLEEP
        window_type = "HOT" if hot else "COLD"

        run_count += 1
        log.info(f"=== Run #{run_count} | {window_type} window | {cycles} cycles ===")

        try:
            success = try_cita(context=profile, cycles=cycles)
        except KeyboardInterrupt:
            log.info("Interrupted by user")
            _ntfy("TIE Checker Stopped", "Manually interrupted", ntfy_config, tags="stop_sign")
            break
        except Exception as e:
            log.error(f"Unexpected error: {e}")
            success = False

        if success:
            log.info("APPOINTMENT BOOKED! Exiting.")
            break

        # Reset solver state for next run (new browser session)
        profile.capmonster_client = None
        profile.current_solver = None
        profile.first_load = True
        profile.bot_result = False

        # Add jitter to avoid pattern detection
        jitter = random.uniform(0, sleep_time * 0.3)
        actual_sleep = sleep_time + jitter

        if not hot:
            next_hot = next_hot_window_seconds()
            if next_hot < actual_sleep:
                actual_sleep = max(next_hot - 30, 10)
                log.info(f"Next hot window in {next_hot // 60}m — sleeping {actual_sleep:.0f}s")
            else:
                log.info(f"Cold window — sleeping {actual_sleep:.0f}s")
        else:
            log.info(f"Hot window — sleeping {actual_sleep:.0f}s")

        time.sleep(actual_sleep)


if __name__ == "__main__":
    main()
