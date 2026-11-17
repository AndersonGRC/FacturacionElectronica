"""Avisos de lo que deja a un negocio sin poder facturar (FE-17).

- Certificado digital: vencido o a menos de 30 días (con el certificado vencido
  la DIAN rechaza todo).
- Resolución de numeración: vencida o a menos de 60 días.
- Rango de numeración: usado al 80 % o más.

Se muestran en el portal (al negocio en su inicio y al administrador en la
ficha) y una vez al día se envían por correo al negocio, desde su propio
correo de entrega (services/entrega.py), si lo tiene configurado.
"""
import logging
import time
from datetime import date, datetime, timezone

from database import get_db_cursor

logger = logging.getLogger(__name__)

DIAS_CERTIFICADO = 30
DIAS_RESOLUCION = 60
PORCENTAJE_RANGO = 80
_CACHE_CERT = {}                 # (ruta, mtime) -> fecha de vencimiento
_CACHE_AVISOS = {}               # tenant_id -> (instante, avisos)
_TTL_S = 300


def vencimiento_certificado(tenant: dict):
    """Fecha en que vence el .p12 del negocio, o None si no se puede leer."""
    ruta = tenant.get('cert_path')
    if not ruta or not tenant.get('cert_password_enc'):
        return None
    try:
        import os
        clave = (ruta, os.path.getmtime(ruta))
        if clave not in _CACHE_CERT:
            from cryptography.hazmat.primitives.serialization import pkcs12
            from utils.encryption import descifrar
            with open(ruta, 'rb') as f:
                _llave, cert, _cadena = pkcs12.load_key_and_certificates(
                    f.read(), descifrar(tenant['cert_password_enc']).encode('utf-8'))
            vence = getattr(cert, 'not_valid_after_utc', None) or \
                cert.not_valid_after.replace(tzinfo=timezone.utc)
            _CACHE_CERT[clave] = vence.date()
        return _CACHE_CERT[clave]
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"alertas: no se pudo leer el certificado de {tenant.get('id')}: {type(exc).__name__}")
        return None


def _dias(fecha, hoy):
    if isinstance(fecha, datetime):
        fecha = fecha.date()
    if isinstance(fecha, str):
        try:
            fecha = date.fromisoformat(fecha[:10])
        except ValueError:
            return None
    return (fecha - hoy).days if isinstance(fecha, date) else None


def avisos(tenant: dict, hoy: date = None, vence_cert=None) -> list:
    """[{'nivel': 'danger'|'warning', 'clave', 'texto'}] del negocio."""
    hoy = hoy or date.today()
    salida = []

    vence = vence_cert if vence_cert is not None else vencimiento_certificado(tenant)
    d = _dias(vence, hoy)
    if d is not None and d < 0:
        salida.append({'nivel': 'danger', 'clave': 'certificado',
                       'texto': f'El certificado digital venció el {vence}. La DIAN rechaza todo '
                                'hasta que subas uno nuevo.'})
    elif d is not None and d <= DIAS_CERTIFICADO:
        salida.append({'nivel': 'warning', 'clave': 'certificado',
                       'texto': f'El certificado digital vence en {d} días ({vence}). Renuévalo y '
                                'súbelo antes de esa fecha para no dejar de facturar.'})

    d = _dias(tenant.get('resolucion_vigencia'), hoy)
    if d is not None and d < 0:
        salida.append({'nivel': 'danger', 'clave': 'resolucion',
                       'texto': 'La resolución de numeración venció. Solicita una nueva en la DIAN.'})
    elif d is not None and d <= DIAS_RESOLUCION:
        salida.append({'nivel': 'warning', 'clave': 'resolucion',
                       'texto': f'La resolución de numeración vence en {d} días. Solicita la nueva '
                                'en la DIAN con tiempo.'})

    try:
        desde, hasta = int(tenant.get('resolucion_desde')), int(tenant.get('resolucion_hasta'))
        actual = int(tenant.get('consecutivo_actual') or 0)
        total = hasta - desde + 1
        usados = max(0, actual - desde + 1)
        if total > 0 and actual >= hasta:
            salida.append({'nivel': 'danger', 'clave': 'rango',
                           'texto': 'Se agotó el rango de numeración. Solicita una nueva resolución.'})
        elif total > 0 and usados * 100 >= total * PORCENTAJE_RANGO:
            salida.append({'nivel': 'warning', 'clave': 'rango',
                           'texto': f'Ya usaste el {usados * 100 // total} % de la numeración '
                                    f'(quedan {hasta - actual}). Solicita una nueva resolución.'})
    except (TypeError, ValueError):
        pass
    return salida


def avisos_de(tenant_id) -> list:
    """Avisos del negocio para las pantallas del portal (caché de 5 min). Nunca lanza."""
    if not tenant_id:
        return []
    clave = str(tenant_id)
    guardado = _CACHE_AVISOS.get(clave)
    if guardado and time.time() - guardado[0] < _TTL_S:
        return guardado[1]
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute("SELECT * FROM tenants WHERE id = %s AND activo = TRUE", (clave,))
            t = cur.fetchone()
        lista = avisos(dict(t)) if t else []
    except Exception as exc:  # noqa: BLE001
        logger.warning(f'alertas: no se pudieron calcular para {clave}: {exc}')
        lista = []
    _CACHE_AVISOS[clave] = (time.time(), lista)
    return lista


def enviar_avisos_diarios() -> dict:
    """Correo diario a cada negocio con avisos, desde su correo de entrega.
    Sin correo configurado no se envía (el aviso sigue en el portal)."""
    from services.entrega import correo_valido, enviar_smtp, faltantes_config, leer_config
    enviados = sin_correo = 0
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT * FROM tenants WHERE activo = TRUE")
        tenants = [dict(t) for t in cur.fetchall()]
    for t in tenants:
        lista = avisos(t)
        if not lista:
            continue
        cfg = leer_config(t)
        destino = (t.get('email') or '').strip()
        if faltantes_config(cfg) or not correo_valido(destino):
            sin_correo += 1
            continue
        cuerpo = ('Hola,\n\nTu facturación electrónica necesita atención:\n\n'
                  + '\n'.join(f'- {a["texto"]}' for a in lista)
                  + '\n\nEste aviso se repite cada día hasta que se resuelva.\n')
        try:
            enviar_smtp(cfg, destino, f"Facturación electrónica de {t.get('nombre')}: requiere atención",
                        cuerpo)
            enviados += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"alertas: no se pudo avisar a {t.get('id')}: {type(exc).__name__}")
    return {'enviados': enviados, 'sin_correo': sin_correo}
