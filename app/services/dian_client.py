"""
DIANClient: cliente para el Web Service de la DIAN.

Método utilizado: SendBillSync (síncrono, respuesta inmediata con ApplicationResponse).
Protocolo: SOAP sobre HTTPS con autenticación JWT (OAuth2 DIAN).

Endpoints:
  Habilitación: https://vpfe-hab.dian.gov.co/WcfDianCustomerServices.svc
  Producción:   https://vpfe.dian.gov.co/WcfDianCustomerServices.svc

Flujo de envío:
  1. Empaquetar XML firmado en ZIP (en memoria, io.BytesIO)
  2. Codificar ZIP en Base64
  3. Construir SOAP envelope SendBillSync
  4. POST con Content-Type: text/xml y Authorization: Bearer {token}
  5. Parsear ApplicationResponse → IsValid, StatusCode, CUFE confirmado
"""

import base64
import hashlib
import io
import logging
import zipfile
from datetime import datetime, timezone, timedelta
from lxml import etree
import requests

logger = logging.getLogger(__name__)

# SOAP namespaces
NS_SOAP   = 'http://schemas.xmlsoap.org/soap/envelope/'
NS_WCF    = 'http://wcf.dian.colombia'

DIAN_URLS = {
    'habilitacion': 'https://vpfe-hab.dian.gov.co/WcfDianCustomerServices.svc',
    'produccion':   'https://vpfe.dian.gov.co/WcfDianCustomerServices.svc',
}

# URL para obtener el token JWT de la DIAN
DIAN_AUTH_URL = {
    'habilitacion': 'https://vpfe-hab.dian.gov.co/WcfDianCustomerServices.svc?wsdl',
    'produccion':   'https://vpfe.dian.gov.co/WcfDianCustomerServices.svc?wsdl',
}


class DIANClient:
    """
    Cliente DIAN multi-ambiente.

    Args:
        ambiente:      'habilitacion' | 'produccion'
        nit_emisor:    NIT del emisor sin dígito de verificación
        clave_tecnica: Clave técnica asignada por DIAN al software registrado
        timeout:       Timeout en segundos para llamadas HTTP
    """

    def __init__(self, ambiente: str, nit_emisor: str,
                 clave_tecnica: str, timeout: int = 30,
                 software_id: str = None):
        self.ambiente      = ambiente
        self.url           = DIAN_URLS.get(ambiente, DIAN_URLS['habilitacion'])
        self.nit           = nit_emisor
        self.clave_tecnica = clave_tecnica
        self.software_id   = software_id
        self.timeout       = timeout

    # ── API pública ───────────────────────────────────────────────────────────

    def enviar_factura(self, xml_firmado: bytes, numero_factura: str,
                       token_dian: str = None) -> dict:
        """
        Envía la factura firmada a la DIAN y retorna el resultado.

        Args:
            xml_firmado:    Bytes del XML UBL 2.1 firmado
            numero_factura: Número de factura (para nombrar el ZIP)
            token_dian:     Token JWT de autenticación DIAN (opcional en habilitación)

        Returns:
            dict con claves:
              is_valid     (bool)
              status_code  (str, "00"=aceptada)
              description  (str)
              errors       (list[str])
              cufe_dian    (str, CUFE confirmado por DIAN)
              raw_xml      (bytes, ApplicationResponse completo)
        """
        # 1. Empaquetar en ZIP
        nombre_xml = f"{numero_factura}.xml"
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(nombre_xml, xml_firmado)
        zip_bytes = zip_buffer.getvalue()
        zip_b64   = base64.b64encode(zip_bytes).decode('utf-8')

        logger.info(f"Enviando factura {numero_factura} a DIAN ({self.ambiente}), "
                    f"ZIP={len(zip_bytes)} bytes")

        # 2. Construir SOAP
        soap_body = self._build_soap_send_bill(zip_b64, f"{numero_factura}.zip")

        # 3. Cabeceras
        # Si no hay token, intentar obtener uno con el software_id
        if not token_dian and self.software_id:
            try:
                token_dian = self.obtener_token_oauth()
            except Exception as e:
                logger.warning(f"No se pudo obtener token OAuth DIAN: {e}. Continuando sin token.")

        headers = {
            'Content-Type': 'text/xml; charset=utf-8',
            'SOAPAction': (
                'http://wcf.dian.colombia/IWcfDianCustomerServices/SendBillSync'
            ),
        }
        if token_dian:
            headers['Authorization'] = f'Bearer {token_dian}'

        # 4. Enviar
        response = requests.post(
            self.url,
            data=soap_body.encode('utf-8'),
            headers=headers,
            timeout=self.timeout,
            verify=True,
        )

        logger.info(f"DIAN respondió HTTP {response.status_code} para {numero_factura}")
        response.raise_for_status()

        # 5. Parsear respuesta
        return self._parsear_response(response.content)

    def obtener_token_oauth(self) -> str:
        """
        Obtiene un token JWT de la DIAN usando el software_id y clave_tecnica.

        Endpoint habilitación: https://vpfe-hab.dian.gov.co/WcfDianCustomerServices.svc
        El token tiene vigencia de ~1 hora. Cachear en Redis con TTL=50min.
        """
        if not self.software_id:
            raise ValueError("software_id requerido para obtener token DIAN")

        # La DIAN usa un SOAP especial para autenticación (GetTokenFromSoftware)
        soap_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope
    xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:wcf="http://wcf.dian.colombia">
  <soapenv:Header/>
  <soapenv:Body>
    <wcf:GetTokenFromSoftware>
      <wcf:softwareId>{self.software_id}</wcf:softwareId>
      <wcf:pin>{self.clave_tecnica}</wcf:pin>
    </wcf:GetTokenFromSoftware>
  </soapenv:Body>
</soapenv:Envelope>"""

        headers = {
            'Content-Type': 'text/xml; charset=utf-8',
            'SOAPAction': 'http://wcf.dian.colombia/IWcfDianCustomerServices/GetTokenFromSoftware',
        }

        resp = requests.post(
            self.url,
            data=soap_body.encode('utf-8'),
            headers=headers,
            timeout=self.timeout,
            verify=True,
        )
        resp.raise_for_status()

        root = etree.fromstring(resp.content)
        for el in root.iter():
            tag = el.tag.split('}')[-1] if '}' in el.tag else el.tag
            if tag in ('GetTokenFromSoftwareResult', 'Token', 'token') and el.text:
                return el.text.strip()

        raise ValueError(f"No se encontró token en la respuesta DIAN: {resp.text[:200]}")

    def obtener_estado_documento(self, cufe: str, token_dian: str = None) -> dict:
        """
        Consulta el estado de un documento en la DIAN por CUFE.
        Útil para reconciliar facturas que quedaron en estado intermedio.
        """
        soap_body = self._build_soap_get_status(cufe)
        headers = {
            'Content-Type': 'text/xml; charset=utf-8',
            'SOAPAction': (
                'http://wcf.dian.colombia/IWcfDianCustomerServices/GetStatusZip'
            ),
        }
        if token_dian:
            headers['Authorization'] = f'Bearer {token_dian}'

        response = requests.post(
            self.url, data=soap_body.encode('utf-8'),
            headers=headers, timeout=self.timeout, verify=True
        )
        response.raise_for_status()
        return self._parsear_response(response.content)

    # ── Construcción SOAP ─────────────────────────────────────────────────────

    def _build_soap_send_bill(self, zip_b64: str, nombre_zip: str) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope
    xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:wcf="http://wcf.dian.colombia">
  <soapenv:Header/>
  <soapenv:Body>
    <wcf:SendBillSync>
      <wcf:fileName>{nombre_zip}</wcf:fileName>
      <wcf:contentFile>{zip_b64}</wcf:contentFile>
    </wcf:SendBillSync>
  </soapenv:Body>
</soapenv:Envelope>"""

    def _build_soap_get_status(self, cufe: str) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope
    xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:wcf="http://wcf.dian.colombia">
  <soapenv:Header/>
  <soapenv:Body>
    <wcf:GetStatusZip>
      <wcf:trackId>{cufe}</wcf:trackId>
    </wcf:GetStatusZip>
  </soapenv:Body>
</soapenv:Envelope>"""

    # ── Parseo del ApplicationResponse ───────────────────────────────────────

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

            # Buscar el Body SOAP
            body = root.find(f'{{{NS_SOAP}}}Body')
            if body is None:
                resultado['errors'].append('No se encontró SOAP Body en la respuesta')
                return resultado

            # Extraer texto de todos los elementos relevantes
            # (la estructura exacta depende de la versión del WS DIAN)
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

            # Si StatusCode es "00" y no se marcó is_valid, corregir
            if resultado['status_code'] == '00' and not resultado['is_valid']:
                resultado['is_valid'] = True

        except etree.XMLSyntaxError as e:
            logger.error(f"Error parseando ApplicationResponse DIAN: {e}")
            resultado['errors'].append(f"XML inválido en respuesta DIAN: {e}")

        return resultado
