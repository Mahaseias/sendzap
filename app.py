import os
import json
import base64
from io import BytesIO
from typing import Optional, Dict, Any

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, EmailStr

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


# =========================
# CONFIG (Render Env Vars)
# =========================
COMPANY_NAME = os.getenv("COMPANY_NAME", "Marca Nova Digital")

# SendGrid
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")  # opcional no começo (dry-run)
FROM_EMAIL = os.getenv("FROM_EMAIL")              # precisa estar verificado no SendGrid p/ enviar

# Vendedores: JSON em string
# Ex: {"+5567999999999":{"name":"Vendedor 1","email":"vendedor@empresa.com"}}
SELLERS_JSON = os.getenv("SELLERS_JSON", "{}")

# Regras comerciais
INSTALL_RATE_DEFAULT = 0.40
DISCOUNT_CASH = 0.10
CARD_INSTALLMENTS = 3


# =========================
# PREÇOS (MVP) - ajuste depois
# =========================
UNIT_PRICES: Dict[str, float] = {
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

LABELS: Dict[str, str] = {
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


# =========================
# HELPERS
# =========================
def money(x: float) -> float:
    return round(float(x), 2)

def split_text(text: str, max_len: int):
    words = (text or "").split()
    lines, cur = [], []
    cur_len = 0
    for w in words:
        add_len = len(w) + (1 if cur else 0)
        if cur_len + add_len > max_len:
            lines.append(" ".join(cur))
            cur = [w]
            cur_len = len(w)
        else:
            cur.append(w)
            cur_len += add_len
    if cur:
        lines.append(" ".join(cur))
    return lines

def resolve_seller_email(seller_phone: str) -> Optional[str]:
    try:
        sellers = json.loads(SELLERS_JSON or "{}")
        seller = sellers.get(seller_phone)
        if not seller:
            return None
        return seller.get("email")
    except Exception:
        return None

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
        labor = qty_int * unit * INSTALL_RATE_DEFAULT  # 40% do item

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

def render_pdf_reportlab(context: dict) -> bytes:
    """
    Gera PDF (A4) com:
      - Materiais detalhados
      - Mão de obra (total)
      - Condições de pagamento
    """
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    x = 40
    y = height - 50

    def line(txt, dy=16, bold=False, size=11):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(x, y, txt)
        y -= dy

    # Header
    c.setFont("Helvetica-Bold", 15)
    c.drawString(x, y, "Proposta de Automação")
    y -= 18
    c.setFont("Helvetica", 11)
    c.drawString(x, y, context.get("company_name", COMPANY_NAME))
    y -= 24

    # Cliente
    client = context["client"]
    line("Cliente", bold=True)
    line(f"Nome: {client.get('name','')}")
    line(f"E-mail: {client.get('email','')}")
    line(f"Telefone: {client.get('phone','')}")
    addr = client.get("address")
    if addr:
        line(f"Endereço: {addr}")
    y -= 10

    # Observações
    notes = (context.get("notes") or "").strip()
    if notes:
        line("Observações da vistoria", bold=True)
        for chunk in split_text(notes, 95):
            line(chunk, size=10, dy=13)
        y -= 10

    # Materiais - tabela
    line("Materiais", bold=True)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(x, y, "Item")
    c.drawString(x + 270, y, "Qtd")
    c.drawString(x + 320, y, "Unit")
    c.drawString(x + 400, y, "Total")
    y -= 14

    c.setFont("Helvetica", 10)
    for row in context["breakdown"]:
        if y < 140:
            c.showPage()
            y = height - 50

        c.drawString(x, y, row["label"][:40])
        c.drawRightString(x + 300, y, str(row["qty"]))
        c.drawRightString(x + 380, y, f"R$ {row['unit_price']:.2f}")
        c.drawRightString(x + 520, y, f"R$ {row['material']:.2f}")
        y -= 14

    y -= 12

    totals = context["totals"]
    line(f"Total materiais: R$ {totals['material_total']:.2f}", bold=True)
    line(f"Mão de obra: R$ {totals['labor_total']:.2f}", bold=True)
    line(f"Valor total: R$ {totals['grand_total']:.2f}", bold=True)
    y -= 6

    line("Condições de pagamento", bold=True)
    line(f"À vista (10% desconto): R$ {totals['cash_total']:.2f}")
    line(f"Cartão: {totals['card_installments']}x sem juros de R$ {totals['card_installment_value']:.2f}")
    y -= 10

    c.setFont("Helvetica", 9)
    c.drawString(x, y, "Valores calculados automaticamente com base no escopo informado.")
    y -= 12

    c.showPage()
    c.save()
    return buf.getvalue()

def send_email_sendgrid(to_email: str, cc_email: Optional[str], subject: str, html_body: str, pdf_bytes: bytes, filename: str):
    """
    Envia e-mail via SendGrid com PDF anexado.
    Se SENDGRID_API_KEY ou FROM_EMAIL não estiverem setados, NÃO envia (dry-run) e retorna.
    """
    if not SENDGRID_API_KEY or not FROM_EMAIL:
        # Dry-run: backend funciona sem SendGrid configurado ainda.
        return {"sent": False, "reason": "SENDGRID_API_KEY/FROM_EMAIL not configured (dry-run)"}

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

    return {"sent": True}


# =========================
# API MODELS
# =========================
class Client(BaseModel):
    name: str
    email: EmailStr
    phone: str
    address: Optional[str] = None

class FlowSubmit(BaseModel):
    seller_phone: str
    client: Client
    items: Dict[str, int]
    notes: Optional[str] = None


# =========================
# FASTAPI APP
# =========================
app = FastAPI(title="Sendzap - Propostas Automação")


@app.get("/")
def root():
    return {"ok": True, "service": "sendzap"}

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/flow/submit")
def flow_submit(payload: FlowSubmit):
    quote = compute_quote(payload.items)

    context = {
        "company_name": COMPANY_NAME,
        "client": payload.client.model_dump(),
        "notes": payload.notes or "",
        "breakdown": quote["breakdown"],
        "totals": quote["totals"],
    }

    pdf_bytes = render_pdf_reportlab(context)

    seller_email = resolve_seller_email(payload.seller_phone)

    subject = f"Proposta - Automação - {payload.client.name}"
    html_body = f"""
    <p>Olá, <b>{payload.client.name}</b>!</p>
    <p>Segue em anexo sua proposta de automação.</p>
    <p>
      <b>Valor total:</b> R$ {quote['totals']['grand_total']:.2f}<br/>
      <b>À vista (10% off):</b> R$ {quote['totals']['cash_total']:.2f}<br/>
      <b>Cartão:</b> {quote['totals']['card_installments']}x de R$ {quote['totals']['card_installment_value']:.2f} (sem juros)
    </p>
    <p>— {COMPANY_NAME}</p>
    """

    try:
        send_result = send_email_sendgrid(
            to_email=str(payload.client.email),
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
        "sent_to": str(payload.client.email),
        "cc": seller_email,
        "sendgrid": send_result,
        "totals": quote["totals"],
        "items_count": len(quote["breakdown"]),
    }
