"""
Entrega del documento electrónico al comprador por correo.

Anexo Técnico 1.9 (Res. 000165 de 2023), numeral 9.1:
  - Asunto: NIT;Nombre del facturador;Número;Código del tipo;Nombre comercial
  - Un único .zip con el contenedor (AttachedDocument) y, opcional, el PDF.
  - Máximo 2 MB por envío.

Cada negocio envía desde SU correo (su servidor SMTP), configurado en el portal
(`tenants.correo_entrega`; la contraseña va cifrada con FERNET_KEY).

Reglas:
  - Solo se entregan documentos ACEPTADOS por la DIAN.
  - En producción la entrega es automática al quedar aceptado (si el negocio la
    encendió) y también se puede reenviar a mano.
  - En habilitación (pruebas) nunca se le escribe al comprador: el envío manual
    llega al correo del propio negocio, para que vea cómo queda.
  - Si la entrega falla, el documento sigue ACEPTADO: queda el evento en su
    historial y se puede reenviar desde el portal.
"""
import io
import ipaddress
import json
import logging
import re
import smtplib
import socket
import ssl
import zipfile
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from database import get_db_cursor
from services.contenedor import ContenedorError, construir
from services.signer import FirmadorDIAN
from services.storage import StorageManager
from utils.encryption import descifrar

logger = logging.getLogger(__name__)

CODIGO_TIPO = {'factura': '01', 'nota_credito': '91', 'nota_debito': '92'}
NOMBRE_TIPO = {'factura': 'factura electrónica', 'nota_credito': 'nota crédito electrónica',
               'nota_debito': 'nota débito electrónica'}
LIMITE_ZIP = 2 * 1024 * 1024          # numeral 9.1: «Peso máximo por envío: 2 Megas»
SEGURIDADES = ('starttls', 'ssl')
TIMEOUT_SMTP = 30
_EMAIL = re.compile(r'^[^@\s;,<>"]+@[^@\s;,<>"]+\.[^@\s;,<>"]+$')


class EntregaError(Exception):
    """El envío no se puede hacer (tamaño, configuración)."""


def correo_valido(texto) -> bool:
    return bool(_EMAIL.match((texto or '').strip()))


def leer_config(tenant: dict) -> dict:
    """Configuración de correo del negocio (vacía si no hay o si falta la columna)."""
    cfg = (tenant or {}).get('correo_entrega') or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg or '{}')
        except ValueError:
            cfg = {}
    return dict(cfg) if isinstance(cfg, dict) else {}


def faltantes_config(cfg: dict) -> list:
    """Lo que falta para poder enviar; lista vacía = completa."""
    falta = []
    if not (cfg.get('host') or '').strip():
        falta.append('servidor de correo')
    if not str(cfg.get('puerto') or '').strip():
        falta.append('puerto')
    if not correo_valido(cfg.get('remitente')):
        falta.append('correo remitente')
    if (cfg.get('usuario') or '').strip() and not cfg.get('password_cifrada'):
        falta.append('contraseña')
    return falta


def _limpio(texto) -> str:
    """El «;» separa los campos del asunto: no puede ir dentro de un nombre."""
    return ' '.join(str(texto or '').replace(';', ' ').split())


def asunto_dian(tenant: dict, numero: str, tipo: str) -> str:
    """NIT;Nombre del facturador;Número;Código del tipo;Nombre comercial."""
    return ';'.join([
        _limpio(tenant.get('nit')),
        _limpio(tenant.get('razon_social') or tenant.get('nombre')),
        _limpio(numero),
        CODIGO_TIPO.get(tipo or 'factura', '01'),
        _limpio(tenant.get('nombre') or tenant.get('razon_social')),
    ])


def armar_zip(numero: str, contenedor: bytes, pdf: bytes = None):
    """Un solo .zip con el contenedor y, si cabe en 2 MB, el PDF.

    Devuelve (bytes_del_zip, incluye_pdf).
    """
    def _zip(con_pdf):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr(f'{numero}.xml', contenedor)
            if con_pdf:
                z.writestr(f'{numero}.pdf', pdf)
        return buf.getvalue()

    if pdf:
        datos = _zip(True)
        if len(datos) <= LIMITE_ZIP:
            return datos, True
    datos = _zip(False)
    if len(datos) > LIMITE_ZIP:
        raise EntregaError('El documento pesa más de 2 MB, el máximo que permite la DIAN '
                           'por correo.')
    return datos, False


def _mensaje_error(exc: Exception, cfg: dict) -> str:
    """Error del correo en palabras simples (nunca incluye la contraseña)."""
    destino = f"{cfg.get('host')}:{cfg.get('puerto')}"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ('el servidor de correo rechazó el usuario o la contraseña. Si es Gmail u '
                'Outlook, usa una «contraseña de aplicación».')
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return 'el servidor de correo rechazó la dirección de destino.'
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return 'el servidor de correo no acepta ese remitente con ese usuario.'
    if isinstance(exc, ssl.SSLError):
        return f'falló la conexión segura con {destino}: revisa el puerto y el tipo de seguridad.'
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return f'el servidor de correo {destino} no respondió a tiempo.'
    if isinstance(exc, (ConnectionError, socket.gaierror, OSError)) \
            and not isinstance(exc, smtplib.SMTPException):
        return f'no se pudo conectar con el servidor de correo {destino}.'
    if isinstance(exc, EntregaError):
        return str(exc)
    return f'error del servidor de correo: {str(exc)[:200]}'


def _verificar_host(host: str):
    """El servidor de correo debe estar en internet.

    El servidor lo escribe cada negocio: sin este control podría apuntar a
    servicios internos del VPS (base de datos, Redis, la VPN).
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise EntregaError(f'No se encontró el servidor de correo {host}.')
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split('%')[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise EntregaError('El servidor de correo debe ser una dirección pública de internet.')


def enviar_smtp(cfg: dict, para: str, asunto: str, cuerpo: str,
                adjunto: bytes = None, nombre_adjunto: str = None, responder_a: str = None):
    """Envía un correo con el servidor SMTP del negocio."""
    seguridad = cfg.get('seguridad') or 'starttls'
    if seguridad not in SEGURIDADES:
        raise EntregaError('El tipo de seguridad del correo no es válido.')
    host = (cfg.get('host') or '').strip()
    puerto = int(cfg.get('puerto'))
    _verificar_host(host)
    usuario = (cfg.get('usuario') or '').strip()
    password = descifrar(cfg['password_cifrada']) if cfg.get('password_cifrada') else ''

    msg = EmailMessage()
    msg['Subject'] = asunto
    msg['From'] = formataddr(((cfg.get('nombre') or '').strip(), cfg['remitente'].strip()))
    msg['To'] = para
    if responder_a and correo_valido(responder_a) \
            and responder_a.strip().lower() != cfg['remitente'].strip().lower():
        msg['Reply-To'] = responder_a.strip()
    msg['Message-ID'] = make_msgid(domain=cfg['remitente'].split('@')[-1].strip())
    msg.set_content(cuerpo)
    if adjunto:
        msg.add_attachment(adjunto, maintype='application', subtype='zip',
                           filename=nombre_adjunto or 'documento.zip')

    contexto = ssl.create_default_context()
    if seguridad == 'ssl':
        servidor = smtplib.SMTP_SSL(host, puerto, timeout=TIMEOUT_SMTP, context=contexto)
    else:
        servidor = smtplib.SMTP(host, puerto, timeout=TIMEOUT_SMTP)
    try:
        if seguridad == 'starttls':
            servidor.ehlo()
            servidor.starttls(context=contexto)
            servidor.ehlo()
        if usuario:
            servidor.login(usuario, password)
        servidor.send_message(msg)
    finally:
        try:
            servidor.quit()
        except Exception:
            pass


def enviar_prueba(cfg: dict, tenant: dict):
    """Correo de prueba al propio remitente, para comprobar la configuración."""
    nombre = tenant.get('nombre') or tenant.get('razon_social') or 'tu negocio'
    enviar_smtp(cfg, cfg['remitente'], f'Prueba del correo de entrega — {_limpio(nombre)}',
                'Este es un correo de prueba del portal tributario.\n\n'
                'Si lo estás leyendo, la configuración funciona: desde esta cuenta se le '
                'enviará a cada comprador su documento electrónico cuando la DIAN lo acepte.\n')


def _registrar(factura_id: str, evento: str, detalle: str):
    try:
        with get_db_cursor() as cur:
            cur.execute("INSERT INTO factura_eventos (factura_id, evento, detalle) "
                        "VALUES (%s, %s, %s)", (factura_id, evento, detalle[:1000]))
    except Exception as e:
        logger.warning(f"No se pudo registrar el evento {evento} de {factura_id}: {e}")


def _cargar(factura_id: str):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT * FROM facturas WHERE id = %s", (factura_id,))
        f = cur.fetchone()
        if not f:
            return None, None
        cur.execute("SELECT * FROM tenants WHERE id = %s", (f['tenant_id'],))
        t = cur.fetchone()
    return dict(f), (dict(t) if t else None)


def _datos(f: dict) -> dict:
    d = f.get('datos_json') or {}
    if isinstance(d, str):
        d = json.loads(d or '{}')
    return d


def preparar_paquete(f: dict, t: dict):
    """Arma, firma y guarda el contenedor; devuelve (zip, incluye_pdf, ruta_contenedor)."""
    if f.get('estado') != 'ACEPTADA':
        raise EntregaError('Solo se entregan documentos aceptados por la DIAN.')
    for campo, nombre in (('xml_path', 'el XML firmado'), ('response_path', 'la respuesta de la DIAN')):
        if not f.get(campo) or not Path(f[campo]).exists():
            raise EntregaError(f'No se encuentra {nombre} de este documento.')
    numero = f['numero_factura']
    contenedor = construir(Path(f['xml_path']).read_bytes(),
                           Path(f['response_path']).read_bytes())
    firmador = FirmadorDIAN(t['cert_path'], descifrar(t['cert_password_enc']))
    contenedor = firmador.firmar(contenedor, minimo_extensiones=1)
    ruta = StorageManager(str(t['id'])).guardar_contenedor(contenedor, numero)
    pdf = None
    if f.get('pdf_path') and Path(f['pdf_path']).exists():
        pdf = Path(f['pdf_path']).read_bytes()
    datos_zip, con_pdf = armar_zip(numero, contenedor, pdf)
    return datos_zip, con_pdf, ruta


def paquete_para_descargar(factura_id: str):
    """(zip, nombre) para entregarlo a mano, p. ej. por WhatsApp."""
    f, t = _cargar(factura_id)
    if not f or not t:
        raise EntregaError('Documento no encontrado.')
    datos_zip, _con_pdf, _ruta = preparar_paquete(f, t)
    return datos_zip, f"{f['numero_factura']}.zip"


def _cuerpo(t: dict, f: dict, cliente: dict, con_pdf: bool, prueba: bool) -> str:
    tipo = _datos(f).get('tipo_documento') or 'factura'
    negocio = t.get('nombre') or t.get('razon_social') or ''
    clave = 'CUFE' if tipo == 'factura' else 'CUDE'
    lineas = []
    if prueba:
        lineas += ['ENVÍO DE PRUEBA (ambiente de habilitación): este documento no tiene '
                   'validez fiscal y no se le envió al comprador.', '']
    lineas += [
        f"Hola {cliente.get('nombre') or ''},".replace(' ,', ','),
        '',
        f"{negocio} te envía la {NOMBRE_TIPO.get(tipo, 'factura electrónica')} "
        f"{f['numero_factura']}, validada por la DIAN.",
        '',
        f"En el archivo {f['numero_factura']}.zip está el documento electrónico (XML)"
        + (' y su representación gráfica (PDF).' if con_pdf else '.'),
        f"{clave}: {f.get('cufe') or ''}",
        '',
        'Si tienes alguna pregunta sobre este documento, responde a este correo.',
    ]
    return '\n'.join(lineas) + '\n'


def entregar_factura(factura_id: str, manual: bool = False) -> dict:
    """Envía el documento al comprador. Devuelve {'estado', 'mensaje'}.

    estado: enviada | sin_config | apagada | sin_correo | pruebas | no_aceptada |
            no_existe | fallo
    """
    f, t = _cargar(factura_id)
    if not f or not t:
        return {'estado': 'no_existe', 'mensaje': 'Documento no encontrado.'}
    if f.get('estado') != 'ACEPTADA':
        return {'estado': 'no_aceptada',
                'mensaje': 'Solo se envían al comprador los documentos aceptados por la DIAN.'}

    produccion = t.get('ambiente') == 'produccion'
    if not produccion and not manual:
        # En pruebas no se escribe a nadie por su cuenta (el set de pruebas
        # emite decenas de documentos).
        return {'estado': 'pruebas', 'mensaje': 'En habilitación no se envía automáticamente.'}

    cfg = leer_config(t)
    falta = faltantes_config(cfg)
    if falta:
        msg = ('No se envió al comprador: falta configurar el correo de entrega ('
               + ', '.join(falta) + ').')
        _registrar(factura_id, 'ENTREGA', msg)
        return {'estado': 'sin_config', 'mensaje': msg}
    if not cfg.get('activo') and not manual:
        msg = ('No se envió al comprador: el envío automático está apagado en «Correo de '
               'entrega». Puedes enviarlo desde el detalle del documento.')
        _registrar(factura_id, 'ENTREGA', msg)
        return {'estado': 'apagada', 'mensaje': msg}

    cliente = _datos(f).get('cliente') or {}
    if produccion:
        destino = (cliente.get('email') or '').strip()
        if not correo_valido(destino):
            msg = ('El comprador no dio un correo válido: entrégale el PDF impreso o descarga '
                   'el paquete (.zip) y envíaselo por otro medio.')
            _registrar(factura_id, 'ENTREGA', msg)
            return {'estado': 'sin_correo', 'mensaje': msg}
        nota = ''
    else:
        destino = cfg['remitente'].strip()
        nota = ' (prueba: en habilitación llega al correo del negocio, nunca al comprador)'

    try:
        datos_zip, con_pdf, _ruta = preparar_paquete(f, t)
    except (ContenedorError, EntregaError) as e:
        msg = f'No se pudo preparar el envío al comprador: {e}'
        _registrar(factura_id, 'ENTREGA_FALLIDA', msg)
        return {'estado': 'fallo', 'mensaje': msg}
    except Exception as e:
        logger.exception(f"Preparando la entrega de {factura_id}")
        msg = f'No se pudo preparar el envío al comprador: {str(e)[:200]}'
        _registrar(factura_id, 'ENTREGA_FALLIDA', msg)
        return {'estado': 'fallo', 'mensaje': msg}

    numero = f['numero_factura']
    tipo = _datos(f).get('tipo_documento') or 'factura'
    try:
        enviar_smtp(cfg, destino, asunto_dian(t, numero, tipo),
                    _cuerpo(t, f, cliente, con_pdf, prueba=not produccion),
                    datos_zip, f'{numero}.zip', responder_a=t.get('email'))
    except Exception as e:
        msg = f'No se pudo enviar a {destino}: {_mensaje_error(e, cfg)}'
        logger.warning(f"Entrega de {factura_id} falló: {type(e).__name__}")
        _registrar(factura_id, 'ENTREGA_FALLIDA', msg)
        # Reintentar solo sirve si el servidor no respondió; una clave mala o un
        # destino rechazado no se arreglan solos.
        definitivo = isinstance(e, (smtplib.SMTPAuthenticationError,
                                    smtplib.SMTPRecipientsRefused,
                                    smtplib.SMTPSenderRefused, EntregaError))
        return {'estado': 'fallo', 'mensaje': msg, 'reintentable': not definitivo}

    msg = (f"Enviada a {destino}{nota}. Adjunto: {numero}.zip "
           f"({'XML y PDF' if con_pdf else 'XML; el PDF no cupo en 2 MB'}).")
    _registrar(factura_id, 'ENTREGADA', msg)
    return {'estado': 'enviada', 'mensaje': msg, 'destino': destino}
