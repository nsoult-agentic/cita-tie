"""
Element selector registry with fallback chains.
Strategy[0] = current known-good selector (fast path).
Strategies[1:] = fallbacks tried if primary fails.
When a fallback wins, a WARNING is logged so selectors can be updated.
"""
from dataclasses import dataclass, field
from typing import List, Tuple

from selenium.webdriver.common.by import By


@dataclass
class ElementDescriptor:
    name: str
    strategies: List[Tuple[str, str]]
    required: bool = True
    wait_timeout: float = 10.0


# ── Personal Info Form ──────────────────────────────────────────────

COUNTRY_SELECT = ElementDescriptor(
    name="country_select",
    strategies=[
        (By.ID, "txtPaisNac"),
        (By.CSS_SELECTOR, "select[id*='Pais']"),
        (By.XPATH, "//select[contains(@id,'Pais') or contains(@name,'pais')]"),
        (By.XPATH, "//label[contains(text(),'país') or contains(text(),'País')]/following::select[1]"),
    ],
)

DOC_TYPE_NIE = ElementDescriptor(
    name="doc_type_nie",
    strategies=[
        (By.ID, "rdbTipoDocNie"),
        (By.CSS_SELECTOR, "input[type='radio'][id*='Nie']"),
        (By.XPATH, "//input[@type='radio'][contains(@id,'Nie') or contains(@value,'nie')]"),
        (By.XPATH, "//label[contains(text(),'NIE')]/preceding-sibling::input[@type='radio']"),
    ],
)

DOC_TYPE_PASSPORT = ElementDescriptor(
    name="doc_type_passport",
    strategies=[
        (By.ID, "rdbTipoDocPas"),
        (By.CSS_SELECTOR, "input[type='radio'][id*='Pas']"),
        (By.XPATH, "//input[@type='radio'][contains(@id,'Pas')]"),
        (By.XPATH, "//label[contains(text(),'Pasaporte')]/preceding-sibling::input[@type='radio']"),
    ],
)

DOC_TYPE_DNI = ElementDescriptor(
    name="doc_type_dni",
    strategies=[
        (By.ID, "rdbTipoDocDni"),
        (By.CSS_SELECTOR, "input[type='radio'][id*='Dni']"),
        (By.XPATH, "//input[@type='radio'][contains(@id,'Dni')]"),
    ],
)

DOC_NUMBER_INPUT = ElementDescriptor(
    name="doc_number",
    strategies=[
        (By.ID, "txtIdCitado"),
        (By.CSS_SELECTOR, "input[id*='IdCitado']"),
        (By.XPATH, "//input[contains(@id,'Citado') or contains(@name,'Citado')]"),
    ],
)

# ── Buttons ─────────────────────────────────────────────────────────

BTN_ENTRAR = ElementDescriptor(
    name="btn_entrar",
    strategies=[
        (By.ID, "btnEntrar"),
        (By.CSS_SELECTOR, "input[id*='Entrar']"),
        (By.XPATH, "//input[contains(@id,'Entrar') or contains(@value,'Entrar')]"),
        (By.XPATH, "//input[contains(@value,'Aceptar')]"),
    ],
)

BTN_ENVIAR = ElementDescriptor(
    name="btn_enviar",
    strategies=[
        (By.ID, "btnEnviar"),
        (By.CSS_SELECTOR, "input[id*='Enviar']"),
        (By.XPATH, "//input[contains(@id,'Enviar') or contains(@value,'Enviar')]"),
    ],
)

BTN_CONSULTAR = ElementDescriptor(
    name="btn_consultar",
    strategies=[
        (By.ID, "btnConsultar"),
        (By.CSS_SELECTOR, "input[id*='Consultar']"),
    ],
    required=False,
)

BTN_SIGUIENTE = ElementDescriptor(
    name="btn_siguiente",
    strategies=[
        (By.ID, "btnSiguiente"),
        (By.CSS_SELECTOR, "input[id*='Siguiente']"),
        (By.XPATH, "//input[contains(@id,'Siguiente') or contains(@value,'Siguiente')]"),
    ],
)

BTN_CONFIRMAR = ElementDescriptor(
    name="btn_confirmar",
    strategies=[
        (By.ID, "btnConfirmar"),
        (By.CSS_SELECTOR, "input[id*='Confirmar']"),
        (By.XPATH, "//input[contains(@id,'Confirmar') or contains(@value,'Confirmar')]"),
    ],
)

# ── Office Selection ────────────────────────────────────────────────

OFFICE_SELECT = ElementDescriptor(
    name="office_select",
    strategies=[
        (By.ID, "idSede"),
        (By.CSS_SELECTOR, "select[id*='Sede']"),
        (By.XPATH, "//select[contains(@id,'Sede') or contains(@name,'sede')]"),
    ],
)

# ── Contact Info ────────────────────────────────────────────────────

PHONE_INPUT = ElementDescriptor(
    name="phone",
    strategies=[
        (By.ID, "txtTelefonoCitado"),
        (By.CSS_SELECTOR, "input[id*='Telefono']"),
        (By.CSS_SELECTOR, "input[type='tel']"),
        (By.XPATH, "//label[contains(text(),'eléfono')]/following::input[1]"),
    ],
)

EMAIL_ONE = ElementDescriptor(
    name="email_one",
    strategies=[
        (By.ID, "emailUNO"),
        (By.CSS_SELECTOR, "input[id*='emailUNO']"),
        (By.CSS_SELECTOR, "input[type='email']"),
        (By.XPATH, "//input[contains(@id,'email')][1]"),
    ],
)

EMAIL_TWO = ElementDescriptor(
    name="email_two",
    strategies=[
        (By.ID, "emailDOS"),
        (By.CSS_SELECTOR, "input[id*='emailDOS']"),
        (By.XPATH, "//input[contains(@id,'email')][2]"),
    ],
    required=False,
)

# ── Confirmation ────────────────────────────────────────────────────

CHK_TOTAL = ElementDescriptor(
    name="chk_total",
    strategies=[
        (By.ID, "chkTotal"),
        (By.CSS_SELECTOR, "input[type='checkbox'][id*='Total']"),
        (By.CSS_SELECTOR, "input[type='checkbox'][id*='chk']"),
    ],
)

CHK_EMAIL = ElementDescriptor(
    name="chk_email",
    strategies=[
        (By.ID, "enviarCorreo"),
        (By.CSS_SELECTOR, "input[type='checkbox'][id*='Correo']"),
        (By.CSS_SELECTOR, "input[type='checkbox'][id*='enviar']"),
    ],
)

JUSTIFICANTE = ElementDescriptor(
    name="justificante",
    strategies=[
        (By.ID, "justificanteFinal"),
        (By.CSS_SELECTOR, "[id*='justificante']"),
        (By.XPATH, "//*[contains(@id,'justificante') or contains(@id,'Justificante')]"),
    ],
    required=False,
)

SMS_CODE_INPUT = ElementDescriptor(
    name="sms_code",
    strategies=[
        (By.ID, "txtCodigoVerificacion"),
        (By.CSS_SELECTOR, "input[id*='Codigo']"),
        (By.CSS_SELECTOR, "input[id*='Verificacion']"),
    ],
    required=False,
)

OBSERVATIONS_INPUT = ElementDescriptor(
    name="observations",
    strategies=[
        (By.ID, "txtObservaciones"),
        (By.CSS_SELECTOR, "textarea[id*='Observaciones']"),
    ],
    required=False,
)

# ── CAPTCHA ─────────────────────────────────────────────────────────

RECAPTCHA_SITE_KEY = ElementDescriptor(
    name="recaptcha_site_key",
    strategies=[
        (By.ID, "reCAPTCHA_site_key"),
        (By.CSS_SELECTOR, "input[id*='reCAPTCHA']"),
        (By.CSS_SELECTOR, "[id*='site_key']"),
    ],
    required=False,
)

RECAPTCHA_ACTION = ElementDescriptor(
    name="recaptcha_action",
    strategies=[
        (By.ID, "action"),
        (By.CSS_SELECTOR, "input[id='action']"),
    ],
    required=False,
)

CAPTCHA_IMAGE = ElementDescriptor(
    name="captcha_image",
    strategies=[
        (By.CSS_SELECTOR, "img.img-thumbnail"),
        (By.CSS_SELECTOR, "img[class*='captcha']"),
        (By.XPATH, "//img[contains(@class,'thumbnail') or contains(@src,'captcha')]"),
    ],
    required=False,
)

CAPTCHA_TEXT_INPUT = ElementDescriptor(
    name="captcha_text",
    strategies=[
        (By.ID, "captcha"),
        (By.CSS_SELECTOR, "input[id*='captcha']"),
    ],
    required=False,
)

# ── Appointment Slots ───────────────────────────────────────────────

SLOT_DATES = ElementDescriptor(
    name="slot_dates",
    strategies=[
        (By.CSS_SELECTOR, "[id^=lCita_]"),
        (By.CSS_SELECTOR, "[id*='Cita_']"),
    ],
    required=False,
)

SLOT_RADIOS = ElementDescriptor(
    name="slot_radios",
    strategies=[
        (By.CSS_SELECTOR, "input[type='radio'][name='rdbCita']"),
        (By.CSS_SELECTOR, "input[type='radio'][name*='Cita']"),
    ],
    required=False,
)

SLOT_TABLE = ElementDescriptor(
    name="slot_table",
    strategies=[
        (By.CSS_SELECTOR, "#CitaMAP_HORAS"),
        (By.CSS_SELECTOR, "table[id*='Cita']"),
        (By.CSS_SELECTOR, "table[id*='HORAS']"),
    ],
    required=False,
)

SLOT_TABLE_HEADERS = ElementDescriptor(
    name="slot_table_headers",
    strategies=[
        (By.CSS_SELECTOR, "#CitaMAP_HORAS thead [class^=colFecha]"),
        (By.CSS_SELECTOR, "#CitaMAP_HORAS thead th"),
    ],
    required=False,
)

SLOT_HUECO = ElementDescriptor(
    name="slot_hueco",
    strategies=[
        (By.CSS_SELECTOR, "[id^=HUECO]"),
        (By.CSS_SELECTOR, "[id*='HUECO']"),
    ],
    required=False,
)

RECAPTCHA_RESPONSE = ElementDescriptor(
    name="recaptcha_response",
    strategies=[
        (By.ID, "g-recaptcha-response"),
        (By.CSS_SELECTOR, "[id*='recaptcha-response']"),
    ],
    required=False,
)
