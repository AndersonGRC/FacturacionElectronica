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

GRACE_MAXIMO = 21600   # 15 días en minutos: la espera más larga que se puede elegir


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

    # Respetar el plazo/modo de aprobación POR TENANT (el "N días que decide el
    # cliente") — igual que el portal. Si es manual, no se envía hasta aprobar.
    from config import Config
    modo  = tenant.get('modo_aprobacion') or 'automatico'
    grace = tenant.get('grace_minutos')
    grace = Config.GRACE_MINUTES if grace is None else int(grace)
    try:
        if modo == 'manual':
            res = emitir_documento(tenant['id'], datos, delay_seconds=60,
                                   requiere_aprobacion=True)
        else:
            res = emitir_documento(tenant['id'], datos, delay_seconds=grace * 60)
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

    Cancela (reversa) una factura que AÚN NO fue enviada a la DIAN.

    Se puede cancelar mientras esté PENDIENTE y:
      - siga dentro de la ventana de gracia (procesar_en > NOW()), o
      - esté esperando aprobación manual (requiere_aprobacion = TRUE), en cuyo
        caso NUNCA se envía sola, así que siempre debe poder reversarse.

    Antes solo se miraba `procesar_en > NOW()`: en modo manual `procesar_en` es
    ahora+60s, por lo que pasado un minuto la factura quedaba imposible de
    cancelar por API aunque jamás se hubiera enviado.
    """
    tenant = request.tenant
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            """UPDATE facturas
               SET estado = 'CANCELADA'
               WHERE id = %s AND tenant_id = %s
                 AND estado = 'PENDIENTE'
                 AND (procesar_en > NOW() OR COALESCE(requiere_aprobacion, FALSE) IS TRUE)
               RETURNING id""",
            (factura_id, tenant['id'])
        )
        updated = cur.fetchone()

    if not updated:
        return jsonify({"error": "No se puede cancelar: la factura no existe, "
                                 "ya fue procesada, o el plazo de cancelación venció"}), 409

    _registrar_evento(factura_id, 'CANCELADA', 'Cancelada por el cliente antes del envío')
    return jsonify({"id": factura_id, "estado": "CANCELADA", "mensaje": "Factura cancelada"}), 200


@facturas_bp.route('/config-envio', methods=['GET', 'PUT'])
@requiere_tenant
def config_envio_api():
    """
    GET  /api/v1/config-envio  → política de envío del tenant
    PUT  /api/v1/config-envio  → la cambia

    Existía solo como pantalla del portal (sesión), así que la app del cliente
    no podía leerla ni cambiarla: el "parametrizable desde el admin" quedaba
    fuera de alcance. Campos:
      modo_aprobacion: 'manual' (nada sale sin aprobación) | 'automatico'
      grace_minutos:   minutos de espera antes del envío en modo automático,
                       que es la ventana para reversar.
    """
    tenant = request.tenant

    if request.method == 'GET':
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute("""SELECT COALESCE(modo_aprobacion, 'automatico') AS modo_aprobacion,
                                  grace_minutos
                           FROM tenants WHERE id = %s""", (tenant['id'],))
            row = cur.fetchone() or {}
        from config import Config
        grace = row.get('grace_minutos')
        return jsonify({
            'modo_aprobacion': row.get('modo_aprobacion') or 'automatico',
            'grace_minutos':   Config.GRACE_MINUTES if grace is None else int(grace),
            'grace_maximo':    GRACE_MAXIMO,
        })

    datos = request.get_json(silent=True) or {}
    modo  = str(datos.get('modo_aprobacion') or '').strip().lower()
    if modo not in ('manual', 'automatico'):
        return jsonify({"error": "modo_aprobacion debe ser 'manual' o 'automatico'"}), 400

    # Tope de 15 días, el mismo que ofrece el portal (PRESETS_ENVIO): evita que un
    # error de captura deje documentos esperando indefinidamente sin que nadie lo note.
    try:
        grace = int(datos.get('grace_minutos'))
    except (TypeError, ValueError):
        return jsonify({"error": "grace_minutos debe ser un entero de minutos"}), 400
    if not 0 <= grace <= GRACE_MAXIMO:
        return jsonify({"error": f"grace_minutos debe estar entre 0 y {GRACE_MAXIMO} (15 días)"}), 400

    with get_db_cursor() as cur:
        cur.execute("""UPDATE tenants
                       SET modo_aprobacion = %s, grace_minutos = %s, actualizado_en = NOW()
                       WHERE id = %s""", (modo, grace, tenant['id']))

    return jsonify({'modo_aprobacion': modo, 'grace_minutos': grace,
                    'mensaje': 'Política de envío actualizada'})


@facturas_bp.route('/facturas', methods=['GET'])
@requiere_tenant
def buscar_factura():
    """
    GET /api/v1/facturas?referencia={referencia_pedido}

    Busca una factura por la referencia de pedido del cliente.
    """
    tenant     = request.tenant
    referencia = request.args.get('referencia', '').strip()

    # Sin 'referencia' → LISTADO (bandeja del panel). Retrocompatible: con
    # 'referencia' mantiene la búsqueda puntual de siempre.
    if not referencia:
        estado = (request.args.get('estado') or '').strip().upper()
        try:
            limite = max(1, min(int(request.args.get('limite', 50)), 200))
        except (TypeError, ValueError):
            limite = 50
        sql = """SELECT id, estado, numero_factura, cufe, referencia_pedido,
                        error_mensaje, intentos, requiere_aprobacion,
                        procesar_en, creado_en, actualizado_en,
                        (estado = 'PENDIENTE'
                         AND (procesar_en > NOW()
                              OR COALESCE(requiere_aprobacion, FALSE) IS TRUE)) AS cancelable
                   FROM facturas
                  WHERE tenant_id = %s"""
        params = [tenant['id']]
        if estado:
            sql += " AND estado = %s"
            params.append(estado)
        sql += " ORDER BY creado_en DESC LIMIT %s"
        params.append(limite)
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute(sql, tuple(params))
            filas = cur.fetchall()
        items = []
        for f in filas:
            r = dict(f)
            r['id'] = str(r['id'])
            for campo in ('procesar_en', 'creado_en', 'actualizado_en'):
                r[campo] = r[campo].isoformat() if r.get(campo) else None
            r['cancelable'] = bool(r.get('cancelable'))   # lo resuelve el SQL
            items.append(r)
        return jsonify({"items": items, "total": len(items)})

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


@facturas_bp.route('/facturas/<factura_id>/enviar', methods=['POST'])
@requiere_tenant
def enviar_factura(factura_id):
    """
    POST /api/v1/facturas/{id}/enviar

    Envía YA a la DIAN un documento que está en cola (PENDIENTE): el que espera
    aprobación manual o el que aún no cumple su espera. Es el mismo acto que el
    botón "Enviar ya" del portal, pero desde la app del cliente, que hasta ahora
    solo podía reversar. Queda en la bitácora quién lo aprobó.
    """
    tenant = request.tenant
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT estado FROM facturas WHERE id = %s AND tenant_id = %s",
                    (factura_id, tenant['id']))
        fila = cur.fetchone()
    if not fila:
        return jsonify({"error": "Factura no encontrada"}), 404
    if fila['estado'] != 'PENDIENTE':
        return jsonify({"error": f"No se puede enviar: el documento está {fila['estado']}"}), 409

    from services.emision import enviar_ahora
    aprobado_por = (request.headers.get('X-Usuario') or '').strip()[:80] or 'app del cliente'
    if not enviar_ahora(factura_id, aprobado_por=f"{aprobado_por} (API)"):
        return jsonify({"error": "No se pudo enviar: el documento cambió de estado"}), 409
    return jsonify({"id": factura_id, "estado": "PENDIENTE",
                    "mensaje": "Enviando a la DIAN"}), 200


@facturas_bp.route('/me', methods=['GET'])
@requiere_tenant
def quien_soy():
    """
    GET /api/v1/me

    A nombre de quién factura esta API key, en qué ambiente y si está lista para
    emitir. Sirve para que la app del cliente compruebe la conexión y muestre
    "tus facturas salen a nombre de …" (sin secretos: ni certificado ni claves).
    """
    t = request.tenant
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("""SELECT (cert_path IS NOT NULL AND cert_path <> '') AS certificado,
                              (clave_tecnica IS NOT NULL AND clave_tecnica <> '') AS clave_tecnica,
                              (software_id IS NOT NULL AND software_id <> '') AS software
                       FROM tenants WHERE id = %s""", (t['id'],))
        listo = dict(cur.fetchone() or {})
    from config import Config
    grace = t.get('grace_minutos')
    vigencia = t.get('resolucion_vigencia')
    return jsonify({
        'nombre': t.get('nombre'),
        'razon_social': t.get('razon_social'),
        'nit': t.get('nit'),
        'dv': t.get('digito_verificacion'),
        'ambiente': t.get('ambiente') or 'habilitacion',
        'prefijo': t.get('prefijo'),
        'resolucion': t.get('resolucion_dian'),
        'rango_desde': t.get('resolucion_desde'),
        'rango_hasta': t.get('resolucion_hasta'),
        'vigencia_hasta': vigencia.isoformat() if hasattr(vigencia, 'isoformat') else vigencia,
        'consecutivo_actual': t.get('consecutivo_actual'),
        'modo_aprobacion': t.get('modo_aprobacion') or 'automatico',
        'grace_minutos': Config.GRACE_MINUTES if grace is None else int(grace),
        'grace_maximo': GRACE_MAXIMO,
        'portal_activo': bool(t.get('portal_activo')),
        'listo_para_emitir': bool(listo.get('certificado') and listo.get('clave_tecnica')
                                  and listo.get('software') and t.get('resolucion_dian')),
    })


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
