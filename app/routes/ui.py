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
from services.emision import emitir_documento, EmisionError, enviar_ahora
from tasks.facturacion import procesar_factura

ui_bp = Blueprint('ui', __name__, url_prefix='/ui')


# ── Estados en lenguaje humano (para el dueño no técnico) ───────────────────
ESTADOS_INFO = {
    'PENDIENTE':  ('En espera de envío', 'PENDIENTE', 'Aún no se envía a la DIAN. Puedes anularla antes del envío.'),
    'PROCESANDO': ('Enviando a la DIAN…', 'PROCESANDO', 'Se está transmitiendo a la DIAN.'),
    'ACEPTADA':   ('Aceptada por la DIAN', 'ACEPTADA', 'Factura válida legalmente.'),
    'RECHAZADA':  ('Rechazada — revisa el motivo', 'RECHAZADA', 'La DIAN la rechazó. Abre el detalle para ver por qué.'),
    'ERROR':      ('Error de envío', 'ERROR', 'Hubo un problema técnico. Reintenta o contacta soporte.'),
    'CANCELADA':  ('Anulada', 'CANCELADA', 'Documento anulado antes de enviarse.'),
}


def _estado_info(estado):
    lbl, cls, hint = ESTADOS_INFO.get((estado or '').upper(), (estado or '—', 'ERROR', ''))
    return {'label': lbl, 'cls': cls, 'hint': hint}


@ui_bp.app_context_processor
def _inject_helpers():
    return {'estado_info': _estado_info}


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


def _login_tenant(f):
    """Solo tenants autenticados (emisión autoservicio del cliente)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('ui_role') == 'tenant':
            return f(*args, **kwargs)
        if session.get('ui_role') == 'admin':
            flash('La emisión autoservicio es del portal del cliente. '
                  'Usa el módulo Set de Pruebas.', 'warning')
            return redirect(url_for('ui.pruebas'))
        return redirect(url_for('ui.tenant_login', next=request.path))
    return decorated


# Catálogos para los formularios de emisión
CONCEPTOS_NC = [('1', 'Devolución parcial de bienes/servicios'), ('2', 'Anulación de factura'),
                ('3', 'Rebaja o descuento'), ('4', 'Ajuste de precio'), ('5', 'Otros')]
CONCEPTOS_ND = [('1', 'Intereses'), ('2', 'Gastos por cobrar'),
                ('3', 'Cambio del valor'), ('4', 'Otros')]
METODOS_PAGO = [('10', 'Efectivo'), ('20', 'Cheque'), ('30', 'Transferencia'),
                ('47', 'Transferencia bancaria'), ('48', 'Tarjeta crédito'),
                ('49', 'Tarjeta débito')]
TIPOS_DOC_CLIENTE = [('CC', 'Cédula'), ('NIT', 'NIT'), ('CE', 'Cédula extranjería'),
                     ('TI', 'Tarjeta identidad'), ('PA', 'Pasaporte')]


def _grace_minutos(tenant_id):
    from config import Config
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT grace_minutos FROM tenants WHERE id=%s", (tenant_id,))
        r = cur.fetchone()
    return (r and r.get('grace_minutos')) or Config.GRACE_MINUTES


def _tenant_envio(tenant_id):
    """Retorna (modo_aprobacion, grace_minutos) del tenant."""
    from config import Config
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT modo_aprobacion, grace_minutos FROM tenants WHERE id=%s", (tenant_id,))
        r = cur.fetchone() or {}
    modo = r.get('modo_aprobacion') or 'automatico'
    grace = r.get('grace_minutos') or Config.GRACE_MINUTES
    return modo, int(grace)


def _plazo_txt(minutos):
    if minutos < 60:
        return f"{minutos} minuto(s)"
    if minutos < 1440:
        return f"{minutos // 60} hora(s)"
    return f"{minutos // 1440} día(s)"


def _parse_items(form):
    """Lee las líneas del formulario (arrays paralelos) → lista de items."""
    descs = form.getlist('item_descripcion')
    cants = form.getlist('item_cantidad')
    precs = form.getlist('item_precio')
    ivas  = form.getlist('item_iva')
    cods  = form.getlist('item_codigo')
    items = []
    for i, d in enumerate(descs):
        if not (d or '').strip():
            continue
        def _g(lst, j, dv): return lst[j] if j < len(lst) and lst[j] != '' else dv
        items.append({
            'descripcion': d.strip(),
            'cantidad': float(_g(cants, i, 1) or 1),
            'precio_unitario': float(_g(precs, i, 0) or 0),
            'impuesto_iva': float(_g(ivas, i, 0) or 0),
            'codigo': (_g(cods, i, '') or '').strip(),
            'codigo_unidad': 'EA',
        })
    return items


def _facturas_aceptadas(tenant_id):
    """Facturas ACEPTADAS del tenant (para referenciar en notas)."""
    from datetime import timezone, timedelta
    tz_co = timezone(timedelta(hours=-5))
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("""
            SELECT numero_factura, cufe, creado_en
            FROM facturas
            WHERE tenant_id=%s AND estado='ACEPTADA' AND numero_factura IS NOT NULL
              AND (datos_json->>'tipo_documento' IS NULL OR datos_json->>'tipo_documento'='factura')
            ORDER BY creado_en DESC LIMIT 200
        """, (tenant_id,))
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({'numero': r['numero_factura'], 'cufe': r['cufe'],
                    'fecha': r['creado_en'].astimezone(tz_co).strftime('%Y-%m-%d')})
    return out


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
            "SELECT id, nombre, ambiente, portal_activo, logo_url FROM tenants WHERE id = %s AND activo = TRUE",
            (tenant_id,)
        )
        t = cur.fetchone()

    if not t or not t['portal_activo']:
        flash('Acceso al portal no habilitado. Contacta a tu administrador.', 'danger')
        return redirect(url_for('ui.tenant_login'))

    session.clear()
    session['ui_role']            = 'tenant'
    session['ui_tenant_id']       = str(t['id'])
    session['ui_tenant_nombre']   = t['nombre']
    session['ui_tenant_ambiente'] = t['ambiente']
    session['ui_tenant_logo']     = t.get('logo_url')
    return redirect(url_for('ui.tenant_home'))


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
        return redirect(url_for('ui.tenant_home'))
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
                SELECT id, nombre, ambiente, portal_password_hash, portal_activo,
                       portal_intentos_fallidos, portal_bloqueado_hasta, logo_url
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
        session['ui_role']            = 'tenant'
        session['ui_tenant_id']       = str(t['id'])
        session['ui_tenant_nombre']   = t['nombre']
        session['ui_tenant_ambiente'] = t['ambiente']
        session['ui_tenant_logo']     = t.get('logo_url')
        return redirect(url_for('ui.tenant_home'))

    return render_template('ui/tenant_portal_login.html')


@ui_bp.route('/tenant-logout')
def tenant_logout():
    session.clear()
    flash('Sesión cerrada correctamente.', 'info')
    return redirect(url_for('ui.tenant_login'))


# ── Inicio del cliente (dashboard del tenant) ───────────────────────────────

@ui_bp.route('/inicio')
@_login_tenant
def tenant_home():
    """Inicio amigable para el dueño: resumen del mes, estado 'listo para
    facturar' y últimos documentos. Reemplaza el aterrizaje en la tabla cruda."""
    tid = session.get('ui_tenant_id')
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("""
            SELECT nombre, ambiente, logo_url, cert_path, resolucion_dian,
                   clave_tecnica, software_id, prefijo, consecutivo_actual
            FROM tenants WHERE id = %s
        """, (tid,))
        t = dict(cur.fetchone() or {})

        cur.execute("""
            SELECT estado, COUNT(*) AS n FROM facturas
            WHERE tenant_id = %s AND creado_en >= date_trunc('month', now())
            GROUP BY estado
        """, (tid,))
        by_estado = {r['estado']: r['n'] for r in cur.fetchall()}

        cur.execute("SELECT COUNT(*) AS n FROM facturas WHERE tenant_id = %s", (tid,))
        total_hist = cur.fetchone()['n']

        cur.execute("""
            SELECT id, numero_factura, referencia_pedido, estado, creado_en
            FROM facturas WHERE tenant_id = %s ORDER BY creado_en DESC LIMIT 5
        """, (tid,))
        recientes = []
        for r in cur.fetchall():
            r = dict(r)
            r['id'] = str(r['id'])
            r['creado_en_fmt'] = r['creado_en'].strftime('%d/%m/%Y %H:%M') if r['creado_en'] else ''
            recientes.append(r)

    mes = {
        'total':      sum(by_estado.values()),
        'aceptadas':  by_estado.get('ACEPTADA', 0),
        'espera':     by_estado.get('PENDIENTE', 0) + by_estado.get('PROCESANDO', 0),
        'rechazadas': by_estado.get('RECHAZADA', 0) + by_estado.get('ERROR', 0),
    }
    checklist = [
        {'ok': bool(t.get('cert_path')),       'label': 'Certificado de firma cargado'},
        {'ok': bool(t.get('resolucion_dian')), 'label': 'Resolución de la DIAN configurada'},
        {'ok': bool(t.get('clave_tecnica')),   'label': 'Clave técnica registrada'},
        {'ok': bool(t.get('software_id')),     'label': 'Software ID asignado'},
        {'ok': bool(t.get('prefijo') and t.get('consecutivo_actual') is not None),
         'label': 'Numeración lista (prefijo y consecutivo)'},
    ]
    return render_template('ui/tenant_home.html',
        t=t, mes=mes, recientes=recientes, total_hist=total_hist,
        checklist=checklist, listo=all(c['ok'] for c in checklist),
        ambiente=t.get('ambiente'))


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
        campos = ['nombre', 'razon_social', 'prefijo', 'activo',
                  'software_id', 'clave_tecnica', 'test_set_id', 'tipo_persona_emisor',
                  'digito_verificacion',
                  'resolucion_dian', 'resolucion_fecha', 'resolucion_desde',
                  'resolucion_hasta', 'resolucion_vigencia',
                  'software_pin', 'regimen_codigo', 'responsabilidad_fiscal',
                  'direccion', 'municipio_codigo', 'municipio_nombre',
                  'departamento_codigo', 'departamento_nombre', 'email', 'telefono',
                  'color_primario', 'logo_url', 'grace_minutos',
                  'cybershop_base_url', 'cybershop_sync_key']
        enteros = ('resolucion_desde', 'resolucion_hasta', 'grace_minutos',
                   'digito_verificacion')
        datos = {}
        for c in campos:
            v = f.get(c, '').strip() or None
            if c == 'activo':
                v = f.get(c) == 'true'
            elif c in enteros:
                v = int(f.get(c)) if (f.get(c, '').strip() or '').isdigit() else None
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


# ── Cambio de ambiente Habilitación ↔ Producción (con swap de config) ──────────

# Campos específicos de cada ambiente: al cambiar, se guardan los del ambiente que
# se deja y se cargan los del que se entra (cada ambiente tiene su propio software,
# resolución, consecutivo y TestSetId).
ENV_FIELDS = ['software_id', 'software_pin', 'clave_tecnica', 'test_set_id',
              'resolucion_dian', 'prefijo', 'resolucion_desde', 'resolucion_hasta',
              'resolucion_fecha', 'resolucion_vigencia', 'consecutivo_actual']


@ui_bp.route('/tenants/<tenant_id>/cambiar-ambiente', methods=['POST'])
@_login_admin
def cambiar_ambiente(tenant_id):
    import psycopg2.extras
    destino = request.form.get('destino')
    if destino not in ('habilitacion', 'produccion'):
        flash('Ambiente inválido.', 'danger')
        return redirect(url_for('ui.tenant_detalle', tenant_id=tenant_id))

    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT * FROM tenants WHERE id=%s", (tenant_id,))
        row = cur.fetchone()
    if not row:
        flash('Tenant no encontrado.', 'danger')
        return redirect(url_for('ui.tenants'))
    row = dict(row)
    actual = row['ambiente']
    if actual == destino:
        flash(f'Ya estás en {destino.upper()}.', 'info')
        return redirect(url_for('ui.tenant_detalle', tenant_id=tenant_id))

    # CANDADO: para pasar a producción, los datos reales deben estar cargados
    # EXPLÍCITAMENTE (marca _configurado) — no basta un snapshot heredado.
    if destino == 'produccion':
        prod = (row.get('ambientes') or {}).get('produccion') or {}
        if not prod.get('_configurado'):
            flash('No puedes pasar a PRODUCCIÓN todavía: primero guarda los '
                  '"Datos de Producción" (resolución y credenciales reales de la DIAN).', 'danger')
            return redirect(url_for('ui.tenant_detalle', tenant_id=tenant_id))
        requeridos = ['software_id', 'clave_tecnica', 'resolucion_dian', 'prefijo',
                      'resolucion_desde', 'resolucion_hasta']
        faltan = [k for k in requeridos if not prod.get(k)]
        if faltan:
            flash('Datos de producción incompletos: faltan ' + ', '.join(faltan) + '.', 'danger')
            return redirect(url_for('ui.tenant_detalle', tenant_id=tenant_id))

    ambientes = row.get('ambientes') or {}
    # 1) Guardar (snapshot) la configuración del ambiente actual (preservando _configurado)
    snap = {}
    for f in ENV_FIELDS:
        v = row.get(f)
        snap[f] = v.isoformat() if hasattr(v, 'isoformat') else v
    if (ambientes.get(actual) or {}).get('_configurado'):
        snap['_configurado'] = True
    ambientes[actual] = snap

    # 2) Cargar la del ambiente destino si ya existe
    updates = {'ambiente': destino, 'ambientes': psycopg2.extras.Json(ambientes)}
    tiene_destino = bool(ambientes.get(destino))
    if tiene_destino:
        for f in ENV_FIELDS:
            updates[f] = ambientes[destino].get(f)
    elif destino == 'produccion':
        updates['test_set_id'] = None
    if destino == 'produccion':
        updates['solicitud_produccion_en'] = None   # solicitud atendida   # producción nunca usa set de pruebas

    set_clause = ', '.join(f"{k}=%s" for k in updates)
    with get_db_cursor() as cur:
        cur.execute(f"UPDATE tenants SET {set_clause} WHERE id=%s",
                    list(updates.values()) + [tenant_id])

    if destino == 'produccion' and not tiene_destino:
        flash('Cambiado a PRODUCCIÓN. ⚠️ Configura los datos REALES de producción '
              '(Software ID, PIN, Clave Técnica y Resolución) antes de facturar; '
              'aún conserva los de habilitación.', 'warning')
    else:
        flash(f'Ambiente cambiado a {destino.upper()} ✅ (configuración restaurada).', 'success')
    return redirect(url_for('ui.tenant_detalle', tenant_id=tenant_id))


@ui_bp.route('/tenants/<tenant_id>/produccion', methods=['POST'])
@_login_admin
def guardar_produccion(tenant_id):
    """Guarda (anticipadamente) la configuración de PRODUCCIÓN en ambientes['produccion']."""
    import psycopg2.extras
    f = request.form
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT ambientes FROM tenants WHERE id=%s", (tenant_id,))
        row = cur.fetchone()
    if not row:
        flash('Tenant no encontrado.', 'danger')
        return redirect(url_for('ui.tenants'))
    ambientes = (row['ambientes'] or {})
    prod = ambientes.get('produccion') or {}
    for k in ('software_id', 'software_pin', 'clave_tecnica', 'resolucion_dian',
              'prefijo', 'resolucion_fecha', 'resolucion_vigencia'):
        prod[k] = (f.get(k) or '').strip() or None
    for k in ('resolucion_desde', 'resolucion_hasta'):
        prod[k] = int(f.get(k)) if (f.get(k, '').strip() or '').isdigit() else None
    prod['consecutivo_actual'] = int(f.get('consecutivo_actual') or 0)
    prod['test_set_id'] = None
    prod['_configurado'] = True   # marca: producción cargada explícitamente (habilita el switch)
    ambientes['produccion'] = prod
    with get_db_cursor() as cur:
        cur.execute("UPDATE tenants SET ambientes=%s WHERE id=%s",
                    (psycopg2.extras.Json(ambientes), tenant_id))
    flash('Datos de producción guardados. Ya puedes usar “Pasar a Producción”.', 'success')
    return redirect(url_for('ui.tenant_detalle', tenant_id=tenant_id))


@ui_bp.route('/solicitar-produccion', methods=['POST'])
@_login_tenant
def solicitar_produccion():
    """El cliente solicita pasar a producción; el administrador la activa."""
    with get_db_cursor() as cur:
        cur.execute("UPDATE tenants SET solicitud_produccion_en=NOW() "
                    "WHERE id=%s AND ambiente='habilitacion'", (session.get('ui_tenant_id'),))
    flash('✅ Solicitud enviada. El administrador habilitará tu facturación en producción. '
          'Mientras tanto puedes seguir probando.', 'success')
    return redirect(url_for('ui.facturas'))


# ── CSRF simple (token en sesión) ──────────────────────────────────────────────

def _csrf_token():
    tok = session.get('csrf_token')
    if not tok:
        tok = uuid.uuid4().hex
        session['csrf_token'] = tok
    return tok


def _csrf_ok(form):
    return form.get('csrf_token') and form.get('csrf_token') == session.get('csrf_token')


# ── Emisión autoservicio (rol tenant) ──────────────────────────────────────────

def _emitir(tenant_id, tipo, form):
    """Arma el payload desde el formulario y emite. Retorna (ok, factura_id|None)."""
    datos = {
        'referencia_pedido': (form.get('referencia') or '').strip()
                              or f"PORTAL-{tipo}-{int(time.time())}",
        'tipo_documento': tipo,
        'metodo_pago': form.get('metodo_pago', '10'),
        'moneda': 'COP',
        'notas': (form.get('notas') or '').strip(),
        'cliente': {
            'tipo_persona': form.get('cli_tipo_persona', 'natural'),
            'tipo_documento': form.get('cli_tipo_doc', 'CC'),
            'numero_documento': (form.get('cli_doc') or '').strip(),
            'nombre': (form.get('cli_nombre') or '').strip(),
            'email': (form.get('cli_email') or '').strip(),
            'telefono': (form.get('cli_telefono') or '').strip(),
            'direccion': (form.get('cli_direccion') or '').strip(),
            'municipio_codigo': (form.get('cli_municipio') or '11001').strip(),
        },
        'items': _parse_items(form),
    }
    if tipo != 'factura':
        datos['documento_referencia'] = {
            'numero': (form.get('ref_numero') or '').strip(),
            'cufe': (form.get('ref_cufe') or '').strip(),
            'fecha': (form.get('ref_fecha') or '').strip(),
        }
        codigo = form.get('concepto_codigo', '2')
        catalogo = dict(CONCEPTOS_NC if tipo == 'nota_credito' else CONCEPTOS_ND)
        datos['concepto_nota'] = {'codigo': codigo,
                                  'descripcion': catalogo.get(codigo, 'Ajuste')}

    modo, grace = _tenant_envio(tenant_id)
    if modo == 'manual':
        res = emitir_documento(tenant_id, datos, delay_seconds=60, requiere_aprobacion=True)
        flash('Documento creado. Queda PENDIENTE DE TU APROBACIÓN — no se envía a la DIAN '
              'hasta que lo apruebes. Apruébalo o recházalo desde el detalle o el monitor.', 'success')
    else:
        res = emitir_documento(tenant_id, datos, delay_seconds=grace * 60)
        flash(f"Documento emitido. Se enviará a la DIAN en {_plazo_txt(grace)}; "
              f"puedes aprobarlo ya o anularlo antes.", 'success')
    return True, res['id']


@ui_bp.route('/emitir', methods=['GET', 'POST'])
@_login_tenant
def emitir():
    """Formulario UNIFICADO: factura, nota crédito o nota débito en un solo lugar."""
    tenant_id = session.get('ui_tenant_id')
    if request.method == 'POST':
        if not _csrf_ok(request.form):
            flash('Sesión expirada, intenta de nuevo.', 'danger')
            return redirect(url_for('ui.emitir'))
        tipo = request.form.get('tipo_documento', 'factura')
        if tipo not in ('factura', 'nota_credito', 'nota_debito'):
            tipo = 'factura'
        try:
            _ok, fid = _emitir(tenant_id, tipo, request.form)
            return redirect(url_for('ui.factura_detalle', factura_id=fid))
        except EmisionError as e:
            flash(e.mensaje, 'danger')
    return render_template('ui/emitir.html', csrf_token=_csrf_token(),
                           metodos=METODOS_PAGO, tipos_doc=TIPOS_DOC_CLIENTE,
                           conceptos_nc=CONCEPTOS_NC, conceptos_nd=CONCEPTOS_ND,
                           facturas=_facturas_aceptadas(tenant_id),
                           grace=_grace_minutos(tenant_id))


@ui_bp.route('/emitir/factura', methods=['GET', 'POST'])
@_login_tenant
def emitir_factura():
    tenant_id = session.get('ui_tenant_id')
    if request.method == 'POST':
        if not _csrf_ok(request.form):
            flash('Sesión expirada, intenta de nuevo.', 'danger')
            return redirect(url_for('ui.emitir_factura'))
        try:
            _ok, fid = _emitir(tenant_id, 'factura', request.form)
            return redirect(url_for('ui.factura_detalle', factura_id=fid))
        except EmisionError as e:
            flash(e.mensaje, 'danger')
    return render_template('ui/emitir_factura.html',
                           csrf_token=_csrf_token(), metodos=METODOS_PAGO,
                           tipos_doc=TIPOS_DOC_CLIENTE, grace=_grace_minutos(tenant_id))


@ui_bp.route('/emitir/nota', methods=['GET', 'POST'])
@_login_tenant
def emitir_nota():
    tenant_id = session.get('ui_tenant_id')
    tipo = request.values.get('tipo', 'nota_credito')
    if tipo not in ('nota_credito', 'nota_debito'):
        tipo = 'nota_credito'
    if request.method == 'POST':
        if not _csrf_ok(request.form):
            flash('Sesión expirada, intenta de nuevo.', 'danger')
            return redirect(url_for('ui.emitir_nota', tipo=tipo))
        try:
            _ok, fid = _emitir(tenant_id, tipo, request.form)
            return redirect(url_for('ui.factura_detalle', factura_id=fid))
        except EmisionError as e:
            flash(e.mensaje, 'danger')
    conceptos = CONCEPTOS_NC if tipo == 'nota_credito' else CONCEPTOS_ND
    return render_template('ui/emitir_nota.html', tipo=tipo,
                           titulo=('Nota Crédito' if tipo == 'nota_credito' else 'Nota Débito'),
                           csrf_token=_csrf_token(), metodos=METODOS_PAGO,
                           tipos_doc=TIPOS_DOC_CLIENTE, conceptos=conceptos,
                           facturas=_facturas_aceptadas(tenant_id),
                           grace=_grace_minutos(tenant_id))


PRESETS_ENVIO = [(1, 'Inmediato (apenas se emite)'), (30, 'A los 30 minutos'),
                 (120, 'A las 2 horas'), (1440, 'Al día siguiente (24 h)'),
                 (10080, 'A los 7 días'), (21600, 'A los 15 días')]


@ui_bp.route('/config-envio', methods=['GET', 'POST'])
@_login_tenant
def config_envio():
    """El cliente elige cuándo se envían sus documentos a la DIAN (o si son manuales)."""
    tenant_id = session.get('ui_tenant_id')
    if request.method == 'POST':
        if not _csrf_ok(request.form):
            flash('Sesión expirada.', 'danger')
            return redirect(url_for('ui.config_envio'))
        pol = request.form.get('politica', '30')
        if pol == 'manual':
            with get_db_cursor() as cur:
                cur.execute("UPDATE tenants SET modo_aprobacion='manual' WHERE id=%s", (tenant_id,))
            flash('Listo: ahora NADA se envía a la DIAN hasta que tú lo apruebes.', 'success')
        else:
            mins = int(pol) if pol.isdigit() else 30
            with get_db_cursor() as cur:
                cur.execute("UPDATE tenants SET modo_aprobacion='automatico', grace_minutos=%s "
                            "WHERE id=%s", (mins, tenant_id))
            flash(f'Listo: los documentos se enviarán automáticamente {_plazo_txt(mins)} '
                  f'después de emitirlos (puedes aprobarlos antes).', 'success')
        return redirect(url_for('ui.config_envio'))
    modo, grace = _tenant_envio(tenant_id)
    return render_template('ui/config_envio.html', modo=modo, grace=grace,
                           presets=PRESETS_ENVIO, csrf_token=_csrf_token())


@ui_bp.route('/catalogo')
@_login_tenant
def catalogo_json():
    """Catálogo de productos de la tienda CyberShop del cliente (para autocompletar)."""
    from services.catalogo import obtener_productos
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT * FROM tenants WHERE id=%s", (session.get('ui_tenant_id'),))
        t = cur.fetchone()
    return jsonify(obtener_productos(dict(t)) if t else [])


# ── Módulo de pruebas (solo admin) ─────────────────────────────────────────────

def _muestra_datos(tipo, i, ref=None):
    base = {
        'referencia_pedido': f"SETP-{tipo}-{int(time.time())}-{i}",
        'tipo_documento': tipo, 'metodo_pago': '10', 'moneda': 'COP',
        'notas': f"Documento de prueba {tipo} #{i + 1}",
        'cliente': {'tipo_persona': 'natural', 'tipo_documento': 'CC',
                    'numero_documento': '1098765432', 'nombre': 'Cliente Prueba DIAN',
                    'email': 'prueba@ejemplo.com', 'telefono': '3001234567',
                    'direccion': 'Calle 1 2-3', 'municipio_codigo': '11001'},
        'items': [{'descripcion': f'Producto de prueba {i + 1}', 'cantidad': 1,
                   'precio_unitario': 10000 + i * 1000, 'impuesto_iva': 19,
                   'codigo_unidad': 'EA', 'codigo': f'TEST-{i + 1:03d}'}],
    }
    if tipo != 'factura' and ref:
        base['documento_referencia'] = {'numero': ref['numero'], 'cufe': ref['cufe'],
                                        'fecha': ref['fecha']}
        base['concepto_nota'] = ({'codigo': '2', 'descripcion': 'Anulación de factura'}
                                 if tipo == 'nota_credito'
                                 else {'codigo': '3', 'descripcion': 'Cambio del valor'})
    return base


def _emitir_y_enviar(tenant_id, datos):
    res = emitir_documento(tenant_id, datos, delay_seconds=0)
    if not res['existente']:
        enviar_ahora(res['id'])
    return res


@ui_bp.route('/pruebas')
@_login_admin
def pruebas():
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT id, nombre, nit, ambiente FROM tenants WHERE activo=TRUE ORDER BY nombre")
        tenants = [dict(r) for r in cur.fetchall()]
        for t in tenants:
            t['id'] = str(t['id'])
    sel = request.args.get('tenant_id', tenants[0]['id'] if tenants else '')
    progreso = None
    if sel:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute("""
                SELECT COALESCE(datos_json->>'tipo_documento','factura') AS tipo, COUNT(*) AS n
                FROM facturas WHERE tenant_id=%s AND estado='ACEPTADA'
                GROUP BY 1""", (sel,))
            progreso = {r['tipo']: r['n'] for r in cur.fetchall()}
    return render_template('ui/set_pruebas.html', tenants=tenants, sel=sel,
                           progreso=progreso or {}, csrf_token=_csrf_token())


@ui_bp.route('/pruebas/lote', methods=['POST'])
@_login_admin
def pruebas_lote():
    if not _csrf_ok(request.form):
        flash('Sesión expirada.', 'danger'); return redirect(url_for('ui.pruebas'))
    tenant_id = request.form.get('tenant_id')
    n_fv = max(0, min(50, int(request.form.get('n_fv', 0) or 0)))
    n_nc = max(0, min(50, int(request.form.get('n_nc', 0) or 0)))
    n_nd = max(0, min(50, int(request.form.get('n_nd', 0) or 0)))
    hechos = {'factura': 0, 'nota_credito': 0, 'nota_debito': 0}

    for i in range(n_fv):
        try:
            _emitir_y_enviar(tenant_id, _muestra_datos('factura', i)); hechos['factura'] += 1
        except EmisionError:
            pass

    refs = _facturas_aceptadas(tenant_id)
    if (n_nc or n_nd) and not refs:
        flash('Emití las facturas. Cuando la DIAN las acepte, vuelve a correr el lote '
              'para generar las notas (necesitan una factura aceptada de referencia).', 'warning')
    else:
        for i in range(n_nc):
            try:
                _emitir_y_enviar(tenant_id, _muestra_datos('nota_credito', i, refs[i % len(refs)]))
                hechos['nota_credito'] += 1
            except EmisionError:
                pass
        for i in range(n_nd):
            try:
                _emitir_y_enviar(tenant_id, _muestra_datos('nota_debito', i, refs[i % len(refs)]))
                hechos['nota_debito'] += 1
            except EmisionError:
                pass

    flash(f"Lote emitido y enviado: {hechos['factura']} facturas, "
          f"{hechos['nota_credito']} notas crédito, {hechos['nota_debito']} notas débito. "
          f"Revisa el monitor para ver la respuesta de la DIAN.", 'success')
    return redirect(url_for('ui.facturas', tenant_id=tenant_id))


@ui_bp.route('/pruebas/individual', methods=['POST'])
@_login_admin
def pruebas_individual():
    if not _csrf_ok(request.form):
        flash('Sesión expirada.', 'danger'); return redirect(url_for('ui.pruebas'))
    tenant_id = request.form.get('tenant_id')
    tipo = request.form.get('tipo', 'factura')
    if tipo not in ('factura', 'nota_credito', 'nota_debito'):
        tipo = 'factura'
    ref = None
    if tipo != 'factura':
        refs = _facturas_aceptadas(tenant_id)
        if not refs:
            flash('No hay facturas aceptadas para referenciar. Emite una factura primero.', 'warning')
            return redirect(url_for('ui.pruebas', tenant_id=tenant_id))
        ref = refs[0]
    try:
        res = _emitir_y_enviar(tenant_id, _muestra_datos(tipo, 0, ref))
        flash('Documento de prueba emitido y enviado a la DIAN.', 'success')
        return redirect(url_for('ui.factura_detalle', factura_id=res['id']))
    except EmisionError as e:
        flash(e.mensaje, 'danger')
        return redirect(url_for('ui.pruebas', tenant_id=tenant_id))


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


@ui_bp.route('/facturas/<factura_id>/anular', methods=['POST'])
@_login_tenant_o_admin
def factura_anular(factura_id):
    """Anula un PENDIENTE dentro del plazo de gracia (tenant sobre los suyos)."""
    scope = _tenant_id_scope()
    with get_db_cursor() as cur:
        if scope:
            cur.execute("""UPDATE facturas SET estado='CANCELADA'
                WHERE id=%s AND tenant_id=%s AND estado='PENDIENTE'
                  AND (procesar_en>NOW() OR requiere_aprobacion)
                RETURNING id""", (factura_id, scope))
        else:
            cur.execute("""UPDATE facturas SET estado='CANCELADA'
                WHERE id=%s AND estado='PENDIENTE'
                  AND (procesar_en>NOW() OR requiere_aprobacion) RETURNING id""",
                        (factura_id,))
        ok = cur.fetchone()
    if ok:
        with get_db_cursor() as cur:
            cur.execute("INSERT INTO factura_eventos (factura_id, evento, detalle) "
                        "VALUES (%s,%s,%s)", (factura_id, 'CANCELADA', 'Anulada desde el portal'))
        flash('Documento anulado. No será enviado a la DIAN.', 'success')
    else:
        flash('No se puede anular: ya fue enviado o el plazo venció.', 'danger')
    return redirect(url_for('ui.factura_detalle', factura_id=factura_id))


@ui_bp.route('/facturas/<factura_id>/enviar-ya', methods=['POST'])
@_login_tenant_o_admin
def factura_enviar_ya(factura_id):
    """Adelanta el envío (sin esperar el plazo)."""
    scope = _tenant_id_scope()
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT tenant_id, estado FROM facturas WHERE id=%s", (factura_id,))
        f = cur.fetchone()
    if not f or (scope and str(f['tenant_id']) != scope):
        flash('No autorizado.', 'danger')
        return redirect(url_for('ui.facturas'))
    if enviar_ahora(factura_id):
        flash('Enviando a la DIAN ahora…', 'success')
    else:
        flash('No se puede enviar: el documento no está pendiente.', 'danger')
    return redirect(url_for('ui.factura_detalle', factura_id=factura_id))


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
            "SELECT xml_path, response_path, pdf_path, numero_factura, tenant_id "
            "FROM facturas WHERE id=%s",
            (factura_id,)
        )
        f = cur.fetchone()

    if not f:
        return 'Factura no encontrada', 404

    if tenant_scope and str(f['tenant_id']) != tenant_scope:
        return 'No autorizado', 403

    if tipo == 'pdf':
        path, suffix = f.get('pdf_path'), '.pdf'
    elif tipo == 'xml':
        path, suffix = f['xml_path'], '_firmado.xml'
    else:
        path, suffix = f['response_path'], '_response.xml'
    if not path or not Path(path).exists():
        return 'Archivo no disponible', 404

    nombre = f['numero_factura'] or factura_id[:8]
    return send_file(path, as_attachment=True, download_name=f"{nombre}{suffix}")
