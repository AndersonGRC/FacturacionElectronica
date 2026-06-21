"""
pdf_builder: representación gráfica (PDF) de los documentos electrónicos DIAN.

Genera un PDF bonito y PERSONALIZABLE por cliente (logo + color de marca leídos
del tenant), con todos los campos que exige la DIAN: emisor, adquiriente,
resolución, detalle, totales, CUFE/CUDE y el código QR de verificación.

Stack: Jinja2 (HTML) + xhtml2pdf (HTML→PDF) + qrcode (QR). Sin dependencias del
sistema. Funciona para factura, nota crédito y nota débito.
"""

import base64
import io
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import qrcode
from jinja2 import Environment, FileSystemLoader, select_autoescape
from xhtml2pdf import pisa

_TPL_DIR = Path(__file__).parent.parent / 'templates'
_env = Environment(loader=FileSystemLoader(str(_TPL_DIR)),
                   autoescape=select_autoescape(['html', 'xml']))

TITULOS = {
    'factura':      'FACTURA ELECTRÓNICA DE VENTA',
    'nota_credito': 'NOTA CRÉDITO ELECTRÓNICA',
    'nota_debito':  'NOTA DÉBITO ELECTRÓNICA',
}

# Color representativo y formal por tipo de documento. La factura usa el color de
# marca del cliente; las notas llevan un color propio para distinguirse a simple vista.
COLOR_POR_TIPO = {
    'nota_credito': '#1B7A43',   # verde formal (crédito a favor)
    'nota_debito':  '#B45309',   # ámbar/ocre formal (cargo adicional)
}

METODO_PAGO = {
    '10': 'Efectivo', '20': 'Cheque', '30': 'Transferencia', '48': 'Tarjeta crédito',
    '49': 'Tarjeta débito', '47': 'Transferencia bancaria', '42': 'Consignación',
    '45': 'Tarjeta', '41': 'Otro',
}


def _d(v):
    return Decimal(str(v)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _money(v):
    """Formato moneda colombiana: $ 1.234.567,89"""
    q = _d(v)
    entero, _, dec = f"{q:.2f}".partition('.')
    neg = entero.startswith('-')
    entero = entero.lstrip('-')
    grupos = []
    while len(entero) > 3:
        grupos.insert(0, entero[-3:]); entero = entero[:-3]
    grupos.insert(0, entero)
    return ('-' if neg else '') + '$ ' + '.'.join(grupos) + ',' + dec


def _qr_data_uri(url: str) -> str:
    qr = qrcode.QRCode(box_size=4, border=1)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')


def _logo_data_uri(logo: str) -> str:
    """Incrusta el logo del cliente como data URI, normalizado para verse bien en PDF.

    Aplana la transparencia sobre fondo blanco (xhtml2pdf renderiza mal el alpha) y
    reescala si es muy grande. Acepta URL http(s) o ruta local.
    """
    if not logo:
        return ''
    if logo.startswith('data:'):
        return logo
    try:
        if logo.startswith(('http://', 'https://')):
            with urllib.request.urlopen(logo, timeout=8) as r:
                raw = r.read()
        else:
            raw = Path(logo).read_bytes()
    except Exception:
        return ''
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        # Aplanar transparencia sobre blanco
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGBA')
            fondo = Image.new('RGB', img.size, (255, 255, 255))
            fondo.paste(img, mask=img.split()[-1])
            img = fondo
        else:
            img = img.convert('RGB')
        # Reescalar si es muy ancho (mejor nitidez/relación en el PDF)
        if img.width > 600:
            img = img.resize((600, max(1, int(img.height * 600 / img.width))))
        out = io.BytesIO()
        img.save(out, format='PNG')
        return 'data:image/png;base64,' + base64.b64encode(out.getvalue()).decode('ascii')
    except Exception:
        # Si PIL falla, devolver el original tal cual
        return 'data:image/png;base64,' + base64.b64encode(raw).decode('ascii')


def _qr_url(tenant, cufe):
    base = ('https://catalogo-vpfe-hab.dian.gov.co'
            if (tenant.get('ambiente') or 'habilitacion') == 'habilitacion'
            else 'https://catalogo-vpfe.dian.gov.co')
    return f"{base}/document/searchqr?documentkey={cufe}"


def generar_pdf(tenant: dict, datos: dict, numero: str, cufe: str,
                fecha: str, hora: str, estado: str = 'ACEPTADA') -> bytes:
    tipo = datos.get('tipo_documento', 'factura')
    if tipo not in TITULOS:
        tipo = 'factura'
    responsable_iva = str(tenant.get('regimen_codigo') or '48') == '48'

    # Detalle y totales
    items, subtotal, total_iva = [], Decimal('0'), Decimal('0')
    for it in datos.get('items', []):
        cant = _d(it.get('cantidad', 1)); precio = _d(it.get('precio_unitario', 0))
        desc = _d(it.get('descuento', 0))
        iva_pct = _d(it.get('impuesto_iva', 19)) if responsable_iva else Decimal('0')
        base = _d(cant * precio - desc)
        iva_val = _d(base * iva_pct / Decimal('100'))
        subtotal += base; total_iva += iva_val
        items.append({
            'codigo': it.get('codigo', ''), 'descripcion': it.get('descripcion', ''),
            'cantidad': f"{cant:g}", 'precio': _money(precio), 'descuento': _money(desc),
            'iva_pct': f"{iva_pct:g}%", 'total': _money(base),
        })
    total = subtotal + total_iva

    cliente = datos.get('cliente', {})
    qr_url = _qr_url(tenant, cufe)

    ctx = {
        'titulo': TITULOS[tipo],
        'es_nota': tipo != 'factura',
        'numero': numero, 'cufe': cufe, 'fecha': fecha, 'hora': hora,
        'estado': estado,
        'ambiente_prueba': (tenant.get('ambiente') or 'habilitacion') == 'habilitacion',
        'color': COLOR_POR_TIPO.get(tipo) or tenant.get('color_primario') or '#0B5394',
        'logo_url': _logo_data_uri(tenant.get('logo_url') or ''),
        'emisor': {
            'nombre': tenant.get('razon_social', ''),
            'nit': f"{tenant.get('nit','')}-{tenant.get('digito_verificacion','')}",
            'direccion': tenant.get('direccion', ''),
            'ciudad': tenant.get('municipio_nombre', ''),
            'email': tenant.get('email', ''),
            'telefono': tenant.get('telefono', ''),
            'regimen': 'Responsable de IVA' if responsable_iva else 'No responsable de IVA',
            'responsabilidad': tenant.get('responsabilidad_fiscal', ''),
        },
        'adquiriente': {
            'nombre': cliente.get('nombre', 'Consumidor Final'),
            'doc': f"{cliente.get('tipo_documento','CC')} {cliente.get('numero_documento','')}",
            'direccion': cliente.get('direccion', ''),
            'email': cliente.get('email', ''),
            'telefono': cliente.get('telefono', ''),
        },
        'resolucion': {
            'numero': tenant.get('resolucion_dian', ''),
            'prefijo': tenant.get('prefijo', ''),
            'desde': tenant.get('resolucion_desde', ''),
            'hasta': tenant.get('resolucion_hasta', ''),
            'vigencia': tenant.get('resolucion_vigencia', ''),
        },
        'doc_ref': datos.get('documento_referencia', {}) or {},
        'concepto': datos.get('concepto_nota', {}) or {},
        'items': items,
        'subtotal': _money(subtotal), 'iva': _money(total_iva), 'total': _money(total),
        'moneda': datos.get('moneda', 'COP'),
        'metodo_pago': METODO_PAGO.get(str(datos.get('metodo_pago', '10')), 'Otro'),
        'notas': datos.get('notas', ''),
        'qr_b64': _qr_data_uri(qr_url),
        'qr_url': qr_url,
    }

    html = _env.get_template('factura_pdf.html').render(**ctx)
    buf = io.BytesIO()
    pisa.CreatePDF(src=html, dest=buf, encoding='utf-8')
    return buf.getvalue()
