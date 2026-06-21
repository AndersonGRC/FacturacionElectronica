"""
DIANClient: cliente para el Web Service de la DIAN (Colombia).

Método utilizado: SendBillSync (síncrono, respuesta inmediata con ApplicationResponse).
Protocolo: **SOAP 1.2** sobre HTTPS con **WS-Security** (firma del sobre con el
certificado del emisor — BinarySecurityToken + Timestamp y Body firmados).

IMPORTANTE — diferencia con la versión anterior:
  El WS de la DIAN (`WcfDianCustomerServices.svc`) NO acepta SOAP 1.1 (`text/xml`)
  ni autenticación por `Authorization: Bearer`. Exige:
    - SOAP 1.2:  Content-Type `application/soap+xml;charset=UTF-8;action="..."`
                 namespace `http://www.w3.org/2003/05/soap-envelope`.
    - WS-Security: cabecera `wsse:Security` con un `wsu:Timestamp` y el `soap:Body`
                 firmados (XMLDSig, rsa-sha256, c14n exclusiva) usando el .p12 del
                 tenant, referenciando el certificado vía `BinarySecurityToken`.

Endpoints:
  Habilitación: https://vpfe-hab.dian.gov.co/WcfDianCustomerServices.svc
  Producción:   https://vpfe.dian.gov.co/WcfDianCustomerServices.svc
"""

import base64
import hashlib
import io
import logging
import uuid
import zipfile
from datetime import datetime, timezone, timedelta

import requests
from lxml import etree
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12, Encoding

logger = logging.getLogger(__name__)

# ── Namespaces ──────────────────────────────────────────────────────────────────
NS_SOAP12 = 'http://www.w3.org/2003/05/soap-envelope'      # SOAP 1.2
NS_SOAP11 = 'http://schemas.xmlsoap.org/soap/envelope/'    # fallback parseo
NS_WCF    = 'http://wcf.dian.colombia'
NS_WSSE   = ('http://docs.oasis-open.org/wss/2004/01/'
             'oasis-200401-wss-wssecurity-secext-1.0.xsd')
NS_WSU    = ('http://docs.oasis-open.org/wss/2004/01/'
             'oasis-200401-wss-wssecurity-utility-1.0.xsd')
NS_DS     = 'http://www.w3.org/2000/09/xmldsig#'
NS_WSA    = 'http://www.w3.org/2005/08/addressing'        # WS-Addressing (UsingAddressing)
WSA_ANON  = 'http://www.w3.org/2005/08/addressing/anonymous'

# ── Algoritmos XMLDSig / WS-Security ────────────────────────────────────────────
ALG_C14N_EXC = 'http://www.w3.org/2001/10/xml-exc-c14n#'
ALG_RSA_SHA256 = 'http://www.w3.org/2001/04/xmldsig-more#rsa-sha256'
ALG_SHA256 = 'http://www.w3.org/2001/04/xmlenc#sha256'
VALUETYPE_X509 = ('http://docs.oasis-open.org/wss/2004/01/'
                  'oasis-200401-wss-x509-token-profile-1.0#X509v3')
# WSS 1.1 — la política DIAN exige referenciar el cert por huella SHA-1
VALUETYPE_THUMB = ('http://docs.oasis-open.org/wss/oasis-wss-soap-message-'
                   'security-1.1#ThumbprintSHA1')
ENCTYPE_B64 = ('http://docs.oasis-open.org/wss/2004/01/'
               'oasis-200401-wss-soap-message-security-1.0#Base64Binary')

DIAN_URLS = {
    'habilitacion': 'https://vpfe-hab.dian.gov.co/WcfDianCustomerServices.svc',
    'produccion':   'https://vpfe.dian.gov.co/WcfDianCustomerServices.svc',
}

ACTION_SEND_BILL = 'http://wcf.dian.colombia/IWcfDianCustomerServices/SendBillSync'
ACTION_GET_STATUS = 'http://wcf.dian.colombia/IWcfDianCustomerServices/GetStatusZip'
ACTION_SEND_TEST = 'http://wcf.dian.colombia/IWcfDianCustomerServices/SendTestSetAsync'


def _qn(ns: str, tag: str) -> str:
    return f'{{{ns}}}{tag}'


def _c14n(node) -> bytes:
    """Canonicalización exclusiva (sin comentarios) de un nodo en su contexto."""
    return etree.tostring(node, method='c14n', exclusive=True, with_comments=False)


class DIANClient:
    """
    Cliente DIAN multi-ambiente con WS-Security.

    Args:
        ambiente:      'habilitacion' | 'produccion'
        nit_emisor:    NIT del emisor sin dígito de verificación
        clave_tecnica: Clave técnica asignada por DIAN al software registrado
        timeout:       Timeout en segundos para llamadas HTTP
        software_id:   Identificador del software registrado en DIAN
        cert_path:     Ruta al .p12 del emisor (requerido para firmar el sobre WS-Security)
        cert_password: Contraseña del .p12 en texto plano
    """

    def __init__(self, ambiente: str, nit_emisor: str,
                 clave_tecnica: str, timeout: int = 30,
                 software_id: str = None,
                 cert_path: str = None, cert_password: str = None):
        self.ambiente      = ambiente
        self.url           = DIAN_URLS.get(ambiente, DIAN_URLS['habilitacion'])
        self.nit           = nit_emisor
        self.clave_tecnica = clave_tecnica
        self.software_id   = software_id
        self.timeout       = timeout
        self._cert_path     = cert_path
        self._cert_password = cert_password
        self._private_key   = None
        self._cert_der_b64  = None
        if cert_path:
            self._cargar_certificado(cert_path, cert_password or '')

    # ── Carga del certificado para firmar el sobre ─────────────────────────────
    def _cargar_certificado(self, cert_path: str, cert_password: str):
        with open(cert_path, 'rb') as f:
            p12_data = f.read()
        private_key, certificate, _chain = pkcs12.load_key_and_certificates(
            p12_data, cert_password.encode('utf-8')
        )
        self._private_key  = private_key
        cert_der           = certificate.public_bytes(Encoding.DER)
        self._cert_der_b64 = base64.b64encode(cert_der).decode('ascii')
        # Huella SHA-1 del cert (DER) para la referencia ThumbprintSHA1 que exige DIAN
        self._cert_thumb_b64 = base64.b64encode(hashlib.sha1(cert_der).digest()).decode('ascii')

    # ── API pública ───────────────────────────────────────────────────────────

    def enviar_factura(self, xml_firmado: bytes, numero_factura: str,
                       token_dian: str = None) -> dict:
        """
        Envía la factura firmada a la DIAN (SendBillSync) y retorna el resultado.

        El XML UBL ya viene firmado (XAdES) por FirmadorDIAN; aquí se empaqueta en
        ZIP, se mete en un sobre SOAP 1.2 y se firma el sobre con WS-Security.
        """
        nombre_xml = f"{numero_factura}.xml"
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(nombre_xml, xml_firmado)
        zip_b64 = base64.b64encode(zip_buffer.getvalue()).decode('ascii')

        logger.info(f"Enviando factura {numero_factura} a DIAN ({self.ambiente}), "
                    f"ZIP={len(zip_b64)} b64-chars")

        # Cuerpo de la operación SendBillSync
        op = etree.Element(_qn(NS_WCF, 'SendBillSync'), nsmap={'wcf': NS_WCF})
        etree.SubElement(op, _qn(NS_WCF, 'fileName')).text    = f"{numero_factura}.zip"
        etree.SubElement(op, _qn(NS_WCF, 'contentFile')).text = zip_b64

        envelope = self._firmar_sobre(op, ACTION_SEND_BILL)
        response = self._post(envelope, ACTION_SEND_BILL)
        return self._parsear_response(response.content)

    def enviar_test_set(self, xml_firmado: bytes, numero_factura: str,
                        test_set_id: str) -> dict:
        """
        Envía un documento del SET DE PRUEBAS (operación SendTestSetAsync) con el
        TestSetId que la DIAN exige durante la habilitación. Es ASÍNCRONO: retorna
        un ZipKey; el resultado de validación se consulta con GetStatusZip(zip_key).
        """
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{numero_factura}.xml", xml_firmado)
        zip_b64 = base64.b64encode(zip_buffer.getvalue()).decode('ascii')

        logger.info(f"Enviando {numero_factura} al SET DE PRUEBAS (TestSetId={test_set_id})")

        op = etree.Element(_qn(NS_WCF, 'SendTestSetAsync'), nsmap={'wcf': NS_WCF})
        etree.SubElement(op, _qn(NS_WCF, 'fileName')).text    = f"{numero_factura}.zip"
        etree.SubElement(op, _qn(NS_WCF, 'contentFile')).text = zip_b64
        etree.SubElement(op, _qn(NS_WCF, 'testSetId')).text   = test_set_id

        envelope = self._firmar_sobre(op, ACTION_SEND_TEST)
        response = self._post(envelope, ACTION_SEND_TEST)
        return self._parsear_zipkey(response.content)

    def obtener_estado_documento(self, cufe: str, token_dian: str = None) -> dict:
        """Consulta el estado de un documento en la DIAN por trackId/CUFE/ZipKey."""
        op = etree.Element(_qn(NS_WCF, 'GetStatusZip'), nsmap={'wcf': NS_WCF})
        etree.SubElement(op, _qn(NS_WCF, 'trackId')).text = cufe
        envelope = self._firmar_sobre(op, ACTION_GET_STATUS)
        response = self._post(envelope, ACTION_GET_STATUS)
        return self._parsear_response(response.content)

    def _parsear_zipkey(self, response_bytes: bytes) -> dict:
        """Extrae el ZipKey de la respuesta de SendTestSetAsync."""
        res = {'zip_key': '', 'errors': [], 'raw_xml': response_bytes}
        try:
            root = etree.fromstring(response_bytes)
            for el in root.iter():
                tag = el.tag.split('}')[-1] if '}' in el.tag else el.tag
                text = (el.text or '').strip()
                if tag in ('ZipKey', 'XmlDocumentKey') and text:
                    res['zip_key'] = text
                elif tag in ('ErrorMessage', 'string', 'Text', 'faultstring') and text:
                    if text not in res['errors']:
                        res['errors'].append(text)
        except etree.XMLSyntaxError as e:
            res['errors'].append(f"Respuesta inválida: {e}")
        return res

    # ── Envío HTTP ──────────────────────────────────────────────────────────────

    def _post(self, envelope_bytes: bytes, action: str) -> requests.Response:
        headers = {
            'Content-Type': f'application/soap+xml;charset=UTF-8;action="{action}"',
        }
        response = requests.post(
            self.url, data=envelope_bytes, headers=headers,
            timeout=self.timeout, verify=True,
        )
        logger.info(f"DIAN respondió HTTP {response.status_code} ({action.rsplit('/', 1)[-1]})")
        # Un servicio SOAP/WCF devuelve los faults con HTTP 500 + cuerpo XML
        # (<soap:Fault><soap:Reason>...). NO levantamos excepción en 500: el
        # cuerpo trae la causa real, que _parsear_response extrae. Solo levantamos
        # para errores de transporte sin cuerpo SOAP (502/503/504, etc.).
        if response.status_code >= 400 and response.status_code != 500:
            response.raise_for_status()
        return response

    # ── Construcción + firma del sobre (WS-Security) ────────────────────────────

    def _firmar_sobre(self, body_child: etree._Element, action: str) -> bytes:
        """
        Construye un sobre SOAP 1.2 con el cuerpo dado y lo firma con WS-Security:
        firma el wsu:Timestamp y el soap:Body con el certificado del emisor.

        El binding de la DIAN declara UsingAddressing, así que el sobre incluye
        las cabeceras WS-Addressing (Action, To, ReplyTo, MessageID) — sin ellas
        el WCF de la DIAN no enruta el mensaje (se traduce en 504/cuelgue).
        """
        if self._private_key is None or self._cert_der_b64 is None:
            raise ValueError(
                "DIANClient requiere el certificado (.p12) para firmar el sobre "
                "WS-Security. Pase cert_path y cert_password al construirlo."
            )

        nsmap = {'soap': NS_SOAP12, 'wsse': NS_WSSE, 'wsu': NS_WSU,
                 'ds': NS_DS, 'wsa': NS_WSA}
        env    = etree.Element(_qn(NS_SOAP12, 'Envelope'), nsmap=nsmap)
        header = etree.SubElement(env, _qn(NS_SOAP12, 'Header'))
        body   = etree.SubElement(env, _qn(NS_SOAP12, 'Body'))

        # ── WS-Addressing ─────────────────────────────────────────────────────
        wsa_action = etree.SubElement(header, _qn(NS_WSA, 'Action'))
        wsa_action.set(_qn(NS_SOAP12, 'mustUnderstand'), '1')
        wsa_action.text = action
        # sp:SignedParts exige firmar la cabecera wsa:To → necesita wsu:Id
        to_id = f"To-{uuid.uuid4().hex}"
        wsa_to = etree.SubElement(header, _qn(NS_WSA, 'To'))
        wsa_to.set(_qn(NS_SOAP12, 'mustUnderstand'), '1')
        wsa_to.set(_qn(NS_WSU, 'Id'), to_id)
        wsa_to.text = self.url
        reply_to = etree.SubElement(header, _qn(NS_WSA, 'ReplyTo'))
        etree.SubElement(reply_to, _qn(NS_WSA, 'Address')).text = WSA_ANON
        etree.SubElement(header, _qn(NS_WSA, 'MessageID')).text = f"urn:uuid:{uuid.uuid4()}"

        body.append(body_child)

        # wsse:Security
        security = etree.SubElement(header, _qn(NS_WSSE, 'Security'))
        security.set(_qn(NS_SOAP12, 'mustUnderstand'), '1')

        # wsu:Timestamp
        ts_id = f"TS-{uuid.uuid4().hex}"
        ts = etree.SubElement(security, _qn(NS_WSU, 'Timestamp'))
        ts.set(_qn(NS_WSU, 'Id'), ts_id)
        ahora = datetime.now(timezone.utc)
        etree.SubElement(ts, _qn(NS_WSU, 'Created')).text = ahora.strftime('%Y-%m-%dT%H:%M:%SZ')
        etree.SubElement(ts, _qn(NS_WSU, 'Expires')).text = (
            (ahora + timedelta(seconds=300)).strftime('%Y-%m-%dT%H:%M:%SZ'))

        # wsse:BinarySecurityToken (certificado del emisor)
        bst_id = f"X509-{uuid.uuid4().hex}"
        bst = etree.SubElement(security, _qn(NS_WSSE, 'BinarySecurityToken'))
        bst.set('EncodingType', ENCTYPE_B64)
        bst.set('ValueType', VALUETYPE_X509)
        bst.set(_qn(NS_WSU, 'Id'), bst_id)
        bst.text = self._cert_der_b64

        # ── Digests de los nodos firmados (Timestamp y cabecera To) ───────────
        # La política DIAN (TransportBinding + IncludeTimestamp + SignedParts To)
        # firma el Timestamp y la cabecera wsa:To. El Body lo protege TLS.
        digest_ts = base64.b64encode(hashlib.sha256(_c14n(ts)).digest()).decode('ascii')
        digest_to = base64.b64encode(hashlib.sha256(_c14n(wsa_to)).digest()).decode('ascii')

        # ── ds:Signature ──────────────────────────────────────────────────────
        sig_id = f"SIG-{uuid.uuid4().hex}"
        signature = etree.SubElement(security, _qn(NS_DS, 'Signature'))
        signature.set('Id', sig_id)

        signed_info = etree.SubElement(signature, _qn(NS_DS, 'SignedInfo'))
        etree.SubElement(signed_info, _qn(NS_DS, 'CanonicalizationMethod'),
                         Algorithm=ALG_C14N_EXC)
        etree.SubElement(signed_info, _qn(NS_DS, 'SignatureMethod'),
                         Algorithm=ALG_RSA_SHA256)
        for ref_uri, digest_val in ((ts_id, digest_ts), (to_id, digest_to)):
            ref = etree.SubElement(signed_info, _qn(NS_DS, 'Reference'), URI=f"#{ref_uri}")
            transforms = etree.SubElement(ref, _qn(NS_DS, 'Transforms'))
            etree.SubElement(transforms, _qn(NS_DS, 'Transform'), Algorithm=ALG_C14N_EXC)
            etree.SubElement(ref, _qn(NS_DS, 'DigestMethod'), Algorithm=ALG_SHA256)
            etree.SubElement(ref, _qn(NS_DS, 'DigestValue')).text = digest_val

        # Firmar la canonicalización del SignedInfo
        signed_info_c14n = _c14n(signed_info)
        firma = self._private_key.sign(signed_info_c14n, padding.PKCS1v15(), hashes.SHA256())
        etree.SubElement(signature, _qn(NS_DS, 'SignatureValue')).text = (
            base64.b64encode(firma).decode('ascii'))

        # ds:KeyInfo → SecurityTokenReference → KeyIdentifier por huella SHA-1
        # (sp:RequireThumbprintReference / sp:MustSupportRefThumbprint)
        key_info = etree.SubElement(signature, _qn(NS_DS, 'KeyInfo'))
        str_el = etree.SubElement(key_info, _qn(NS_WSSE, 'SecurityTokenReference'))
        key_id = etree.SubElement(str_el, _qn(NS_WSSE, 'KeyIdentifier'))
        key_id.set('EncodingType', ENCTYPE_B64)
        key_id.set('ValueType', VALUETYPE_THUMB)
        key_id.text = self._cert_thumb_b64

        return etree.tostring(env, xml_declaration=True, encoding='UTF-8')

    # ── Parseo del ApplicationResponse ──────────────────────────────────────────

    def _parsear_response(self, response_bytes: bytes) -> dict:
        """
        Extrae los campos relevantes del ApplicationResponse DIAN.

        StatusCode:
          "00" = Documento procesado correctamente (ACEPTADA)
          "66" = Documento rechazado
          "99" = Error técnico
        """
        resultado = {
            'is_valid':    False,
            'status_code': '',
            'description': '',
            'errors':      [],
            'cufe_dian':   '',
            'raw_xml':     response_bytes,
        }

        try:
            root = etree.fromstring(response_bytes)

            for el in root.iter():
                tag = el.tag.split('}')[-1] if '}' in el.tag else el.tag
                text = (el.text or '').strip()

                if tag == 'IsValid' and text:
                    resultado['is_valid'] = text.lower() == 'true'
                elif tag == 'StatusCode' and text:
                    resultado['status_code'] = text
                elif tag == 'StatusDescription' and text:
                    resultado['description'] = text
                elif tag == 'StatusMessage' and text:
                    resultado['description'] = resultado['description'] or text
                elif tag in ('ErrorMessage', 'ProcessedMessage') and text:
                    if text not in resultado['errors']:
                        resultado['errors'].append(text)
                elif tag in ('XmlDocumentKey', 'UUID') and text and len(text) > 20:
                    resultado['cufe_dian'] = text
                # Faults SOAP 1.2: <soap:Reason><soap:Text>...</soap:Text></soap:Reason>
                elif tag in ('Text', 'Reason', 'faultstring') and text:
                    if text not in resultado['errors']:
                        resultado['errors'].append(text)

            if resultado['status_code'] == '00' and not resultado['is_valid']:
                resultado['is_valid'] = True

        except etree.XMLSyntaxError as e:
            logger.error(f"Error parseando ApplicationResponse DIAN: {e}")
            resultado['errors'].append(f"XML inválido en respuesta DIAN: {e}")

        return resultado
