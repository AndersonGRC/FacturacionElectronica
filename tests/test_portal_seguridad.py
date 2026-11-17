"""Portal tributario: token en los formularios, set de pruebas y API de administración."""
import re
from pathlib import Path

import pytest

from conftest import APP_DIR, leer

PLANTILLAS = APP_DIR / 'templates' / 'ui'
LLAVE = 'llave-maestra-de-prueba-0123456789'


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv('MASTER_API_KEY', LLAVE)
    from app import create_app
    a = create_app()
    a.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    import routes.ui as ui
    ui._FALLOS_ADMIN.clear()
    return a


@pytest.fixture
def admin(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s['ui_role'] = 'admin'
        s['csrf_token'] = 'token-valido'
    return c


# ── Token en todos los formularios (FE-13) ───────────────────────────────────

def test_todo_formulario_post_del_portal_lleva_token():
    sin_token = []
    for plantilla in PLANTILLAS.glob('*.html'):
        if plantilla.name in ('login.html', 'tenant_portal_login.html'):
            continue
        texto = plantilla.read_text(encoding='utf-8')
        for m in re.finditer(r'<form\b[^>]*method="POST"[^>]*>(.*?)</form>', texto,
                             re.IGNORECASE | re.DOTALL):
            if 'name="csrf_token"' not in m.group(1):
                linea = texto[:m.start()].count('\n') + 1
                sin_token.append(f'{plantilla.name}:{linea}')
    assert sin_token == []


def test_un_post_sin_token_no_cambia_nada(admin, crear_tenant):
    t = crear_tenant(nombre='Original')
    r = admin.post(f"/ui/tenants/{t['id']}/guardar", data={'nombre': 'Cambiado'})
    assert r.status_code == 302
    assert leer('SELECT nombre FROM tenants WHERE id=%s', t['id'])[0]['nombre'] == 'Original'


def test_un_post_con_token_si_guarda(admin, crear_tenant):
    t = crear_tenant(nombre='Original')
    admin.post(f"/ui/tenants/{t['id']}/guardar",
               data={'nombre': 'Cambiado', 'razon_social': 'X', 'activo': 'true',
                     'digito_verificacion': '1', 'csrf_token': 'token-valido'})
    assert leer('SELECT nombre FROM tenants WHERE id=%s', t['id'])[0]['nombre'] == 'Cambiado'


def test_regenerar_la_llave_ya_no_se_hace_con_un_enlace(admin, crear_tenant):
    t = crear_tenant()
    antes = t['api_key_hash']
    assert admin.get(f"/ui/tenants/{t['id']}/regen").status_code == 405
    admin.post(f"/ui/tenants/{t['id']}/regen")                       # sin token
    assert leer('SELECT api_key_hash FROM tenants WHERE id=%s', t['id'])[0]['api_key_hash'] == antes
    admin.post(f"/ui/tenants/{t['id']}/regen", data={'csrf_token': 'token-valido'})
    assert leer('SELECT api_key_hash FROM tenants WHERE id=%s', t['id'])[0]['api_key_hash'] != antes


def test_el_formulario_del_emisor_sale_con_token(admin, crear_tenant):
    t = crear_tenant()
    html = admin.get(f"/ui/tenants/{t['id']}").data.decode('utf-8')
    # Regenerar llave, cambiar ambiente, guardar, producción, certificado y portal
    # (el de desbloqueo solo aparece si el portal está bloqueado).
    formularios = len(re.findall(r'<form\b[^>]*method="POST"', html))
    assert formularios >= 6
    assert html.count('name="csrf_token" value="token-valido"') == formularios


# ── Set de pruebas solo en habilitación (FE-05) ──────────────────────────────

@pytest.fixture
def sin_envio(monkeypatch):
    import routes.ui as ui
    enviados = []
    monkeypatch.setattr(ui, 'enviar_ahora', lambda fid, aprobado_por=None: enviados.append(fid))
    return enviados


def test_el_set_de_pruebas_no_emite_en_produccion(admin, crear_tenant, sin_envio):
    t = crear_tenant(ambiente='produccion')
    r = admin.post('/ui/pruebas/lote', data={'tenant_id': t['id'], 'n_fv': 3,
                                             'csrf_token': 'token-valido'})
    assert r.status_code == 302
    assert leer('SELECT id FROM facturas WHERE tenant_id=%s', t['id']) == []
    assert sin_envio == []
    r = admin.post('/ui/pruebas/individual', data={'tenant_id': t['id'], 'tipo': 'factura',
                                                   'csrf_token': 'token-valido'})
    assert leer('SELECT id FROM facturas WHERE tenant_id=%s', t['id']) == []


def test_el_set_de_pruebas_si_emite_en_habilitacion(admin, crear_tenant, sin_envio):
    t = crear_tenant(ambiente='habilitacion')
    admin.post('/ui/pruebas/lote', data={'tenant_id': t['id'], 'n_fv': 2,
                                         'csrf_token': 'token-valido'})
    assert len(leer('SELECT id FROM facturas WHERE tenant_id=%s', t['id'])) == 2
    assert len(sin_envio) == 2


def test_la_lista_del_set_no_muestra_emisores_en_produccion(admin, crear_tenant):
    crear_tenant(nombre='Emisor En Pruebas')
    crear_tenant(nombre='Emisor En Produccion', ambiente='produccion')
    html = admin.get('/ui/pruebas').data.decode('utf-8')
    assert 'Emisor En Pruebas' in html and 'Emisor En Produccion' not in html


# ── Login de administrador (FE-15) ───────────────────────────────────────────

def test_el_login_de_admin_se_bloquea_tras_cinco_fallos(app):
    c = app.test_client()
    for _ in range(5):
        c.post('/ui/login', data={'password': 'equivocada'})
    r = c.post('/ui/login', data={'password': LLAVE})
    assert r.status_code == 429
    with c.session_transaction() as s:
        assert s.get('ui_role') != 'admin'


def test_el_login_de_admin_entra_con_la_llave(app):
    c = app.test_client()
    r = c.post('/ui/login', data={'password': LLAVE})
    assert r.status_code == 302
    with c.session_transaction() as s:
        assert s.get('ui_role') == 'admin'


def test_sin_llave_maestra_nadie_entra_con_contrasena_vacia(app, monkeypatch):
    monkeypatch.setenv('MASTER_API_KEY', '')
    c = app.test_client()
    c.post('/ui/login', data={'password': ''})
    with c.session_transaction() as s:
        assert s.get('ui_role') != 'admin'


# ── API de administración (FE-14) ────────────────────────────────────────────

def test_la_api_crea_todo_emisor_en_habilitacion(app):
    c = app.test_client()
    r = c.post('/api/v1/admin/tenants', headers={'X-Master-Key': LLAVE},
               json={'nombre': 'Nuevo', 'nit': '901234567', 'digito_verificacion': 1,
                     'razon_social': 'Nuevo SAS', 'ambiente': 'produccion'})
    assert r.status_code == 201
    cuerpo = r.get_json()
    assert cuerpo['ambiente'] == 'habilitacion' and 'aviso' in cuerpo
    assert leer('SELECT ambiente FROM tenants WHERE id=%s', cuerpo['id'])[0]['ambiente'] == 'habilitacion'


def test_la_api_no_cambia_el_ambiente(app, crear_tenant):
    t = crear_tenant()
    c = app.test_client()
    r = c.put(f"/api/v1/admin/tenants/{t['id']}", headers={'X-Master-Key': LLAVE},
              json={'ambiente': 'produccion'})
    assert r.status_code == 409
    assert leer('SELECT ambiente FROM tenants WHERE id=%s', t['id'])[0]['ambiente'] == 'habilitacion'
    r = c.put(f"/api/v1/admin/tenants/{t['id']}", headers={'X-Master-Key': LLAVE},
              json={'nombre': 'Renombrado'})
    assert r.status_code == 200


# ── Avisos de certificado, resolución y rango (FE-17) ───────────────────────

def test_avisos_de_certificado_resolucion_y_rango():
    from datetime import date
    from services.alertas import avisos
    hoy = date(2026, 11, 15)
    base = {'resolucion_desde': 1, 'resolucion_hasta': 100, 'consecutivo_actual': 10,
            'resolucion_vigencia': '2028-10-26'}
    assert avisos(base, hoy, vence_cert=date(2027, 6, 19)) == []
    claves = lambda t, cert: [(a['clave'], a['nivel']) for a in avisos(t, hoy, vence_cert=cert)]
    assert claves(base, date(2026, 12, 1)) == [('certificado', 'warning')]
    assert claves(base, date(2026, 11, 1)) == [('certificado', 'danger')]
    assert claves({**base, 'resolucion_vigencia': '2026-12-20'}, date(2027, 6, 19)) == [('resolucion', 'warning')]
    assert claves({**base, 'consecutivo_actual': 85}, date(2027, 6, 19)) == [('rango', 'warning')]
    assert claves({**base, 'consecutivo_actual': 100}, date(2027, 6, 19)) == [('rango', 'danger')]


def test_el_portal_muestra_el_aviso_y_el_correo_diario_sale(admin, crear_tenant, monkeypatch):
    import services.alertas as al
    import services.entrega as en
    t = crear_tenant(consecutivo_actual=90)
    al._CACHE_AVISOS.clear()
    html = admin.get(f"/ui/tenants/{t['id']}").data.decode('utf-8')
    assert 'Ya usaste el 90 % de la numeración' in html
    enviados = []
    monkeypatch.setattr(en, 'enviar_smtp', lambda cfg, para, asunto, cuerpo, *a, **k: enviados.append((para, cuerpo)))
    assert al.enviar_avisos_diarios() == {'enviados': 0, 'sin_correo': 1}     # sin correo configurado
    import psycopg2.extras
    from database import get_db_cursor
    with get_db_cursor() as cur:
        cur.execute("UPDATE tenants SET correo_entrega = %s WHERE id = %s",
                    (psycopg2.extras.Json({'host': 'smtp.x.com', 'puerto': '587', 'remitente': 'a@x.com'}), t['id']))
    assert al.enviar_avisos_diarios()['enviados'] == 1
    assert enviados[0][0] == 'emisor@ejemplo.com' and '90 %' in enviados[0][1]
