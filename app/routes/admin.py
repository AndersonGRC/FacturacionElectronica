"""
Blueprint admin: gestión de tenants y certificados.
Protegido con X-Master-Key — solo para administradores del microservicio.
"""

import hashlib
import json
import os
import uuid
from flask import Blueprint, request, jsonify
from database import get_db_cursor
from security import requiere_master_key
from utils.encryption import cifrar
from services.storage import StorageManager

admin_bp = Blueprint('admin', __name__, url_prefix='/api/v1/admin')


@admin_bp.route('/tenants', methods=['POST'])
@requiere_master_key
def crear_tenant():
    """
    Registra un nuevo tenant y genera su API Key.

    Body JSON requerido:
      nombre, nit, digito_verificacion, razon_social
    Body JSON opcional:
      ambiente (default: 'habilitacion'), prefijo, clave_tecnica

    IMPORTANTE: El api_key se retorna UNA SOLA VEZ en claro.
    El cliente DEBE guardarlo de inmediato — no puede recuperarse después.
    """
    datos = request.get_json(silent=True)
    if not datos:
        return jsonify({"error": "JSON requerido"}), 400

    campos_req = ['nombre', 'nit', 'digito_verificacion', 'razon_social']
    faltantes  = [c for c in campos_req if c not in datos]
    if faltantes:
        return jsonify({"error": f"Campos requeridos: {faltantes}"}), 422

    # Generar API Key aleatorio (64 chars hex = 256 bits de entropía)
    api_key      = os.urandom(32).hex()
    api_key_hash = hashlib.sha256(api_key.encode('utf-8')).hexdigest()
    tenant_id    = str(uuid.uuid4())

    try:
        with get_db_cursor() as cur:
            cur.execute(
                """INSERT INTO tenants
                   (id, nombre, nit, digito_verificacion, razon_social,
                    api_key_hash, ambiente, prefijo, clave_tecnica)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    tenant_id,
                    datos['nombre'],
                    datos['nit'],
                    int(datos['digito_verificacion']),
                    datos['razon_social'],
                    api_key_hash,
                    datos.get('ambiente', 'habilitacion'),
                    datos.get('prefijo', ''),
                    datos.get('clave_tecnica', ''),
                )
            )
    except Exception as e:
        if 'unique' in str(e).lower():
            return jsonify({"error": f"NIT ya registrado: {datos['nit']}"}), 409
        raise

    return jsonify({
        "id":       tenant_id,
        "api_key":  api_key,
        "advertencia": (
            "Guarda el api_key de inmediato — no puede recuperarse después. "
            "El servidor solo almacena su hash SHA256."
        )
    }), 201


@admin_bp.route('/tenants/<tenant_id>', methods=['GET'])
@requiere_master_key
def obtener_tenant(tenant_id):
    """Retorna configuración del tenant (sin datos sensibles)."""
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            """SELECT id, nombre, nit, digito_verificacion, razon_social,
                      ambiente, cert_path, clave_tecnica,
                      resolucion_dian, resolucion_desde, resolucion_hasta,
                      resolucion_vigencia, prefijo, consecutivo_actual,
                      activo, creado_en, actualizado_en
               FROM tenants WHERE id = %s""",
            (tenant_id,)
        )
        tenant = cur.fetchone()

    if not tenant:
        return jsonify({"error": "Tenant no encontrado"}), 404

    row = dict(tenant)
    row['creado_en']     = row['creado_en'].isoformat() if row['creado_en'] else None
    row['actualizado_en'] = row['actualizado_en'].isoformat() if row['actualizado_en'] else None
    return jsonify(row)


@admin_bp.route('/tenants/<tenant_id>', methods=['PUT'])
@requiere_master_key
def actualizar_tenant(tenant_id):
    """
    Actualiza configuración del tenant.
    Campos actualizables: nombre, razon_social, ambiente, prefijo,
                          clave_tecnica, resolucion_*, activo
    """
    datos = request.get_json(silent=True)
    if not datos:
        return jsonify({"error": "JSON requerido"}), 400

    campos_permitidos = [
        'nombre', 'razon_social', 'ambiente', 'prefijo',
        'clave_tecnica', 'resolucion_dian', 'resolucion_fecha',
        'resolucion_desde', 'resolucion_hasta', 'resolucion_vigencia',
        'activo',
    ]
    updates = {k: v for k, v in datos.items() if k in campos_permitidos}
    if not updates:
        return jsonify({"error": "No hay campos válidos para actualizar"}), 422

    set_clause = ', '.join(f"{k} = %s" for k in updates)
    values     = list(updates.values()) + [tenant_id]

    with get_db_cursor() as cur:
        cur.execute(
            f"UPDATE tenants SET {set_clause} WHERE id = %s",
            values
        )

    return jsonify({"mensaje": "Tenant actualizado"})


@admin_bp.route('/tenants/<tenant_id>/certificado', methods=['POST'])
@requiere_master_key
def subir_certificado(tenant_id):
    """
    Sube o reemplaza el certificado .p12 del tenant.

    Acepta multipart/form-data con:
      certificado: archivo .p12
      password:    contraseña del .p12 en texto plano

    La contraseña se cifra con Fernet antes de guardar en la BD.
    El archivo .p12 se guarda en storage/certificates/{tenant_id}/cert.p12
    """
    if 'certificado' not in request.files:
        return jsonify({"error": "Campo 'certificado' (archivo .p12) requerido"}), 400

    archivo   = request.files['certificado']
    password  = request.form.get('password', '')

    if not archivo.filename:
        return jsonify({"error": "Nombre de archivo vacío"}), 400

    # Verificar que el tenant existe
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT id FROM tenants WHERE id = %s", (tenant_id,))
        if not cur.fetchone():
            return jsonify({"error": "Tenant no encontrado"}), 404

    # Guardar .p12
    storage  = StorageManager(tenant_id)
    cert_dir = storage.get_cert_dir()
    cert_path = str(cert_dir / 'cert.p12')
    archivo.save(cert_path)

    # Cifrar contraseña con Fernet
    password_enc = cifrar(password) if password else ''

    with get_db_cursor() as cur:
        cur.execute(
            """UPDATE tenants
               SET cert_path = %s, cert_password_enc = %s, actualizado_en = NOW()
               WHERE id = %s""",
            (cert_path, password_enc, tenant_id)
        )

    return jsonify({
        "mensaje":    "Certificado subido y contraseña cifrada correctamente",
        "cert_path":  cert_path,
    })


@admin_bp.route('/tenants', methods=['GET'])
@requiere_master_key
def listar_tenants():
    """Lista todos los tenants (activos e inactivos)."""
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            """SELECT id, nombre, nit, razon_social, ambiente,
                      activo, consecutivo_actual, creado_en
               FROM tenants
               ORDER BY creado_en DESC"""
        )
        rows = cur.fetchall()

    tenants = []
    for r in rows:
        row = dict(r)
        row['creado_en'] = row['creado_en'].isoformat() if row['creado_en'] else None
        tenants.append(row)

    return jsonify(tenants)
