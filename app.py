import os, json
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, EmailStr
from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML
import requests

# ---------- CONFIG ----------
COMPANY_NAME = os.getenv("COMPANY_NAME", "Marca Nova Digital")
FROM_EMAIL = os.getenv("FROM_EMAIL")  # precisa ser verificado no SendGrid
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
SELLERS_JSON = os.getenv("SELLERS_JSON", "{}")

INSTALL_RATE_DEFAULT = 0.40
DISCOUNT_CASH = 0.10
CARD_INSTALLMENTS = 3

UNIT_PRICES = {
    "lamp_smart": 180.00,
    "led_strip": 150.00,
    "dimmer": 220.00,
    "switch_smart": 150.00,
    "relay_1ch": 90.00,
    "relay_2ch": 140.00,
    "relay_4ch": 230.00,
    "smart_plug": 120.00,
    "ir_ac": 220.00,
    "ir_tv": 180.00,
    "blinds": 1200.00,
    "sensor_presence": 220.00,
    "sensor_door": 150.00,
    "sensor_leak": 180.00,
    "siren": 300.00,
}

LABELS = {
    "lamp_smart": "Lâmpada inteligente",
    "led_strip": "Fita LED / Driver",
    "dimmer": "Dimmer inteligente",
    "switch_smart": "Interruptor inteligente",
    "relay_1ch": "Módulo relé 1 canal",
    "relay_2ch": "Módulo relé 2 canais",
    "relay_4ch": "Módulo relé 4 canais",
    "smart_plug": "Tomada inteligente",
    "ir_ac": "IR para ar-condicionado",
    "ir_tv": "IR para TV / Projetor",
    "blinds": "Motor para persiana",
    "sensor_presence": "Sensor de presença",
    "sensor_door": "Sensor de abertura",
    "sensor_leak": "Sensor de vazamento",
    "siren": "Sirene",
}

def money(x: float) -> float:
    return round(float(x), 2)

def compute_quote(items: Dict[str, int]) -> Dict[str, Any]:
    breakdown = []
    material_total = 0.0
    labor_total = 0.0

    for sku, qty in items.items():
        qty_int = int(qty or 0)
        if qty_int <= 0:
            continue
        unit = float(UNIT_PRICES.get(sku, 0.0))
        material = qty_int * unit
        labor = qty_int * unit * INSTALL_RATE_DEFAULT
        material_total += material
        labor_total += labor

        breakdown.append({
            "sku": sku,
            "label": LABELS.get(sku, sku),
            "qty": qty_int,
            "unit_price": money(unit),
            "material": money(material),
        })

    grand_total = material_total + labor_total
    cash_total = grand_total * (1.0 - DISCOUNT_CASH)
    installment_value = grand_total / CARD_INSTALLMENTS

    return {
        "breakdown": breakdown,
        "totals": {
            "material_total": money(material_total),
            "labor_total": money(labor_total),
            "grand_total": money(grand_total),
            "cash_total": money(cash_total),
            "card_installments": CARD_INSTALLMENTS,
            "card_installment_value": money(installment_value),
            "rules": {
                "labor_rate_default": INSTALL_RATE_DEFAULT,
                "cash_discount": DISCOUNT_CASH,
            }
        }
    }

# ---------- EMAIL (SENDGRID) ----------
def send_email_sendgrid(to_email: str, cc_email: Optional[str], subject: str, html_body: str, pdf_bytes: bytes, filename: str):
    if not SENDGRID_API_KEY or not FROM_EMAIL:
        raise RuntimeError("SENDGRID_API_KEY e/ou FROM_EMAIL não configurados")

    # SendGrid expects base64 string for attachments
    import base64
    attachment_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

    payload = {
        "personalizations": [{
            "to": [{"email": to_email}],
            **({"cc": [{"email": cc_email}]} if cc_email else {})
        }],
        "from": {"email": FROM_EMAIL},
        "subject": subject,
        "content": [{"type": "text/html", "value": html_body}],
        "attachments": [{
            "content": attachment_b64,
            "type": "application/pdf",
            "filename": filename,
            "disposition": "attachment"
        }]
    }

    r = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": f"Bearer {SENDGRID_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30
    )
    if r.status_code not in (200, 202):
        raise RuntimeError(f"SendGrid erro {r.status_code}: {r.text}")

# ---------- PDF ----------
env = Environment(
    loader=FileSystemLoader("templates"),
    autoescape=select_autoescape(["html", "xml"])
)

def render_pdf(context: Dict[str, Any]) -> bytes:
    template = env.get_template("proposta.html")
    html_str = template.render(**context)
    return HTML(string=html_str, base_url=os.getcwd()).write_pdf()

# ---------- MODELS ----------
class Client(BaseModel):
    name: str
    email: EmailStr
    phone: str
    address: Optional[str] = None

class FlowSubmit(BaseModel):
    seller_phone: str
    client: Client
    items: Dict[str, int]  # {"lamp_smart": 2, ...}
    notes: Optional[str] = None

# ---------- APP ----------
app = FastAPI(title="Proposta Automação - Flows")

def resolve_seller_email(seller_phone: str) -> Optional[str]:
    try:
        sellers = json.loads(SELLERS_JSON or "{}")
        seller = sellers.get(seller_phone)
        return seller.get("email") if seller else None
    except Exception:
        return None

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/flow/submit")
def flow_submit(payload: FlowSubmit):
    quote = compute_quote(payload.items)

    # Contexto do PDF
    context = {
        "company_name": COMPANY_NAME,
        "client": payload.client.model_dump(),
        "seller_phone": payload.seller_phone,
        "notes": payload.notes or "",
        "breakdown": quote["breakdown"],
        "totals": quote["totals"],
    }

    pdf_bytes = render_pdf(context)

    seller_email = resolve_seller_email(payload.seller_phone)

    subject = f"Proposta - Automação Residencial/Predial - {payload.client.name}"
    html_body = f"""
    <p>Olá, <b>{payload.client.name}</b>!</p>
    <p>Segue em anexo sua proposta de automação.</p>
    <p><b>Valor total:</b> R$ {quote['totals']['grand_total']:.2f}<br/>
       <b>À vista (10% off):</b> R$ {quote['totals']['cash_total']:.2f}<br/>
       <b>Cartão:</b> {quote['totals']['card_installments']}x de R$ {quote['totals']['card_installment_value']:.2f} (sem juros)</p>
    <p>— {COMPANY_NAME}</p>
    """

    try:
        send_email_sendgrid(
            to_email=payload.client.email,
            cc_email=seller_email,
            subject=subject,
            html_body=html_body,
            pdf_bytes=pdf_bytes,
            filename=f"Proposta_{payload.client.name.replace(' ', '_')}.pdf"
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {
        "ok": True,
        "sent_to": payload.client.email,
        "cc": seller_email,
        "totals": quote["totals"],
        "items_count": len(quote["breakdown"]),
    }

# Endpoint opcional para Twilio bater (depois você pluga Flow aqui ou redireciona)
@app.post("/twilio/webhook")
async def twilio_webhook(req: Request):
    # Por enquanto só confirma que recebeu (para testar Twilio)
    form = await req.form()
    return {"ok": True, "from": form.get("From"), "body": form.get("Body")}
