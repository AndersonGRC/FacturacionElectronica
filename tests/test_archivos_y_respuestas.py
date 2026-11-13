"""Conservación de archivos (art. 632 ET) y lectura de la respuesta de la DIAN."""
from services.dian_client import DIANClient, es_documento_ya_procesado
from services.storage import StorageManager


def test_un_xml_distinto_no_pisa_el_guardado(tmp_path):
    s = StorageManager('tenant-x', base_path=str(tmp_path))
    primero = s.guardar_xml(b'<Invoice>primero</Invoice>', 'PRUE1')
    otra_vez = s.guardar_xml(b'<Invoice>primero</Invoice>', 'PRUE1')
    segundo = s.guardar_xml(b'<Invoice>otro</Invoice>', 'PRUE1')

    assert otra_vez == primero                           # mismo contenido: mismo archivo
    assert segundo != primero
    from pathlib import Path
    assert Path(primero).read_bytes() == b'<Invoice>primero</Invoice>'
    assert Path(segundo).read_bytes() == b'<Invoice>otro</Invoice>'


def test_las_respuestas_de_cada_intento_se_conservan(tmp_path):
    s = StorageManager('tenant-x', base_path=str(tmp_path))
    a = s.guardar_response(b'<r>intento 1</r>', 'PRUE1')
    b = s.guardar_response(b'<r>intento 2</r>', 'PRUE1')
    assert a != b


RESPUESTA_REGLA_90 = b"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
 <s:Body>
  <SendBillSyncResponse xmlns="http://wcf.dian.colombia">
   <SendBillSyncResult xmlns:b="http://schemas.datacontract.org/2004/07/DianResponse"
                       xmlns:c="http://schemas.microsoft.com/2003/10/Serialization/Arrays">
    <b:ErrorMessage>
     <c:string>Regla: 90, Rechazo: Documento procesado anteriormente.</c:string>
    </b:ErrorMessage>
    <b:IsValid>false</b:IsValid>
    <b:StatusCode>99</b:StatusCode>
    <b:StatusDescription>Validaci\xc3\xb3n contiene errores en campos mandatorios.</b:StatusDescription>
    <b:XmlDocumentKey>cufe-de-ejemplo-de-mas-de-veinte-caracteres</b:XmlDocumentKey>
   </SendBillSyncResult>
  </SendBillSyncResponse>
 </s:Body>
</s:Envelope>"""


def test_se_leen_las_reglas_que_trae_la_lista_de_errores():
    """Antes el texto de cada <string> se perdía y el motivo del rechazo no se veía."""
    r = DIANClient(ambiente='habilitacion', nit_emisor='1', clave_tecnica='x') \
        ._parsear_response(RESPUESTA_REGLA_90)
    assert r['is_valid'] is False
    assert 'Regla: 90, Rechazo: Documento procesado anteriormente.' in r['errors']
    assert es_documento_ya_procesado(r)


def test_otros_rechazos_no_se_confunden_con_la_regla_90():
    assert not es_documento_ya_procesado(
        {'errors': ['Regla: FAK48, Rechazo: tipo de documento'], 'description': ''})
    assert not es_documento_ya_procesado(
        {'errors': ['Regla: 900, Rechazo: otra cosa'], 'description': ''})
    assert es_documento_ya_procesado(
        {'errors': [], 'description': 'Documento procesado anteriormente'})
