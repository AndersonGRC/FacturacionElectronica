"""Pruebas del microservicio de facturación electrónica.

Corren contra una base PostgreSQL de PRUEBA (DB_NAME debe terminar en `_test`)
y con la DIAN simulada: ninguna prueba se conecta a la DIAN ni firma con un
certificado real.

    DB_NAME=facturacion_dian_test DB_USER=... DB_PASSWORD=... pytest tests
"""
import hashlib
import os
import sys
import uuid
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
APP_DIR = RAIZ / 'app'
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


def _base_de_prueba() -> bool:
    return os.getenv('DB_NAME', '').endswith('_test')


@pytest.fixture(scope='session', autouse=True)
def _esquema(tmp_path_factory):
    if not _base_de_prueba():
        pytest.skip('Falta DB_NAME=<base>_test: las pruebas nunca usan la base real.')
    os.environ['STORAGE_BASE'] = str(tmp_path_factory.mktemp('storage'))
    from database import get_db_connection
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute((RAIZ / 'schema.sql').read_text(encoding='utf-8'))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _limpiar(_esquema):
    from database import get_db_cursor
    with get_db_cursor() as cur:
        cur.execute('TRUNCATE factura_eventos, facturas, tenants CASCADE')
    yield


@pytest.fixture(autouse=True)
def entregas_programadas(_esquema, monkeypatch):
    """La entrega al comprador va por Celery: en las pruebas solo se anota."""
    import tasks.facturacion as tf
    programadas = []
    monkeypatch.setattr(tf, '_programar_entrega', programadas.append)
    return programadas


@pytest.fixture
def crear_tenant():
    """Crea un emisor de prueba. Por defecto: habilitación, rango 1-100, vigente."""
    from database import get_db_cursor

    def _crear(**extra):
        datos = {
            'id': str(uuid.uuid4()),
            'nombre': 'Emisor Prueba',
            'nit': str(900000000 + len(extra) + uuid.uuid4().int % 99999),
            'digito_verificacion': 1,
            'razon_social': 'Emisor Prueba SAS',
            'api_key_hash': hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest(),
            'ambiente': 'habilitacion',
            'cert_path': '/no/existe/cert.p12',
            'cert_password_enc': 'cifrada',
            'clave_tecnica': 'clave-de-prueba',
            'software_id': 'software-de-prueba',
            'software_pin': '12345',
            'prefijo': 'PRUE',
            'resolucion_dian': '18760000001',
            'resolucion_desde': 1,
            'resolucion_hasta': 100,
            'resolucion_vigencia': '2099-12-31',
            'consecutivo_actual': 0,
            'regimen_codigo': '49',
            'tipo_persona_emisor': 'natural',
            'direccion': 'Calle 1 # 2-3',
            'municipio_codigo': '11001',
            'municipio_nombre': 'Bogotá, D.C.',
            'departamento_codigo': '11',
            'departamento_nombre': 'Bogotá',
            'email': 'emisor@ejemplo.com',
            'telefono': '3000000000',
        }
        datos.update(extra)
        columnas = ', '.join(datos)
        marcas = ', '.join(['%s'] * len(datos))
        with get_db_cursor() as cur:
            cur.execute(f'INSERT INTO tenants ({columnas}) VALUES ({marcas})',
                        list(datos.values()))
        return datos

    return _crear


@pytest.fixture
def crear_factura():
    from database import get_db_cursor
    import psycopg2.extras

    def _crear(tenant_id, tipo='factura', **extra):
        datos = {
            'referencia_pedido': f'PED-{uuid.uuid4().hex[:8]}',
            'tipo_documento': tipo, 'metodo_pago': '10', 'moneda': 'COP',
            'cliente': {'tipo_persona': 'natural', 'tipo_documento': 'CC',
                        'numero_documento': '1098765432', 'nombre': 'Cliente Prueba',
                        'email': 'cliente@ejemplo.com', 'telefono': '3001234567',
                        'direccion': 'Calle 1 2-3', 'municipio_codigo': '11001'},
            'items': [{'descripcion': 'Producto', 'cantidad': 1, 'precio_unitario': 10000,
                       'impuesto_iva': 0, 'codigo_unidad': 'EA', 'codigo': 'P-001'}],
        }
        datos.update(extra)
        fid = str(uuid.uuid4())
        with get_db_cursor() as cur:
            cur.execute(
                """INSERT INTO facturas (id, tenant_id, referencia_pedido, datos_json)
                   VALUES (%s, %s, %s, %s)""",
                (fid, tenant_id, datos['referencia_pedido'], psycopg2.extras.Json(datos)))
        return fid

    return _crear


def leer(sql, *params):
    from database import get_db_cursor
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
