"""
Vendored from https://github.com/cita-bot/cita-bot (AGPL-3.0)
Modified for PAI: headless Docker, ntfy notifications, no os._exit, SMS HTTP endpoint.
All PAI modifications marked with # PAI: comments.
"""
import io
import json
import logging
import os
import random
import re
import sys
import tempfile
import threading
import time
import urllib.parse
from base64 import b64decode
from dataclasses import dataclass, field
from datetime import datetime as dt
from enum import Enum
from http.server import BaseHTTPRequestHandler, HTTPServer
from json.decoder import JSONDecodeError
from typing import Any, Dict, Optional

import asyncio

import backoff
import requests
from capmonstercloudclient import CapMonsterClient, ClientOptions
from capmonstercloudclient.requests import RecaptchaV3ProxylessRequest, ImageToTextRequest
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.service import Service  # PAI: modern Selenium API
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select
from selenium.webdriver.support.wait import WebDriverWait

from .speaker import new_speaker

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


class _SMSCodeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _sms_code_value
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/code" and "value" in params:
            _sms_code_value = params["value"][0]
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

    def log_message(self, *args):
        pass


def _wait_for_sms_code_http(timeout=300, port=8080):
    """Start HTTP server and wait for SMS code submission."""
    global _sms_code_value
    _sms_code_value = None
    _sms_code_event.clear()

    server = HTTPServer(("0.0.0.0", port), _SMSCodeHandler)
    server.timeout = 5

    def serve():
        deadline = time.time() + timeout
        while not _sms_code_event.is_set() and time.time() < deadline:
            server.handle_request()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    _sms_code_event.wait(timeout=timeout)
    return _sms_code_value


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
    chrome_driver_path: str = "/usr/bin/chromedriver"  # PAI: Debian path
    chrome_profile_name: Optional[str] = None
    chrome_profile_path: Optional[str] = None
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


def init_wedriver(context: CustomerProfile):
    options = webdriver.ChromeOptions()
    if context.chrome_profile_path:
        options.add_argument(f"user-data-dir={context.chrome_profile_path}")
    if context.chrome_profile_name:
        options.add_argument(f"profile-directory={context.chrome_profile_name}")

    # PAI: headless mode for Docker
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--ignore-certificate-errors")
    settings = {
        "recentDestinations": [{"id": "Save as PDF"}],
        "selectedDestinationId": "Save as PDF",
        "version": 2,
    }
    prefs = {
        "printing.print_preview_sticky_settings.appState": json.dumps(settings),
        "download.default_directory": os.getcwd(),
    }
    options.add_experimental_option("prefs", prefs)
    options.add_argument("--kiosk-printing")

    # PAI: modern Selenium API with Service
    service = Service(executable_path=context.chrome_driver_path)
    browser = webdriver.Chrome(service=service, options=options)
    browser.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    browser.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": ua})
    return browser


def try_cita(context: CustomerProfile, cycles: int = CYCLES):
    driver = init_wedriver(context)
    return start_with(driver, context, cycles)  # PAI: return result


def start_with(driver: webdriver, context: CustomerProfile, cycles: int = CYCLES):
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
    for i in range(cycles):
        try:
            logging.info(f"\033[33m[Attempt {i + 1}/{cycles}]\033[0m")
            result = cycle_cita(driver, context, fast_forward_url, fast_forward_url2)
        except KeyboardInterrupt:
            raise
        except TimeoutException:
            logging.error("Timeout exception")
        except Exception as e:
            logging.error(f"SMTH BROKEN: {e}")
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

    # PAI: always quit driver, never os._exit
    try:
        driver.quit()
    except Exception:
        pass

    return success


def toma_huellas_step2(driver: webdriver, context: CustomerProfile):
    try:
        WebDriverWait(driver, DELAY).until(
            EC.presence_of_element_located((By.ID, "txtPaisNac"))
        )
    except TimeoutException:
        logging.error("Timed out waiting for form to load")
        return None
    select = Select(driver.find_element(By.ID, "txtPaisNac"))
    select.select_by_visible_text(context.country)
    if context.doc_type == DocType.PASSPORT:
        driver.find_element(By.ID, "rdbTipoDocPas").send_keys(Keys.SPACE)
    elif context.doc_type == DocType.NIE:
        driver.find_element(By.ID, "rdbTipoDocNie").send_keys(Keys.SPACE)
    element = driver.find_element(By.ID, "txtIdCitado")
    element.send_keys(context.doc_value, Keys.TAB, context.name)
    return True


def recogida_de_tarjeta_step2(driver: webdriver, context: CustomerProfile):
    try:
        WebDriverWait(driver, DELAY).until(
            EC.presence_of_element_located((By.ID, "txtIdCitado"))
        )
    except TimeoutException:
        logging.error("Timed out waiting for form to load")
        return None
    if context.doc_type == DocType.PASSPORT:
        driver.find_element(By.ID, "rdbTipoDocPas").send_keys(Keys.SPACE)
    elif context.doc_type == DocType.NIE:
        driver.find_element(By.ID, "rdbTipoDocNie").send_keys(Keys.SPACE)
    element = driver.find_element(By.ID, "txtIdCitado")
    element.send_keys(context.doc_value, Keys.TAB, context.name)
    return True


def solicitud_asilo_step2(driver: webdriver, context: CustomerProfile):
    try:
        WebDriverWait(driver, DELAY).until(
            EC.presence_of_element_located((By.ID, "txtIdCitado"))
        )
    except TimeoutException:
        logging.error("Timed out waiting for form to load")
        return None
    if context.doc_type == DocType.PASSPORT:
        driver.find_element(By.ID, "rdbTipoDocPas").send_keys(Keys.SPACE)
    elif context.doc_type == DocType.NIE:
        driver.find_element(By.ID, "rdbTipoDocNie").send_keys(Keys.SPACE)
    element = driver.find_element(By.ID, "txtIdCitado")
    element.send_keys(
        context.doc_value, Keys.TAB, context.name, Keys.TAB, context.year_of_birth
    )
    select = Select(driver.find_element(By.ID, "txtPaisNac"))
    select.select_by_visible_text(context.country)
    return True


def brexit_step2(driver: webdriver, context: CustomerProfile):
    try:
        WebDriverWait(driver, DELAY).until(
            EC.presence_of_element_located((By.ID, "txtIdCitado"))
        )
    except TimeoutException:
        logging.error("Timed out waiting for form to load")
        return None
    if context.doc_type == DocType.PASSPORT:
        driver.find_element(By.ID, "rdbTipoDocPas").send_keys(Keys.SPACE)
    elif context.doc_type == DocType.NIE:
        driver.find_element(By.ID, "rdbTipoDocNie").send_keys(Keys.SPACE)
    element = driver.find_element(By.ID, "txtIdCitado")
    element.send_keys(context.doc_value, Keys.TAB, context.name)
    return True


def carta_invitacion_step2(driver: webdriver, context: CustomerProfile):
    try:
        WebDriverWait(driver, DELAY).until(
            EC.presence_of_element_located((By.ID, "txtIdCitado"))
        )
    except TimeoutException:
        logging.error("Timed out waiting for form to load")
        return None
    if context.doc_type == DocType.PASSPORT:
        driver.find_element(By.ID, "rdbTipoDocPas").send_keys(Keys.SPACE)
    elif context.doc_type == DocType.DNI:
        driver.find_element(By.ID, "rdbTipoDocDni").send_keys(Keys.SPACE)
    elif context.doc_type == DocType.NIE:
        driver.find_element(By.ID, "rdbTipoDocNie").send_keys(Keys.SPACE)
    element = driver.find_element(By.ID, "txtIdCitado")
    element.send_keys(context.doc_value, Keys.TAB, context.name)
    return True


def certificados_step2(driver: webdriver, context: CustomerProfile):
    try:
        WebDriverWait(driver, DELAY).until(
            EC.presence_of_element_located((By.ID, "txtIdCitado"))
        )
    except TimeoutException:
        logging.error("Timed out waiting for form to load")
        return None
    if context.doc_type == DocType.PASSPORT:
        driver.find_element(By.ID, "rdbTipoDocPas").send_keys(Keys.SPACE)
    elif context.doc_type == DocType.NIE:
        driver.find_element(By.ID, "rdbTipoDocNie").send_keys(Keys.SPACE)
    elif context.doc_type == DocType.DNI:
        driver.find_element(By.ID, "rdbTipoDocDni").send_keys(Keys.SPACE)
    element = driver.find_element(By.ID, "txtIdCitado")
    element.send_keys(context.doc_value, Keys.TAB, context.name)
    return True


def autorizacion_de_regreso_step2(driver: webdriver, context: CustomerProfile):
    try:
        WebDriverWait(driver, DELAY).until(
            EC.presence_of_element_located((By.ID, "txtIdCitado"))
        )
    except TimeoutException:
        logging.error("Timed out waiting for form to load")
        return None
    if context.doc_type == DocType.PASSPORT:
        driver.find_element(By.ID, "rdbTipoDocPas").send_keys(Keys.SPACE)
    elif context.doc_type == DocType.NIE:
        driver.find_element(By.ID, "rdbTipoDocNie").send_keys(Keys.SPACE)
    element = driver.find_element(By.ID, "txtIdCitado")
    element.send_keys(context.doc_value, Keys.TAB, context.name)
    return True


def asignacion_nie_step2(driver: webdriver, context: CustomerProfile):
    try:
        WebDriverWait(driver, DELAY).until(
            EC.presence_of_element_located((By.ID, "txtIdCitado"))
        )
    except TimeoutException:
        logging.error("Timed out waiting for form to load")
        return None
    if context.doc_type == DocType.PASSPORT:
        option = driver.find_element(By.ID, "rdbTipoDocPas")
        if option:
            option.send_keys(Keys.SPACE)
    element = driver.find_element(By.ID, "txtIdCitado")
    element.send_keys(
        context.doc_value, Keys.TAB, context.name, Keys.TAB, context.year_of_birth
    )
    select = Select(driver.find_element(By.ID, "txtPaisNac"))
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
        if len(driver.find_elements(By.ID, "reCAPTCHA_site_key")) > 0:
            captcha_result = solve_recaptcha(driver, context)
        elif len(driver.find_elements(By.CSS_SELECTOR, "img.img-thumbnail")) > 0:
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
    """Get or create CapMonster Cloud client."""
    if not context.capmonster_client:
        context.capmonster_client = CapMonsterClient(
            options=ClientOptions(api_key=context.capmonster_api_key)
        )
    return context.capmonster_client


def solve_recaptcha(driver: webdriver, context: CustomerProfile):
    site_key = driver.find_element(By.ID, "reCAPTCHA_site_key").get_attribute("value")
    page_action = driver.find_element(By.ID, "action").get_attribute("value")
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
            driver.execute_script(
                f"document.getElementById('g-recaptcha-response').value = '{g_response}'"
            )
            return True
        else:
            logging.error(f"CapMonster: empty response — {responses}")
            return None
    except Exception as e:
        logging.error(f"CapMonster reCAPTCHA error: {e}")
        return None


def solve_image_captcha(driver: webdriver, context: CustomerProfile):
    context.current_solver = "image_to_text"

    try:
        img = driver.find_elements(By.CSS_SELECTOR, "img.img-thumbnail")[0]
        img_data = img.get_attribute("src").split(",")[1].strip()
        logging.info("CapMonster: solving image CAPTCHA...")

        client = _get_capmonster_client(context)
        request = ImageToTextRequest(body=img_data)

        responses = asyncio.run(client.solve_captcha(request))
        captcha_text = responses.get("text", "")
        if captcha_text:
            logging.info(f"CapMonster: image CAPTCHA text: {captcha_text}")
            element = driver.find_element(By.ID, "captcha")
            element.send_keys(captcha_text)
            return True
        else:
            logging.error(f"CapMonster: empty image response — {responses}")
            return None
    except Exception as e:
        logging.error(f"CapMonster image CAPTCHA error: {e}")
        return None


def find_best_date_slots(driver: webdriver, context: CustomerProfile):
    try:
        els = driver.find_elements(By.CSS_SELECTOR, "[id^=lCita_]")
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
        el = driver.find_element(By.ID, "idSede")
        select = Select(el)
        if context.save_artifacts:
            offices_path = os.path.join(
                os.getcwd(), f"offices-{dt.now()}.html".replace(":", "-")
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


def office_selection(driver: webdriver, context: CustomerProfile):
    driver.execute_script("enviar('solicitud');")
    for i in range(REFRESH_PAGE_CYCLES):
        resp_text = body_text(driver)
        if "Seleccione la oficina donde solicitar la cita" in resp_text:
            logging.info("[Step 2/6] Office selection")
            time.sleep(0.3)
            try:
                WebDriverWait(driver, DELAY).until(
                    EC.presence_of_element_located((By.ID, "btnSiguiente"))
                )
            except TimeoutException:
                logging.error("Timed out waiting for offices to load")
                return None
            res = select_office(driver, context)
            if res is None:
                time.sleep(5)
                driver.refresh()
                continue
            btn = driver.find_element(By.ID, "btnSiguiente")
            btn.send_keys(Keys.ENTER)
            return True
        elif "En este momento no hay citas disponibles" in resp_text:
            logging.info("No appointments available — retrying...")
            time.sleep(5)
            driver.refresh()
            continue
        else:
            logging.info("[Step 2/6] Office selection -> No offices")
            return None


def phone_mail(driver: webdriver, context: CustomerProfile):
    try:
        WebDriverWait(driver, DELAY).until(
            EC.presence_of_element_located((By.ID, "txtTelefonoCitado"))
        )
        logging.info("[Step 3/6] Contact info")
    except TimeoutException:
        logging.error("Timed out waiting for contact info page to load")
        return None
    element = driver.find_element(By.ID, "txtTelefonoCitado")
    element.send_keys(context.phone)
    try:
        element = driver.find_element(By.ID, "emailUNO")
        element.send_keys(context.email)
        element = driver.find_element(By.ID, "emailDOS")
        element.send_keys(context.email)
    except Exception:
        pass
    add_reason(driver, context)
    driver.execute_script("enviar();")
    return cita_selection(driver, context)


def confirm_appointment(driver: webdriver, context: CustomerProfile):
    driver.find_element(By.ID, "chkTotal").send_keys(Keys.SPACE)
    driver.find_element(By.ID, "enviarCorreo").send_keys(Keys.SPACE)
    btn = driver.find_element(By.ID, "btnConfirmar")
    btn.send_keys(Keys.ENTER)
    resp_text = body_text(driver)
    ctime = dt.now()
    if "CITA CONFIRMADA Y GRABADA" in resp_text:
        context.bot_result = True
        code = driver.find_element(By.ID, "justificanteFinal").text
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
    elif "Lo sentimos, el código introducido no es correcto" in resp_text:
        logging.error("Incorrect code entered")
        _ntfy("SMS code incorrect", "The SMS code was wrong. Will retry.", priority="high", tags="x")
    else:
        error_name = f"/app/data/error-{ctime}.png".replace(":", "-")
        driver.save_screenshot(error_name)
        _ntfy("Booking error", "Unexpected page at confirmation step. Check screenshots.", priority="high", tags="warning")
    return None


def log_backoff(details):
    logging.error(
        f"Unable to load initial page, backing off {details['wait']:0.1f} seconds"
    )


@backoff.on_exception(
    backoff.constant,
    TimeoutException,
    interval=350,
    max_tries=(10 if os.environ.get("CITA_TEST") else None),
    on_backoff=log_backoff,
    logger=None,
)
def initial_page(
    driver: webdriver, context: CustomerProfile, fast_forward_url, fast_forward_url2
):
    if context.first_load:
        driver.delete_all_cookies()
    driver.set_page_load_timeout(300 if context.first_load else 50)
    time.sleep(1)
    driver.get(fast_forward_url)
    time.sleep(5)
    if context.first_load:
        try:
            driver.execute_script("window.localStorage.clear();")
            driver.execute_script("window.sessionStorage.clear();")
        except Exception as e:
            logging.error(e)
    driver.get(fast_forward_url2)
    time.sleep(5)
    resp_text = body_text(driver)
    if "INTERNET CITA PREVIA" not in resp_text:
        # PAI: diagnostic logging — what does the page actually say?
        logging.error(f"Expected 'INTERNET CITA PREVIA' not found. Page title: {driver.title}")
        logging.error(f"Current URL: {driver.current_url}")
        logging.error(f"Body text (first 500 chars): {resp_text[:500]}")
        if context.save_artifacts:
            driver.save_screenshot(f"/app/data/initial-fail-{dt.now()}.png".replace(":", "-"))
        context.first_load = True
        raise TimeoutException
    context.first_load = False


def cycle_cita(
    driver: webdriver, context: CustomerProfile, fast_forward_url, fast_forward_url2
):
    initial_page(driver, context, fast_forward_url, fast_forward_url2)
    try:
        WebDriverWait(driver, DELAY).until(
            EC.presence_of_element_located((By.ID, "btnEntrar"))
        )
    except TimeoutException:
        logging.error("Timed out waiting for Instructions page to load")
        return None

    if os.environ.get("CITA_TEST") and context.operation_code == OperationType.TOMA_HUELLAS:
        logging.info("Instructions page loaded")
        return True

    driver.find_element(By.ID, "btnEntrar").send_keys(Keys.ENTER)
    logging.info("[Step 1/6] Personal info")

    success = False
    if context.operation_code == OperationType.TOMA_HUELLAS:
        success = toma_huellas_step2(driver, context)
    elif context.operation_code == OperationType.RECOGIDA_DE_TARJETA:
        success = recogida_de_tarjeta_step2(driver, context)
    elif context.operation_code == OperationType.SOLICITUD_ASILO:
        success = solicitud_asilo_step2(driver, context)
    elif context.operation_code == OperationType.BREXIT:
        success = brexit_step2(driver, context)
    elif context.operation_code == OperationType.CARTA_INVITACION:
        success = carta_invitacion_step2(driver, context)
    elif context.operation_code in [
        OperationType.CERTIFICADOS_NIE,
        OperationType.CERTIFICADOS_NIE_NO_COMUN,
        OperationType.CERTIFICADOS_RESIDENCIA,
        OperationType.CERTIFICADOS_UE,
    ]:
        success = certificados_step2(driver, context)
    elif context.operation_code == OperationType.AUTORIZACION_DE_REGRESO:
        success = autorizacion_de_regreso_step2(driver, context)
    elif context.operation_code == OperationType.ASIGNACION_NIE:
        success = asignacion_nie_step2(driver, context)
    if not success:
        return None

    time.sleep(2)
    driver.find_element(By.ID, "btnEnviar").send_keys(Keys.ENTER)

    try:
        WebDriverWait(driver, 7).until(
            EC.presence_of_element_located((By.ID, "btnConsultar"))
        )
    except TimeoutException:
        logging.error("Timed out waiting for Solicitar page to load")

    try:
        wait_exact_time(driver, context)
    except TimeoutException:
        logging.error("Timed out waiting for exact time")
        return None

    selection_result = office_selection(driver, context)
    if selection_result is None:
        return None

    return phone_mail(driver, context)


def cita_selection(driver: webdriver, context: CustomerProfile):
    resp_text = body_text(driver)
    if "DISPONE DE 5 MINUTOS" in resp_text:
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
            return None
        time.sleep(2)
        success = process_captcha(driver, context)
        if not success:
            return None
        try:
            driver.find_elements(By.CSS_SELECTOR, "input[type='radio'][name='rdbCita']")[
                position - 1
            ].send_keys(Keys.SPACE)
        except Exception as e:
            logging.error(e)
        driver.execute_script("envia();")
        time.sleep(0.5)
        driver.switch_to.alert.accept()
    elif "Seleccione una de las siguientes citas disponibles" in resp_text:
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
            date_els = driver.find_elements(
                By.CSS_SELECTOR, "#CitaMAP_HORAS thead [class^=colFecha]"
            )
            dates = sorted([*map(lambda x: x.text, date_els)])
            slots: Dict[str, list] = {}
            slot_table = driver.find_element(By.CSS_SELECTOR, "#CitaMAP_HORAS tbody")
            for row in slot_table.find_elements(By.CSS_SELECTOR, "tr"):
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
                        slot = cell.find_element(
                            By.CSS_SELECTOR, "[id^=HUECO]"
                        ).get_attribute("id")
                        slots[dates[idx]] = [slot]
                    except Exception:
                        pass
            best_date = find_best_date(sorted(slots), context)
            if not best_date:
                return None
            slot = slots[best_date][0]
            time.sleep(2)
            success = process_captcha(driver, context)
            if not success:
                return None
            driver.execute_script(
                f"confirmarHueco({{id: '{slot}'}}, {slot[5:]});"
            )
            driver.switch_to.alert.accept()
        except Exception as e:
            logging.error(e)
            return None
    else:
        logging.info("[Step 4/6] Cita attempt -> missed selection")
        return None

    resp_text = body_text(driver)
    if "Debe confirmar los datos de la cita asignada" in resp_text:
        logging.info("[Step 5/6] Cita attempt -> confirmation hit!")
        # PAI: CapMonster Cloud doesn't have report_correct, skip
        try:
            sms_verification = driver.find_element(
                By.ID, "txtCodigoVerificacion"
            )
        except Exception:
            sms_verification = None

        if context.sms_webhook_token:
            # Path A: webhook.site SMS automation
            if sms_verification:
                code = get_code(context)
                if code:
                    logging.info(f"Received code: {code}")
                    sms_verification = driver.find_element(
                        By.ID, "txtCodigoVerificacion"
                    )
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
                # PAI: SMS needed — start HTTP endpoint and notify
                sms_port = context.sms_code_port
                _ntfy(
                    "SMS CODE NEEDED!",
                    f"Open http://172.16.10.25:{sms_port}/ to enter the SMS code. You have 5 minutes.",
                    priority="urgent",
                    tags="rotating_light,iphone",
                )
                logging.info(f"Waiting for SMS code on HTTP port {sms_port}...")
                code = _wait_for_sms_code_http(timeout=300, port=sms_port)
                if code:
                    logging.info(f"Received SMS code: {code}")
                    sms_verification.send_keys(code)
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
        # PAI: CapMonster Cloud doesn't have report_incorrect, skip
        if context.save_artifacts:
            driver.save_screenshot(
                f"/app/data/failed-confirmation-{dt.now()}.png".replace(":", "-")
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
            element = driver.find_element(By.ID, "txtObservaciones")
            element.send_keys(context.reason_or_type)
    except Exception as e:
        logging.error(e)
