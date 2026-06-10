"""
Blueprint UI: panel de administración web del microservicio DIAN.

Roles:
  admin  — MASTER_API_KEY o SSO desde CyberShop. Ve y gestiona todo.
  tenant — usuario/contraseña configurados por el admin. Ve solo sus facturas.

Rutas admin:
  GET  /ui/                        Dashboard
  GET  /ui/tenants                 Lista de tenants
  GET  /ui/tenants/nuevo           Formulario nuevo tenant
  GET  /ui/tenants/<id>            Formulario editar tenant
  POST /ui/tenants/guardar         Crear/actualizar tenant
  POST /ui/tenants/<id>/cert       Subir certificado .p12
  POST /ui/tenants/<id>/regen      Regenerar API Key
  POST /ui/tenants/<id>/portal     Guardar credenciales portal del tenant

Rutas compartidas (admin + tenant):
  GET  /ui/facturas                Monitor de facturas
  GET  /ui/facturas/<id>           Detalle de factura
  GET  /ui/facturas/<id>/archivo   Descargar XML o response

Rutas admin (acciones):
  POST /ui/facturas/<id>/cancelar  Cancelar factura pendiente
  POST /ui/facturas/<id>/enviar-ahora  Envío inmediato

Autenticación:
  GET  /ui/login                   Login admin
  POST /ui/login                   Autenticar admin
  GET  /ui/logout                  Cerrar sesión admin
  GET  /ui/auto-login              SSO desde CyberShop (token HMAC)
  GET  /ui/tenant-login            Login tenant
  POST /ui/tenant-login            Autenticar tenant
  GET  /ui/tenant-logout           Cerrar sesión tenant
  GET  /ui/tenant-auto-login       SSO desde app del cliente (token HMAC)
"""

import hashlib
import hmac
import os
import re
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (Blueprint, flash, redirect, render_template,
                   request, send_file, session, url_for, jsonify)
from werkzeug.security import generate_password_hash, check_password_hash

from database import get_db_cursor
from utils.encryption import cifrar
from services.storage import StorageManager
from tasks.facturacion import procesar_factura

ui_bp = Blueprint('ui', __name__, url_prefix='/ui')


# ── Auth decorators ────────────────────────────────────────────────────────────

def _login_admin(f):
    """Solo admins. Tenants son redirigidos a sus facturas."""
    @wraps(f)
    def decorated(*args, **kwargs):
        role = session.get('ui_role')
        if role == 'admin':
            return f(*args, **kwargs)
        if role == 'tenant':
            flash('Sección restringida a administradores.', 'warning')
            return redirect(url_for('ui.facturas'))
        return redirect(url_for('ui.login', next=request.path))
    return decorated


def _login_tenant_o_admin(f):
    """Admin o tenant autenticado."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('ui_role') in ('admin', 'tenant'):
            return f(*args, **kwargs)
        return redirect(url_for('ui.login', next=request.path))
    return decorated


def _tenant_id_scope():
    """Retorna tenant_id si la sesión es de tipo tenant, None si es admin."""
    if session.get('ui_role') == 'tenant':
        return session.get('ui_tenant_id')
    return None


# ── Admin SSO (CyberShop) ─────────────────────────────────────────────────────

@ui_bp.route('/auto-login')
def auto_login():
    """SSO desde CyberShop — token HMAC firmado con MASTER_API_KEY. Vigencia 30s."""
    token = request.args.get('token', '')
    ts    = request.args.get('ts', '')
    next_ = request.args.get('next', url_for('ui.dashboard'))

    try:
        ts_int = int(ts)
    except (ValueError, TypeError):
        return redirect(url_for('ui.login'))

    if abs(time.time() - ts_int) > 30:
        flash('Enlace expirado. Intenta de nuevo desde CyberShop.', 'danger')
        return redirect(url_for('ui.login'))

    master_key = os.getenv('MASTER_API_KEY', '')
    expected   = hmac.new(master_key.encode(),
                          f"{ts_int}:dian-autologin".encode(),
                          hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, token):
        flash('Token inválido.', 'danger')
        return redirect(url_for('ui.login'))

    session.clear()
    session['ui_auth'] = True
    session['ui_role'] = 'admin'
    return redirect(next_)


# ── Tenant SSO (desde app del cliente) ────────────────────────────────────────

@ui_bp.route('/tenant-auto-login')
def tenant_auto_login():
    """
    SSO para apps de clientes.
    token = HMAC-SHA256(MASTER_API_KEY, f"{ts}:{tenant_id}:portal-autologin")
    Vigencia: 30 segundos.
    """
    token     = request.args.get('token', '')
    ts        = request.args.get('ts', '')
    tenant_id = request.args.get('tenant', '')

    try:
        ts_int = int(ts)
    except (ValueError, TypeError):
        flash('Enlace inválido.', 'danger')
        return redirect(url_for('ui.tenant_login'))

    if abs(time.time() - ts_int) > 30:
        flash('Enlace expirado. Solicita uno nuevo desde tu aplicación.', 'danger')
        return redirect(url_for('ui.tenant_login'))

    if not tenant_id:
        flash('Parámetro tenant requerido.', 'danger')
        return redirect(url_for('ui.tenant_login'))

    master_key = os.getenv('MASTER_API_KEY', '')
    expected   = hmac.new(master_key.encode(),
                          f"{ts_int}:{tenant_id}:portal-autologin".encode(),
                          hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, token):
        flash('Token inválido.', 'danger')
        return redirect(url_for('ui.tenant_login'))

    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            "SELECT id, nombre, portal_activo FROM tenants WHERE id = %s AND activo = TRUE",
            (tenant_id,)
        )
        t = cur.fetchone()

    if not t or not t['portal_activo']:
        flash('Acceso al portal no habilitado. Contacta a tu administrador.', 'danger')
        return redirect(url_for('ui.tenant_login'))

    session.clear()
    session['ui_role']           = 'tenant'
    session['ui_tenant_id']      = str(t['id'])
    session['ui_tenant_nombre']  = t['nombre']
    return redirect(url_for('ui.facturas'))


# ── Admin login / logout ───────────────────────────────────────────────────────

@ui_bp.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('ui_role') == 'admin':
        return redirect(url_for('ui.dashboard'))
    if request.method == 'POST':
        master_key = os.getenv('MASTER_API_KEY', '')
        provided   = request.form.get('password', '')
        if hmac.compare_digest(master_key.encode(), provided.encode()):
            session.clear()
            session['ui_auth'] = True
            session['ui_role'] = 'admin'
            return redirect(request.args.get('next') or url_for('ui.dashboard'))
        flash('Contraseña incorrecta', 'danger')
    return render_template('ui/login.html')


@ui_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('ui.login'))


# ── Tenant login / logout ──────────────────────────────────────────────────────

@ui_bp.route('/tenant-login', methods=['GET', 'POST'])
def tenant_login():
    if session.get('ui_role') == 'tenant':
        return redirect(url_for('ui.facturas'))
    if session.get('ui_role') == 'admin':
        return redirect(url_for('ui.dashboard'))

    if request.method == 'POST':
        usuario  = request.form.get('usuario', '').strip().lower()
        password = request.form.get('password', '')

        if not usuario or not password:
            flash('Usuario y contraseña son requeridos.', 'danger')
            return render_template('ui/tenant_portal_login.html')

        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute("""
                SELECT id, nombre, portal_password_hash, portal_activo,
                       portal_intentos_fallidos, portal_bloqueado_hasta
                FROM tenants
                WHERE LOWER(portal_usuario) = %s
            """, (usuario,))
            t = cur.fetchone()

        if not t:
            flash('Credenciales incorrectas.', 'danger')
            return render_template('ui/tenant_portal_login.html')

        if not t['portal_activo']:
            flash('El acceso al portal no está habilitado para esta empresa. '
                  'Contacta a tu administrador.', 'warning')
            return render_template('ui/tenant_portal_login.html')

        # Verificar bloqueo
        if t['portal_bloqueado_hasta']:
            from datetime import timezone as tz
            ahora = datetime.now(tz.utc)
            bloqueado = t['portal_bloqueado_hasta'].replace(tzinfo=tz.utc)
            if bloqueado > ahora:
                minutos = int((bloqueado - ahora).total_seconds() // 60) + 1
                flash(f'Cuenta bloqueada por intentos fallidos. '
                      f'Intenta de nuevo en {minutos} minuto(s).', 'danger')
                return render_template('ui/tenant_portal_login.html')

        if not check_password_hash(t['portal_password_hash'] or '', password):
            nuevos_intentos = (t['portal_intentos_fallidos'] or 0) + 1
            if nuevos_intentos >= 5:
                with get_db_cursor() as cur:
                    cur.execute("""
                        UPDATE tenants
                        SET portal_intentos_fallidos = %s,
                            portal_bloqueado_hasta = NOW() + INTERVAL '15 minutes'
                        WHERE id = %s
                    """, (nuevos_intentos, str(t['id'])))
                flash('Demasiados intentos fallidos. Cuenta bloqueada 15 minutos.', 'danger')
            else:
                with get_db_cursor() as cur:
                    cur.execute(
                        "UPDATE tenants SET portal_intentos_fallidos = %s WHERE id = %s",
                        (nuevos_intentos, str(t['id']))
                    )
                restantes = 5 - nuevos_intentos
                flash(f'Credenciales incorrectas. Te quedan {restantes} intento(s).', 'danger')
            return render_template('ui/tenant_portal_login.html')

        # Éxito — resetear contador y crear sesión
        with get_db_cursor() as cur:
            cur.execute("""
                UPDATE tenants
                SET portal_intentos_fallidos = 0, portal_bloqueado_hasta = NULL
                WHERE id = %s
            """, (str(t['id']),))

        session.clear()
        session['ui_role']          = 'tenant'
        session['ui_tenant_id']     = str(t['id'])
        session['ui_tenant_nombre'] = t['nombre']
        return redirect(url_for('ui.facturas'))

    return render_template('ui/tenant_portal_login.html')


@ui_bp.route('/tenant-logout')
def tenant_logout():
    session.clear()
    flash('Sesión cerrada correctamente.', 'info')
    return redirect(url_for('ui.tenant_login'))


# ── Dashboard (admin only) ─────────────────────────────────────────────────────

@ui_bp.route('/')
@_login_admin
def dashboard():
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT COUNT(*) FROM tenants WHERE activo=TRUE")
        n_tenants = cur.fetchone()['count']

        cur.execute("SELECT COUNT(*) FROM facturas WHERE estado='PENDIENTE'")
        pendientes = cur.fetchone()['count']

        cur.execute("""
            SELECT COUNT(*) FROM facturas
            WHERE estado='ACEPTADA' AND creado_en::date = CURRENT_DATE
        """)
        aceptadas_hoy = cur.fetchone()['count']

        cur.execute("SELECT COUNT(*) FROM facturas WHERE estado IN ('ERROR','RECHAZADA')")
        errores = cur.fetchone()['count']

        cur.execute("""
            SELECT f.id, f.referencia_pedido, f.numero_factura, f.estado,
                   f.creado_en, t.nombre AS tenant_nombre
            FROM facturas f
            JOIN tenants t ON t.id = f.tenant_id
            ORDER BY f.creado_en DESC LIMIT 20
        """)
        facturas_raw = cur.fetchall()

        cur.execute("SELECT id, nombre, nit, ambiente, activo FROM tenants ORDER BY creado_en")
        tenants = [dict(r) for r in cur.fetchall()]

    now = datetime.now(timezone.utc)
    ultimas = []
    for f in facturas_raw:
        f = dict(f)
        delta = now - f['creado_en'].replace(tzinfo=timezone.utc)
        mins  = int(delta.total_seconds() // 60)
        f['hace'] = f"{mins}m" if mins < 60 else f"{mins//60}h"
        f['id']   = str(f['id'])
        ultimas.append(f)

    return render_template('ui/dashboard.html',
        stats=dict(tenants=n_tenants, pendientes=pendientes,
                   aceptadas_hoy=aceptadas_hoy, errores=errores),
        ultimas_facturas=ultimas,
        tenants=tenants,
    )


# ── Tenants (admin only) ───────────────────────────────────────────────────────

@ui_bp.route('/tenants')
@_login_admin
def tenants():
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("""
            SELECT id, nombre, nit, razon_social, ambiente, prefijo,
                   consecutivo_actual, cert_path, activo
            FROM tenants ORDER BY nombre
        """)
        rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r['id'] = str(r['id'])
    return render_template('ui/tenants.html', tenants=rows)


@ui_bp.route('/tenants/nuevo')
@_login_admin
def tenant_nuevo():
    return render_template('ui/tenant_form.html', tenant=None)


@ui_bp.route('/tenants/<tenant_id>')
@_login_admin
def tenant_detalle(tenant_id):
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT * FROM tenants WHERE id = %s", (tenant_id,))
        t = cur.fetchone()
    if not t:
        flash('Tenant no encontrado', 'danger')
        return redirect(url_for('ui.tenants'))
    tenant = dict(t)
    tenant['id'] = str(tenant['id'])
    for k in ('resolucion_fecha', 'resolucion_vigencia', 'portal_bloqueado_hasta'):
        if tenant.get(k):
            tenant[k] = str(tenant[k])
    return render_template('ui/tenant_form.html', tenant=tenant)


@ui_bp.route('/tenants/guardar', methods=['POST'])
@ui_bp.route('/tenants/<tenant_id>/guardar', methods=['POST'])
@_login_admin
def tenant_guardar(tenant_id=None):
    f = request.form

    if tenant_id:
        campos = ['nombre', 'razon_social', 'ambiente', 'prefijo', 'activo',
                  'software_id', 'clave_tecnica', 'tipo_persona_emisor',
                  'resolucion_dian', 'resolucion_fecha', 'resolucion_desde',
                  'resolucion_hasta', 'resolucion_vigencia']
        datos = {}
        for c in campos:
            v = f.get(c, '').strip() or None
            if c == 'activo':
                v = f.get(c) == 'true'
            elif c in ('resolucion_desde', 'resolucion_hasta'):
                v = int(f.get(c)) if f.get(c, '').strip().isdigit() else None
            datos[c] = v

        set_clause = ', '.join(f"{k}=%s" for k in datos)
        values     = list(datos.values()) + [tenant_id]
        with get_db_cursor() as cur:
            cur.execute(f"UPDATE tenants SET {set_clause} WHERE id=%s", values)
        flash('Tenant actualizado correctamente', 'success')
        return redirect(url_for('ui.tenant_detalle', tenant_id=tenant_id))

    else:
        api_key      = os.urandom(32).hex()
        api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        new_id       = str(uuid.uuid4())
        with get_db_cursor() as cur:
            cur.execute("""
                INSERT INTO tenants
                (id, nombre, nit, digito_verificacion, razon_social,
                 api_key_hash, ambiente, prefijo, clave_tecnica,
                 software_id, tipo_persona_emisor)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                new_id,
                f.get('nombre', '').strip(),
                f.get('nit', '').strip(),
                int(f.get('digito_verificacion', 0) or 0),
                f.get('razon_social', '').strip(),
                api_key_hash,
                f.get('ambiente', 'habilitacion'),
                f.get('prefijo', '').strip(),
                f.get('clave_tecnica', '').strip() or None,
                f.get('software_id', '').strip() or None,
                f.get('tipo_persona_emisor', 'juridica'),
            ))
        session['nuevo_api_key']    = api_key
        session['nuevo_api_key_id'] = new_id
        flash(f'Tenant creado. API Key: {api_key} — Guárdalo ahora, no se mostrará de nuevo.', 'warning')
        return redirect(url_for('ui.tenant_detalle', tenant_id=new_id))


@ui_bp.route('/tenants/<tenant_id>/portal', methods=['POST'])
@_login_admin
def tenant_guardar_portal(tenant_id):
    """Guarda las credenciales del portal tributario para el tenant."""
    f              = request.form
    usuario_raw    = f.get('portal_usuario', '').strip().lower()
    password_raw   = f.get('portal_password', '').strip()
    portal_activo  = f.get('portal_activo') == 'true'
    desbloquear    = f.get('desbloquear_portal') == '1'

    datos = {'portal_activo': portal_activo}

    if desbloquear:
        datos['portal_intentos_fallidos'] = 0
        datos['portal_bloqueado_hasta']   = None

    if usuario_raw:
        if not re.match(r'^[a-z0-9._-]{3,100}$', usuario_raw):
            flash('Usuario inválido. Solo letras, números, puntos, guiones. Mín 3 chars.', 'danger')
            return redirect(url_for('ui.tenant_detalle', tenant_id=tenant_id))
        # Verificar unicidad
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute(
                "SELECT id FROM tenants WHERE LOWER(portal_usuario)=%s AND id!=%s",
                (usuario_raw, tenant_id)
            )
            if cur.fetchone():
                flash(f'El usuario "{usuario_raw}" ya está en uso por otro tenant.', 'danger')
                return redirect(url_for('ui.tenant_detalle', tenant_id=tenant_id))
        datos['portal_usuario'] = usuario_raw

    if password_raw:
        if len(password_raw) < 8:
            flash('La contraseña debe tener al menos 8 caracteres.', 'danger')
            return redirect(url_for('ui.tenant_detalle', tenant_id=tenant_id))
        datos['portal_password_hash']     = generate_password_hash(password_raw)
        datos['portal_intentos_fallidos'] = 0
        datos['portal_bloqueado_hasta']   = None

    set_clause = ', '.join(f"{k}=%s" for k in datos)
    values     = list(datos.values()) + [tenant_id]
    with get_db_cursor() as cur:
        cur.execute(f"UPDATE tenants SET {set_clause} WHERE id=%s", values)

    flash('Acceso al portal actualizado correctamente.', 'success')
    return redirect(url_for('ui.tenant_detalle', tenant_id=tenant_id))


@ui_bp.route('/tenants/<tenant_id>/cert', methods=['POST'])
@_login_admin
def tenant_subir_cert(tenant_id):
    if 'certificado' not in request.files or not request.files['certificado'].filename:
        flash('Selecciona un archivo .p12', 'danger')
        return redirect(url_for('ui.tenant_detalle', tenant_id=tenant_id))

    archivo  = request.files['certificado']
    password = request.form.get('password', '')

    storage   = StorageManager(tenant_id)
    cert_dir  = storage.get_cert_dir()
    cert_path = str(cert_dir / 'cert.p12')
    archivo.save(cert_path)

    password_enc = cifrar(password) if password else ''
    with get_db_cursor() as cur:
        cur.execute(
            "UPDATE tenants SET cert_path=%s, cert_password_enc=%s WHERE id=%s",
            (cert_path, password_enc, tenant_id)
        )
    flash('Certificado actualizado correctamente', 'success')
    return redirect(url_for('ui.tenant_detalle', tenant_id=tenant_id))


@ui_bp.route('/tenants/<tenant_id>/regen')
@_login_admin
def tenant_regenerar_key(tenant_id):
    api_key      = os.urandom(32).hex()
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    with get_db_cursor() as cur:
        cur.execute("UPDATE tenants SET api_key_hash=%s WHERE id=%s",
                    (api_key_hash, tenant_id))
    flash(f'Nuevo API Key: {api_key} — Guárdalo ahora.', 'warning')
    return redirect(url_for('ui.tenant_detalle', tenant_id=tenant_id))


# ── Facturas (admin + tenant) ──────────────────────────────────────────────────

@ui_bp.route('/facturas')
@_login_tenant_o_admin
def facturas():
    page       = int(request.args.get('page', 1))
    per_page   = 30
    offset_val = (page - 1) * per_page
    tenant_scope = _tenant_id_scope()

    filtros = {
        'tenant_id': request.args.get('tenant_id', ''),
        'estado':    request.args.get('estado', ''),
        'q':         request.args.get('q', ''),
        'desde':     request.args.get('desde', ''),
    }

    where, params = ['1=1'], []

    # Tenant: forzar scope propio, ignorar filtro de URL
    if tenant_scope:
        where.append('f.tenant_id = %s')
        params.append(tenant_scope)
    elif filtros['tenant_id']:
        where.append('f.tenant_id = %s')
        params.append(filtros['tenant_id'])

    if filtros['estado']:
        where.append('f.estado = %s')
        params.append(filtros['estado'])
    if filtros['q']:
        where.append('(f.referencia_pedido ILIKE %s OR f.numero_factura ILIKE %s)')
        params.extend([f"%{filtros['q']}%", f"%{filtros['q']}%"])
    if filtros['desde']:
        where.append('f.creado_en::date >= %s')
        params.append(filtros['desde'])

    where_sql = ' AND '.join(where)

    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(f"SELECT COUNT(*) FROM facturas f WHERE {where_sql}", params)
        total = cur.fetchone()['count']

        cur.execute(f"""
            SELECT f.id, f.referencia_pedido, f.numero_factura, f.cufe,
                   f.estado, f.intentos, f.creado_en, f.procesar_en,
                   t.nombre AS tenant_nombre
            FROM facturas f
            JOIN tenants t ON t.id = f.tenant_id
            WHERE {where_sql}
            ORDER BY f.creado_en DESC
            LIMIT %s OFFSET %s
        """, params + [per_page, offset_val])
        rows = cur.fetchall()

        # Lista de tenants solo para admins (dropdown de filtro)
        tenants_lista = []
        if not tenant_scope:
            cur.execute("SELECT id, nombre FROM tenants ORDER BY nombre")
            tenants_lista = [dict(r) for r in cur.fetchall()]
            for t in tenants_lista:
                t['id'] = str(t['id'])

    facturas_list = []
    for r in rows:
        r = dict(r)
        r['id']            = str(r['id'])
        r['creado_en_fmt']   = r['creado_en'].strftime('%Y-%m-%d %H:%M')   if r['creado_en']   else ''
        r['procesar_en_fmt'] = r['procesar_en'].strftime('%Y-%m-%d %H:%M') if r.get('procesar_en') else ''
        facturas_list.append(r)

    pages      = (total + per_page - 1) // per_page
    filtros_qs = '&'.join(f"{k}={v}" for k, v in filtros.items() if v)

    return render_template('ui/facturas.html',
        facturas=facturas_list, total=total, page=page,
        pages=pages, tenants=tenants_lista,
        filtros=filtros, filtros_qs=filtros_qs,
        es_tenant=(tenant_scope is not None),
    )


@ui_bp.route('/facturas/<factura_id>')
@_login_tenant_o_admin
def factura_detalle(factura_id):
    tenant_scope = _tenant_id_scope()

    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("""
            SELECT f.*, t.nombre AS tenant_nombre
            FROM facturas f JOIN tenants t ON t.id = f.tenant_id
            WHERE f.id = %s
        """, (factura_id,))
        f = cur.fetchone()

    if not f:
        flash('Factura no encontrada', 'danger')
        return redirect(url_for('ui.facturas'))

    # Tenant solo puede ver sus propias facturas
    if tenant_scope and str(f['tenant_id']) != tenant_scope:
        flash('Factura no encontrada', 'danger')
        return redirect(url_for('ui.facturas'))

    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("""
            SELECT evento, detalle, creado_en
            FROM factura_eventos WHERE factura_id = %s
            ORDER BY creado_en ASC
        """, (factura_id,))
        eventos_raw = cur.fetchall()

    factura = dict(f)
    factura['id'] = str(factura['id'])
    for k in ('creado_en', 'actualizado_en'):
        if factura.get(k):
            factura[k] = factura[k].strftime('%Y-%m-%d %H:%M:%S')
    if factura.get('procesar_en'):
        factura['procesar_en'] = factura['procesar_en'].strftime('%Y-%m-%d %H:%M:%S')

    eventos = []
    for ev in eventos_raw:
        ev = dict(ev)
        ev['creado_en'] = ev['creado_en'].strftime('%H:%M:%S %d/%m')
        eventos.append(ev)

    return render_template('ui/factura_detalle.html',
                           factura=factura, eventos=eventos,
                           es_tenant=(tenant_scope is not None))


@ui_bp.route('/facturas/<factura_id>/cancelar', methods=['POST'])
@_login_admin
def factura_cancelar(factura_id):
    with get_db_cursor() as cur:
        cur.execute("""
            UPDATE facturas SET estado = 'CANCELADA'
            WHERE id = %s AND estado = 'PENDIENTE' AND procesar_en > NOW()
            RETURNING id
        """, (factura_id,))
        ok = cur.fetchone()
    if not ok:
        flash('No se puede cancelar: ya fue procesada o el plazo venció.', 'danger')
    else:
        with get_db_cursor() as cur:
            cur.execute(
                "INSERT INTO factura_eventos (factura_id, evento, detalle) VALUES (%s,%s,%s)",
                (factura_id, 'CANCELADA', 'Cancelada manualmente desde el panel UI')
            )
        flash('Factura cancelada. No será enviada a la DIAN.', 'success')
    return redirect(url_for('ui.factura_detalle', factura_id=factura_id))


@ui_bp.route('/facturas/<factura_id>/enviar-ahora', methods=['POST'])
@_login_admin
def factura_enviar_ahora(factura_id):
    with get_db_cursor() as cur:
        cur.execute("""
            UPDATE facturas SET procesar_en = NOW()
            WHERE id = %s AND estado = 'PENDIENTE'
            RETURNING id
        """, (factura_id,))
        ok = cur.fetchone()
    if not ok:
        flash('No se puede reenviar: la factura no está en estado PENDIENTE.', 'danger')
    else:
        task = procesar_factura.delay(factura_id)
        with get_db_cursor() as cur:
            cur.execute("UPDATE facturas SET celery_task_id=%s WHERE id=%s",
                        (task.id, factura_id))
            cur.execute(
                "INSERT INTO factura_eventos (factura_id, evento, detalle) VALUES (%s,%s,%s)",
                (factura_id, 'ENCOLADA', f'Envío inmediato solicitado desde UI. task_id={task.id}')
            )
        flash('Factura encolada para envío inmediato a la DIAN.', 'success')
    return redirect(url_for('ui.factura_detalle', factura_id=factura_id))


@ui_bp.route('/facturas/<factura_id>/archivo/<tipo>')
@_login_tenant_o_admin
def descargar_archivo(factura_id, tipo):
    tenant_scope = _tenant_id_scope()

    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(
            "SELECT xml_path, response_path, numero_factura, tenant_id FROM facturas WHERE id=%s",
            (factura_id,)
        )
        f = cur.fetchone()

    if not f:
        return 'Factura no encontrada', 404

    if tenant_scope and str(f['tenant_id']) != tenant_scope:
        return 'No autorizado', 403

    path = f['xml_path'] if tipo == 'xml' else f['response_path']
    if not path or not Path(path).exists():
        return 'Archivo no disponible', 404

    nombre = f['numero_factura'] or factura_id[:8]
    suffix = '_firmado.xml' if tipo == 'xml' else '_response.xml'
    return send_file(path, as_attachment=True, download_name=f"{nombre}{suffix}")
