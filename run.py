"""
Smart scheduler for TIE appointment booking.
Runs aggressive polling during known release windows, lighter polling otherwise.
Secrets read from /secrets/ (mounted volume). Config from environment variables.
"""
import json
import logging
import os
import pathlib
import random
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from bcncita import CustomerProfile, DocType, Office, OperationType, Province, try_cita
from bcncita.cita import fingerprint_test

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger("scheduler")

SECRETS_DIR = os.environ.get("SECRETS_DIR", "/secrets")
HEARTBEAT_INTERVAL = 6 * 3600  # 6 hours

# ── Stats tracker ───────────────────────────────────────────────────

_stats = {
    "start_time": None,
    "total_runs": 0,
    "total_attempts": 0,
    "rate_limits": 0,
    "page_loads": 0,
    "errors": 0,
    "last_attempt": None,
    "last_state": "starting",
    "booked": False,
}


# ── Remote orchestration state (manual control of one Selenium driver) ──
_ctl = {"driver": None, "profile": None}


def _ctl_get_driver():
    """Lazily create/reuse a single Selenium driver for remote orchestration."""
    if _ctl["driver"] is None:
        from bcncita.cita import init_wedriver
        _ctl["driver"] = init_wedriver(_ctl.get("profile"))
    return _ctl["driver"]


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        import urllib.parse
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path.startswith("/control/"):
            self._control(parsed)
            return

        if parsed.path == "/code":
            # SMS code handler — sets the shared event in bcncita.cita
            params = urllib.parse.parse_qs(parsed.query)
            if "value" in params:
                from bcncita.cita import _sms_code_event
                import bcncita.cita as _cita_mod
                _cita_mod._sms_code_value = params["value"][0]
                _sms_code_event.set()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h2>Code received! You can close this page.</h2>")
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                html = (
                    "<html><body style='font-family:sans-serif;max-width:400px;margin:40px auto'>"
                    "<h2>TIE Appointment - SMS Code</h2>"
                    "<form action='/code' method='GET'>"
                    "<input name='value' placeholder='Enter SMS code' "
                    "style='font-size:24px;padding:10px;width:100%'><br><br>"
                    "<button style='font-size:24px;padding:10px 30px'>Submit</button>"
                    "</form></body></html>"
                )
                self.wfile.write(html.encode())
            return

        # Default: health endpoint
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        stats = {**_stats}
        stats["uptime_hours"] = round((time.time() - stats["start_time"]) / 3600, 1) if stats["start_time"] else 0
        stats["start_time"] = str(datetime.fromtimestamp(stats["start_time"])) if stats["start_time"] else None
        self.wfile.write(json.dumps(stats, indent=2).encode())

    def _cjson(self, obj, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj, default=str).encode())

    def _control(self, parsed):
        """Remote browser orchestration. Secrets (NIE) stay in this container."""
        import urllib.parse
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import Select
        q = urllib.parse.parse_qs(parsed.query)
        cmd = parsed.path[len("/control/"):]
        try:
            d = _ctl_get_driver()
            if cmd == "nav":
                d.get(q["url"][0])
                time.sleep(float(q.get("wait", ["2"])[0]))
                return self._cjson({"url": d.current_url, "title": d.title})
            if cmd == "state":
                return self._cjson({"url": d.current_url, "title": d.title})
            if cmd == "text":
                return self._cjson({"text": d.find_element(By.TAG_NAME, "body").text[:7000]})
            if cmd == "dom":
                sel = q.get("selector", ["body"])[0]
                html = d.execute_script(
                    "var e=document.querySelector(arguments[0]);return e?e.outerHTML:null;", sel)
                return self._cjson({"selector": sel, "html": (html or "")[:9000]})
            if cmd == "screenshot":
                path = "/app/data/control-%s.png" % datetime.now().strftime("%H%M%S")
                d.save_screenshot(path)
                return self._cjson({"saved": path})
            if cmd == "click":
                d.find_element(By.CSS_SELECTOR, q["selector"][0]).click()
                time.sleep(float(q.get("wait", ["1.5"])[0]))
                return self._cjson({"ok": True, "url": d.current_url, "title": d.title})
            if cmd == "select":
                Select(d.find_element(By.CSS_SELECTOR, q["selector"][0])).select_by_value(q["value"][0])
                time.sleep(float(q.get("wait", ["1.5"])[0]))
                return self._cjson({"ok": True, "url": d.current_url, "title": d.title})
            if cmd == "fillprofile":
                from bcncita.cita import fill_personal_info
                ok = fill_personal_info(d, _ctl["profile"])
                return self._cjson({"ok": bool(ok), "url": d.current_url})
            if cmd == "quit":
                # Over Remote, .quit() DELETES the live session (kills the Cl@ve
                # login). Default: drop only the local reference (reattach later).
                # Use ?hard=1 to actually end the browser session.
                hard = q.get("hard", ["0"])[0] == "1"
                if hard:
                    try:
                        d.quit()
                    except Exception:
                        pass
                _ctl["driver"] = None
                return self._cjson({"ok": True, "hard": hard})
            if cmd == "solvecaptcha":
                # Solve the current page's image CAPTCHA via the container's
                # CapMonster key and RETURN the text (for verification).
                import asyncio
                import base64 as _b64
                import requests as _rq
                from bcncita.cita import _get_capmonster_client, CAPTCHA_IMAGE
                from bcncita.resilience import find_elements_resilient
                from capmonstercloudclient.requests import ImageToTextRequest
                imgs = find_elements_resilient(d, CAPTCHA_IMAGE)
                if not imgs:
                    return self._cjson({"error": "no captcha image on page"}, 404)
                src = imgs[0].get_attribute("src") or ""
                if src.startswith("data:"):
                    img_data = src.split(",", 1)[1].strip()
                else:
                    img_data = _b64.b64encode(_rq.get(src, timeout=10).content).decode()
                client = _get_capmonster_client(_ctl["profile"])
                resp = asyncio.run(client.solve_captcha(ImageToTextRequest(body=img_data)))
                return self._cjson({"text": resp.get("text", ""),
                                    "src_type": "data" if src.startswith("data:") else "url"})
            if cmd == "ntfy":
                # Send a push via the container's ntfy.json (works from the Bash
                # watchers: curl /control/ntfy?title=&msg=&priority=&tags=).
                from bcncita.cita import _ntfy
                _ntfy(q.get("title", ["cita-tie"])[0], q.get("msg", [""])[0],
                      priority=q.get("priority", ["default"])[0], tags=q.get("tags", [""])[0])
                return self._cjson({"ok": True})
            return self._cjson({"error": "unknown command: %s" % cmd}, 404)
        except Exception as e:
            return self._cjson({"error": str(e).splitlines()[0] if str(e) else type(e).__name__}, 500)

    def do_POST(self):
        import urllib.parse
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/control/eval":
            ln = int(self.headers.get("Content-Length", "0"))
            js = self.rfile.read(ln).decode("utf-8")
            try:
                d = _ctl_get_driver()
                return self._cjson({"result": d.execute_script(js)})
            except Exception as e:
                return self._cjson({"error": str(e).splitlines()[0] if str(e) else type(e).__name__}, 500)
        self._cjson({"error": "not found"}, 404)

    def log_message(self, *args):
        pass


def _start_health_server(port=8080):
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    import socket
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info(f"Health endpoint running on :{port}/")


# Community-observed release windows (Europe/Madrid local time)
# Format: (start_hour, start_min, end_hour, end_min)
HOT_WINDOWS = [
    (0, 0, 1, 30),    # 00:00-01:30 — midnight release
    (8, 0, 10, 0),    # 08:00-10:00 — morning release (most common)
    (12, 0, 13, 0),   # 12:00-13:00 — noon release
    (14, 0, 15, 0),   # 14:00-15:00 — afternoon cancellations
    (20, 0, 21, 0),   # 20:00-21:00 — evening release
]

HOT_CYCLES = int(os.environ.get("HOT_CYCLES", "30"))
HOT_SLEEP = int(os.environ.get("HOT_SLEEP", "15"))
COLD_CYCLES = int(os.environ.get("COLD_CYCLES", "5"))
COLD_SLEEP = int(os.environ.get("COLD_SLEEP", "180"))

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

    province_str = os.environ.get("PROVINCE", "BARCELONA").upper()
    province = getattr(Province, province_str, Province.BARCELONA)
    if province.name != province_str:
        log.warning(f"Unknown province '{province_str}' — defaulting to BARCELONA")

    profile = CustomerProfile(
        name=profile_data["name"],
        doc_type=doc_type,
        doc_value=profile_data["doc_value"],
        phone=profile_data["phone"],
        email=profile_data["email"],
        province=province,
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

    log.info(f"Profile loaded ({profile.doc_type.value}: {profile.doc_value[:3]}***)")
    log.info(f"Province: {province.name}")
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


def cleanup_old_screenshots(data_dir="/app/data", max_age_days=7):
    """Delete screenshots older than max_age_days to prevent PII accumulation."""
    cutoff = time.time() - (max_age_days * 86400)
    for f in pathlib.Path(data_dir).glob("*.png"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                log.info(f"Cleaned up old screenshot: {f.name}")
        except Exception:
            pass


def _heartbeat_summary():
    """Build a short stats summary for ntfy."""
    up_h = round((time.time() - _stats["start_time"]) / 3600, 1) if _stats["start_time"] else 0
    return (
        f"Uptime: {up_h}h | Runs: {_stats['total_runs']} | "
        f"Attempts: {_stats['total_attempts']} | "
        f"Rate limits: {_stats['rate_limits']} | Errors: {_stats['errors']} | "
        f"State: {_stats['last_state']}"
    )


def main():
    # PAI: fingerprint test mode — test bot detection signals and exit
    if os.environ.get("FINGERPRINT_TEST"):
        fingerprint_test()
        return

    # PAI: orchestrate-only mode — pause the automated polling loop and expose the
    # /control/* API so the booking flow is driven remotely, step-by-step. The
    # container holds the secrets (NIE) and uses them internally (/control/fillprofile),
    # so PII never leaves the container.
    if os.environ.get("ORCHESTRATE_ONLY", "").lower() == "true":
        _stats["start_time"] = time.time()
        _stats["last_state"] = "orchestrate-only (automation paused)"
        _start_health_server(port=int(os.environ.get("SMS_CODE_PORT", "8080")))
        try:
            profile, _ = build_profile()
            _ctl["profile"] = profile
            log.info("ORCHESTRATE_ONLY: profile loaded; automated polling PAUSED. Control API on /control/*")
        except SystemExit:
            log.error("ORCHESTRATE_ONLY: profile load failed; control API still up (no profile)")
        while True:
            time.sleep(3600)

    # Start health server FIRST so the endpoint is observable even if
    # build_profile() exits or Firefox/Xvfb init fails downstream.
    _stats["start_time"] = time.time()
    _start_health_server(port=int(os.environ.get("SMS_CODE_PORT", "8080")))

    profile, ntfy_config = build_profile()
    cleanup_old_screenshots()

    _ntfy(
        "TIE Checker Started",
        f"Monitoring for {profile.operation_code.name} appointments in {profile.province.name.title()}",
        ntfy_config,
        priority="default",
        tags="mag,robot_face",
    )

    last_heartbeat = time.time()

    while True:
        hot = is_hot_window()
        cycles = HOT_CYCLES if hot else COLD_CYCLES
        sleep_time = HOT_SLEEP if hot else COLD_SLEEP
        window_type = "HOT" if hot else "COLD"

        _stats["total_runs"] += 1
        _stats["total_attempts"] += cycles
        _stats["last_state"] = f"{window_type} polling"
        log.info(f"=== Run #{_stats['total_runs']} | {window_type} window | {cycles} cycles ===")

        try:
            success = try_cita(context=profile, cycles=cycles)
        except KeyboardInterrupt:
            log.info("Interrupted by user")
            _ntfy("TIE Checker Stopped", _heartbeat_summary(), ntfy_config, tags="stop_sign")
            break
        except Exception as e:
            msg = str(e).split("\n")[0] if str(e) else type(e).__name__
            log.error(f"Unexpected error: {msg}")
            _stats["errors"] += 1
            success = False

        if success:
            _stats["booked"] = True
            _stats["last_state"] = "BOOKED"
            log.info("APPOINTMENT BOOKED! Exiting.")
            break

        # Periodic heartbeat
        if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
            _ntfy("TIE Checker Status", _heartbeat_summary(), ntfy_config, priority="low", tags="chart_with_upwards_trend")
            last_heartbeat = time.time()

        # Reset solver state for next run (new browser session)
        profile.capmonster_client = None
        profile.current_solver = None
        profile.first_load = os.environ.get("PRESERVE_COOKIES", "").lower() != "true"
        profile.bot_result = False
        profile._rate_limit_count = 0

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
