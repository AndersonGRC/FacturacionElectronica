"""
Servicio de emisión compartido.

Centraliza la lógica de crear un documento electrónico (factura / nota crédito /
nota débito): validación, idempotencia, inserción en estado PENDIENTE con su fecha
programada y encolado en Celery.

Lo usan:
  - La API REST  → routes/facturas.py::crear_factura (X-API-Key), con DELAY_DIAS.
  - El portal    → routes/ui.py (emisión del tenant), con un plazo de gracia corto.
  - El módulo de pruebas del administrador.
"""

import os
import uuid

import psycopg2.extras
from database import get_db_cursor
from tasks.facturacion import procesar_factura

CAMPOS_REQ = ['referencia_pedido', 'cliente', 'items', 'metodo_pago']
TIPOS_DOC_VALIDOS = {'CC', 'CE', 'NIT', 'TI', 'PA', 'RC'}
METODOS_VALIDOS = {'10', '20', '30', '41', '42', '43', '44', '45', '46', '47', '48', '49'}
TIPOS_DOCUMENTO = {'factura', 'nota_credito', 'nota_debito'}


class EmisionError(Exception):
    """Error de validación de emisión (apto para HTTP 4xx / flash)."""

    def __init__(self, mensaje: str, status: int = 422):
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.status = status


def _registrar_evento(factura_id, evento, detalle=None):
    try:
        with get_db_cursor() as cur:
            cur.execute(
                "INSERT INTO factura_eventos (factura_id, evento, detalle) VALUES (%s,%s,%s)",
                (factura_id, evento, detalle))
    except Exception:
        pass


def validar(datos: dict):
    """Valida el payload. Lanza EmisionError si algo está mal."""
    if not isinstance(datos, dict) or not datos:
        raise EmisionError("Se requiere un cuerpo JSON válido", 400)

    faltantes = [c for c in CAMPOS_REQ if c not in datos]
    if faltantes:
        raise EmisionError(f"Campos requeridos faltantes: {faltantes}")

    if not isinstance(datos.get('items'), list) or len(datos['items']) == 0:
        raise EmisionError("items debe ser una lista no vacía")

    err = _validar_cliente(datos.get('cliente', {}))
    if err:
        raise EmisionError(err)

    for idx, item in enumerate(datos['items']):
        err = _validar_item(item, idx)
        if err:
            raise EmisionError(err)

    if str(datos.get('metodo_pago', '')).strip() not in METODOS_VALIDOS:
        raise EmisionError(f"metodo_pago inválido. Válidos: {sorted(METODOS_VALIDOS)}")

    tipo = datos.get('tipo_documento', 'factura')
    if tipo not in TIPOS_DOCUMENTO:
        raise EmisionError(f"tipo_documento inválido. Válidos: {sorted(TIPOS_DOCUMENTO)}")

    if tipo in ('nota_credito', 'nota_debito'):
        ref = datos.get('documento_referencia') or {}
        if not ref.get('numero') or not ref.get('cufe'):
            raise EmisionError("Las notas requieren documento_referencia con numero y cufe "
                               "(la factura electrónica que ajustan).")


def emitir_documento(tenant_id, datos: dict, *, delay_seconds: int = None,
                     requiere_aprobacion: bool = False) -> dict:
    """
    Registra el documento y lo encola. Idempotente por (tenant_id, referencia_pedido).

    delay_seconds: segundos hasta el envío programado. Si es None, usa DELAY_DIAS (días).
    requiere_aprobacion: si True, NO se envía automáticamente — queda esperando que el
        usuario lo apruebe (enviar) o rechace (anular) desde el portal.
    Retorna dict con: existente(bool), id, estado, procesar_en (iso), mensaje.
    """
    validar(datos)
    referencia = datos['referencia_pedido']

    # Idempotencia
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            """SELECT id, estado, numero_factura, cufe, creado_en
               FROM facturas WHERE tenant_id = %s AND referencia_pedido = %s""",
            (str(tenant_id), referencia))
        existente = cur.fetchone()
    if existente:
        row = dict(existente)
        return {
            'existente': True,
            'id': str(row['id']), 'estado': row['estado'],
            'numero_factura': row.get('numero_factura'), 'cufe': row.get('cufe'),
            'procesar_en': None,
            'mensaje': "Ya existe un documento para esta referencia de pedido.",
        }

    if delay_seconds is None:
        delay_seconds = int(os.getenv('DELAY_DIAS', '2')) * 86400

    factura_id = str(uuid.uuid4())
    with get_db_cursor() as cur:
        cur.execute(
            """INSERT INTO facturas
               (id, tenant_id, referencia_pedido, estado, datos_json, procesar_en,
                requiere_aprobacion)
               VALUES (%s, %s, %s, 'PENDIENTE', %s, NOW() + (%s || ' seconds')::INTERVAL, %s)""",
            (factura_id, str(tenant_id), referencia,
             psycopg2.extras.Json(datos), int(delay_seconds), bool(requiere_aprobacion)))

    _registrar_evento(factura_id, 'RECIBIDA',
                      'Pendiente de tu aprobación (no se enviará solo)' if requiere_aprobacion
                      else f"Programada para envío en {int(delay_seconds)//60} min")

    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT procesar_en FROM facturas WHERE id = %s", (factura_id,))
        row = cur.fetchone()
    procesar_en = row['procesar_en'].isoformat() if row and row['procesar_en'] else None

    return {
        'existente': False, 'id': factura_id, 'estado': 'PENDIENTE',
        'procesar_en': procesar_en,
        'mensaje': "Documento recibido. Será enviado a la DIAN al vencer el plazo; "
                   "puede anularse antes desde el portal.",
    }


def enviar_ahora(factura_id: str):
    """Adelanta el envío de un PENDIENTE: procesar_en = NOW() y encola la tarea."""
    with get_db_cursor() as cur:
        cur.execute("""UPDATE facturas SET procesar_en = NOW(), requiere_aprobacion = FALSE
                       WHERE id = %s AND estado = 'PENDIENTE' RETURNING id""", (factura_id,))
        ok = cur.fetchone()
    if ok:
        task = procesar_factura.delay(factura_id)
        with get_db_cursor() as cur:
            cur.execute("UPDATE facturas SET celery_task_id=%s WHERE id=%s", (task.id, factura_id))
    return bool(ok)


# ── Validadores ──────────────────────────────────────────────────────────────

def _validar_cliente(cliente):
    if not isinstance(cliente, dict):
        return "cliente debe ser un objeto JSON"
    for campo in ['tipo_persona', 'tipo_documento', 'numero_documento', 'nombre', 'email']:
        if campo not in cliente:
            return f"cliente.{campo} es requerido"
    if cliente['tipo_persona'] not in ('natural', 'juridica'):
        return "cliente.tipo_persona debe ser 'natural' o 'juridica'"
    if str(cliente['tipo_documento']).upper() not in TIPOS_DOC_VALIDOS:
        return f"cliente.tipo_documento inválido. Válidos: {sorted(TIPOS_DOC_VALIDOS)}"
    if not str(cliente['numero_documento']).strip():
        return "cliente.numero_documento no puede estar vacío"
    if not str(cliente.get('nombre', '')).strip():
        return "cliente.nombre no puede estar vacío"
    if '@' not in str(cliente.get('email', '')):
        return "cliente.email debe tener formato válido"
    return None


def _validar_item(item, idx):
    if not isinstance(item, dict):
        return f"items[{idx}] debe ser un objeto JSON"
    for campo in ['descripcion', 'cantidad', 'precio_unitario']:
        if campo not in item:
            return f"items[{idx}].{campo} es requerido"
    try:
        if float(item['cantidad']) <= 0:
            return f"items[{idx}].cantidad debe ser mayor a 0"
    except (ValueError, TypeError):
        return f"items[{idx}].cantidad debe ser numérico"
    try:
        if float(item['precio_unitario']) < 0:
            return f"items[{idx}].precio_unitario no puede ser negativo"
    except (ValueError, TypeError):
        return f"items[{idx}].precio_unitario debe ser numérico"
    if 'impuesto_iva' in item:
        try:
            iva = float(item['impuesto_iva'])
            if not (0 <= iva <= 100):
                return f"items[{idx}].impuesto_iva debe estar entre 0 y 100"
        except (ValueError, TypeError):
            return f"items[{idx}].impuesto_iva debe ser numérico"
    return None
