"""Comprador con NIT, tarifas de IVA y casos ampliados (FE-09, FE-10, FE-11).

Reglas del Anexo 1.9 que antes se incumplían: FAK24/FAK25/FAK47 (DV del NIT del
comprador, rechazo), FAK55 (correo del comprador, rechazo) y un subtotal de IVA
por tarifa.
"""
from decimal import Decimal

import pytest
from lxml import etree

from conftest import leer

NS = {'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
      'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'}
CLIENTE = 'cac:AccountingCustomerParty/cac:Party/'
EMISOR = 'cac:AccountingSupplierParty/cac:Party/'


def _xml(tenant, cliente=None, items=None, **extra):
    from services.xml_builder import XMLBuilder
    datos = {'tipo_documento': 'factura', 'metodo_pago': '10', 'moneda': 'COP',
             'cliente': cliente or {'tipo_persona': 'natural', 'tipo_documento': 'CC',
                                    'numero_documento': '1098765432', 'nombre': 'Cliente',
                                    'email': 'cliente@ejemplo.com'},
             'items': items or [{'descripcion': 'P', 'cantidad': 1, 'precio_unitario': 10000,
                                 'impuesto_iva': 19}], **extra}
    return etree.fromstring(XMLBuilder(tenant, datos, 'PRUE1', 'c' * 96,
                                       '2026-11-13', '10:00:00-05:00').build())


EMPRESA = {'tipo_persona': 'juridica', 'tipo_documento': 'NIT',
           'numero_documento': '890903938', 'nombre': 'Empresa SAS',
           'email': 'compras@empresa.com', 'municipio_codigo': '05001',
           'regimen': '48', 'responsabilidades': ['O-13', 'O-15']}


def test_digito_de_verificacion():
    from services.xml_builder import digito_verificacion
    assert digito_verificacion('800197268') == '4'      # DIAN
    assert digito_verificacion('890903938') == '8'
    assert digito_verificacion('860034313') == '7'


def test_comprador_con_nit_lleva_dv_responsabilidades_y_ciudad(crear_tenant):
    x = _xml(crear_tenant(), EMPRESA)
    for ruta in ('cac:PartyTaxScheme/cbc:CompanyID', 'cac:PartyLegalEntity/cbc:CompanyID',
                 'cac:PartyIdentification/cbc:ID'):
        cid = x.find(CLIENTE + ruta, NS)
        assert cid.text == '890903938'
        assert cid.get('schemeName') == '31' and cid.get('schemeID') == '8'
    tlc = x.find(CLIENTE + 'cac:PartyTaxScheme/cbc:TaxLevelCode', NS)
    assert tlc.text == 'O-13;O-15' and tlc.get('listName') == '48'
    assert x.findtext(CLIENTE + 'cac:PartyTaxScheme/cac:TaxScheme/cbc:ID', namespaces=NS) == '01'
    assert x.findtext(CLIENTE + 'cbc:AdditionalAccountID', namespaces=NS) is None
    assert x.findtext('cac:AccountingCustomerParty/cbc:AdditionalAccountID', namespaces=NS) == '1'
    dire = CLIENTE + 'cac:PhysicalLocation/cac:Address/'
    assert x.findtext(dire + 'cbc:ID', namespaces=NS) == '05001'
    assert x.findtext(dire + 'cbc:CityName', namespaces=NS) == 'Medellín'
    assert x.findtext(dire + 'cbc:CountrySubentity', namespaces=NS) == 'Antioquia'
    assert x.findtext(dire + 'cbc:CountrySubentityCode', namespaces=NS) == '05'


def test_nit_con_guion_se_separa_y_el_dv_se_calcula(crear_tenant):
    x = _xml(crear_tenant(), {**EMPRESA, 'numero_documento': '890.903.938-1'})
    cid = x.find(CLIENTE + 'cac:PartyTaxScheme/cbc:CompanyID', NS)
    assert cid.text == '890903938' and cid.get('schemeID') == '8'


def test_cedula_no_lleva_dv(crear_tenant):
    x = _xml(crear_tenant())
    cid = x.find(CLIENTE + 'cac:PartyTaxScheme/cbc:CompanyID', NS)
    assert cid.get('schemeName') == '13' and cid.get('schemeID') is None
    assert x.findtext(CLIENTE + 'cac:PartyTaxScheme/cbc:TaxLevelCode', namespaces=NS) == 'R-99-PN'
    assert x.findtext(CLIENTE + 'cac:PartyTaxScheme/cac:TaxScheme/cbc:ID', namespaces=NS) == 'ZZ'


def test_sin_correo_del_comprador_va_el_del_emisor(crear_tenant):
    t = crear_tenant()
    x = _xml(t, {'tipo_persona': 'natural', 'tipo_documento': 'CC',
                 'numero_documento': '1', 'nombre': 'Sin correo', 'email': ''})
    assert x.findtext(CLIENTE + 'cac:Contact/cbc:ElectronicMail', namespaces=NS) == t['email']


def test_emisor_no_responsable_declara_no_aplica(crear_tenant):
    x = _xml(crear_tenant(regimen_codigo='49'))
    assert x.findtext(EMISOR + 'cac:PartyTaxScheme/cac:TaxScheme/cbc:ID', namespaces=NS) == 'ZZ'
    assert x.find(EMISOR + 'cac:PartyTaxScheme/cbc:TaxLevelCode', NS).get('listName') == '49'
    x = _xml(crear_tenant(regimen_codigo='48'))
    assert x.findtext(EMISOR + 'cac:PartyTaxScheme/cac:TaxScheme/cbc:ID', namespaces=NS) == '01'
    assert x.find(EMISOR + 'cac:PartyTaxScheme/cbc:TaxLevelCode', NS).get('listName') == '48'


MIXTO = [{'descripcion': 'E', 'cantidad': 1, 'precio_unitario': 10000, 'impuesto_iva': 0},
         {'descripcion': 'C', 'cantidad': 2, 'precio_unitario': 10000, 'impuesto_iva': 5},
         {'descripcion': 'G', 'cantidad': 1, 'precio_unitario': 10000, 'impuesto_iva': 19}]


def _subtotales(x):
    tt = x.find('cac:TaxTotal', NS)                     # el del documento (primer hijo)
    return tt.findtext('cbc:TaxAmount', namespaces=NS), [
        (s.findtext('cbc:TaxableAmount', namespaces=NS), s.findtext('cbc:TaxAmount', namespaces=NS),
         s.findtext('cac:TaxCategory/cbc:Percent', namespaces=NS))
        for s in tt.findall('cac:TaxSubtotal', NS)]


def test_un_subtotal_de_iva_por_tarifa(crear_tenant):
    x = _xml(crear_tenant(regimen_codigo='48'), items=MIXTO)
    total, subs = _subtotales(x)
    assert subs == [('10000.00', '0.00', '0.00'), ('20000.00', '1000.00', '5.00'),
                    ('10000.00', '1900.00', '19.00')]
    assert total == '2900.00'
    assert x.findtext('cac:LegalMonetaryTotal/cbc:PayableAmount', namespaces=NS) == '42900.00'


def test_simular_responsable_de_iva_solo_en_habilitacion(crear_tenant):
    hab = crear_tenant(regimen_codigo='49', ambiente='habilitacion')
    total, _subs = _subtotales(_xml(hab, items=MIXTO, prueba_responsable_iva=True))
    assert total == '2900.00'
    prod = crear_tenant(regimen_codigo='49', ambiente='produccion')
    total, subs = _subtotales(_xml(prod, items=MIXTO, prueba_responsable_iva=True))
    assert total == '0.00' and subs == [('40000.00', '0.00', '0.00')]


def test_el_cufe_usa_el_mismo_numero_de_comprador_que_el_xml(crear_tenant, crear_factura,
                                                             monkeypatch):
    import tasks.facturacion as tf
    vistos = {}
    real = tf.calcular_cufe

    def espiar(**kw):
        vistos.update(kw)
        return real(**kw)

    class DianFalsa:
        def __init__(self, **kw):
            pass

        def enviar_factura(self, xml_firmado, numero_factura, token_dian=None):
            vistos['xml'] = xml_firmado
            return {'is_valid': True, 'status_code': '00', 'description': 'ok', 'errors': [],
                    'cufe_dian': '', 'raw_xml': b'<r/>'}

    class FirmaFalsa:
        def __init__(self, *a):
            pass

        def firmar(self, xml):
            return xml

    monkeypatch.setattr(tf, 'calcular_cufe', espiar)
    monkeypatch.setattr(tf, 'DIANClient', DianFalsa)
    monkeypatch.setattr(tf, 'FirmadorDIAN', FirmaFalsa)
    monkeypatch.setattr(tf, 'descifrar', lambda _v: 'x')
    monkeypatch.setattr('services.pdf_builder.generar_pdf', lambda *a, **k: b'%PDF')
    t = crear_tenant()
    fid = crear_factura(t['id'], cliente={**EMPRESA, 'numero_documento': '890903938-8'})
    assert tf.procesar_factura(fid)['status'] == 'ACEPTADA'
    assert vistos['num_doc_receptor'] == '890903938'
    x = etree.fromstring(vistos['xml'])
    assert x.findtext(CLIENTE + 'cac:PartyTaxScheme/cbc:CompanyID', namespaces=NS) == '890903938'


def test_los_casos_ampliados_arman_xml_valido(crear_tenant):
    from routes.ui import _casos_ampliados
    from services.emision import validar
    t = crear_tenant(regimen_codigo='49')
    casos = _casos_ampliados()
    assert len(casos) == 7
    for nombre, datos in casos:
        validar(datos)                                   # pasa la validación de emisión
        x = _xml(t, datos['cliente'], datos['items'],
                 **{k: v for k, v in datos.items() if k == 'prueba_responsable_iva'})
        lineas = x.findall('cac:InvoiceLine', NS)
        assert len(lineas) == len(datos['items']), nombre
        # Suma de líneas = subtotal del documento (regla de la DIAN).
        suma = sum(Decimal(l.findtext('cbc:LineExtensionAmount', namespaces=NS)) for l in lineas)
        assert str(suma) == x.findtext('cac:LegalMonetaryTotal/cbc:LineExtensionAmount',
                                       namespaces=NS), nombre


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv('MASTER_API_KEY', 'llave-maestra-de-prueba-0123456789')
    from app import create_app
    a = create_app()
    a.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    return a


def test_el_boton_de_casos_ampliados_solo_funciona_en_habilitacion(app, crear_tenant,
                                                                   monkeypatch):
    import routes.ui as ui
    enviados = []
    monkeypatch.setattr(ui, 'enviar_ahora', lambda fid, aprobado_por=None: enviados.append(fid))
    c = app.test_client()
    with c.session_transaction() as s:
        s['ui_role'] = 'admin'
        s['csrf_token'] = 'token-valido'
    prod = crear_tenant(ambiente='produccion')
    c.post('/ui/pruebas/ampliado', data={'tenant_id': prod['id'], 'csrf_token': 'token-valido'})
    assert leer('SELECT id FROM facturas WHERE tenant_id=%s', prod['id']) == [] and enviados == []
    hab = crear_tenant()
    c.post('/ui/pruebas/ampliado', data={'tenant_id': hab['id'], 'csrf_token': 'token-valido'})
    assert len(leer('SELECT id FROM facturas WHERE tenant_id=%s', hab['id'])) == 7
    assert len(enviados) == 7
    html = c.get(f"/ui/pruebas?tenant_id={hab['id']}").data.decode()
    assert 'Casos ampliados' in html and 'IVA mixto' in html


# ── Notas: solo sobre una factura aceptada del mismo emisor (FE-20) ─────────

def _nota(numero, cufe):
    return {'referencia_pedido': f'NC-{numero}', 'tipo_documento': 'nota_credito',
            'metodo_pago': '10', 'cliente': {'tipo_persona': 'natural', 'tipo_documento': 'CC',
                                             'numero_documento': '1', 'nombre': 'C', 'email': 'c@c.co'},
            'items': [{'descripcion': 'P', 'cantidad': 1, 'precio_unitario': 100}],
            'documento_referencia': {'numero': numero, 'cufe': cufe, 'fecha': '2000-01-01'},
            'concepto_nota': {'codigo': '2', 'descripcion': 'Anulación'}}


def test_una_nota_solo_ajusta_una_factura_aceptada_de_este_emisor(crear_tenant, crear_factura,
                                                                  monkeypatch, tmp_path):
    from database import get_db_cursor
    from services import emision
    monkeypatch.setattr(emision, 'procesar_factura', None, raising=False)
    propio, ajeno = crear_tenant(), crear_tenant()
    fid = crear_factura(propio['id'])
    xml = tmp_path / 'f.xml'
    xml.write_bytes(_xml_bytes(propio))
    with get_db_cursor() as cur:
        cur.execute("""UPDATE facturas SET estado='RECHAZADA', numero_factura='PRUE7', cufe='abc',
                       xml_path=%s WHERE id=%s""", (str(xml), fid))
    with pytest.raises(emision.EmisionError, match='aceptada'):
        emision.emitir_documento(propio['id'], _nota('PRUE7', 'abc'), delay_seconds=3600)
    with get_db_cursor() as cur:
        cur.execute("UPDATE facturas SET estado='ACEPTADA' WHERE id=%s", (fid,))
    with pytest.raises(emision.EmisionError, match='no existe'):
        emision.emitir_documento(ajeno['id'], _nota('PRUE7', 'abc'), delay_seconds=3600)
    res = emision.emitir_documento(propio['id'], _nota('PRUE7', 'abc'), delay_seconds=3600)
    guardado = leer('SELECT datos_json FROM facturas WHERE id=%s', res['id'])[0]['datos_json']
    assert guardado['documento_referencia']['fecha'] == '2026-11-13'      # la del XML validado


def _xml_bytes(tenant):
    from lxml import etree as _et
    return _et.tostring(_xml(tenant))


def test_un_rechazado_no_genera_pdf(crear_tenant, crear_factura, monkeypatch):
    import tasks.facturacion as tf
    generados = []

    class DianFalsa:
        def __init__(self, **kw):
            pass

        def enviar_factura(self, xml_firmado, numero_factura, token_dian=None):
            return {'is_valid': False, 'status_code': '99', 'description': 'Rechazo',
                    'errors': ['Regla: FAD06, Rechazo: CUFE'], 'cufe_dian': '', 'raw_xml': b'<r/>'}

    class FirmaFalsa:
        def __init__(self, *a):
            pass

        def firmar(self, xml):
            return xml

    monkeypatch.setattr(tf, 'DIANClient', DianFalsa)
    monkeypatch.setattr(tf, 'FirmadorDIAN', FirmaFalsa)
    monkeypatch.setattr(tf, 'descifrar', lambda _v: 'x')
    monkeypatch.setattr('services.pdf_builder.generar_pdf',
                        lambda *a, **k: generados.append(1) or b'%PDF')
    fid = crear_factura(crear_tenant()['id'])
    assert tf.procesar_factura(fid)['status'] == 'RECHAZADA'
    assert generados == [] and leer('SELECT pdf_path FROM facturas WHERE id=%s', fid)[0]['pdf_path'] is None
