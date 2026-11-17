"""Entrega del documento al comprador (FE-01): contenedor, correo y portal.

Anexo Técnico 1.9 (Res. 000165 de 2023), numerales 6.4 y 9.1. El servidor de
correo y la firma de las facturas están simulados; la firma del contenedor se
prueba con un certificado autofirmado generado en la prueba.
"""
import base64
import email
import hashlib
import io
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from lxml import etree

from conftest import leer

NS = {'a': 'urn:oasis:names:specification:ubl:schema:xsd:AttachedDocument-2',
      'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
      'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
      'ext': 'urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2',
      'ds': 'http://www.w3.org/2000/09/xmldsig#'}

AR = ('<?xml version="1.0" encoding="UTF-8"?>'
      '<ApplicationResponse xmlns="urn:oasis:names:specification:ubl:schema:xsd:ApplicationResponse-2"'
      ' xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"'
      ' xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2">'
      '<cbc:IssueDate>2026-11-13</cbc:IssueDate><cbc:IssueTime>10:15:00-05:00</cbc:IssueTime>'
      '<cac:DocumentResponse><cac:Response><cbc:ResponseCode>02</cbc:ResponseCode>'
      '<cbc:Description>Documento validado por la DIAN</cbc:Description></cac:Response>'
      '</cac:DocumentResponse></ApplicationResponse>')


def _soap(ar: str = AR) -> bytes:
    b64 = base64.b64encode(ar.encode()).decode()
    return ('<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>'
            '<SendBillSyncResponse xmlns="http://wcf.dian.colombia"><SendBillSyncResult'
            ' xmlns:b="http://schemas.datacontract.org/2004/07/DianResponse">'
            '<b:IsValid>true</b:IsValid><b:StatusCode>00</b:StatusCode>'
            f'<b:XmlBase64Bytes>{b64}</b:XmlBase64Bytes></SendBillSyncResult>'
            '</SendBillSyncResponse></s:Body></s:Envelope>').encode()


@pytest.fixture
def fernet(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv('FERNET_KEY', Fernet.generate_key().decode())
    from utils.encryption import cifrar
    return cifrar


@pytest.fixture
def emisor(crear_tenant, fernet):
    """Emisor con el certificado (simulado) y el correo de entrega listo."""
    def _crear(ambiente='produccion', correo=True, **extra):
        t = crear_tenant(ambiente=ambiente, cert_password_enc=fernet('clave-p12'), **extra)
        if correo:
            _poner_correo(t['id'], fernet)
        return t
    return _crear


def _poner_correo(tenant_id, cifrar, **cambios):
    import psycopg2.extras
    from database import get_db_cursor
    cfg = {'host': 'smtp.negocio.com', 'puerto': '587', 'seguridad': 'starttls',
           'usuario': 'facturas@negocio.com', 'password_cifrada': cifrar('clave-smtp'),
           'remitente': 'facturas@negocio.com', 'nombre': 'Mi Negocio', 'activo': True}
    cfg.update(cambios)
    with get_db_cursor() as cur:
        cur.execute('UPDATE tenants SET correo_entrega = %s WHERE id = %s',
                    (psycopg2.extras.Json(cfg), tenant_id))
    return cfg


@pytest.fixture
def aceptada(crear_factura):
    """Documento ACEPTADO con su XML real (XMLBuilder), respuesta DIAN y PDF."""
    from database import get_db_cursor
    from services.storage import StorageManager
    from services.xml_builder import XMLBuilder

    def _crear(tenant, email_cliente='cliente@ejemplo.com', pdf=b'%PDF-1.4 prueba',
               respuesta=None, estado='ACEPTADA'):
        cliente = {'tipo_persona': 'natural', 'tipo_documento': 'CC',
                   'numero_documento': '1098765432', 'nombre': 'Cliente Prueba',
                   'email': email_cliente, 'telefono': '3001234567',
                   'direccion': 'Calle 1 2-3', 'municipio_codigo': '11001'}
        fid = crear_factura(tenant['id'], cliente=cliente)
        datos = leer('SELECT datos_json FROM facturas WHERE id=%s', fid)[0]['datos_json']
        numero, cufe = 'PRUE1', 'a' * 96
        xml = XMLBuilder(tenant, datos, numero, cufe, '2026-11-13', '10:00:00-05:00').build()
        sm = StorageManager(tenant['id'])
        rutas = (sm.guardar_xml(xml, numero),
                 sm.guardar_response(respuesta if respuesta is not None else _soap(), numero),
                 sm.guardar_pdf(pdf, numero) if pdf else None)
        with get_db_cursor() as cur:
            cur.execute("""UPDATE facturas SET estado=%s, numero_factura=%s, cufe=%s,
                           xml_path=%s, response_path=%s, pdf_path=%s WHERE id=%s""",
                        (estado, numero, cufe, *rutas, fid))
        return fid, xml
    return _crear


class Correo:
    """Servidor SMTP simulado: anota lo que se envió."""

    def __init__(self):
        self.enviados, self.logins, self.conexiones = [], [], []
        self.falla = None


@pytest.fixture
def correo(monkeypatch):
    import services.entrega as en
    control = Correo()

    class SMTPFalso:
        def __init__(self, host, puerto, timeout=None, context=None):
            control.conexiones.append((host, puerto, type(self).__name__))
            if control.falla:
                raise control.falla

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def login(self, usuario, clave):
            control.logins.append((usuario, clave))

        def send_message(self, msg):
            control.enviados.append(msg)

        def quit(self):
            pass

    class SMTPSSLFalso(SMTPFalso):
        pass

    class FirmaFalsa:
        def __init__(self, cert_path, clave):
            control.clave_cert = clave

        def firmar(self, xml, minimo_extensiones=2):
            control.minimo_extensiones = minimo_extensiones
            return xml

    monkeypatch.setattr(en.smtplib, 'SMTP', SMTPFalso)
    monkeypatch.setattr(en.smtplib, 'SMTP_SSL', SMTPSSLFalso)
    monkeypatch.setattr(en, 'FirmadorDIAN', FirmaFalsa)
    monkeypatch.setattr(en, '_verificar_host', lambda host: None)
    return control


def _eventos(fid):
    return leer('SELECT evento, detalle FROM factura_eventos WHERE factura_id=%s ORDER BY id', fid)


def _adjunto(msg):
    partes = [p for p in msg.iter_attachments()]
    assert len(partes) == 1, 'la DIAN pide un único .zip'
    return partes[0].get_filename(), zipfile.ZipFile(io.BytesIO(partes[0].get_content()))


# ── Contenedor electrónico (numeral 6.4) ──────────────────────────────────────

def test_el_contenedor_lleva_el_documento_y_la_validacion(emisor, aceptada):
    from services.contenedor import construir
    t = emisor()
    _fid, xml = aceptada(t)
    ahora = datetime(2026, 11, 13, 11, 0, tzinfo=timezone(timedelta(hours=-5)))
    crudo = construir(xml, _soap(), ahora=ahora)
    assert b'<![CDATA[' in crudo
    c = etree.fromstring(crudo)

    def txt(ruta):
        return c.findtext(ruta, namespaces=NS)

    assert etree.QName(c).localname == 'AttachedDocument'
    assert txt('cbc:CustomizationID') == 'Documentos adjuntos'
    assert txt('cbc:ProfileID') == 'Factura Electrónica de Venta'
    assert txt('cbc:ProfileExecutionID') == '1'                 # producción
    assert txt('cbc:DocumentType') == 'Contenedor de Factura Electrónica'
    assert txt('cbc:ParentDocumentID') == 'PRUE1'
    assert txt('cbc:IssueDate') == '2026-11-13'
    # El documento va completo y sin tocar.
    assert txt('cac:Attachment/cac:ExternalReference/cbc:Description') == xml.decode()
    assert txt('cac:Attachment/cac:ExternalReference/cbc:MimeCode') == 'text/xml'
    emisor_id = c.find('cac:SenderParty/cac:PartyTaxScheme/cbc:CompanyID', NS)
    assert emisor_id.text == t['nit'] and emisor_id.get('schemeAgencyID') == '195'
    assert txt('cac:ReceiverParty/cac:PartyTaxScheme/cbc:CompanyID') == '1098765432'
    ref = 'cac:ParentDocumentLineReference/cac:DocumentReference/'
    assert c.find(ref + 'cbc:UUID', NS).get('schemeName') == 'CUFE-SHA384'
    assert txt(ref + 'cbc:UUID') == 'a' * 96
    assert txt(ref + 'cbc:DocumentType') == 'ApplicationResponse'
    assert txt(ref + 'cac:Attachment/cac:ExternalReference/cbc:Description') == AR
    assert txt(ref + 'cac:ResultOfVerification/cbc:ValidatorID') == \
        'Unidad Especial Dirección de Impuestos y Aduanas Nacionales'
    assert txt(ref + 'cac:ResultOfVerification/cbc:ValidationResultCode') == '02'
    assert txt(ref + 'cac:ResultOfVerification/cbc:ValidationDate') == '2026-11-13'
    assert txt(ref + 'cac:ResultOfVerification/cbc:ValidationTime') == '10:15:00-05:00'
    assert len(c.findall('ext:UBLExtensions/ext:UBLExtension/ext:ExtensionContent', NS)) == 1


def test_sin_la_validacion_de_la_dian_no_hay_contenedor(emisor, aceptada):
    from services.contenedor import ContenedorError, construir
    t = emisor()
    _fid, xml = aceptada(t)
    with pytest.raises(ContenedorError):
        construir(xml, b'<respuesta>ok</respuesta>')


def test_la_firma_del_contenedor_es_valida_y_conserva_el_cdata(emisor, aceptada, tmp_path):
    """Firma real con un certificado autofirmado: el digest debe coincidir."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID
    from services.contenedor import construir
    from services.signer import FirmadorDIAN

    clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Prueba')])
    ahora = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(nombre).issuer_name(nombre)
            .public_key(clave.public_key()).serial_number(1)
            .not_valid_before(ahora - timedelta(days=1)).not_valid_after(ahora + timedelta(days=1))
            .sign(clave, hashes.SHA256()))
    p12 = tmp_path / 'cert.p12'
    p12.write_bytes(pkcs12.serialize_key_and_certificates(
        b'prueba', clave, cert, None, serialization.BestAvailableEncryption(b'clave')))

    t = emisor()
    _fid, xml = aceptada(t)
    contenedor = construir(xml, _soap())
    firmador = FirmadorDIAN(str(p12), 'clave')
    with pytest.raises(ValueError):
        firmador.firmar(contenedor)                       # factura: exige 2 extensiones
    firmado = firmador.firmar(contenedor, minimo_extensiones=1)
    assert firmado.count(b'<![CDATA[') == 2

    raiz = etree.fromstring(firmado)
    firma = raiz.find('.//ds:Signature', NS)
    assert firma is not None
    digest = firma.find('ds:SignedInfo/ds:Reference/ds:DigestValue', NS).text
    firma.getparent().remove(firma)
    calculado = base64.b64encode(hashlib.sha256(
        etree.tostring(raiz, method='c14n', exclusive=True, with_comments=False)).digest()).decode()
    assert calculado == digest


# ── Correo al comprador (numeral 9.1) ────────────────────────────────────────

def test_en_produccion_llega_al_comprador_con_el_asunto_de_la_dian(emisor, aceptada, correo):
    from services.entrega import entregar_factura
    t = emisor()
    fid, _xml = aceptada(t)
    r = entregar_factura(fid)
    assert r['estado'] == 'enviada'
    assert len(correo.enviados) == 1
    msg = correo.enviados[0]
    assert msg['To'] == 'cliente@ejemplo.com'
    assert msg['Subject'] == f"{t['nit']};Emisor Prueba SAS;PRUE1;01;Emisor Prueba"
    assert 'facturas@negocio.com' in msg['From'] and 'Mi Negocio' in msg['From']
    assert msg['Reply-To'] == 'emisor@ejemplo.com'
    assert correo.logins == [('facturas@negocio.com', 'clave-smtp')]
    assert correo.clave_cert == 'clave-p12' and correo.minimo_extensiones == 1
    nombre, z = _adjunto(msg)
    assert nombre == 'PRUE1.zip'
    assert sorted(z.namelist()) == ['PRUE1.pdf', 'PRUE1.xml']
    assert b'AttachedDocument' in z.read('PRUE1.xml')
    assert _eventos(fid)[-1]['evento'] == 'ENTREGADA'
    assert leer('SELECT estado FROM facturas WHERE id=%s', fid)[0]['estado'] == 'ACEPTADA'


def test_el_punto_y_coma_no_rompe_el_asunto(emisor, aceptada, correo):
    from services.entrega import entregar_factura
    t = emisor(razon_social='Pérez; Hijos SAS', nombre='Tienda;Pérez')
    fid, _xml = aceptada(t)
    entregar_factura(fid)
    assert correo.enviados[0]['Subject'].split(';') == \
        [t['nit'], 'Pérez Hijos SAS', 'PRUE1', '01', 'Tienda Pérez']


def test_en_pruebas_nunca_se_le_escribe_al_comprador(emisor, aceptada, correo):
    from services.entrega import entregar_factura
    t = emisor(ambiente='habilitacion')
    fid, _xml = aceptada(t)
    assert entregar_factura(fid)['estado'] == 'pruebas'           # automático: nada
    assert correo.enviados == [] and _eventos(fid) == []
    r = entregar_factura(fid, manual=True)                        # botón: al negocio
    assert r['estado'] == 'enviada'
    assert correo.enviados[0]['To'] == 'facturas@negocio.com'
    assert 'PRUEBA' in correo.enviados[0].get_body(('plain',)).get_content()


def test_sin_correo_del_comprador_queda_anotado(emisor, aceptada, correo):
    from services.entrega import entregar_factura
    t = emisor()
    fid, _xml = aceptada(t, email_cliente='')
    assert entregar_factura(fid)['estado'] == 'sin_correo'
    assert correo.enviados == []
    assert 'no dio un correo' in _eventos(fid)[-1]['detalle']


def test_sin_configurar_el_correo_queda_anotado(emisor, aceptada, correo):
    from services.entrega import entregar_factura
    t = emisor(correo=False)
    fid, _xml = aceptada(t)
    r = entregar_factura(fid)
    assert r['estado'] == 'sin_config' and correo.enviados == []
    assert _eventos(fid)[-1]['evento'] == 'ENTREGA'


def test_con_el_envio_automatico_apagado_solo_sale_a_mano(emisor, aceptada, correo, fernet):
    from services.entrega import entregar_factura
    t = emisor(correo=False)
    _poner_correo(t['id'], fernet, activo=False)
    fid, _xml = aceptada(t)
    assert entregar_factura(fid)['estado'] == 'apagada'
    assert correo.enviados == []
    assert entregar_factura(fid, manual=True)['estado'] == 'enviada'


def test_si_el_correo_falla_el_documento_sigue_aceptado(emisor, aceptada, correo):
    import smtplib
    from services.entrega import entregar_factura
    t = emisor()
    fid, _xml = aceptada(t)
    correo.falla = smtplib.SMTPAuthenticationError(535, b'Username and Password not accepted')
    r = entregar_factura(fid)
    assert r['estado'] == 'fallo' and r['reintentable'] is False
    ev = _eventos(fid)[-1]
    assert ev['evento'] == 'ENTREGA_FALLIDA' and 'contraseña' in ev['detalle']
    assert 'clave-smtp' not in ev['detalle']
    assert leer('SELECT estado FROM facturas WHERE id=%s', fid)[0]['estado'] == 'ACEPTADA'
    correo.falla = TimeoutError()
    assert entregar_factura(fid)['reintentable'] is True


def test_solo_se_entregan_documentos_aceptados(emisor, aceptada, correo):
    from services.entrega import entregar_factura
    t = emisor()
    fid, _xml = aceptada(t, estado='RECHAZADA')
    assert entregar_factura(fid, manual=True)['estado'] == 'no_aceptada'
    assert correo.enviados == []


def test_si_el_pdf_no_cabe_en_2_mb_va_solo_el_xml(emisor, aceptada, correo):
    import os
    from services.entrega import entregar_factura
    t = emisor()
    fid, _xml = aceptada(t, pdf=os.urandom(3 * 1024 * 1024))
    assert entregar_factura(fid)['estado'] == 'enviada'
    _nombre, z = _adjunto(correo.enviados[0])
    assert z.namelist() == ['PRUE1.xml']
    assert 'no cupo' in _eventos(fid)[-1]['detalle']


def test_no_se_conecta_a_servidores_internos(monkeypatch, fernet):
    from services.entrega import EntregaError, _verificar_host
    for host in ('localhost', '127.0.0.1', '10.200.0.3', '192.168.1.10'):
        with pytest.raises(EntregaError):
            _verificar_host(host)


# ── Al quedar aceptada ───────────────────────────────────────────────────────

def test_al_aceptarse_en_produccion_se_programa_la_entrega(crear_tenant, crear_factura,
                                                           entregas_programadas, monkeypatch):
    import tasks.facturacion as tf
    ok = {'is_valid': True, 'status_code': '00', 'description': 'Procesado Correctamente.',
          'errors': [], 'cufe_dian': '', 'raw_xml': _soap()}

    class DianFalsa:
        def __init__(self, **kw):
            pass

        def enviar_factura(self, xml_firmado, numero_factura, token_dian=None):
            return ok

    class FirmaFalsa:
        def __init__(self, *a):
            pass

        def firmar(self, xml):
            return xml

    monkeypatch.setattr(tf, 'DIANClient', DianFalsa)
    monkeypatch.setattr(tf, 'FirmadorDIAN', FirmaFalsa)
    monkeypatch.setattr(tf, 'descifrar', lambda _v: 'x')
    monkeypatch.setattr('services.pdf_builder.generar_pdf', lambda *a, **k: b'%PDF')

    prod = crear_tenant(ambiente='produccion')
    fid = crear_factura(prod['id'])
    assert tf.procesar_factura(fid)['status'] == 'ACEPTADA'
    assert entregas_programadas == [fid]

    hab = crear_tenant(ambiente='habilitacion')
    fid2 = crear_factura(hab['id'])
    assert tf.procesar_factura(fid2)['status'] == 'ACEPTADA'
    assert entregas_programadas == [fid]                   # en pruebas no se programa


# ── Portal ───────────────────────────────────────────────────────────────────

@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv('MASTER_API_KEY', 'llave-maestra-de-prueba-0123456789')
    from app import create_app
    a = create_app()
    a.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
    return a


def _cliente(app, rol='admin', tenant_id=None):
    c = app.test_client()
    with c.session_transaction() as s:
        s['ui_role'] = rol
        s['csrf_token'] = 'token-valido'
        if tenant_id:
            s['ui_tenant_id'] = tenant_id
    return c


FORM = {'host': 'smtp.gmail.com', 'puerto': '587', 'seguridad': 'starttls',
        'usuario': 'ventas@negocio.com', 'password': 'clave-de-aplicacion',
        'remitente': 'ventas@negocio.com', 'nombre': 'Negocio', 'activo': '1',
        'csrf_token': 'token-valido'}


def _cfg(tid):
    return leer('SELECT correo_entrega FROM tenants WHERE id=%s', tid)[0]['correo_entrega']


def test_el_portal_guarda_la_contrasena_cifrada(app, crear_tenant, fernet):
    from utils.encryption import descifrar
    t = crear_tenant()
    c = _cliente(app)
    r = c.post(f"/ui/correo-entrega?tenant_id={t['id']}", data=FORM)
    assert r.status_code == 302
    cfg = _cfg(t['id'])
    assert cfg['host'] == 'smtp.gmail.com' and cfg['activo'] is True
    assert 'clave-de-aplicacion' not in str(cfg)
    assert descifrar(cfg['password_cifrada']) == 'clave-de-aplicacion'
    # Guardar sin escribir la contraseña la conserva.
    c.post(f"/ui/correo-entrega?tenant_id={t['id']}", data={**FORM, 'password': ''})
    assert descifrar(_cfg(t['id'])['password_cifrada']) == 'clave-de-aplicacion'
    html = c.get(f"/ui/correo-entrega?tenant_id={t['id']}").data.decode()
    assert 'clave-de-aplicacion' not in html and 'Guardada' in html


def test_sin_token_no_se_guarda(app, crear_tenant, fernet):
    t = crear_tenant()
    c = _cliente(app)
    c.post(f"/ui/correo-entrega?tenant_id={t['id']}", data={**FORM, 'csrf_token': ''})
    assert _cfg(t['id']) in ({}, None)


def test_un_cliente_solo_configura_su_propio_correo(app, crear_tenant, fernet):
    propio, ajeno = crear_tenant(), crear_tenant()
    c = _cliente(app, rol='tenant', tenant_id=propio['id'])
    c.post(f"/ui/correo-entrega?tenant_id={ajeno['id']}", data=FORM)
    assert _cfg(ajeno['id']) in ({}, None)
    assert _cfg(propio['id'])['host'] == 'smtp.gmail.com'


def test_no_se_enciende_sin_datos_completos(app, crear_tenant, fernet):
    t = crear_tenant()
    c = _cliente(app)
    r = c.post(f"/ui/correo-entrega?tenant_id={t['id']}",
               data={**FORM, 'host': '', 'remitente': 'no-es-correo'})
    assert r.status_code == 200
    assert _cfg(t['id']) in ({}, None)


def test_el_boton_del_detalle_envia_y_otro_cliente_no_puede(app, emisor, aceptada, correo):
    t = emisor()
    fid, _xml = aceptada(t)
    ajeno = emisor()
    c = _cliente(app, rol='tenant', tenant_id=ajeno['id'])
    c.post(f'/ui/facturas/{fid}/entregar', data={'csrf_token': 'token-valido'})
    assert correo.enviados == []
    c = _cliente(app, rol='tenant', tenant_id=t['id'])
    c.post(f'/ui/facturas/{fid}/entregar', data={'csrf_token': 'token-valido'})
    assert len(correo.enviados) == 1
    html = c.get(f'/ui/facturas/{fid}').data.decode()
    assert 'Entrega al comprador' in html and 'Reenviar al comprador' in html


def test_el_paquete_se_descarga_para_entregarlo_a_mano(app, emisor, aceptada, correo):
    t = emisor()
    fid, _xml = aceptada(t)
    c = _cliente(app, rol='tenant', tenant_id=t['id'])
    r = c.get(f'/ui/facturas/{fid}/archivo/paquete')
    assert r.status_code == 200 and r.mimetype == 'application/zip'
    z = zipfile.ZipFile(io.BytesIO(r.data))
    assert sorted(z.namelist()) == ['PRUE1.pdf', 'PRUE1.xml']
    otro = _cliente(app, rol='tenant', tenant_id=emisor()['id'])
    assert otro.get(f'/ui/facturas/{fid}/archivo/paquete').status_code == 403
