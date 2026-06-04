"""
Vendored from https://github.com/cita-bot/cita-bot (AGPL-3.0)
Modified for PAI: headless Docker, ntfy notifications, no os._exit, SMS HTTP endpoint.
All PAI modifications marked with # PAI: comments.
Resilience layer: selectors.py + resilience.py for fallback strategies and page state detection.
"""
import io
import json
import logging
import os
import random
import re
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime as dt
from enum import Enum
from json.decoder import JSONDecodeError
from typing import Any, Dict, Optional

import asyncio

import backoff
import requests
from capmonstercloudclient import CapMonsterClient, ClientOptions
from capmonstercloudclient.requests import RecaptchaV3ProxylessRequest, ImageToTextRequest
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select
from selenium.webdriver.support.wait import WebDriverWait

from .speaker import new_speaker
from .resilience import (
    find_element_resilient,
    find_elements_resilient,
    detect_page_state,
    submit_form_resilient,
    PageState,
    StepError,
    get_fallback_report,
    reset_fallback_tracking,
)
from .selectors import *

__all__ = [
    "try_cita",
    "start_with",
    "init_wedriver",
    "CustomerProfile",
    "DocType",
    "OperationType",
    "Office",
    "Province",
]

CYCLES = 144
REFRESH_PAGE_CYCLES = 12
DELAY = 30

speaker = new_speaker()


# PAI: classify cycle result for metrics logging
def _classify_result(result, driver):
    """Return a short label for the cycle outcome based on result and last log."""
    if result is True:
        return "SUCCESS"
    # Check recent page state for more detail
    try:
        ps = detect_page_state(driver)
        return ps.name
    except Exception:
        return "ERROR"


# PAI: fingerprint test mode — navigates to bot detection sites, screenshots, exits
def fingerprint_test():
    """Run fingerprint audit against detection sites. Set FINGERPRINT_TEST=1 to activate."""
    logging.info("=== FINGERPRINT TEST MODE ===")
    # PAI: use the SAME hardened options + WebExtension as the booking flow,
    # so this test actually validates the production fingerprint.
    options = _stealth_options()
    browser = webdriver.Firefox(options=options)
    _install_stealth_addon(browser)

    try:
        # Test 1: CreepJS
        logging.info("Navigating to CreepJS...")
        browser.get("https://abrahamjuliot.github.io/creepjs/")
        time.sleep(15)
        browser.save_screenshot("/app/data/fingerprint-creepjs.png")
        logging.info("CreepJS screenshot saved")

        # Test 2: SannyBot
        logging.info("Navigating to bot.sannysoft.com...")
        browser.get("https://bot.sannysoft.com/")
        time.sleep(5)
        browser.save_screenshot("/app/data/fingerprint-sannysoft.png")
        logging.info("SannyBot screenshot saved")

        # Log key signals
        webdriver_val = browser.execute_script("return navigator.webdriver")
        plugins_count = browser.execute_script("return navigator.plugins.length")
        ua = browser.execute_script("return navigator.userAgent")
        logging.info(f"[FINGERPRINT] navigator.webdriver={webdriver_val}")
        logging.info(f"[FINGERPRINT] navigator.plugins.length={plugins_count}")
        logging.info(f"[FINGERPRINT] userAgent={ua}")

        # Test 3: the REAL F5-protected site — the only test that proves the WAF
        # accepts us. PASS = booking page renders; FAIL = "requested URL was rejected".
        logging.info("Navigating to the live ICP+ site (F5 check)...")
        browser.get("https://icp.administracionelectronica.gob.es/icpplus/citar?p=43&locale=es")
        time.sleep(8)
        browser.save_screenshot("/app/data/fingerprint-icp-f5.png")
        state = detect_page_state(browser)
        if state == PageState.RATE_LIMITED:
            logging.error("[FINGERPRINT] F5 CHECK FAILED — still served the WAF rejection page")
        else:
            logging.info(f"[FINGERPRINT] F5 CHECK PASSED — page state={state.name} (not RATE_LIMITED)")
    finally:
        browser.quit()
    logging.info("=== FINGERPRINT TEST COMPLETE ===")


# PAI: diagnostic capture on any failure — screenshot + page state + context
def _capture_diagnostics(driver: webdriver, label: str, save_artifacts: bool = True):
    """Log page state, URL, title, and first 500 chars of body text. Save screenshot."""
    try:
        url = driver.current_url
        title = driver.title
        page_state = detect_page_state(driver)
        body = ""
        try:
            body_el = driver.find_element(By.TAG_NAME, "body")
            body = body_el.text[:500] if body_el else ""
        except Exception:
            pass
        logging.error(f"[DIAG:{label}] URL: {url}")
        logging.error(f"[DIAG:{label}] Title: {title}")
        logging.error(f"[DIAG:{label}] PageState: {page_state}")
        if body:
            logging.error(f"[DIAG:{label}] Body (500): {body[:500]}")
        try:
            page_source = driver.page_source
            source_len = len(page_source) if page_source else 0
            logging.error(f"[DIAG:{label}] page_source_len: {source_len}")
            if source_len < 2000:
                logging.error(f"[DIAG:{label}] page_source: {page_source}")
        except Exception:
            pass
        if save_artifacts:
            ts = dt.now().strftime("%Y%m%d-%H%M%S")
            driver.save_screenshot(f"/app/data/diag-{label}-{ts}.png")
    except Exception as e:
        logging.error(f"[DIAG:{label}] Diagnostic capture failed: {e}")


# PAI: ntfy notification helper — reads config from /secrets/ntfy.json
_ntfy_config = None


def _load_ntfy_config():
    global _ntfy_config
    if _ntfy_config is not None:
        return _ntfy_config
    secrets_dir = os.environ.get("SECRETS_DIR", "/secrets")
    path = os.path.join(secrets_dir, "ntfy.json")
    try:
        with open(path) as f:
            _ntfy_config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _ntfy_config = {}
    return _ntfy_config


def _ntfy(title, message, priority="default", tags=""):
    config = _load_ntfy_config()
    url = config.get("url", "")
    topic = config.get("topic", "")
    if not url or not topic:
        return
    try:
        requests.post(
            f"{url}/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=10,
        )
    except Exception as e:
        logging.error(f"ntfy send failed: {e}")


# PAI: SMS code HTTP endpoint for manual code entry
_sms_code_value = None
_sms_code_event = threading.Event()




class DocType(str, Enum):
    DNI = "dni"
    NIE = "nie"
    PASSPORT = "passport"


class OperationType(str, Enum):
    AUTORIZACION_DE_REGRESO = "20"
    BREXIT = "4094"
    CARTA_INVITACION = "4037"
    CERTIFICADOS_NIE = "4096"
    CERTIFICADOS_NIE_NO_COMUN = "4079"
    CERTIFICADOS_RESIDENCIA = "4049"
    CERTIFICADOS_UE = "4038"
    RECOGIDA_DE_TARJETA = "4036"
    SOLICITUD_ASILO = "4078"
    TOMA_HUELLAS = "4010"
    ASIGNACION_NIE = "4031"
    FINGERP_RINT = "4047"


class Office(str, Enum):
    BADALONA = "18"
    BARCELONA = "16"
    BARCELONA_MALLORCA = "14"
    CASTELLDEFELS = "19"
    CERDANYOLA = "20"
    CORNELLA = "21"
    ELPRAT = "23"
    GRANOLLERS = "28"
    HOSPITALET = "17"
    IGUALADA = "26"
    MANRESA = "38"
    MATARO = "27"
    MONTCADA = "31"
    RIPOLLET = "32"
    RUBI = "29"
    SABADELL = "30"
    SANTACOLOMA = "35"
    SANTADRIA = "33"
    SANTBOI = "24"
    SANTCUGAT = "34"
    SANTFELIU = "22"
    TERRASSA = "36"
    VIC = "37"
    VILADECANS = "25"
    VILAFRANCA = "46"
    VILANOVA = "39"
    OUE_SANTA_CRUZ = "1"
    PLAYA_AMERICAS = "2"
    PUERTO_CRUZ = "3"


class Province(str, Enum):
    A_CORUÑA = "15"
    ALBACETE = "2"
    ALICANTE = "3"
    ALMERÍA = "4"
    ARABA = "1"
    ASTURIAS = "33"
    ÁVILA = "5"
    BADAJOZ = "6"
    BARCELONA = "8"
    BIZKAIA = "48"
    BURGOS = "9"
    CÁCERES = "10"
    CÁDIZ = "11"
    CANTABRIA = "39"
    CASTELLÓN = "12"
    CEUTA = "51"
    CIUDAD_REAL = "13"
    CÓRDOBA = "14"
    CUENCA = "16"
    GIPUZKOA = "20"
    GIRONA = "17"
    GRANADA = "18"
    GUADALAJARA = "19"
    HUELVA = "21"
    HUESCA = "22"
    ILLES_BALEARS = "7"
    JAÉN = "23"
    LA_RIOJA = "26"
    LAS_PALMAS = "35"
    LEÓN = "24"
    LLEIDA = "25"
    LUGO = "27"
    MADRID = "28"
    MÁLAGA = "29"
    MELILLA = "52"
    MURCIA = "30"
    NAVARRA = "31"
    ORENSE = "32"
    PALENCIA = "34"
    PONTEVEDRA = "36"
    SALAMANCA = "37"
    S_CRUZ_TENERIFE = "38"
    SEGOVIA = "40"
    SEVILLA = "41"
    SORIA = "42"
    TARRAGONA = "43"
    TERUEL = "44"
    TOLEDO = "45"
    VALENCIA = "46"
    VALLADOLID = "47"
    ZAMORA = "49"
    ZARAGOZA = "50"


@dataclass
class CustomerProfile:
    name: str
    doc_type: DocType
    doc_value: str
    phone: str
    email: str
    province: Province = Province.BARCELONA
    operation_code: OperationType = OperationType.TOMA_HUELLAS
    country: str = "RUSIA"
    year_of_birth: Optional[str] = None
    offices: Optional[list] = field(default_factory=list)
    except_offices: Optional[list] = field(default_factory=list)
    capmonster_api_key: Optional[str] = None  # PAI: CapMonster Cloud API key
    auto_captcha: bool = True
    auto_office: bool = True
    min_date: Optional[str] = None
    max_date: Optional[str] = None
    min_time: Optional[str] = None
    max_time: Optional[str] = None
    save_artifacts: bool = False
    sms_webhook_token: Optional[str] = None
    wait_exact_time: Optional[list] = None
    reason_or_type: str = "solicitud de asilo"
    bot_result: bool = False
    first_load: Optional[bool] = True
    log_settings: Optional[dict] = field(default_factory=lambda: {"stream": sys.stdout})
    capmonster_client: Any = None  # PAI: CapMonster Cloud client instance
    current_solver: Any = None
    sms_code_port: int = 8080  # PAI: port for SMS code HTTP endpoint

    def __post_init__(self):
        if self.operation_code == OperationType.RECOGIDA_DE_TARJETA:
            assert len(self.offices) == 1, "Indicate office for card pickup"


def _stealth_options() -> "webdriver.FirefoxOptions":
    """Shared Firefox options for both the booking flow and fingerprint test.

    Runs HEADFUL under Xvfb (see Dockerfile xvfb-run wrapper) — headless Firefox
    is a detectable signal class for WAF bot-defense. navigator.webdriver hiding
    is handled by a document_start WebExtension (install_addon below), NOT by the
    dom.webdriver.enabled pref (geckodriver overrides it) or a one-shot
    execute_script (wiped on every navigation, loses the race vs inline detection).
    """
    options = webdriver.FirefoxOptions()

    # PAI: PERSISTENT profile on the /app/data volume (a host bind mount) so
    # cookies — including the long-lived Cl@ve gateway SSO — survive container
    # restarts. Avoids re-scanning the QR after every redeploy.
    try:
        os.makedirs("/app/data/ffprofile", exist_ok=True)
    except Exception:
        pass
    options.add_argument("-profile")
    options.add_argument("/app/data/ffprofile")

    # PAI: NO --headless. Process runs under xvfb-run, so Firefox is headful
    # against a virtual display — eliminates the headless-detection category.
    options.add_argument("--width=1920")
    options.add_argument("--height=1080")

    # Language preferences — Spanish (consistent with a Spain-based applicant)
    options.set_preference("intl.accept_languages", "es-ES,es,en")
    options.set_preference("general.useragent.locale", "es-ES")

    # PAI: present a current desktop Firefox UA instead of leaking the Debian
    # firefox-esr build string. Platform kept Linux for internal consistency.
    options.set_preference(
        "general.useragent.override",
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    )

    # Download directory
    options.set_preference("browser.download.dir", "/app/data")
    options.set_preference("browser.download.folderList", 2)

    # Auto-accept POST resubmission dialogs on page refresh
    options.set_preference("dom.confirm_repost.testing.always_accept", True)

    # Disable telemetry/update checks
    options.set_preference("toolkit.telemetry.enabled", False)
    options.set_preference("app.update.enabled", False)
    options.set_preference("browser.shell.checkDefaultBrowser", False)

    # PAI: removed dom.webdriver.enabled (geckodriver overrides it) and
    # useAutomationExtension (Chrome-only no-op). webdriver hiding is via the
    # WebExtension installed in init_wedriver / fingerprint_test.
    options.set_preference("privacy.resistFingerprinting", False)

    return options


def _install_stealth_addon(browser: webdriver) -> None:
    """Load the document_start WebExtension that persistently hides webdriver."""
    ext_path = os.path.join(os.path.dirname(__file__), "stealth_ext")
    try:
        browser.install_addon(ext_path, temporary=True)
    except Exception as e:
        logging.warning(f"stealth extension failed to load: {e}")


SESSION_FILE = "/app/data/session.json"


def _remote_fx_options():
    """FirefoxOptions for a REMOTE (Grid) session — same anti-detection prefs/UA
    as the local build, minus the local -profile path (profile lives in the
    browser container)."""
    o = webdriver.FirefoxOptions()
    o.add_argument("--width=1920")
    o.add_argument("--height=1080")
    o.set_preference("intl.accept_languages", "es-ES,es,en")
    o.set_preference("general.useragent.locale", "es-ES")
    o.set_preference(
        "general.useragent.override",
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    )
    o.set_preference("dom.confirm_repost.testing.always_accept", True)
    o.set_preference("toolkit.telemetry.enabled", False)
    o.set_preference("app.update.enabled", False)
    o.set_preference("browser.shell.checkDefaultBrowser", False)
    o.set_preference("privacy.resistFingerprinting", False)
    return o


def _attached_driver(executor, session_id):
    """Bind to an EXISTING geckodriver session WITHOUT issuing newSession —
    so we never spawn a second browser (which would destroy the in-memory Cl@ve
    session)."""
    from selenium.webdriver.remote.webdriver import WebDriver as _RWD

    class _Attached(_RWD):
        def start_session(self, capabilities):  # noqa: override — do NOT POST /session
            self.session_id = session_id
            self.caps = {}

    return _Attached(command_executor=executor, options=_remote_fx_options())


def _session_alive(executor, session_id):
    try:
        d = _attached_driver(executor, session_id)
        _ = d.current_url  # cheap round-trip to the live session
        return d
    except Exception as e:
        logging.warning(f"Saved session not alive: {str(e).splitlines()[0]}")
        return None


def _read_session_file():
    try:
        with open(SESSION_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def _write_session_file(executor, session_id):
    try:
        with open(SESSION_FILE, "w") as f:
            json.dump({"executor": executor, "session_id": session_id}, f)
    except Exception as e:
        logging.error(f"session file write failed: {e}")


def _install_stealth_addon_remote(driver, ext_dir):
    """Install the webdriver-hide WebExtension over Remote via geckodriver's
    moz/addon/install endpoint (the local install_addon helper isn't on Remote)."""
    import base64
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(ext_dir):
            for fn in files:
                p = os.path.join(root, fn)
                z.write(p, os.path.relpath(p, ext_dir))
    b64 = base64.b64encode(buf.getvalue()).decode()
    try:
        driver.command_executor._commands["INSTALL_ADDON"] = (
            "POST", "/session/$sessionId/moz/addon/install")
        driver.execute("INSTALL_ADDON", {"addon": b64, "temporary": True})
        logging.info("Remote stealth addon installed")
    except Exception as e:
        logging.warning(f"remote addon install failed: {str(e).splitlines()[0]}")


def init_wedriver(context: CustomerProfile):
    """Remote WebDriver against the long-lived cita-browser when WEBDRIVER_URL is
    set: reattach the saved session (preserving the Cl@ve login across code
    redeploys) or create a fresh one. Falls back to local headful Firefox otherwise."""
    url = os.environ.get("WEBDRIVER_URL")
    if url:
        saved = _read_session_file()
        if saved and saved.get("executor") == url and saved.get("session_id"):
            d = _session_alive(url, saved["session_id"])
            if d is not None:
                logging.info(f"Reattached live browser session {saved['session_id']}")
                return d
            logging.warning("Saved session dead — creating a fresh remote session")
        d = webdriver.Remote(command_executor=url, options=_remote_fx_options())
        _install_stealth_addon_remote(d, os.path.join(os.path.dirname(__file__), "stealth_ext"))
        _write_session_file(url, d.session_id)
        logging.info(f"Created fresh remote session {d.session_id} — Cl@ve QR re-scan needed")
        try:
            _ntfy("Cl@ve re-scan needed",
                  "Fresh browser session created — scan the QR (noVNC :7900 or the control flow).",
                  priority="high", tags="warning")
        except Exception:
            pass
        return d

    # Local fallback (no WEBDRIVER_URL): headful Xvfb Firefox in this container
    options = _stealth_options()
    browser = webdriver.Firefox(options=options)
    _install_stealth_addon(browser)
    return browser


def try_cita(context: CustomerProfile, cycles: int = CYCLES):
    driver = init_wedriver(context)
    return start_with(driver, context, cycles)  # PAI: return result


def start_with(driver: webdriver, context: CustomerProfile, cycles: int = CYCLES):
    if not logging.getLogger().handlers:
        logging.basicConfig(
            format="%(asctime)s - %(message)s", level=logging.INFO, **context.log_settings
        )
    if context.sms_webhook_token:
        delete_message(context.sms_webhook_token)

    operation_category = "icpplus"
    operation_param = "tramiteGrupo[1]"

    if context.province == Province.BARCELONA:
        operation_category = "icpplustieb"
        operation_param = "tramiteGrupo[0]"
    elif context.province in [
        Province.ALICANTE,
        Province.ILLES_BALEARS,
        Province.LAS_PALMAS,
        Province.S_CRUZ_TENERIFE,
    ]:
        operation_category = "icpco"
    elif context.province == Province.MADRID:
        operation_category = "icpplustiem"
    elif context.province == Province.MÁLAGA:
        operation_category = "icpco"
        operation_param = "tramiteGrupo[0]"
    elif context.province in [Province.MELILLA, Province.SEVILLA]:
        operation_param = "tramiteGrupo[0]"

    fast_forward_url = (
        f"https://icp.administracionelectronica.gob.es/{operation_category}/citar?p={context.province.value}&locale=es"
    )
    fast_forward_url2 = (
        f"https://icp.administracionelectronica.gob.es/{operation_category}/acInfo?{operation_param}={context.operation_code.value}"
    )

    success = False
    result = None
    for i in range(cycles):
        try:
            _ = driver.title
        except Exception:
            logging.warning("Browser session dead — recreating")
            try:
                driver.quit()
            except Exception:
                pass
            driver = init_wedriver(context)
        try:
            logging.info(f"\033[33m[Attempt {i + 1}/{cycles}]\033[0m")
            _cycle_start = time.time()
            result = cycle_cita(driver, context, fast_forward_url, fast_forward_url2)
            _cycle_dur = time.time() - _cycle_start
            _rl_count = getattr(context, '_rate_limit_count', 0)
            _fl = getattr(context, 'first_load', True)
            # PAI: structured metrics for experiment analysis
            logging.info(
                f"[METRICS] cycle={i+1} result={_classify_result(result, driver)} "
                f"duration_s={_cycle_dur:.1f} rate_limit_count={_rl_count} first_load={_fl}"
            )
        except KeyboardInterrupt:
            raise
        except TimeoutException:
            logging.error("Timeout exception")
        except Exception as e:
            # Log only the error message, not the full Selenium/Chrome stacktrace
            msg = str(e).split("\n")[0] if str(e) else type(e).__name__
            logging.error(f"Error: {msg}")
            continue

        if result:
            success = True
            logging.info("WIN")
            # PAI: ntfy success notification
            _ntfy(
                "TIE APPOINTMENT BOOKED!",
                "Cita confirmada y grabada. Check screenshots in /app/data/",
                priority="urgent",
                tags="white_check_mark,calendar",
            )
            break

    if not success:
        logging.error("FAIL - all cycles exhausted")
        # PAI: no ntfy here — the scheduler in run.py handles retry notifications

    # PAI: fallback report after cycle loop
    report = get_fallback_report()
    if report:
        logging.warning(f"Session fallback report:\n{report}")
        _ntfy("Fallback selectors used", report, priority="low", tags="mag")
    reset_fallback_tracking()

    # PAI: always quit driver, never os._exit
    try:
        driver.quit()
    except Exception:
        pass

    return success


# ── Operations that need country select ────────────────────────────
_OPS_NEED_COUNTRY = {
    OperationType.TOMA_HUELLAS,
    OperationType.SOLICITUD_ASILO,
    OperationType.ASIGNACION_NIE,
}

# ── Operations that need year_of_birth ─────────────────────────────
_OPS_NEED_YEAR_OF_BIRTH = {
    OperationType.SOLICITUD_ASILO,
    OperationType.ASIGNACION_NIE,
}


def fill_personal_info(driver: webdriver, context: CustomerProfile):
    """Unified personal info form — replaces 8 duplicated step2 functions."""
    needs_country = context.operation_code in _OPS_NEED_COUNTRY
    needs_year = context.operation_code in _OPS_NEED_YEAR_OF_BIRTH

    # For TOMA_HUELLAS: country select appears first, wait on it
    if context.operation_code == OperationType.TOMA_HUELLAS:
        el = find_element_resilient(driver, COUNTRY_SELECT, timeout=DELAY)
        if not el:
            logging.error("Timed out waiting for form to load")
            _capture_diagnostics(driver, "country-select-missing")
            return None
        select = Select(el)
        select.select_by_visible_text(context.country)
    else:
        # All other ops: wait for the doc number input to appear
        el = find_element_resilient(driver, DOC_NUMBER_INPUT, timeout=DELAY)
        if not el:
            logging.error("Timed out waiting for form to load")
            _capture_diagnostics(driver, "doc-input-missing")
            return None

    # Select document type radio button
    if context.doc_type == DocType.PASSPORT:
        radio = find_element_resilient(driver, DOC_TYPE_PASSPORT)
        if radio:
            radio.send_keys(Keys.SPACE)
    elif context.doc_type == DocType.NIE:
        radio = find_element_resilient(driver, DOC_TYPE_NIE)
        if radio:
            radio.send_keys(Keys.SPACE)
    elif context.doc_type == DocType.DNI:
        # Only carta_invitacion and certificados support DNI in the original
        radio = find_element_resilient(driver, DOC_TYPE_DNI)
        if radio:
            radio.send_keys(Keys.SPACE)

    # Fill in doc number + name (+ year_of_birth if needed)
    element = find_element_resilient(driver, DOC_NUMBER_INPUT)
    if not element:
        logging.error("Could not find document number input")
        _capture_diagnostics(driver, "doc-input-missing-2nd")
        return None

    if needs_year and context.year_of_birth:
        element.send_keys(
            context.doc_value, Keys.TAB, context.name, Keys.TAB, context.year_of_birth
        )
    else:
        element.send_keys(context.doc_value, Keys.TAB, context.name)

    # Country select for ops that need it AFTER doc fields (SOLICITUD_ASILO, ASIGNACION_NIE)
    if needs_country and context.operation_code != OperationType.TOMA_HUELLAS:
        country_el = find_element_resilient(driver, COUNTRY_SELECT)
        if country_el:
            select = Select(country_el)
            select.select_by_visible_text(context.country)

    return True


def wait_exact_time(driver: webdriver, context: CustomerProfile):
    if context.wait_exact_time:
        WebDriverWait(driver, 1200).until(
            lambda _x: [dt.now().minute, dt.now().second] in context.wait_exact_time
        )


def body_text(driver: webdriver):
    try:
        WebDriverWait(driver, DELAY).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        return driver.find_element(By.TAG_NAME, "body").text
    except TimeoutException:
        logging.info("Timed out waiting for body to load")
        return ""


def process_captcha(driver: webdriver, context: CustomerProfile):
    if context.auto_captcha:
        if not context.capmonster_api_key:
            logging.error("CapMonster API key is empty")
            return None
        recaptcha_key = find_element_resilient(driver, RECAPTCHA_SITE_KEY, timeout=2)
        captcha_imgs = find_elements_resilient(driver, CAPTCHA_IMAGE, timeout=2)
        if recaptcha_key:
            captcha_result = solve_recaptcha(driver, context)
        elif captcha_imgs:
            captcha_result = solve_image_captcha(driver, context)
        else:
            captcha_result = True
        if not captcha_result:
            return None
    else:
        # PAI: in headless mode, send ntfy instead of audio alarm
        logging.info("CAPTCHA detected — manual solving required")
        _ntfy(
            "CAPTCHA needs manual solving",
            "auto_captcha is disabled. Enable it with CAPMONSTER_API_KEY.",
            priority="high",
            tags="warning",
        )
        return None  # PAI: can't do input() in Docker
    return True


def _get_capmonster_client(context: CustomerProfile) -> CapMonsterClient:
    """Create a fresh CapMonster Cloud client per call — no caching."""
    return CapMonsterClient(options=ClientOptions(api_key=context.capmonster_api_key))


def solve_recaptcha(driver: webdriver, context: CustomerProfile):
    site_key_el = find_element_resilient(driver, RECAPTCHA_SITE_KEY)
    action_el = find_element_resilient(driver, RECAPTCHA_ACTION)
    if not site_key_el or not action_el:
        logging.error("Could not find reCAPTCHA elements")
        return None
    site_key = site_key_el.get_attribute("value")
    page_action = action_el.get_attribute("value")
    logging.info(f"CapMonster: reCAPTCHA v3 — site_key={site_key}, action={page_action}")

    client = _get_capmonster_client(context)
    context.current_solver = "recaptcha_v3"

    request = RecaptchaV3ProxylessRequest(
        websiteUrl="https://icp.administracionelectronica.gob.es",
        websiteKey=site_key,
        pageAction=page_action,
        minScore=0.9,
    )

    try:
        responses = asyncio.run(client.solve_captcha(request))
        g_response = responses.get("gRecaptchaResponse", "")
        if g_response:
            logging.info(f"CapMonster: solved reCAPTCHA v3 ({len(g_response)} chars)")
            recaptcha_resp = find_element_resilient(driver, RECAPTCHA_RESPONSE, timeout=2)
            if recaptcha_resp:
                driver.execute_script(
                    f"arguments[0].value = '{g_response}'", recaptcha_resp
                )
            else:
                # Fallback to direct ID approach
                driver.execute_script(
                    f"document.getElementById('g-recaptcha-response').value = '{g_response}'"
                )
            return True
        else:
            logging.error(f"CapMonster: empty response — {responses}")
            return None
    except Exception as e:
        logging.error(f"CapMonster reCAPTCHA error: {str(e).split(chr(10))[0]}")
        return None


def solve_image_captcha(driver: webdriver, context: CustomerProfile):
    context.current_solver = "image_to_text"

    try:
        imgs = find_elements_resilient(driver, CAPTCHA_IMAGE)
        if not imgs:
            logging.error("No captcha images found")
            return None
        img = imgs[0]
        src = img.get_attribute("src")
        if src.startswith("data:"):
            img_data = src.split(",", 1)[1].strip()
        else:
            import base64 as b64module
            resp = requests.get(src, timeout=10)
            img_data = b64module.b64encode(resp.content).decode()
        logging.info("CapMonster: solving image CAPTCHA...")

        client = _get_capmonster_client(context)
        request = ImageToTextRequest(body=img_data)

        responses = asyncio.run(client.solve_captcha(request))
        captcha_text = responses.get("text", "")
        if captcha_text:
            logging.info(f"CapMonster: image CAPTCHA text: {captcha_text}")
            element = find_element_resilient(driver, CAPTCHA_TEXT_INPUT)
            if element:
                element.send_keys(captcha_text)
            else:
                logging.error("Could not find captcha text input")
                return None
            return True
        else:
            logging.error(f"CapMonster: empty image response — {responses}")
            return None
    except Exception as e:
        logging.error(f"CapMonster image CAPTCHA error: {str(e).split(chr(10))[0]}")
        return None


def find_best_date_slots(driver: webdriver, context: CustomerProfile):
    try:
        els = find_elements_resilient(driver, SLOT_DATES)
        dates = sorted([*map(lambda x: x.text, els)])
        best_date = find_best_date(dates, context)
        if best_date:
            return dates.index(best_date) + 1
    except Exception as e:
        logging.error(e)
    return None


def find_best_date(dates, context: CustomerProfile):
    if not context.min_date and not context.max_date:
        return dates[0]
    pattern = re.compile(r"\d{2}/\d{2}/\d{4}")
    date_format = "%d/%m/%Y"
    for date in dates:
        try:
            found = pattern.findall(date)[0]
            if found:
                appt_date = dt.strptime(found, date_format)
                if context.min_date:
                    if appt_date < dt.strptime(context.min_date, date_format):
                        continue
                if context.max_date:
                    if appt_date > dt.strptime(context.max_date, date_format):
                        continue
                return date
        except Exception as e:
            logging.error(e)
            continue
    logging.info(f"Nothing found for dates {context.min_date} - {context.max_date}")
    return None


def select_office(driver: webdriver, context: CustomerProfile):
    if not context.auto_office:
        logging.info("auto_office disabled — skipping office selection")
        return None  # PAI: can't do input() in Docker
    else:
        el = find_element_resilient(driver, OFFICE_SELECT)
        if not el:
            logging.error("Could not find office select element")
            _capture_diagnostics(driver, "office-select-missing")
            return None
        select = Select(el)
        if context.save_artifacts:
            offices_path = os.path.join(
                "/app/data", f"offices-{dt.now()}.html".replace(":", "-")
            )
            with io.open(offices_path, "w", encoding="utf-8") as f:
                f.write(el.get_attribute("innerHTML"))
        if context.offices:
            for office in context.offices:
                try:
                    select.select_by_value(office.value)
                    return True
                except Exception as e:
                    logging.error(e)
                    if context.operation_code == OperationType.RECOGIDA_DE_TARJETA:
                        return None
        for i in range(5):
            options = list(
                filter(lambda o: o.get_attribute("value") != "", select.options)
            )
            default_count = len(select.options)
            first_element = 0 if len(options) == default_count else 1
            select.select_by_index(random.randint(first_element, default_count - 1))
            if el.get_attribute("value") not in context.except_offices:
                return True
            continue
        return None


def handle_validation_page(driver: webdriver, context: CustomerProfile):
    """Handle the acValidarEntrada intermediate validation page (ICP+ Step 4/8).

    After personal info submission, the server sometimes returns this page
    with a btnEnviar button that must be clicked to proceed. If the page
    is blank (F5 WAF challenge), wait and retry once.
    """
    page_state = detect_page_state(driver)

    if page_state != PageState.VALIDATION_PAGE:
        return True

    logging.info("[Step 1.5/6] Validation page (acValidarEntrada) detected")

    # Log page source for F5 challenge diagnosis
    try:
        page_source = driver.page_source
        source_len = len(page_source) if page_source else 0
        logging.info(f"[DIAG:validation-page] page_source length: {source_len}")
        if source_len < 1000:
            logging.info(f"[DIAG:validation-page] page_source: {page_source}")
        else:
            logging.info(f"[DIAG:validation-page] page_source (first 1000): {page_source[:1000]}")
    except Exception:
        pass

    # Try to find and click btnEnviar
    btn = find_element_resilient(driver, BTN_ENVIAR, timeout=5)
    if btn:
        logging.info("[Step 1.5/6] Clicking btnEnviar on validation page")
        try:
            driver.execute_script("arguments[0].scrollIntoView(true);", btn)
            time.sleep(0.3)
            btn.click()
        except Exception:
            driver.execute_script("arguments[0].click();", btn)
        time.sleep(1)
        return True

    # Blank page — likely F5 JS challenge. Wait and check again.
    logging.warning("[Step 1.5/6] Blank validation page (no btnEnviar) — possible F5 challenge")
    _capture_diagnostics(driver, "validation-page-blank")

    time.sleep(5)

    # Re-check: did the challenge resolve and expose btnEnviar?
    btn = find_element_resilient(driver, BTN_ENVIAR, timeout=5)
    if btn:
        logging.info("[Step 1.5/6] btnEnviar appeared after wait — clicking")
        try:
            driver.execute_script("arguments[0].scrollIntoView(true);", btn)
            time.sleep(0.3)
            btn.click()
        except Exception:
            driver.execute_script("arguments[0].click();", btn)
        time.sleep(1)
        return True

    # Check if page state changed (F5 may have redirected)
    new_state = detect_page_state(driver)
    if new_state != PageState.VALIDATION_PAGE and new_state != PageState.UNKNOWN:
        logging.info(f"[Step 1.5/6] Page transitioned to {new_state.name} after wait")
        return True

    logging.error("[Step 1.5/6] Validation page stuck — aborting cycle")
    _capture_diagnostics(driver, "validation-page-stuck")
    return None


def office_selection(driver: webdriver, context: CustomerProfile):
    # Only submit if we're not already on a recognized flow page
    # (handle_validation_page may have already advanced us past the submit step)
    pre_state = detect_page_state(driver)
    if pre_state not in (PageState.OFFICE_SELECTION, PageState.NO_APPOINTMENTS,
                         PageState.RATE_LIMITED, PageState.INITIAL_LANDING):
        submit_form_resilient(driver, "enviar('solicitud');", BTN_ENVIAR)
    for i in range(REFRESH_PAGE_CYCLES):
        resp_text = body_text(driver)
        page_state = detect_page_state(driver, resp_text)

        if page_state == PageState.OFFICE_SELECTION:
            logging.info("[Step 2/6] Office selection")
            time.sleep(0.3)
            btn = find_element_resilient(driver, BTN_SIGUIENTE, timeout=DELAY)
            if not btn:
                logging.error("Timed out waiting for offices to load")
                _capture_diagnostics(driver, "offices-siguiente-missing")
                return None
            res = select_office(driver, context)
            if res is None:
                time.sleep(5)
                driver.refresh()
                continue
            btn = find_element_resilient(driver, BTN_SIGUIENTE)
            if btn:
                try:
                    driver.execute_script("arguments[0].scrollIntoView(true);", btn)
                    time.sleep(0.3)
                    btn.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", btn)
            return True
        elif page_state == PageState.NO_APPOINTMENTS:
            logging.info("No appointments available — starting fresh cycle")
            # PAI DIAG: capture the EXACT page (screenshot + full rendered HTML +
            # URL) to settle whether this is a genuine no-citas page (trámite not
            # registered) or a real availability page being misread.
            try:
                ts = dt.now().strftime("%Y%m%d-%H%M%S")
                driver.save_screenshot(f"/app/data/no-appointments-{ts}.png")
                with io.open(f"/app/data/no-appointments-{ts}.html", "w", encoding="utf-8") as f:
                    f.write(driver.page_source or "")
                logging.info(f"[DIAG:no-appointments] url={driver.current_url} saved no-appointments-{ts}.png/.html")
            except Exception as e:
                logging.error(f"[DIAG:no-appointments] capture failed: {e}")
            return None
        elif page_state == PageState.RATE_LIMITED:
            logging.warning("Rate limited during office selection — aborting cycle")
            _capture_diagnostics(driver, "rate-limited-office-selection")
            _rate_limit_count = getattr(context, '_rate_limit_count', 0) + 1
            context._rate_limit_count = _rate_limit_count
            wait_min = min(5 * _rate_limit_count, 20)
            wait_sec = int(wait_min * 60 + random.uniform(0, 60))
            logging.warning(f"Rate limited (hit #{_rate_limit_count}) — sleeping {wait_sec}s ({wait_min}+ min)")
            time.sleep(wait_sec)
            context.first_load = True
            return None
        elif page_state == PageState.INITIAL_LANDING:
            logging.warning("Session reset — bounced back to province selection")
            _capture_diagnostics(driver, "session-reset-office")
            return None
        else:
            logging.info("[Step 2/6] Office selection -> unexpected state")
            _capture_diagnostics(driver, "office-unexpected-state")
            return None


def phone_mail(driver: webdriver, context: CustomerProfile):
    element = find_element_resilient(driver, PHONE_INPUT, timeout=DELAY)
    if not element:
        logging.error("Timed out waiting for contact info page to load")
        _capture_diagnostics(driver, "phone-input-missing")
        return None
    logging.info("[Step 3/6] Contact info")
    element.send_keys(context.phone)
    try:
        email_one = find_element_resilient(driver, EMAIL_ONE)
        if email_one:
            email_one.send_keys(context.email)
        email_two = find_element_resilient(driver, EMAIL_TWO)
        if email_two:
            email_two.send_keys(context.email)
    except Exception:
        pass
    add_reason(driver, context)
    submit_form_resilient(driver, "enviar();", BTN_ENVIAR)
    return cita_selection(driver, context)


def confirm_appointment(driver: webdriver, context: CustomerProfile):
    chk = find_element_resilient(driver, CHK_TOTAL)
    if chk:
        chk.send_keys(Keys.SPACE)
    chk_email = find_element_resilient(driver, CHK_EMAIL)
    if chk_email:
        chk_email.send_keys(Keys.SPACE)
    btn = find_element_resilient(driver, BTN_CONFIRMAR)
    if btn:
        try:
            driver.execute_script("arguments[0].scrollIntoView(true);", btn)
            time.sleep(0.3)
            btn.click()
        except Exception:
            driver.execute_script("arguments[0].click();", btn)
    resp_text = body_text(driver)
    ctime = dt.now()

    page_state = detect_page_state(driver, resp_text)

    if page_state == PageState.SUCCESS:
        context.bot_result = True
        code_el = find_element_resilient(driver, JUSTIFICANTE)
        code = code_el.text if code_el else "UNKNOWN"
        logging.info(f"[Step 6/6] Justificante cita: {code}")
        # PAI: save screenshot and notify
        if context.save_artifacts:
            image_name = f"/app/data/CONFIRMED-CITA-{ctime}.png".replace(":", "-")
            driver.save_screenshot(image_name)
        _ntfy(
            "CITA CONFIRMADA!",
            f"Justificante: {code}",
            priority="urgent",
            tags="white_check_mark,tada",
        )
        return True
    elif page_state == PageState.SMS_CODE_WRONG:
        logging.error("Incorrect code entered")
        _ntfy("SMS code incorrect", "The SMS code was wrong. Will retry.", priority="high", tags="x")
    else:
        if context.save_artifacts:
            error_name = f"/app/data/error-{ctime}.png".replace(":", "-")
            driver.save_screenshot(error_name)
        _ntfy("Booking error", f"Unexpected page at confirmation step (state={page_state.name}). Check screenshots.", priority="high", tags="warning")
    return None


def log_backoff(details):
    logging.error(
        f"Unable to load initial page, backing off {details['wait']:0.1f} seconds"
    )


def _select_and_submit_tramite(driver: webdriver, context: CustomerProfile, op_param, op_val):
    """Replicate the REAL human portada flow: pick the office (sede) + trámite on
    the form and submit via envia() — instead of GET-ing the acInfo deep-link,
    which omits the office and the per-session form token and makes the server
    return 'no hay citas'. The site re-renders the selection page (selectSede)
    once with the trámite reset, so re-select and re-submit (loop)."""
    # Select office (sede) + trámite and submit via envia() — the JS submit the
    # page's own Aceptar button calls. This version advanced cleanly past the
    # selection page; real-click attempts got intercepted by the header overlay.
    for attempt in range(4):
        tramite = driver.find_elements(By.NAME, op_param)
        if not tramite:
            logging.info(f"Trámite+office submitted (attempt {attempt}); advanced")
            return
        try:
            sede = driver.find_elements(By.NAME, "sede")
            if sede:
                try:
                    Select(sede[0]).select_by_value("99")  # "Cualquier oficina"
                except Exception:
                    pass
            Select(tramite[0]).select_by_value(op_val)
        except Exception as e:
            logging.error(f"sede/trámite selection failed: {e}")
            return
        time.sleep(random.uniform(0.8, 1.8))
        try:
            driver.execute_script("envia();")
        except Exception:
            submit_form_resilient(driver, "envia();", BTN_ENTRAR)
        time.sleep(random.uniform(2, 4))
        # Dismiss the "Por favor, selecciona un trámite" modal if it appears
        try:
            mbtns = driver.find_elements(By.CSS_SELECTOR, ".jconfirm-buttons button")
            if mbtns:
                mbtns[0].click()
                time.sleep(0.6)
        except Exception:
            pass
        if not driver.find_elements(By.NAME, op_param):
            logging.info(f"Trámite+office submitted (attempt {attempt + 1}); advanced")
            return
    logging.warning("Still on selection page after 4 submit attempts")


@backoff.on_exception(
    backoff.expo,
    TimeoutException,
    base=2,
    factor=30,
    max_value=600,
    max_tries=(10 if os.environ.get("CITA_TEST") else 5),
    on_backoff=log_backoff,
    logger=None,
)
def initial_page(
    driver: webdriver, context: CustomerProfile, fast_forward_url, fast_forward_url2
):
    # PAI: do NOT wipe cookies. F5 BIG-IP ASM sets trust cookies (TSPD/TS01...)
    # after its JS challenge. Wiping them every cycle makes each attempt look like
    # a cold, untrusted client — which is exactly what triggers the
    # "requested URL was rejected" block on the FIRST attempt.
    driver.set_page_load_timeout(300 if context.first_load else 50)
    time.sleep(1)
    driver.get(fast_forward_url)
    time.sleep(random.uniform(1.5, 4))  # PAI: randomize wait to look more human

    # PAI: check first page for rate limiting using detect_page_state
    page_state = detect_page_state(driver)
    if page_state == PageState.RATE_LIMITED:
        _rate_limit_count = getattr(context, '_rate_limit_count', 0) + 1
        context._rate_limit_count = _rate_limit_count
        # Exponential: 5min, 10min, 15min, cap 20min
        wait_min = min(5 * _rate_limit_count, 20)
        wait_sec = int(wait_min * 60 + random.uniform(0, 60))
        logging.warning(f"Rate limited (hit #{_rate_limit_count}) — sleeping {wait_sec}s ({wait_min}+ min)")
        time.sleep(wait_sec)
        context.first_load = True
        raise TimeoutException

    # PAI: do NOT clear localStorage/sessionStorage either — F5 ASM challenge
    # state can live there; clearing it forces a fresh (untrusted) challenge.

    time.sleep(random.uniform(1, 3))  # PAI: pause between navigations
    # PAI: instead of GET-ing the acInfo deep-link (which skips the office and the
    # per-session form token → "no hay citas"), select office + trámite on the
    # portada form and submit via envia(), like a human.
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(fast_forward_url2).query)
    op_param = next(iter(qs.keys()), "tramiteGrupo[1]")
    op_val = qs.get(op_param, [context.operation_code.value])[0]
    _select_and_submit_tramite(driver, context, op_param, op_val)
    time.sleep(random.uniform(1.5, 4))

    # PAI: detect page state after second navigation
    page_state = detect_page_state(driver)

    if page_state == PageState.RATE_LIMITED:
        _rate_limit_count = getattr(context, '_rate_limit_count', 0) + 1
        context._rate_limit_count = _rate_limit_count
        wait_min = min(5 * _rate_limit_count, 20)
        wait_sec = int(wait_min * 60 + random.uniform(0, 60))
        logging.warning(f"Rate limited (hit #{_rate_limit_count}) — sleeping {wait_sec}s ({wait_min}+ min)")
        time.sleep(wait_sec)
        context.first_load = True
        raise TimeoutException

    if page_state == PageState.ERROR:
        logging.error(f"System error detected. Page title: {driver.title}")
        logging.error(f"Current URL: {driver.current_url}")
        if context.save_artifacts:
            driver.save_screenshot(f"/app/data/initial-fail-{dt.now()}.png".replace(":", "-"))
        context.first_load = True
        raise TimeoutException

    # Accept INITIAL_LANDING or known flow states as success
    if page_state in (PageState.INITIAL_LANDING, PageState.INSTRUCTIONS,
                      PageState.PERSONAL_INFO, PageState.OFFICE_SELECTION):
        context.first_load = False
        return

    if page_state == PageState.UNKNOWN:
        logging.warning(f"Unknown page state after navigation. Title: {driver.title}, URL: {driver.current_url}")
        if context.save_artifacts:
            driver.save_screenshot(f"/app/data/unknown-state-{dt.now()}.png".replace(":", "-"))
        context.first_load = True
        raise TimeoutException

    # Fallback: verify body text contains expected content
    resp_text = body_text(driver)
    if "CITA PREVIA" not in resp_text:
        logging.error(f"Expected 'CITA PREVIA' not found. Page title: {driver.title}")
        logging.error(f"Current URL: {driver.current_url}")
        logging.error(f"Body text (first 500 chars): {resp_text[:500]}")
        if context.save_artifacts:
            driver.save_screenshot(f"/app/data/initial-fail-{dt.now()}.png".replace(":", "-"))
        context.first_load = True
        raise TimeoutException
    context.first_load = False
    context._rate_limit_count = 0  # Reset on success


def cycle_cita(
    driver: webdriver, context: CustomerProfile, fast_forward_url, fast_forward_url2
):
    initial_page(driver, context, fast_forward_url, fast_forward_url2)

    btn = find_element_resilient(driver, BTN_ENTRAR, timeout=DELAY)
    if not btn:
        logging.error("Timed out waiting for Instructions page to load")
        _capture_diagnostics(driver, "entrar-not-found", context.save_artifacts)
        return None

    if os.environ.get("CITA_TEST") and context.operation_code == OperationType.TOMA_HUELLAS:
        logging.info("Instructions page loaded")
        return True

    # Wait for clickable, scroll into view, then click
    try:
        WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.ID, btn.get_attribute("id") or "btnEntrar")))
        driver.execute_script("arguments[0].scrollIntoView(true);", btn)
        time.sleep(0.3)
        btn.click()
    except Exception:
        # Fallback: JS click bypasses overlay/visibility issues
        driver.execute_script("arguments[0].click();", btn)
    logging.info("[Step 1/6] Personal info")

    # PAI: check page state before filling form — WAF may have blocked this navigation
    time.sleep(1)
    page_state = detect_page_state(driver)
    if page_state == PageState.RATE_LIMITED:
        logging.warning("Rate limited after Entrar click — aborting cycle")
        _capture_diagnostics(driver, "rate-limited-after-entrar", context.save_artifacts)
        _rate_limit_count = getattr(context, '_rate_limit_count', 0) + 1
        context._rate_limit_count = _rate_limit_count
        wait_min = min(5 * _rate_limit_count, 20)
        wait_sec = int(wait_min * 60 + random.uniform(0, 60))
        logging.warning(f"Rate limited (hit #{_rate_limit_count}) — sleeping {wait_sec}s ({wait_min}+ min)")
        time.sleep(wait_sec)
        context.first_load = True
        return None
    if page_state == PageState.INITIAL_LANDING:
        logging.warning("Bounced back to landing page after Entrar — session dead")
        _capture_diagnostics(driver, "session-reset-after-entrar", context.save_artifacts)
        return None

    success = fill_personal_info(driver, context)
    if not success:
        return None

    time.sleep(0.5)
    enviar_btn = find_element_resilient(driver, BTN_ENVIAR)
    if enviar_btn:
        try:
            driver.execute_script("arguments[0].scrollIntoView(true);", enviar_btn)
            time.sleep(0.3)
            enviar_btn.click()
        except Exception:
            driver.execute_script("arguments[0].click();", enviar_btn)
    else:
        logging.error("Could not find enviar button")
        _capture_diagnostics(driver, "enviar-missing", context.save_artifacts)
        return None

    # Handle acValidarEntrada intermediate page if present
    time.sleep(1)
    validation_result = handle_validation_page(driver, context)
    if validation_result is None:
        return None

    # Wait for Solicitar page (non-required element, short timeout)
    find_element_resilient(driver, BTN_CONSULTAR, timeout=7)

    try:
        wait_exact_time(driver, context)
    except TimeoutException:
        logging.error("Timed out waiting for exact time")
        _capture_diagnostics(driver, "exact-time-timeout", context.save_artifacts)
        return None

    selection_result = office_selection(driver, context)
    if selection_result is None:
        return None

    return phone_mail(driver, context)


def cita_selection(driver: webdriver, context: CustomerProfile):
    resp_text = body_text(driver)
    page_state = detect_page_state(driver, resp_text)

    if page_state == PageState.SLOT_SELECTION_5MIN:
        logging.info("[Step 4/6] Cita attempt -> selection hit!")
        # PAI: notify that we found a slot
        _ntfy(
            "SLOT FOUND!",
            "Appointment slot detected! Attempting to book...",
            priority="urgent",
            tags="rotating_light",
        )
        if context.save_artifacts:
            driver.save_screenshot(
                f"/app/data/citas-{dt.now()}.png".replace(":", "-")
            )
        position = find_best_date_slots(driver, context)
        if not position:
            _ntfy(
                "Step 4 — slot seen, no bookable date",
                "Reached the slot page but no date matched filters / slot already taken (likely external).",
                priority="high",
                tags="warning",
            )
            return None
        time.sleep(0.5)
        success = process_captcha(driver, context)
        if not success:
            return None
        try:
            radios = find_elements_resilient(driver, SLOT_RADIOS)
            if radios and position - 1 < len(radios):
                radios[position - 1].send_keys(Keys.SPACE)
            else:
                logging.error(f"Radio button at position {position} not found")
        except Exception as e:
            logging.error(e)
        submit_form_resilient(driver, "envia();")
        time.sleep(0.5)
        try:
            WebDriverWait(driver, 3).until(EC.alert_is_present())
            driver.switch_to.alert.accept()
        except (TimeoutException, Exception):
            pass
    elif page_state == PageState.SLOT_SELECTION_TABLE:
        logging.info("[Step 4/6] Cita attempt -> selection hit!")
        _ntfy(
            "SLOT FOUND!",
            "Appointment slot detected! Attempting to book...",
            priority="urgent",
            tags="rotating_light",
        )
        if context.save_artifacts:
            driver.save_screenshot(
                f"/app/data/citas-{dt.now()}.png".replace(":", "-")
            )
        try:
            date_els = find_elements_resilient(driver, SLOT_TABLE_HEADERS)
            dates = sorted([*map(lambda x: x.text, date_els)])
            slots: Dict[str, list] = {}
            slot_table_el = find_element_resilient(driver, SLOT_TABLE)
            if not slot_table_el:
                logging.error("Could not find slot table")
                return None
            slot_tbody = slot_table_el.find_element(By.CSS_SELECTOR, "tbody")
            for row in slot_tbody.find_elements(By.CSS_SELECTOR, "tr"):
                appt_time = row.find_elements(By.TAG_NAME, "th")[0].text
                if context.min_time:
                    if appt_time < context.min_time:
                        continue
                if context.max_time:
                    if appt_time > context.max_time:
                        break
                for idx, cell in enumerate(row.find_elements(By.TAG_NAME, "td")):
                    try:
                        if slots.get(dates[idx]):
                            continue
                        hueco_els = cell.find_elements(By.CSS_SELECTOR, "[id^=HUECO]")
                        if not hueco_els:
                            continue
                        slot = hueco_els[0].get_attribute("id")
                        slots[dates[idx]] = [slot]
                    except Exception:
                        pass
            best_date = find_best_date(sorted(slots), context)
            if not best_date:
                _ntfy(
                    "Step 4 — slot seen, no bookable date",
                    "Reached the slot table but no date matched filters / slot already taken (likely external).",
                    priority="high",
                    tags="warning",
                )
                return None
            slot = slots[best_date][0]
            time.sleep(0.5)
            success = process_captcha(driver, context)
            if not success:
                return None
            submit_form_resilient(
                driver,
                f"confirmarHueco({{id: '{slot}'}}, {slot[5:]});"
            )
            try:
                WebDriverWait(driver, 3).until(EC.alert_is_present())
                driver.switch_to.alert.accept()
            except (TimeoutException, Exception):
                pass
        except Exception as e:
            logging.error(e)
            return None
    else:
        logging.info("[Step 4/6] Cita attempt -> missed selection")
        _capture_diagnostics(driver, "cita-missed-selection")
        # PAI: telemetry — make the (rare) live attempt diagnosable.
        # NO_APPOINTMENTS / RATE_LIMITED = external (slot gone / WAF).
        # UNKNOWN = internal (page changed / stale selectors — check the screenshot).
        _ntfy(
            "Step 4 — no slot booked",
            f"Reached cita page, state={page_state.name}. See diag-cita-missed-selection screenshot.",
            priority="high",
            tags="warning",
        )
        return None

    resp_text = body_text(driver)
    page_state = detect_page_state(driver, resp_text)

    if page_state == PageState.CONFIRMATION:
        logging.info("[Step 5/6] Cita attempt -> confirmation hit!")
        # PAI: CapMonster Cloud doesn't have report_correct, skip
        sms_verification = find_element_resilient(driver, SMS_CODE_INPUT, timeout=3)

        if context.sms_webhook_token:
            # Path A: webhook.site SMS automation
            if sms_verification:
                code = get_code(context)
                if code:
                    logging.info("Received SMS code")
                    sms_verification = find_element_resilient(driver, SMS_CODE_INPUT)
                    if sms_verification:
                        sms_verification.send_keys(code)
            confirm_appointment(driver, context)
            if context.save_artifacts:
                driver.save_screenshot(
                    f"/app/data/FINAL-SCREEN-{dt.now()}.png".replace(":", "-")
                )
            # PAI: return result instead of os._exit
            return context.bot_result or None
        else:
            # Path B: no webhook — use HTTP endpoint for SMS code
            if not sms_verification:
                # No SMS needed — auto-confirm
                confirm_appointment(driver, context)
                return context.bot_result or None
            else:
                # PAI: SMS needed — wait for code via unified health server in run.py
                global _sms_code_value
                sms_port = context.sms_code_port
                # PAI: the in-container port (sms_port) is NOT the host-published port.
                # Use SMS_PUBLIC_URL (set in compose) so the link is reachable from a
                # phone on the LAN, not just localhost on the NUC.
                sms_url = os.environ.get("SMS_PUBLIC_URL", "http://172.16.10.25:8085").rstrip("/")
                _ntfy(
                    "SMS CODE NEEDED!",
                    f"Open {sms_url}/code to enter the SMS code. You have 5 minutes.",
                    priority="urgent",
                    tags="rotating_light,iphone",
                )
                logging.info(f"Waiting for SMS code via health server on port {sms_port}...")
                _sms_code_event.clear()
                _sms_code_value = None
                _sms_code_event.wait(timeout=300)
                code = _sms_code_value
                if code:
                    logging.info("Received SMS code via HTTP")
                    sms_el = find_element_resilient(driver, SMS_CODE_INPUT, timeout=5)
                    if sms_el:
                        sms_el.send_keys(code)
                    confirm_appointment(driver, context)
                    return context.bot_result or None
                else:
                    logging.error("SMS code timeout — no code received in 5 minutes")
                    _ntfy(
                        "SMS code expired",
                        "No SMS code entered within 5 minutes. Appointment lost.",
                        priority="high",
                        tags="x",
                    )
                    return None
    else:
        logging.info("[Step 5/6] Cita attempt -> missed confirmation")
        _capture_diagnostics(driver, "missed-confirmation")
        # PAI: CapMonster Cloud doesn't have report_incorrect, skip
        if context.save_artifacts:
            driver.save_screenshot(
                f"/app/data/failed-confirmation-{dt.now()}.png".replace(":", "-")
            )
        # PAI: telemetry — slot was submitted but confirmation page not reached.
        # Slot taken (external) or page changed (internal). State tells which.
        _ntfy(
            "Step 5 — confirmation not reached",
            f"Submitted slot, landed on state={page_state.name}. See diag-missed-confirmation screenshot.",
            priority="high",
            tags="warning",
        )
        return None


def get_messages(sms_webhook_token):
    try:
        url = f"https://webhook.site/token/{sms_webhook_token}/requests?page=1&sorting=newest"
        return requests.get(url).json()["data"]
    except JSONDecodeError:
        raise Exception("sms_webhook_token is incorrect")


def delete_message(sms_webhook_token, message_id=""):
    url = f"https://webhook.site/token/{sms_webhook_token}/request/{message_id}"
    requests.delete(url)


def get_code(context: CustomerProfile):
    for i in range(60):
        messages = get_messages(context.sms_webhook_token)
        if not messages:
            time.sleep(5)
            continue
        content = messages[0].get("text_content")
        match = re.search("CODIGO (.*), DE", content)
        if match:
            delete_message(context.sms_webhook_token, messages[0].get("uuid"))
            return match.group(1)
    return None


def add_reason(driver: webdriver, context: CustomerProfile):
    try:
        if context.operation_code == OperationType.SOLICITUD_ASILO:
            element = find_element_resilient(driver, OBSERVATIONS_INPUT)
            if element:
                element.send_keys(context.reason_or_type)
    except Exception as e:
        logging.error(e)
