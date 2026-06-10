"""
FirmadorDIAN: firma digital del XML UBL 2.1.

La DIAN requiere firma XAdES-BES embebida en el nodo UBLExtensions del documento.
Este módulo usa:
  - `cryptography` para cargar el certificado .p12 y extraer clave + cert X.509
  - `signxml` para la mecánica XMLDsig (enveloped, rsa-sha256, c14n exclusiva)
  - lxml para construir los nodos XAdES requeridos por la DIAN
"""

import base64
import hashlib
from datetime import datetime, timezone
from lxml import etree
from cryptography.hazmat.primitives.serialization import pkcs12, Encoding, PublicFormat
from cryptography.hazmat.primitives import serialization
from signxml import XMLSigner, methods

# Namespaces
NS_EXT   = 'urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2'
NS_DS    = 'http://www.w3.org/2000/09/xmldsig#'
NS_XADES = 'http://uri.etsi.org/01903/v1.3.2#'


class FirmadorDIAN:
    """
    Carga un certificado .p12 y firma un documento XML UBL 2.1.

    Args:
        cert_path:     Ruta absoluta al archivo .p12
        cert_password: Contraseña del .p12 en texto plano
    """

    def __init__(self, cert_path: str, cert_password: str):
        with open(cert_path, 'rb') as f:
            p12_data = f.read()

        self.private_key, self.certificate, self.chain = (
            pkcs12.load_key_and_certificates(
                p12_data,
                cert_password.encode('utf-8')
            )
        )

    # ── API pública ───────────────────────────────────────────────────────────

    def firmar(self, xml_bytes: bytes) -> bytes:
        """
        Firma el XML UBL 2.1.

        Pasos:
          1. Parsear XML
          2. Reemplazar ExtensionContent vacío con los nodos XAdES
          3. Firmar con signxml (rsa-sha256, c14n exclusiva)
          4. Retornar XML firmado como bytes UTF-8

        Returns:
            bytes: XML firmado, sin declaración pretty_print para que
                   la firma no se invalide por whitespace adicional.
        """
        root = etree.fromstring(xml_bytes)

        # Preparar nodos XAdES dentro de UBLExtensions antes de firmar
        self._inyectar_xades(root)

        # Serializar clave privada y certificado en PEM para signxml
        key_pem  = self.private_key.private_bytes(
            Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()
        )
        cert_pem = self.certificate.public_bytes(Encoding.PEM)

        signer = XMLSigner(
            method=methods.enveloped,
            signature_algorithm='rsa-sha256',
            digest_algorithm='sha256',
            c14n_algorithm='http://www.w3.org/2001/10/xml-exc-c14n#',
        )

        signed_root = signer.sign(
            root,
            key=key_pem,
            cert=cert_pem,
        )

        return etree.tostring(
            signed_root,
            xml_declaration=True,
            encoding='UTF-8',
            pretty_print=False
        )

    # ── Construcción de nodos XAdES ───────────────────────────────────────────

    def _inyectar_xades(self, root: etree._Element):
        """
        Rellena el nodo ext:ExtensionContent con los elementos XAdES-BES.

        La DIAN requiere que la firma se posicione dentro de:
          Invoice/ext:UBLExtensions/ext:UBLExtension/ext:ExtensionContent
        """
        # Localizar el ExtensionContent vacío creado por XMLBuilder
        ext_content = root.find(
            f'{{{NS_EXT}}}UBLExtensions'
            f'/{{{NS_EXT}}}UBLExtension'
            f'/{{{NS_EXT}}}ExtensionContent'
        )
        if ext_content is None:
            raise ValueError("No se encontró ext:ExtensionContent en el XML")

        # Construir xades:QualifyingProperties
        qp = etree.SubElement(
            ext_content,
            f'{{{NS_XADES}}}QualifyingProperties',
            Target='xmldsig-' + hashlib.md5(b'factura').hexdigest()[:8],
        )
        sp = etree.SubElement(qp, f'{{{NS_XADES}}}SignedProperties',
                               Id='xmldsig-SignedProperties')
        ssp = etree.SubElement(sp, f'{{{NS_XADES}}}SignedSignatureProperties')

        # Tiempo de firma
        st = etree.SubElement(ssp, f'{{{NS_XADES}}}SigningTime')
        st.text = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        # Información del certificado firmante
        sc  = etree.SubElement(ssp, f'{{{NS_XADES}}}SigningCertificate')
        cert_el = etree.SubElement(sc, f'{{{NS_XADES}}}Cert')
        cd  = etree.SubElement(cert_el, f'{{{NS_XADES}}}CertDigest')

        dm  = etree.SubElement(cd, f'{{{NS_DS}}}DigestMethod',
                                Algorithm='http://www.w3.org/2001/04/xmlenc#sha256')
        dv  = etree.SubElement(cd, f'{{{NS_DS}}}DigestValue')
        # Digest SHA256 del certificado DER
        cert_der = self.certificate.public_bytes(Encoding.DER)
        dv.text  = base64.b64encode(hashlib.sha256(cert_der).digest()).decode()

        # Issuer y serial del certificado
        issuer   = etree.SubElement(cert_el, f'{{{NS_XADES}}}IssuerSerial')
        x509_iss = etree.SubElement(issuer, f'{{{NS_DS}}}X509IssuerName')
        x509_ser = etree.SubElement(issuer, f'{{{NS_DS}}}X509SerialNumber')
        x509_iss.text = self.certificate.issuer.rfc4514_string()
        x509_ser.text = str(self.certificate.serial_number)
