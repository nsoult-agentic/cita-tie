"""
Resilience layer for cita-tie.
Wraps Selenium interactions with fallback strategies, page state detection,
screenshot capture, and ntfy notifications.
"""
import functools
import json
import logging
import os
import random
import time
from datetime import datetime as dt
from enum import Enum, auto
from typing import Optional

import requests as req
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from .selectors import ElementDescriptor

log = logging.getLogger("resilience")

# ── Fallback tracking ───────────────────────────────────────────────

_fallback_hits: dict = {}


def find_element_resilient(
    driver: WebDriver,
    descriptor: ElementDescriptor,
    timeout: Optional[float] = None,
    multiple: bool = False,
) -> Optional[WebElement]:
    """
    Try each strategy in order. Return the first match.
    Strategy[0] = fast path (known-good ID, short timeout).
    Strategy[1:] = fallbacks with brief probing.
    """
    effective_timeout = timeout or descriptor.wait_timeout

    for idx, (by, value) in enumerate(descriptor.strategies):
        try:
            if idx == 0:
                # Fast path: short wait for known-good selector
                wait = min(effective_timeout, 3.0)
                if multiple:
                    WebDriverWait(driver, wait).until(
                        lambda d: len(d.find_elements(by, value)) > 0
                    )
                    return driver.find_elements(by, value)
                else:
                    return WebDriverWait(driver, wait).until(
                        EC.presence_of_element_located((by, value))
                    )
            else:
                # Fallback: brief probe
                fallback_wait = min(1.0 + idx * 0.5, 5.0)
                if multiple:
                    WebDriverWait(driver, fallback_wait).until(
                        lambda d: len(d.find_elements(by, value)) > 0
                    )
                    els = driver.find_elements(by, value)
                    if els:
                        _record_fallback(descriptor.name, by, value)
                        return els
                else:
                    el = WebDriverWait(driver, fallback_wait).until(
                        EC.presence_of_element_located((by, value))
                    )
                    _record_fallback(descriptor.name, by, value)
                    return el
        except Exception:
            continue

    if descriptor.required:
        log.error(f"ELEMENT NOT FOUND: {descriptor.name} — tried {len(descriptor.strategies)} strategies")
    return None


def find_elements_resilient(
    driver: WebDriver,
    descriptor: ElementDescriptor,
    timeout: Optional[float] = None,
) -> list:
    """Convenience wrapper that always returns a list."""
    result = find_element_resilient(driver, descriptor, timeout=timeout, multiple=True)
    return result if result else []


def _record_fallback(name: str, by: str, value: str):
    count = _fallback_hits.get(name, (None, None, 0))[2]
    _fallback_hits[name] = (by, value, count + 1)
    log.warning(f"FALLBACK: '{name}' found via ({by}, '{value}') — update selectors.py")


def get_fallback_report() -> str:
    if not _fallback_hits:
        return ""
    lines = [f"  {n}: {by}='{v}' ({c}x)" for n, (by, v, c) in _fallback_hits.items()]
    return "Fallback selectors used:\n" + "\n".join(lines)


def reset_fallback_tracking():
    _fallback_hits.clear()


# ── Page State Detection ────────────────────────────────────────────

class PageState(Enum):
    RATE_LIMITED = auto()
    ERROR = auto()
    INITIAL_LANDING = auto()
    INSTRUCTIONS = auto()
    PERSONAL_INFO = auto()
    OFFICE_SELECTION = auto()
    NO_APPOINTMENTS = auto()
    CONTACT_INFO = auto()
    SLOT_SELECTION_5MIN = auto()
    SLOT_SELECTION_TABLE = auto()
    CONFIRMATION = auto()
    SUCCESS = auto()
    SMS_CODE_WRONG = auto()
    UNKNOWN = auto()


# (text_patterns: [(substring, case_sensitive)], element_ids: [id])
_STATE_SIGNALS = {
    PageState.RATE_LIMITED: {
        "text": [("Too Many Requests", False), ("Request Rejected", False),
                 ("requested URL was rejected", False)],
        "title": ["429", "Request Rejected"],
        "elements": [],
    },
    PageState.ERROR: {
        "text": [("Se ha producido un error en el sistema", False),
                 ("error en el sistema", False)],
        "title": [],
        "elements": [],
    },
    PageState.SUCCESS: {
        "text": [("CITA CONFIRMADA Y GRABADA", False), ("CITA CONFIRMADA", False)],
        "title": [],
        "elements": ["justificanteFinal"],
    },
    PageState.SMS_CODE_WRONG: {
        "text": [("código introducido no es correcto", False)],
        "title": [],
        "elements": [],
    },
    PageState.CONFIRMATION: {
        "text": [("Debe confirmar los datos", False), ("confirmar los datos de la cita", False)],
        "title": [],
        "elements": ["btnConfirmar"],
    },
    PageState.SLOT_SELECTION_5MIN: {
        "text": [("DISPONE DE 5 MINUTOS", False), ("5 MINUTOS", False)],
        "title": [],
        "elements": [],
    },
    PageState.SLOT_SELECTION_TABLE: {
        "text": [("Seleccione una de las siguientes citas", False)],
        "title": [],
        "elements": ["CitaMAP_HORAS"],
    },
    PageState.NO_APPOINTMENTS: {
        "text": [("no hay citas disponibles", False), ("no hay citas", False)],
        "title": [],
        "elements": [],
    },
    PageState.CONTACT_INFO: {
        "text": [],
        "title": [],
        "elements": ["txtTelefonoCitado"],
    },
    PageState.OFFICE_SELECTION: {
        "text": [("Seleccione la oficina", False), ("oficina donde solicitar", False)],
        "title": [],
        "elements": ["idSede"],
    },
    PageState.PERSONAL_INFO: {
        "text": [],
        "title": [],
        "elements": ["txtIdCitado"],
    },
    PageState.INSTRUCTIONS: {
        "text": [],
        "title": [],
        "elements": ["btnEntrar"],
    },
    PageState.INITIAL_LANDING: {
        "text": [("CITA PREVIA", False)],
        "title": [],
        "elements": [],
    },
}

# Check in this order: blockers first, then success, then flow states in reverse
_PRIORITY_ORDER = [
    PageState.RATE_LIMITED,
    PageState.ERROR,
    PageState.SUCCESS,
    PageState.SMS_CODE_WRONG,
    PageState.CONFIRMATION,
    PageState.SLOT_SELECTION_5MIN,
    PageState.SLOT_SELECTION_TABLE,
    PageState.NO_APPOINTMENTS,
    PageState.CONTACT_INFO,
    PageState.OFFICE_SELECTION,
    PageState.PERSONAL_INFO,
    PageState.INSTRUCTIONS,
    PageState.INITIAL_LANDING,
]


def detect_page_state(driver: WebDriver, body_text_cache: str = None) -> PageState:
    """Classify current page using multiple signals."""
    if body_text_cache is None:
        try:
            body_text_cache = driver.find_element("tag name", "body").text
        except Exception:
            body_text_cache = ""

    title = ""
    try:
        title = driver.title or ""
    except Exception:
        pass

    for state in _PRIORITY_ORDER:
        signals = _STATE_SIGNALS[state]

        for pattern, case_sensitive in signals["text"]:
            if case_sensitive and pattern in body_text_cache:
                return state
            if not case_sensitive and pattern.lower() in body_text_cache.lower():
                return state

        for pattern in signals["title"]:
            if pattern.lower() in title.lower():
                return state

        for elem_id in signals["elements"]:
            try:
                if driver.find_elements("id", elem_id):
                    return state
            except Exception:
                continue

    return PageState.UNKNOWN


# ── Form Submission ─────────────────────────────────────────────────

def submit_form_resilient(driver: WebDriver, js_function: str, fallback_descriptor=None) -> bool:
    """Try JS function call first, fall back to clicking a button."""
    try:
        driver.execute_script(js_function)
        return True
    except Exception as e:
        log.warning(f"JS call '{js_function}' failed: {e}")

    if fallback_descriptor:
        btn = find_element_resilient(driver, fallback_descriptor, timeout=3)
        if btn:
            log.info(f"Fallback: clicking {fallback_descriptor.name}")
            btn.click()
            return True

    # Last resort: any submit button
    try:
        submits = driver.find_elements("css selector", "input[type='submit'], button[type='submit']")
        if submits:
            log.warning("Last resort: clicking first submit button on page")
            submits[0].click()
            return True
    except Exception:
        pass

    log.error(f"Form submission failed for '{js_function}'")
    return False


# ── Step Runner ─────────────────────────────────────────────────────

class StepError(Exception):
    def __init__(self, step_num, step_name, message, recoverable=True):
        self.step_num = step_num
        self.step_name = step_name
        self.message = message
        self.recoverable = recoverable
        super().__init__(f"[Step {step_num}/6] {step_name}: {message}")


def step_runner(step_num: int, step_name: str, max_retries: int = 0):
    """Decorator: wraps step with screenshot, ntfy, retry on StepError."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(driver, context, *args, **kwargs):
            for attempt in range(1, max_retries + 2):
                try:
                    return func(driver, context, *args, **kwargs)
                except StepError as e:
                    _handle_failure(driver, context, e, attempt, max_retries + 1)
                    if not e.recoverable or attempt > max_retries:
                        return None
                except Exception as e:
                    err = StepError(step_num, step_name, str(e))
                    _handle_failure(driver, context, err, attempt, max_retries + 1)
                    if attempt > max_retries:
                        return None
            return None
        return wrapper
    return decorator


def _handle_failure(driver, context, error: StepError, attempt: int, max_attempts: int):
    ts = dt.now().strftime("%Y%m%d-%H%M%S")

    if context.save_artifacts:
        try:
            driver.save_screenshot(f"/app/data/step{error.step_num}-fail-{ts}.png")
        except Exception:
            pass

    try:
        state = detect_page_state(driver)
        url = driver.current_url
    except Exception:
        state = PageState.UNKNOWN
        url = "unknown"

    detail = (
        f"Step {error.step_num}/6 ({error.step_name}) — attempt {attempt}/{max_attempts}\n"
        f"Error: {error.message}\n"
        f"State: {state.name} | URL: {url}"
    )
    log.error(detail)

    if attempt >= max_attempts:
        _ntfy_resilience(f"Step {error.step_num} Failed", detail, priority="high", tags="warning")


# ── ntfy helper (reads from /secrets/) ──────────────────────────────

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


def _ntfy_resilience(title, message, priority="default", tags=""):
    config = _load_ntfy_config()
    url = config.get("url", "")
    topic = config.get("topic", "")
    if not url or not topic:
        return
    try:
        req.post(
            f"{url}/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=10,
        )
    except Exception:
        pass
