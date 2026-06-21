"""
Blueprint facturas: endpoints para envío y consulta de facturas electrónicas.
"""

import hashlib
import hmac
import os
import time
import uuid
from flask import Blueprint, request, jsonify
import psycopg2.extras
from database import get_db_cursor
from security import requiere_tenant
from tasks.facturacion import procesar_factura
from services.emision import emitir_documento, EmisionError

facturas_bp = Blueprint('facturas', __name__, url_prefix='/api/v1')


@facturas_bp.route('/facturas', methods=['POST'])
@requiere_tenant
def crear_factura():
    """
    POST /api/v1/facturas

    Recibe el JSON genérico de venta, registra la factura con estado PENDIENTE
    y encola la tarea Celery. Responde 202 INMEDIATAMENTE sin esperar el resultado.

    Idempotencia: si ya existe una factura con el mismo (tenant_id, referencia_pedido),
    retorna el registro existente con 200 OK — no crea duplicados.

    Body JSON requerido:
      referencia_pedido  (str)
      cliente            (dict: tipo_persona, tipo_documento, numero_documento,
                                nombre, email, telefono, direccion, municipio_codigo)
      items              (list: descripcion, cantidad, precio_unitario,
                                descuento, codigo_unidad, impuesto_iva)
      metodo_pago        (str: "10"=efectivo, "20"=cheque, "48"=tarjeta)

    Body JSON opcional:
      moneda  (default: "COP")
      notas   (str)
    """
    tenant = request.tenant
    datos  = request.get_json(silent=True)
    if not datos:
        return jsonify({"error": "Se requiere JSON en el body"}), 400

    try:
        res = emitir_documento(tenant['id'], datos)
    except EmisionError as e:
        return jsonify({"error": e.mensaje}), e.status

    if res['existente']:
        return jsonify({
            "id": res['id'], "estado": res['estado'],
            "numero_factura": res.get('numero_factura'), "cufe": res.get('cufe'),
            "mensaje": res['mensaje'],
        }), 200

    return jsonify({
        "id": res['id'], "estado": res['estado'],
        "procesar_en": res['procesar_en'], "mensaje": res['mensaje'],
    }), 202


@facturas_bp.route('/facturas/<factura_id>/estado', methods=['GET'])
@requiere_tenant
def consultar_estado(factura_id):
    """
    GET /api/v1/facturas/{id}/estado

    Consulta el estado actual de una factura (polling).
    Retorna estado, CUFE, número de factura y errores si los hay.

    Estados posibles:
      PENDIENTE   → en cola, aún no procesada
      PROCESANDO  → worker activo, construyendo/firmando/enviando
      ACEPTADA    → DIAN aceptó el documento (CUFE disponible)
      RECHAZADA   → DIAN rechazó el documento (error_mensaje describe el motivo)
      ERROR       → error técnico, ver error_mensaje; se reintentará si intentos < 3
    """
    tenant = request.tenant
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            """SELECT id, estado, numero_factura, cufe,
                      error_mensaje, intentos, creado_en, actualizado_en, procesar_en
               FROM facturas
               WHERE id = %s AND tenant_id = %s""",
            (factura_id, tenant['id'])
        )
        factura = cur.fetchone()

    if not factura:
        return jsonify({"error": "Factura no encontrada"}), 404

    row = dict(factura)
    row['id']             = str(row['id'])
    row['creado_en']      = row['creado_en'].isoformat()      if row['creado_en']      else None
    row['actualizado_en'] = row['actualizado_en'].isoformat() if row['actualizado_en'] else None
    row['procesar_en']    = row['procesar_en'].isoformat()    if row.get('procesar_en') else None
    return jsonify(row)


@facturas_bp.route('/facturas/<factura_id>/cancelar', methods=['POST'])
@requiere_tenant
def cancelar_factura(factura_id):
    """
    POST /api/v1/facturas/{id}/cancelar

    Cancela una factura PENDIENTE que aún no ha sido enviada a la DIAN.
    Solo se puede cancelar si el estado es PENDIENTE y procesar_en > NOW().
    """
    tenant = request.tenant
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            """UPDATE facturas
               SET estado = 'CANCELADA'
               WHERE id = %s AND tenant_id = %s
                 AND estado = 'PENDIENTE'
                 AND procesar_en > NOW()
               RETURNING id""",
            (factura_id, tenant['id'])
        )
        updated = cur.fetchone()

    if not updated:
        return jsonify({"error": "No se puede cancelar: la factura no existe, "
                                 "ya fue procesada, o el plazo de cancelación venció"}), 409

    _registrar_evento(factura_id, 'CANCELADA', 'Cancelada por el cliente antes del envío')
    return jsonify({"id": factura_id, "estado": "CANCELADA", "mensaje": "Factura cancelada"}), 200


@facturas_bp.route('/facturas', methods=['GET'])
@requiere_tenant
def buscar_factura():
    """
    GET /api/v1/facturas?referencia={referencia_pedido}

    Busca una factura por la referencia de pedido del cliente.
    """
    tenant     = request.tenant
    referencia = request.args.get('referencia', '').strip()

    if not referencia:
        return jsonify({"error": "Parámetro 'referencia' requerido"}), 400

    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            """SELECT id, estado, numero_factura, cufe,
                      referencia_pedido, error_mensaje, intentos,
                      creado_en, actualizado_en
               FROM facturas
               WHERE tenant_id = %s AND referencia_pedido = %s""",
            (tenant['id'], referencia)
        )
        factura = cur.fetchone()

    if not factura:
        return jsonify({"error": "Factura no encontrada para esa referencia"}), 404

    row = dict(factura)
    row['id']             = str(row['id'])
    row['creado_en']      = row['creado_en'].isoformat()      if row['creado_en']      else None
    row['actualizado_en'] = row['actualizado_en'].isoformat() if row['actualizado_en'] else None
    return jsonify(row)


@facturas_bp.route('/facturas/<factura_id>/eventos', methods=['GET'])
@requiere_tenant
def obtener_eventos(factura_id):
    """
    GET /api/v1/facturas/{id}/eventos

    Retorna el audit log completo de la factura (para diagnóstico).
    """
    tenant = request.tenant

    # Verificar que la factura pertenece al tenant
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            "SELECT id FROM facturas WHERE id = %s AND tenant_id = %s",
            (factura_id, tenant['id'])
        )
        if not cur.fetchone():
            return jsonify({"error": "Factura no encontrada"}), 404

        cur.execute(
            """SELECT evento, detalle, creado_en
               FROM factura_eventos
               WHERE factura_id = %s
               ORDER BY creado_en ASC""",
            (factura_id,)
        )
        eventos = cur.fetchall()

    return jsonify([
        {
            "evento":    e['evento'],
            "detalle":   e['detalle'],
            "creado_en": e['creado_en'].isoformat(),
        }
        for e in eventos
    ])


@facturas_bp.route('/facturas/<factura_id>/pdf', methods=['GET'])
@requiere_tenant
def descargar_pdf(factura_id):
    """GET /api/v1/facturas/{id}/pdf — representación gráfica (PDF)."""
    from flask import send_file
    from pathlib import Path
    tenant = request.tenant
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            "SELECT pdf_path, numero_factura FROM facturas WHERE id=%s AND tenant_id=%s",
            (factura_id, tenant['id'])
        )
        f = cur.fetchone()
    if not f or not f['pdf_path'] or not Path(f['pdf_path']).exists():
        return jsonify({"error": "PDF no disponible para esta factura"}), 404
    return send_file(f['pdf_path'], as_attachment=True,
                     download_name=f"{f['numero_factura']}.pdf", mimetype='application/pdf')


TIPOS_DOC_VALIDOS = {'CC','CE','NIT','TI','PA','RC'}

def _validar_cliente(cliente: dict):
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

def _validar_item(item: dict, idx: int):
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

def _registrar_evento(factura_id: str, evento: str, detalle: str = None):
    try:
        with get_db_cursor() as cur:
            cur.execute(
                """INSERT INTO factura_eventos (factura_id, evento, detalle)
                   VALUES (%s, %s, %s)""",
                (factura_id, evento, detalle)
            )
    except Exception:
        pass


@facturas_bp.route('/portal/token', methods=['GET'])
@requiere_tenant
def portal_token():
    """
    GET /api/v1/portal/token
    Header: X-API-Key <tenant_api_key>

    Genera un enlace SSO de 30 segundos para redirigir al usuario al portal
    sin que el cliente tenga que implementar HMAC.

    Respuesta:
      { "url": "https://portaltributario.../ui/tenant-auto-login?...", "expires_in": 30 }
    """
    tenant = request.tenant
    if not tenant.get('portal_activo', False):
        return jsonify({"error": "El portal no está habilitado para este tenant. "
                                 "Contacta al administrador."}), 403

    master_key = os.getenv('MASTER_API_KEY', '')
    ts         = int(time.time())
    tenant_id  = str(tenant['id'])
    token      = hmac.new(
        master_key.encode(),
        f"{ts}:{tenant_id}:portal-autologin".encode(),
        hashlib.sha256,
    ).hexdigest()

    base_url = os.getenv('PORTAL_BASE_URL',
                         'https://portaltributario.cybershopcol.com')
    url = f"{base_url}/ui/tenant-auto-login?tenant={tenant_id}&token={token}&ts={ts}"

    return jsonify({"url": url, "expires_in": 30})
