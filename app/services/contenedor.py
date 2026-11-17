"""
Contenedor electrónico (AttachedDocument) para entregar un documento al comprador.

Anexo Técnico 1.9 (Res. 000165 de 2023):
  - numeral 6.4: un solo XML con el documento validado (factura, nota crédito o
    nota débito) y la respuesta de la DIAN (ApplicationResponse);
  - numeral 9.1: es lo que se le envía al adquiriente, dentro de un .zip.

La DIAN no valida el contenedor (numeral 8.5), pero va firmado por el emisor
(la firma va en el único ext:ExtensionContent; ver FirmadorDIAN.firmar).
"""
import base64
from datetime import datetime, timedelta, timezone

from lxml import etree

NS_ATT = 'urn:oasis:names:specification:ubl:schema:xsd:AttachedDocument-2'
NS_CAC = 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2'
NS_CBC = 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'
NS_EXT = 'urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2'
NS_DS = 'http://www.w3.org/2000/09/xmldsig#'
NS_XADES = 'http://uri.etsi.org/01903/v1.3.2#'
NS_XADES141 = 'http://uri.etsi.org/01903/v1.4.1#'

NSMAP = {None: NS_ATT, 'cac': NS_CAC, 'cbc': NS_CBC, 'ext': NS_EXT,
         'ds': NS_DS, 'xades': NS_XADES, 'xades141': NS_XADES141}

COLOMBIA = timezone(timedelta(hours=-5))
VALIDADOR = 'Unidad Especial Dirección de Impuestos y Aduanas Nacionales'


class ContenedorError(Exception):
    """No se puede armar el contenedor con los archivos guardados."""


def _cbc(padre, tag, texto=None, **attrs):
    el = etree.SubElement(padre, f'{{{NS_CBC}}}{tag}', **attrs)
    if texto is not None:
        el.text = str(texto)
    return el


def _cac(padre, tag):
    return etree.SubElement(padre, f'{{{NS_CAC}}}{tag}')


def _hijo(nodo, ruta):
    """Primer elemento de la ruta 'cac:A/cbc:B' (o None)."""
    return nodo.find(ruta, namespaces={'cac': NS_CAC, 'cbc': NS_CBC})


def _texto(nodo, ruta):
    el = _hijo(nodo, ruta)
    return (el.text or '').strip() if el is not None else ''


def application_response(respuesta_dian: bytes) -> bytes:
    """Saca el ApplicationResponse de la respuesta de la DIAN.

    La DIAN lo devuelve en base64 dentro de <XmlBase64Bytes> (SendBillSync,
    GetStatus y GetStatusZip). Si lo guardado ya es el ApplicationResponse, se
    usa tal cual.
    """
    try:
        raiz = etree.fromstring(respuesta_dian)
    except (etree.XMLSyntaxError, ValueError):
        raise ContenedorError('La respuesta guardada de la DIAN no es un XML válido.')
    if etree.QName(raiz).localname == 'ApplicationResponse':
        return respuesta_dian
    for el in raiz.iter():
        if isinstance(el.tag, str) and etree.QName(el).localname == 'XmlBase64Bytes':
            texto = (el.text or '').strip()
            if texto:
                try:
                    return base64.b64decode(texto, validate=True)
                except (ValueError, base64.binascii.Error):
                    raise ContenedorError('La validación de la DIAN viene dañada (base64).')
    raise ContenedorError('La respuesta de la DIAN no trae el documento de validación '
                          '(ApplicationResponse).')


def _copiar_party_tax_scheme(origen, destino_party):
    """Copia al contenedor los datos tributarios de una parte (AE10-AE20 / AE22-AE32)."""
    if origen is None:
        raise ContenedorError('El documento no trae los datos tributarios de emisor o comprador.')
    pts = _cac(destino_party, 'PartyTaxScheme')
    _cbc(pts, 'RegistrationName', _texto(origen, 'cbc:RegistrationName'))
    cid = _hijo(origen, 'cbc:CompanyID')
    if cid is None:
        raise ContenedorError('El documento no trae el número de identificación de una parte.')
    _cbc(pts, 'CompanyID', (cid.text or '').strip(),
         **{k: v for k, v in cid.attrib.items()
            if k in ('schemeAgencyID', 'schemeID', 'schemeName')})
    tlc = _hijo(origen, 'cbc:TaxLevelCode')
    if tlc is not None:
        _cbc(pts, 'TaxLevelCode', (tlc.text or '').strip(),
             **({'listName': tlc.get('listName')} if tlc.get('listName') else {}))
    esquema = _cac(pts, 'TaxScheme')
    _cbc(esquema, 'ID', _texto(origen, 'cac:TaxScheme/cbc:ID') or 'ZZ')
    _cbc(esquema, 'Name', _texto(origen, 'cac:TaxScheme/cbc:Name') or 'No aplica')


def construir(xml_firmado: bytes, respuesta_dian: bytes, ahora: datetime = None) -> bytes:
    """Arma el AttachedDocument (sin firmar) con el documento y su validación.

    xml_firmado: el documento tal como la DIAN lo validó (se embebe sin tocar).
    respuesta_dian: la respuesta guardada (SOAP con XmlBase64Bytes o el
    ApplicationResponse directo).
    """
    try:
        doc = etree.fromstring(xml_firmado)
    except (etree.XMLSyntaxError, ValueError):
        raise ContenedorError('El XML firmado guardado no es válido.')

    numero = _texto(doc, 'cbc:ID')
    uuid_el = _hijo(doc, 'cbc:UUID')
    if not numero or uuid_el is None or not (uuid_el.text or '').strip():
        raise ContenedorError('El documento guardado no tiene número o CUFE/CUDE.')
    esquema_uuid = uuid_el.get('schemeName') or 'CUFE-SHA384'
    ambiente = _texto(doc, 'cbc:ProfileExecutionID') or '2'

    texto_doc = xml_firmado.decode('utf-8')
    ar_bytes = application_response(respuesta_dian)
    texto_ar = ar_bytes.decode('utf-8')
    if ']]>' in texto_doc or ']]>' in texto_ar:
        raise ContenedorError('El documento contiene «]]>» y no se puede embeber.')
    try:
        ar = etree.fromstring(ar_bytes)
    except (etree.XMLSyntaxError, ValueError):
        raise ContenedorError('La validación de la DIAN no es un XML válido.')

    fecha_validacion = _texto(ar, 'cbc:IssueDate')
    hora_validacion = _texto(ar, 'cbc:IssueTime')
    if not fecha_validacion:
        raise ContenedorError('La validación de la DIAN no trae su fecha.')
    codigo = _texto(ar, 'cac:DocumentResponse/cac:Response/cbc:ResponseCode') or '02'

    ahora = (ahora or datetime.now(COLOMBIA)).astimezone(COLOMBIA)
    fecha_doc = _texto(doc, 'cbc:IssueDate')
    fecha_cont = ahora.strftime('%Y-%m-%d')
    # AE05: la fecha del contenedor no puede ser anterior a la de lo que contiene.
    fecha_cont = max(fecha_cont, fecha_doc or fecha_cont, fecha_validacion)

    raiz = etree.Element(f'{{{NS_ATT}}}AttachedDocument', nsmap=NSMAP)
    ext = etree.SubElement(raiz, f'{{{NS_EXT}}}UBLExtensions')
    etree.SubElement(etree.SubElement(ext, f'{{{NS_EXT}}}UBLExtension'),
                     f'{{{NS_EXT}}}ExtensionContent')
    _cbc(raiz, 'UBLVersionID', 'UBL 2.1')
    _cbc(raiz, 'CustomizationID', 'Documentos adjuntos')
    _cbc(raiz, 'ProfileID', 'Factura Electrónica de Venta')
    _cbc(raiz, 'ProfileExecutionID', ambiente)
    _cbc(raiz, 'ID', numero)
    _cbc(raiz, 'IssueDate', fecha_cont)
    _cbc(raiz, 'IssueTime', ahora.strftime('%H:%M:%S') + '-05:00')
    _cbc(raiz, 'DocumentType', 'Contenedor de Factura Electrónica')
    _cbc(raiz, 'ParentDocumentID', numero)

    _copiar_party_tax_scheme(
        _hijo(doc, 'cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme'),
        _cac(raiz, 'SenderParty'))
    _copiar_party_tax_scheme(
        _hijo(doc, 'cac:AccountingCustomerParty/cac:Party/cac:PartyTaxScheme'),
        _cac(raiz, 'ReceiverParty'))

    ref = _cac(_cac(raiz, 'Attachment'), 'ExternalReference')
    _cbc(ref, 'MimeCode', 'text/xml')
    _cbc(ref, 'EncodingCode', 'UTF-8')
    _cbc(ref, 'Description').text = etree.CDATA(texto_doc)

    linea = _cac(raiz, 'ParentDocumentLineReference')
    _cbc(linea, 'LineID', '1')
    dref = _cac(linea, 'DocumentReference')
    _cbc(dref, 'ID', numero)
    _cbc(dref, 'UUID', uuid_el.text.strip(), schemeName=esquema_uuid)
    _cbc(dref, 'IssueDate', fecha_validacion)
    _cbc(dref, 'DocumentType', 'ApplicationResponse')
    ref_ar = _cac(_cac(dref, 'Attachment'), 'ExternalReference')
    _cbc(ref_ar, 'MimeCode', 'text/xml')
    _cbc(ref_ar, 'EncodingCode', 'UTF-8')
    _cbc(ref_ar, 'Description').text = etree.CDATA(texto_ar)
    resultado = _cac(dref, 'ResultOfVerification')
    _cbc(resultado, 'ValidatorID', VALIDADOR)
    _cbc(resultado, 'ValidationResultCode', codigo)
    _cbc(resultado, 'ValidationDate', fecha_validacion)
    _cbc(resultado, 'ValidationTime', hora_validacion or '00:00:00-05:00')

    return etree.tostring(raiz, xml_declaration=True, encoding='UTF-8')
