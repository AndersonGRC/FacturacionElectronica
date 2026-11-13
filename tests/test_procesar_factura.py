"""El procesamiento de una factura: reintentos, Regla 90, rango y producción.

La DIAN y la firma están simuladas. Cada prueba recrea un caso que antes
terminaba mal (ver docs de cada una).
"""
import pytest
import requests

from conftest import leer

OK = {'is_valid': True, 'status_code': '00', 'description': 'Procesado Correctamente.',
      'errors': [], 'cufe_dian': '', 'raw_xml': b'<respuesta>ok</respuesta>'}
REGLA_90 = {'is_valid': False, 'status_code': '99',
            'description': 'Validación contiene errores en campos mandatorios.',
            'errors': ['Regla: 90, Rechazo: Documento procesado anteriormente.'],
            'cufe_dian': '', 'raw_xml': b'<respuesta>regla 90</respuesta>'}


class Dian:
    """Controla la DIAN simulada: qué responde y qué recibió."""

    def __init__(self):
        self.envios = []            # XML recibido por SendBillSync
        self.envios_set = []        # XML recibido por SendTestSetAsync
        self.consultas_cufe = []
        self.consultas_zip = []
        self.respuestas = []        # cola de respuestas (o excepciones) del envío
        self.respuesta_consulta = OK
        self.respuestas_zip = []

    def _siguiente(self, cola, defecto):
        r = cola.pop(0) if cola else defecto
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def dian(monkeypatch):
    import tasks.facturacion as tf
    control = Dian()

    class DianFalsa:
        def __init__(self, **kw):
            self.kw = kw

        def enviar_factura(self, xml_firmado, numero_factura, token_dian=None):
            control.envios.append(xml_firmado)
            return control._siguiente(control.respuestas, OK)

        def consultar_por_cufe(self, cufe):
            control.consultas_cufe.append(cufe)
            return control._siguiente([], control.respuesta_consulta)

        def enviar_test_set(self, xml_firmado, numero_factura, test_set_id):
            control.envios_set.append(xml_firmado)
            return {'zip_key': 'ZIP-123', 'errors': []}

        def obtener_estado_documento(self, zip_key):
            control.consultas_zip.append(zip_key)
            return control._siguiente(control.respuestas_zip, OK)

    class FirmaFalsa:
        firmas = 0

        def __init__(self, cert_path, password):
            pass

        def firmar(self, xml_bytes):
            FirmaFalsa.firmas += 1
            return xml_bytes

    monkeypatch.setattr(tf, 'DIANClient', DianFalsa)
    monkeypatch.setattr(tf, 'FirmadorDIAN', FirmaFalsa)
    monkeypatch.setattr(tf, 'descifrar', lambda _v: 'clave-del-p12')
    monkeypatch.setattr(tf.time, 'sleep', lambda _s: None)
    monkeypatch.setattr('services.pdf_builder.generar_pdf', lambda *a, **k: b'%PDF-falso')
    control.firma = FirmaFalsa
    return control


def _procesar(fid):
    from tasks.facturacion import procesar_factura
    return procesar_factura(fid)


def _factura(fid):
    return leer('SELECT * FROM facturas WHERE id = %s', fid)[0]


def _eventos(fid):
    return [e['evento'] for e in
            leer('SELECT evento FROM factura_eventos WHERE factura_id = %s ORDER BY id', fid)]


def _consecutivo(tid):
    return leer('SELECT consecutivo_actual FROM tenants WHERE id = %s', tid)[0]['consecutivo_actual']


# ── Reintentos (FE-03) ───────────────────────────────────────────────────────

def test_el_xml_y_el_cufe_quedan_guardados_antes_de_enviar(dian, crear_tenant, crear_factura):
    t = crear_tenant()
    fid = crear_factura(t['id'])
    vistos = []
    original = dian._siguiente

    def mirar(cola, defecto):
        vistos.append(_factura(fid))      # estado de la fila en el momento del envío
        return original(cola, defecto)
    dian._siguiente = mirar

    assert _procesar(fid)['status'] == 'ACEPTADA'
    en_el_envio = vistos[0]
    assert en_el_envio['cufe'] and en_el_envio['xml_path'] and en_el_envio['numero_factura'] == 'PRUE1'
    f = _factura(fid)
    assert f['estado'] == 'ACEPTADA' and f['cufe'] == en_el_envio['cufe']


def test_un_reintento_reenvia_el_mismo_xml_y_no_toma_otro_numero(dian, crear_tenant, crear_factura):
    """Antes el reintento calculaba otra hora y otro CUFE con el mismo número."""
    t = crear_tenant()
    fid = crear_factura(t['id'])
    dian.respuestas = [requests.exceptions.ReadTimeout('la DIAN no respondió a tiempo')]

    with pytest.raises(requests.exceptions.ReadTimeout):
        _procesar(fid)
    tras_timeout = _factura(fid)
    assert tras_timeout['estado'] == 'ERROR'
    assert tras_timeout['numero_factura'] == 'PRUE1' and tras_timeout['cufe']

    assert _procesar(fid)['status'] == 'ACEPTADA'
    assert dian.envios[0] == dian.envios[1]             # byte a byte el mismo documento
    assert dian.firma.firmas == 1                        # se firmó una sola vez
    assert _consecutivo(t['id']) == 1                    # no se gastó otro número
    f = _factura(fid)
    assert f['cufe'] == tras_timeout['cufe'] and f['xml_path'] == tras_timeout['xml_path']
    assert 'REUTILIZA' in _eventos(fid)


def test_regla_90_consulta_por_cufe_y_la_deja_aceptada(dian, crear_tenant, crear_factura):
    """El primer envío llegó pero respondió tarde: el reenvío da Regla 90."""
    t = crear_tenant()
    fid = crear_factura(t['id'])
    dian.respuestas = [requests.exceptions.ReadTimeout('tarde'), REGLA_90]

    with pytest.raises(requests.exceptions.ReadTimeout):
        _procesar(fid)
    resultado = _procesar(fid)

    assert resultado['status'] == 'ACEPTADA'
    f = _factura(fid)
    assert f['estado'] == 'ACEPTADA'
    assert dian.consultas_cufe == [f['cufe']]


def test_regla_90_sin_confirmacion_no_queda_rechazada(dian, crear_tenant, crear_factura):
    t = crear_tenant()
    fid = crear_factura(t['id'])
    dian.respuestas = [REGLA_90]
    dian.respuesta_consulta = {'is_valid': False, 'errors': ['sin respuesta'], 'raw_xml': b''}

    assert _procesar(fid)['status'] == 'REVISION'
    f = _factura(fid)
    assert f['estado'] == 'ERROR'                         # nunca RECHAZADA
    assert 'ya tiene este documento' in f['error_mensaje']
    assert 'REVISION' in _eventos(fid)
    assert len(dian.envios) == 1                          # no se reenvió otro


def test_un_rechazo_real_sigue_siendo_rechazo(dian, crear_tenant, crear_factura):
    t = crear_tenant()
    fid = crear_factura(t['id'])
    dian.respuestas = [{'is_valid': False, 'status_code': '99', 'description': 'Errores',
                        'errors': ['Regla: FAK48, Rechazo: tipo de documento inválido'],
                        'cufe_dian': '', 'raw_xml': b'<r/>'}]
    assert _procesar(fid)['status'] == 'RECHAZADA'
    assert dian.consultas_cufe == []


def test_set_de_pruebas_reintento_consulta_y_no_reenvia(dian, crear_tenant, crear_factura):
    t = crear_tenant(test_set_id='set-de-pruebas')
    fid = crear_factura(t['id'])
    dian.respuestas_zip = [requests.exceptions.ConnectionError('se cayó la red')]

    with pytest.raises(requests.exceptions.ConnectionError):
        _procesar(fid)
    assert _factura(fid)['zip_key'] == 'ZIP-123'

    assert _procesar(fid)['status'] == 'ACEPTADA'
    assert len(dian.envios_set) == 1                      # el set no recibió un repetido
    assert dian.consultas_zip == ['ZIP-123', 'ZIP-123']


# ── Rango y vigencia de la resolución (FE-04) ────────────────────────────────

def test_no_se_numera_fuera_del_rango(dian, crear_tenant, crear_factura):
    t = crear_tenant(resolucion_hasta=5, consecutivo_actual=5)
    fid = crear_factura(t['id'])

    resultado = _procesar(fid)              # no lanza: no hay reintento automático

    assert resultado['status'] == 'error'
    f = _factura(fid)
    assert f['estado'] == 'ERROR' and f['numero_factura'] is None
    assert 'agotó el rango' in f['error_mensaje']
    assert _consecutivo(t['id']) == 5
    assert dian.envios == []


def test_no_se_numera_con_la_resolucion_vencida(dian, crear_tenant, crear_factura):
    t = crear_tenant(resolucion_vigencia='2020-01-01')
    fid = crear_factura(t['id'])
    assert _procesar(fid)['status'] == 'error'
    assert 'venció' in _factura(fid)['error_mensaje']
    assert _consecutivo(t['id']) == 0
    assert dian.envios == []


def test_el_ultimo_numero_del_rango_si_se_usa(dian, crear_tenant, crear_factura):
    t = crear_tenant(resolucion_hasta=5, consecutivo_actual=4)
    fid = crear_factura(t['id'])
    assert _procesar(fid)['status'] == 'ACEPTADA'
    assert _factura(fid)['numero_factura'] == 'PRUE5'


def test_un_numero_bajo_el_rango_no_se_usa(dian, crear_tenant, crear_factura):
    t = crear_tenant(resolucion_desde=990000000, resolucion_hasta=995000000)
    fid = crear_factura(t['id'])
    assert _procesar(fid)['status'] == 'error'
    assert 'por debajo del rango' in _factura(fid)['error_mensaje']


# ── Producción sin datos inventados (FE-25) ──────────────────────────────────

def test_produccion_exige_los_datos_reales_del_emisor(dian, crear_tenant, crear_factura):
    t = crear_tenant(ambiente='produccion', telefono=None, clave_tecnica=None)
    fid = crear_factura(t['id'])

    assert _procesar(fid)['status'] == 'error'
    f = _factura(fid)
    assert 'teléfono' in f['error_mensaje'] and 'clave técnica' in f['error_mensaje']
    assert _consecutivo(t['id']) == 0                    # no se gastó el número
    assert dian.envios == []


def test_produccion_completa_si_factura(dian, crear_tenant, crear_factura):
    t = crear_tenant(ambiente='produccion')
    fid = crear_factura(t['id'])
    assert _procesar(fid)['status'] == 'ACEPTADA'
