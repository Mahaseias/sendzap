import os
import json
import base64
import time
import re
from io import BytesIO
from typing import Optional, Dict, Any, List

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, EmailStr
from urllib.parse import parse_qs
from twilio.twiml.messaging_response import MessagingResponse

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

# Sessao de wizard em memoria (MVP)
SESSIONS: Dict[str, Dict[str, Any]] = {}


# Sessao do webhook Twilio WhatsApp (MVP)
SESSION_TTL_SECONDS = 2 * 60 * 60  # 2h
_sessions: Dict[str, Dict[str, Any]] = {}  # key: seller_phone (whatsapp:+55...)


# Catalogo MVP do novo fluxo WhatsApp
CATALOG = [
    {"key": "lampadas", "label": "Lampadas"},
    {"key": "interruptores", "label": "Interruptores"},
    {"key": "sensores", "label": "Sensores"},
    {"key": "modulos", "label": "Modulos"},
    {"key": "contatoras", "label": "Contatoras"},
    {"key": "irrigacao", "label": "Irrigacao"},
    {"key": "central_comando", "label": "Central de comando"},
    {"key": "comando_voz", "label": "Comando por voz"},
]

KEY_BY_INDEX = {i + 1: item["key"] for i, item in enumerate(CATALOG)}
LABEL_BY_KEY = {item["key"]: item["label"] for item in CATALOG}

# Mapeia catalogo do WhatsApp para SKUs usados no quote atual
CATALOG_TO_SKU = {
    "lampadas": "lamp_smart",
    "interruptores": "switch_smart",
    "sensores": "sensor_presence",
    "modulos": "relay_2ch",
    "contatoras": "relay_4ch",
    "irrigacao": "smart_plug",
    "central_comando": "relay_1ch",
    "comando_voz": "ir_tv",
}


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
def _now() -> float:
    return time.time()


def _cleanup_sessions():
    t = _now()
    expired = [k for k, v in _sessions.items() if (t - v.get("updated_at", 0)) > SESSION_TTL_SECONDS]
    for k in expired:
        _sessions.pop(k, None)


def _get_session(seller: str) -> Dict[str, Any]:
    _cleanup_sessions()
    s = _sessions.get(seller)
    if not s:
        s = {
            "state": "MENU",
            "draft": {
                "client_name": None,
                "client_email": None,
                "selected_keys": [],
                "quantities": {},  # key -> int
            },
            "updated_at": _now(),
        }
        _sessions[seller] = s
    return s


def _set_state(s: Dict[str, Any], state: str):
    s["state"] = state
    s["updated_at"] = _now()


def _reset_draft(s: Dict[str, Any]):
    s["draft"] = {
        "client_name": None,
        "client_email": None,
        "selected_keys": [],
        "quantities": {},
    }
    s["updated_at"] = _now()


def _normalize_text(text: str) -> str:
    return (text or "").strip()


def _is_email(email: str) -> bool:
    email = email.strip()
    return bool(re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$", email))


def _render_catalog_menu() -> str:
    lines = [
        "Selecione os itens digitando os numeros separados por virgula.",
        "Exemplo: 1,3,8",
        "",
    ]
    for i, item in enumerate(CATALOG, start=1):
        lines.append(f"{i}) {item['label']}")
    lines.append("")
    lines.append("Comandos: cancelar | menu")
    return "\n".join(lines)


def _summary(draft: Dict[str, Any]) -> str:
    client = f"{draft.get('client_name')} <{draft.get('client_email')}>"
    items = draft.get("selected_keys", [])
    qty = draft.get("quantities", {})
    lines = [
        "Resumo da proposta:",
        f"Cliente: {client}",
        "",
    ]
    if not items:
        lines.append("(nenhum item selecionado)")
    else:
        for k in items:
            lines.append(f"- {LABEL_BY_KEY.get(k, k)}: {qty.get(k, 0)}")
    lines.append("")
    lines.append("Responda com: confirmar | editar | cancelar")
    return "\n".join(lines)


def _next_qty_key(draft: Dict[str, Any]) -> Optional[str]:
    for k in draft.get("selected_keys", []):
        if k not in draft.get("quantities", {}):
            return k
    return None


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


WIZARD_ITEMS_ORDER = [
    ("lamp_smart", "Quantas lampadas inteligentes? (0-99)"),
    ("relay_2ch", "Quantos modulos rele 2 canais? (0-99)"),
    ("sensor_presence", "Quantos sensores de presenca? (0-99)"),
]


def twiml(message: str) -> str:
    # resposta simples TwiML (WhatsApp)
    safe = (message or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{safe}</Message></Response>'


def get_session(user_id: str) -> Dict[str, Any]:
    if user_id not in SESSIONS:
        SESSIONS[user_id] = {
            "step": "idle",
            "seller_phone": user_id.replace("whatsapp:", "").replace(" ", ""),
            "client": {"name": "", "email": "", "phone": "", "address": ""},
            "items": {k: 0 for k, _ in WIZARD_ITEMS_ORDER},
            "notes": "",
        }
    return SESSIONS[user_id]


def reset_session(user_id: str):
    if user_id in SESSIONS:
        del SESSIONS[user_id]


def parse_int_0_99(text: str) -> Optional[int]:
    t = (text or "").strip()
    if not t.isdigit():
        return None
    n = int(t)
    if 0 <= n <= 99:
        return n
    return None


def next_item_key(idx: int) -> Optional[str]:
    if 0 <= idx < len(WIZARD_ITEMS_ORDER):
        return WIZARD_ITEMS_ORDER[idx][0]
    return None


def next_item_prompt(idx: int) -> Optional[str]:
    if 0 <= idx < len(WIZARD_ITEMS_ORDER):
        return WIZARD_ITEMS_ORDER[idx][1]
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


@app.post("/twilio/whatsapp")
async def twilio_whatsapp(request: Request):
    """
    Webhook inbound do WhatsApp via Twilio.
    Recebe form-url-encoded (Body, From, etc).
    Responde TwiML (MessagingResponse).
    """
    form = await request.form()
    incoming = _normalize_text(form.get("Body", ""))
    seller = form.get("From", "")  # ex: "whatsapp:+5567..."

    s = _get_session(seller)
    draft = s["draft"]
    state = s["state"]

    # comandos globais
    cmd = incoming.lower()
    if cmd in {"cancelar", "cancel", "cancela", "3"}:
        _reset_draft(s)
        _set_state(s, "MENU")
        resp = MessagingResponse()
        resp.message("Ok. Proposta cancelada.\n\nDigite:\n1) Iniciar proposta\n2) Continuar proposta")
        return PlainTextResponse(str(resp), media_type="application/xml")

    if cmd in {"menu", "inicio", "start"}:
        _set_state(s, "MENU")
        resp = MessagingResponse()
        resp.message("Menu:\n1) Iniciar proposta\n2) Continuar proposta\n3) Cancelar")
        return PlainTextResponse(str(resp), media_type="application/xml")

    resp = MessagingResponse()

    if state == "MENU":
        if cmd in {"1", "iniciar", "nova", "novo"}:
            _reset_draft(s)
            _set_state(s, "CLIENT_NAME")
            resp.message("Vamos la. Qual o nome do cliente (ou empresa)?")
            return PlainTextResponse(str(resp), media_type="application/xml")

        if cmd in {"2", "continuar", "retomar"}:
            if not draft.get("client_name"):
                _reset_draft(s)
                _set_state(s, "CLIENT_NAME")
                resp.message("Nao encontrei proposta em aberto. Vamos iniciar.\n\nQual o nome do cliente?")
            else:
                if not draft.get("client_email"):
                    _set_state(s, "CLIENT_EMAIL")
                    resp.message(f"Retomando. Cliente: {draft.get('client_name')}\n\nInforme o email do cliente:")
                elif not draft.get("selected_keys"):
                    _set_state(s, "SELECT_ITEMS")
                    resp.message(_render_catalog_menu())
                else:
                    k = _next_qty_key(draft)
                    if k:
                        _set_state(s, "QTY")
                        resp.message(f"Quantas {LABEL_BY_KEY.get(k, k)}?")
                    else:
                        _set_state(s, "SUMMARY")
                        resp.message(_summary(draft))
            return PlainTextResponse(str(resp), media_type="application/xml")

        resp.message("Menu:\n1) Iniciar proposta\n2) Continuar proposta\n3) Cancelar")
        return PlainTextResponse(str(resp), media_type="application/xml")

    if state == "CLIENT_NAME":
        if len(incoming) < 2:
            resp.message("Nome muito curto. Informe o nome do cliente/empresa:")
            return PlainTextResponse(str(resp), media_type="application/xml")
        draft["client_name"] = incoming
        s["updated_at"] = _now()
        _set_state(s, "CLIENT_EMAIL")
        resp.message("Agora informe o email do cliente:")
        return PlainTextResponse(str(resp), media_type="application/xml")

    if state == "CLIENT_EMAIL":
        if not _is_email(incoming):
            resp.message("Email invalido. Digite novamente (ex.: nome@empresa.com):")
            return PlainTextResponse(str(resp), media_type="application/xml")
        draft["client_email"] = incoming
        s["updated_at"] = _now()
        _set_state(s, "SELECT_ITEMS")
        resp.message(_render_catalog_menu())
        return PlainTextResponse(str(resp), media_type="application/xml")

    if state == "SELECT_ITEMS":
        numbers = re.findall(r"\d+", incoming)
        chosen: List[str] = []
        for n in numbers:
            idx = int(n)
            if idx in KEY_BY_INDEX:
                chosen.append(KEY_BY_INDEX[idx])
        chosen = list(dict.fromkeys(chosen))  # unique keep order

        if not chosen:
            resp.message("Nao entendi. Selecione por numeros (ex.: 1,3,8)\n\n" + _render_catalog_menu())
            return PlainTextResponse(str(resp), media_type="application/xml")

        draft["selected_keys"] = chosen
        draft["quantities"] = {}
        s["updated_at"] = _now()
        _set_state(s, "QTY")
        k = _next_qty_key(draft)
        resp.message(f"Perfeito. Quantas {LABEL_BY_KEY.get(k, k)}?")
        return PlainTextResponse(str(resp), media_type="application/xml")

    if state == "QTY":
        k = _next_qty_key(draft)
        if not k:
            _set_state(s, "SUMMARY")
            resp.message(_summary(draft))
            return PlainTextResponse(str(resp), media_type="application/xml")

        m = re.findall(r"\d+", incoming)
        if not m:
            resp.message(f"Digite apenas um numero.\nQuantas {LABEL_BY_KEY.get(k, k)}?")
            return PlainTextResponse(str(resp), media_type="application/xml")
        q = int(m[0])
        if q < 0 or q > 100000:
            resp.message("Quantidade invalida. Digite um numero valido.")
            return PlainTextResponse(str(resp), media_type="application/xml")

        if q == 0:
            draft["selected_keys"] = [x for x in draft["selected_keys"] if x != k]
        else:
            draft["quantities"][k] = q

        s["updated_at"] = _now()
        nextk = _next_qty_key(draft)
        if nextk:
            resp.message(f"Quantas {LABEL_BY_KEY.get(nextk, nextk)}?")
        else:
            _set_state(s, "SUMMARY")
            resp.message(_summary(draft))
        return PlainTextResponse(str(resp), media_type="application/xml")

    if state == "SUMMARY":
        if cmd in {"editar", "edit", "2"}:
            _set_state(s, "SELECT_ITEMS")
            resp.message("Ok. Vamos editar os itens.\n\n" + _render_catalog_menu())
            return PlainTextResponse(str(resp), media_type="application/xml")

        if cmd in {"confirmar", "confirm", "1"}:
            _set_state(s, "SUBMIT")
            try:
                items: Dict[str, int] = {}
                for k in draft.get("selected_keys", []):
                    qty = int(draft["quantities"].get(k, 0))
                    if qty <= 0:
                        continue
                    sku = CATALOG_TO_SKU.get(k, k)
                    items[sku] = items.get(sku, 0) + qty

                payload = FlowSubmit(
                    seller_phone=seller.replace("whatsapp:", ""),
                    client=Client(
                        name=draft["client_name"],
                        email=draft["client_email"],
                        phone="",
                    ),
                    items=items,
                    notes=None,
                )
                flow_submit(payload)

                _reset_draft(s)
                _set_state(s, "MENU")
                resp.message("Proposta enviada com sucesso para o email do cliente.\n\nDigite:\n1) Iniciar proposta\n2) Continuar proposta")
                return PlainTextResponse(str(resp), media_type="application/xml")
            except HTTPException as e:
                _set_state(s, "SUMMARY")
                resp.message(f"Falha ao gerar/enviar: {e.detail}\n\nResponda: confirmar | editar | cancelar")
                return PlainTextResponse(str(resp), media_type="application/xml")
            except Exception as e:
                _set_state(s, "SUMMARY")
                resp.message(f"Falha ao gerar/enviar: {e}\n\nResponda: confirmar | editar | cancelar")
                return PlainTextResponse(str(resp), media_type="application/xml")

        resp.message("Responda com: confirmar | editar | cancelar")
        return PlainTextResponse(str(resp), media_type="application/xml")

    _set_state(s, "MENU")
    resp.message("Menu:\n1) Iniciar proposta\n2) Continuar proposta\n3) Cancelar")
    return PlainTextResponse(str(resp), media_type="application/xml")


@app.post("/twilio/webhook", response_class=PlainTextResponse)
async def twilio_webhook(request: Request):
    """
    Webhook Twilio (WhatsApp)
    Recebe form-encoded:
      - From: whatsapp:+55...
      - Body: mensagem do usuario
    """
    raw = await request.body()
    form = parse_qs(raw.decode("utf-8"))

    from_ = (form.get("From", [""])[0] or "").strip()
    body = (form.get("Body", [""])[0] or "").strip()
    text = body.lower().strip()

    if not from_:
        return PlainTextResponse(twiml("Erro: From vazio."), media_type="application/xml")

    s = get_session(from_)

    # comandos globais
    if text in ("cancelar", "cancel", "sair", "reset"):
        reset_session(from_)
        return PlainTextResponse(twiml("Sessao cancelada. Para iniciar: digite 'nova'."), media_type="application/xml")

    if text in ("ajuda", "help", "?"):
        return PlainTextResponse(twiml(
            "Comandos:\n- nova (inicia proposta)\n- cancelar (zera sessao)\n\nSiga as perguntas e responda com os valores."
        ), media_type="application/xml")

    # iniciar
    if s["step"] == "idle":
        if text in ("nova", "iniciar", "proposta", "orcamento", "orçamento"):
            s["step"] = "client_name"
            return PlainTextResponse(twiml("Vamos iniciar a proposta.\n\nNome do cliente?"), media_type="application/xml")
        return PlainTextResponse(twiml("Digite 'nova' para iniciar uma proposta ou 'ajuda'."), media_type="application/xml")

    # coleta cliente
    if s["step"] == "client_name":
        if len(body) < 2:
            return PlainTextResponse(twiml("Nome invalido. Informe o nome do cliente:"), media_type="application/xml")
        s["client"]["name"] = body
        s["step"] = "client_email"
        return PlainTextResponse(twiml("E-mail do cliente?"), media_type="application/xml")

    if s["step"] == "client_email":
        # validacao simples (deixa o pydantic validar depois tambem)
        if "@" not in body or "." not in body:
            return PlainTextResponse(twiml("E-mail invalido. Informe novamente:"), media_type="application/xml")
        s["client"]["email"] = body
        s["step"] = "client_phone"
        return PlainTextResponse(twiml("Telefone do cliente? (ex: +55 67 99999-9999)"), media_type="application/xml")

    if s["step"] == "client_phone":
        if len(body) < 8:
            return PlainTextResponse(twiml("Telefone invalido. Informe novamente:"), media_type="application/xml")
        s["client"]["phone"] = body
        s["step"] = "client_address"
        return PlainTextResponse(twiml("Endereco do cliente? (ou digite 0 para pular)"), media_type="application/xml")

    if s["step"] == "client_address":
        s["client"]["address"] = "" if body.strip() == "0" else body.strip()
        s["step"] = "items_0"
        return PlainTextResponse(twiml(next_item_prompt(0) or "Informe o primeiro item."), media_type="application/xml")

    # itens (sequencial)
    if s["step"].startswith("items_"):
        idx = int(s["step"].split("_")[1])
        n = parse_int_0_99(body)
        if n is None:
            return PlainTextResponse(twiml("Valor invalido. Responda com numero de 0 a 99."), media_type="application/xml")

        key = next_item_key(idx)
        if key:
            s["items"][key] = n

        next_idx = idx + 1
        if next_idx < len(WIZARD_ITEMS_ORDER):
            s["step"] = f"items_{next_idx}"
            return PlainTextResponse(twiml(next_item_prompt(next_idx) or "Informe o proximo item."), media_type="application/xml")

        s["step"] = "notes"
        return PlainTextResponse(twiml("Observacoes da vistoria? (ou digite 0 para pular)"), media_type="application/xml")

    # notes
    if s["step"] == "notes":
        s["notes"] = "" if body.strip() == "0" else body.strip()
        # preview de totais
        quote = compute_quote(s["items"])
        totals = quote["totals"]
        s["step"] = "confirm"

        msg = (
            "Resumo:\n"
            f"- Materiais: R$ {totals['material_total']:.2f}\n"
            f"- Mao de obra: R$ {totals['labor_total']:.2f}\n"
            f"- Total: R$ {totals['grand_total']:.2f}\n"
            f"- A vista: R$ {totals['cash_total']:.2f}\n"
            f"- 3x: R$ {totals['card_installment_value']:.2f}\n\n"
            "Gerar e enviar proposta agora? (sim/nao)"
        )
        return PlainTextResponse(twiml(msg), media_type="application/xml")

    # confirm
    if s["step"] == "confirm":
        if text in ("nao", "não", "n"):
            s["step"] = "idle"
            return PlainTextResponse(twiml("Ok. Sessao encerrada. Digite 'nova' para iniciar outra."), media_type="application/xml")

        if text not in ("sim", "s", "yes", "y"):
            return PlainTextResponse(twiml("Responda apenas 'sim' ou 'nao'."), media_type="application/xml")

        # envia proposta (reusa pipeline atual)
        try:
            # evita CC duplicado
            seller_email = resolve_seller_email(s["seller_phone"])
            if seller_email and seller_email.strip().lower() == str(s["client"]["email"]).strip().lower():
                seller_email = None

            quote = compute_quote(s["items"])
            context = {
                "company_name": COMPANY_NAME,
                "client": s["client"],
                "notes": s["notes"],
                "breakdown": quote["breakdown"],
                "totals": quote["totals"],
            }
            pdf_bytes = render_pdf_reportlab(context)

            subject = f"Proposta - Automacao - {s['client']['name']}"
            html_body = f"""
            <p>Ola, <b>{s['client']['name']}</b>!</p>
            <p>Segue em anexo sua proposta de automacao.</p>
            <p>
              <b>Valor total:</b> R$ {quote['totals']['grand_total']:.2f}<br/>
              <b>A vista (10% off):</b> R$ {quote['totals']['cash_total']:.2f}<br/>
              <b>Cartao:</b> {quote['totals']['card_installments']}x de R$ {quote['totals']['card_installment_value']:.2f} (sem juros)
            </p>
            <p>- {COMPANY_NAME}</p>
            """

            send_email_sendgrid(
                to_email=str(s["client"]["email"]),
                cc_email=seller_email,
                subject=subject,
                html_body=html_body,
                pdf_bytes=pdf_bytes,
                filename=f"Proposta_{s['client']['name'].replace(' ', '_')}.pdf",
            )

        except Exception as e:
            # mantem sessao para tentar novamente
            return PlainTextResponse(twiml(f"Falha ao enviar: {e}"), media_type="application/xml")

        # encerra e limpa
        reset_session(from_)
        return PlainTextResponse(twiml("Proposta gerada e enviada por e-mail. Digite 'nova' para outra."), media_type="application/xml")

    # fallback
    return PlainTextResponse(twiml("Nao entendi. Digite 'ajuda'."), media_type="application/xml")
