import hashlib
import hmac
import os
from functools import wraps
from flask import request, jsonify
from database import get_db_cursor


def _hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode('utf-8')).hexdigest()


def get_tenant_by_api_key(api_key: str) -> dict | None:
    """Busca el tenant activo correspondiente a un API Key."""
    key_hash = _hash_api_key(api_key)
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            """SELECT id, nombre, nit, digito_verificacion, razon_social,
                      ambiente, cert_path, cert_password_enc,
                      clave_tecnica, token_dian, token_dian_expira,
                      resolucion_dian, resolucion_desde, resolucion_hasta,
                      resolucion_vigencia, prefijo, consecutivo_actual
               FROM tenants
               WHERE api_key_hash = %s AND activo = TRUE""",
            (key_hash,)
        )
        return cur.fetchone()


def requiere_tenant(f):
    """Decorador: valida X-API-Key del tenant y lo adjunta a request.tenant."""
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get('X-API-Key', '').strip()
        if not api_key:
            return jsonify({"error": "Header X-API-Key requerido"}), 401
        tenant = get_tenant_by_api_key(api_key)
        if not tenant:
            return jsonify({"error": "API Key inválido o tenant inactivo"}), 403
        request.tenant = dict(tenant)
        return f(*args, **kwargs)
    return decorated


def requiere_master_key(f):
    """Decorador: protege rutas /admin con la MASTER_API_KEY del servidor."""
    @wraps(f)
    def decorated(*args, **kwargs):
        master_key = os.getenv('MASTER_API_KEY', '')
        provided   = request.headers.get('X-Master-Key', '')
        if not master_key:
            return jsonify({"error": "MASTER_API_KEY no configurada en el servidor"}), 500
        # hmac.compare_digest es resistente a timing attacks
        if not hmac.compare_digest(
            master_key.encode('utf-8'),
            provided.encode('utf-8')
        ):
            return jsonify({"error": "No autorizado"}), 403
        return f(*args, **kwargs)
    return decorated
