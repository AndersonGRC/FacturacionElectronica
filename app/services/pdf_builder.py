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
import os
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
# marca del cliente; las notas llevan el color semántico de CYBERSHOP Design System
# (text-success / text-warning) para distinguirse a simple vista.
COLOR_POR_TIPO = {
    'nota_credito': '#0E7A47',   # verde (crédito a favor)
    'nota_debito':  '#9C6414',   # ámbar oscuro (cargo adicional)
}
COLOR_MARCA = '#103098'          # blue-600 de CYBERSHOP Design System

_FUENTES = Path(__file__).parent.parent / 'assets' / 'fonts'
FUENTES = {
    'regular': (_FUENTES / 'WorkSans-Regular.ttf').as_posix(),
    'semibold': (_FUENTES / 'WorkSans-SemiBold.ttf').as_posix(),
    'bold': (_FUENTES / 'WorkSans-Bold.ttf').as_posix(),
}


def _registrar_fuentes():
    """Work Sans (la tipografía de CYBERSHOP Design System) registrada en reportlab y
    nombrada para xhtml2pdf. Se registra directo (no con @font-face): xhtml2pdf copia la
    fuente a un temporal que en Windows no puede reabrir. Si algo falla, queda Helvetica."""
    try:
        from reportlab.lib.fonts import addMapping
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from xhtml2pdf import default as xdefault
        pdfmetrics.registerFont(TTFont('WorkSans', FUENTES['regular']))
        pdfmetrics.registerFont(TTFont('WorkSans-Bold', FUENTES['bold']))
        pdfmetrics.registerFont(TTFont('WorkSansSemi', FUENTES['semibold']))
        addMapping('WorkSans', 0, 0, 'WorkSans')
        addMapping('WorkSans', 1, 0, 'WorkSans-Bold')
        addMapping('WorkSans', 0, 1, 'WorkSans')
        addMapping('WorkSans', 1, 1, 'WorkSans-Bold')
        addMapping('WorkSansSemi', 0, 0, 'WorkSansSemi')
        addMapping('WorkSansSemi', 1, 0, 'WorkSans-Bold')
        xdefault.DEFAULT_FONT['worksans'] = 'WorkSans'
        xdefault.DEFAULT_FONT['worksanssemi'] = 'WorkSansSemi'
        return True
    except Exception:  # noqa: BLE001
        return False


FUENTES_OK = _registrar_fuentes()

# Software y fabricante: la representación gráfica debe nombrarlos (Res. DIAN 000165 de 2023).
SOFTWARE = {
    'nombre': os.getenv('SOFTWARE_NOMBRE', 'Software CyberShop — Facturación electrónica'),
    'fabricante': os.getenv('SOFTWARE_FABRICANTE', 'Anderson German Roa Cabiativa'),
    'nit': os.getenv('SOFTWARE_FABRICANTE_NIT', '1027150819-7'),
}

TIPO_DOC = {'13': 'C.C.', 'CC': 'C.C.', '31': 'NIT', 'NIT': 'NIT', '22': 'C.E.', 'CE': 'C.E.',
            '41': 'Pasaporte', 'PA': 'Pasaporte', '42': 'Doc. extranjero', '12': 'T.I.', 'TI': 'T.I.',
            '11': 'Registro civil', '21': 'Tarjeta de extranjería', '47': 'PEP', '50': 'NIT extranjero'}
UNIDADES = {'94': 'Und', 'EA': 'Und', 'ZZ': 'Und', 'NIU': 'Und', 'KGM': 'Kg', 'GRM': 'g', 'MTR': 'm',
            'MTK': 'm²', 'LTR': 'L', 'HUR': 'Hora', 'DAY': 'Día', 'MON': 'Mes', 'SET': 'Juego'}

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


_UNIDADES_L = ('', 'un', 'dos', 'tres', 'cuatro', 'cinco', 'seis', 'siete', 'ocho', 'nueve', 'diez',
               'once', 'doce', 'trece', 'catorce', 'quince', 'dieciséis', 'diecisiete', 'dieciocho',
               'diecinueve', 'veinte', 'veintiún', 'veintidós', 'veintitrés', 'veinticuatro', 'veinticinco',
               'veintiséis', 'veintisiete', 'veintiocho', 'veintinueve')
_DECENAS = ('', '', '', 'treinta', 'cuarenta', 'cincuenta', 'sesenta', 'setenta', 'ochenta', 'noventa')
_CENTENAS = ('', 'ciento', 'doscientos', 'trescientos', 'cuatrocientos', 'quinientos', 'seiscientos',
             'setecientos', 'ochocientos', 'novecientos')


def _menor_mil(n: int) -> str:
    if n == 0:
        return ''
    if n == 100:
        return 'cien'
    c, resto = divmod(n, 100)
    partes = [_CENTENAS[c]] if c else []
    if resto < 30:
        partes.append(_UNIDADES_L[resto])
    else:
        d, u = divmod(resto, 10)
        partes.append(_DECENAS[d] + (f' y {_UNIDADES_L[u]}' if u else ''))
    return ' '.join(p for p in partes if p)


def en_letras(valor) -> str:
    """$ 1.234.567,50 → «Un millón doscientos treinta y cuatro mil quinientos sesenta y siete
    pesos con 50/100 M/CTE»."""
    q = _d(abs(_d(valor)))
    entero = int(q)
    centavos = int((q - entero) * 100)
    if entero == 0:
        texto = 'cero'
    else:
        partes = []
        millones, resto = divmod(entero, 1_000_000)
        miles, unidades = divmod(resto, 1000)
        if millones:
            mm, m = divmod(millones, 1000)
            bloque = []
            if mm:
                bloque.append('mil' if mm == 1 else f'{_menor_mil(mm)} mil')
            if m:
                bloque.append(_menor_mil(m))
            palabra = ' '.join(bloque)
            partes.append('un millón' if millones == 1 else f'{palabra} millones')
        if miles:
            partes.append('mil' if miles == 1 else f'{_menor_mil(miles)} mil')
        if unidades:
            partes.append(_menor_mil(unidades))
        texto = ' '.join(partes)
        if entero % 1_000_000 == 0:
            texto += ' de'
    texto = texto[0].upper() + texto[1:]
    return f'{texto} pesos con {centavos:02d}/100 M/CTE'


def _mezclar_con_blanco(color: str, cuanto: float = 0.9) -> str:
    """Tinte claro del color de marca (superficie sutil, como blue-50 del sistema)."""
    try:
        c = color.lstrip('#')
        r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
        mezcla = [round(x + (255 - x) * cuanto) for x in (r, g, b)]
        return '#%02X%02X%02X' % tuple(mezcla)
    except Exception:
        return '#EAF0FE'


def _qr_data_uri(url: str) -> str:
    qr = qrcode.QRCode(box_size=4, border=1)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')


def _ancho_logo(data_uri: str, alto: int = 48, maximo: int = 210) -> int:
    """Ancho que conserva la proporción del logo a la altura dada (xhtml2pdf lo estira si
    solo se le da la altura)."""
    if not data_uri.startswith('data:'):
        return 0
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(base64.b64decode(data_uri.split(',', 1)[1])))
        return max(1, min(maximo, round(alto * img.width / img.height)))
    except Exception:  # noqa: BLE001
        return 0


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
    bruto, descuentos = Decimal('0'), Decimal('0')
    for it in datos.get('items', []):
        cant = _d(it.get('cantidad', 1)); precio = _d(it.get('precio_unitario', 0))
        desc = _d(it.get('descuento', 0))
        iva_pct = _d(it.get('impuesto_iva', 19)) if responsable_iva else Decimal('0')
        base = _d(cant * precio - desc)
        iva_val = _d(base * iva_pct / Decimal('100'))
        subtotal += base; total_iva += iva_val
        bruto += _d(cant * precio); descuentos += desc
        unidad = str(it.get('unidad_medida') or it.get('unidad') or '94')
        items.append({
            'codigo': it.get('codigo', ''), 'descripcion': it.get('descripcion', ''),
            'unidad': UNIDADES.get(unidad, unidad),
            'cantidad': format(cant.normalize(), 'f') if cant != cant.to_integral() else str(int(cant)),
            'precio': _money(precio),
            'descuento': _money(desc) if desc else '—',
            'iva_pct': f"{iva_pct.normalize():f}%", 'total': _money(base),
        })
    total = subtotal + total_iva

    cliente = datos.get('cliente', {})
    qr_url = _qr_url(tenant, cufe)
    color = COLOR_POR_TIPO.get(tipo) or tenant.get('color_primario') or COLOR_MARCA
    logo = _logo_data_uri(tenant.get('logo_url') or '')
    logo_ancho = _ancho_logo(logo, alto=48, maximo=210)
    doc_tipo = str(cliente.get('tipo_documento', 'CC'))
    vencimiento = datos.get('fecha_vencimiento') or ''
    credito = bool(vencimiento) or str(datos.get('forma_pago', '1')) == '2'

    ctx = {
        'titulo': TITULOS[tipo],
        'es_nota': tipo != 'factura',
        'numero': numero, 'cufe': cufe, 'fecha': fecha, 'hora': hora,
        'vencimiento': vencimiento,
        'estado': estado,
        'ambiente_prueba': (tenant.get('ambiente') or 'habilitacion') == 'habilitacion',
        'color': color,
        'tinte': _mezclar_con_blanco(color, 0.92),
        'fuentes': FUENTES,
        'software': SOFTWARE,
        'responsable_iva': responsable_iva,
        'forma_pago': 'Crédito' if credito else 'Contado',
        'tipo_operacion': 'Estándar',
        'bruto': _money(bruto), 'descuentos': _money(descuentos),
        'total_letras': en_letras(total),
        'logo_url': logo,
        'logo_ancho': logo_ancho,
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
            'doc_tipo': TIPO_DOC.get(doc_tipo, doc_tipo),
            'doc_numero': cliente.get('numero_documento', ''),
            'tipo_persona': {'natural': 'natural', 'juridica': 'jurídica', '1': 'jurídica',
                             '2': 'natural'}.get(str(cliente.get('tipo_persona', '')).lower(), ''),
            'ciudad': cliente.get('municipio_nombre') or cliente.get('ciudad', ''),
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
