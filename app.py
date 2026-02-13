from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

def render_pdf_reportlab(context: dict) -> bytes:
    """
    context esperado:
      - company_name
      - client {name,email,phone,address}
      - breakdown [{label, qty, unit_price, material}]
      - totals {material_total, labor_total, grand_total, cash_total, card_installments, card_installment_value}
      - notes
    """
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    x = 40
    y = height - 50

    def line(txt, dy=16, bold=False):
        nonlocal y
        if bold:
            c.setFont("Helvetica-Bold", 11)
        else:
            c.setFont("Helvetica", 11)
        c.drawString(x, y, txt)
        y -= dy

    # Header
    c.setFont("Helvetica-Bold", 14)
    c.drawString(x, y, "Proposta de Automação")
    y -= 18
    c.setFont("Helvetica", 11)
    c.drawString(x, y, context.get("company_name", ""))
    y -= 24

    client = context["client"]
    line("Cliente", bold=True)
    line(f"Nome: {client.get('name','')}")
    line(f"E-mail: {client.get('email','')}")
    line(f"Telefone: {client.get('phone','')}")
    addr = client.get("address")
    if addr:
        line(f"Endereço: {addr}")
    y -= 10

    notes = (context.get("notes") or "").strip()
    if notes:
        line("Observações da vistoria", bold=True)
        for chunk in split_text(notes, 90):
            line(chunk)
        y -= 10

    # Materiais (tabela simples)
    line("Materiais", bold=True)

    # Cabeçalho tabela
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x, y, "Item")
    c.drawString(x + 260, y, "Qtd")
    c.drawString(x + 310, y, "Unit")
    c.drawString(x + 390, y, "Total")
    y -= 14

    c.setFont("Helvetica", 10)
    for row in context["breakdown"]:
        if y < 120:
            c.showPage()
            y = height - 50

        c.drawString(x, y, row["label"][:38])
        c.drawRightString(x + 290, y, str(row["qty"]))
        c.drawRightString(x + 370, y, f"R$ {row['unit_price']:.2f}")
        c.drawRightString(x + 500, y, f"R$ {row['material']:.2f}")
        y -= 14

    y -= 10
    totals = context["totals"]

    line(f"Total materiais: R$ {totals['material_total']:.2f}", bold=True)
    line(f"Mão de obra: R$ {totals['labor_total']:.2f}", bold=True)
    line(f"Valor total: R$ {totals['grand_total']:.2f}", bold=True)
    y -= 6

    line("Condições de pagamento", bold=True)
    line(f"À vista (10% desconto): R$ {totals['cash_total']:.2f}")
    line(f"Cartão: {totals['card_installments']}x sem juros de R$ {totals['card_installment_value']:.2f}")

    c.showPage()
    c.save()
    return buf.getvalue()

def split_text(text: str, max_len: int):
    words = text.split()
    lines, cur = [], []
    cur_len = 0
    for w in words:
        if cur_len + len(w) + (1 if cur else 0) > max_len:
            lines.append(" ".join(cur))
            cur = [w]
            cur_len = len(w)
        else:
            cur.append(w)
            cur_len += len(w) + (1 if cur_len else 0)
    if cur:
        lines.append(" ".join(cur))
    return lines
