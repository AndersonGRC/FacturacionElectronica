"""
FirmadorDIAN: firma digital XAdES-EPES v1.3.2 del XML UBL 2.1 (factura DIAN).

Implementación MANUAL (lxml + cryptography) para control total del perfil exacto
que exige la DIAN — signxml no encaja (emite SigningCertificateV2 y usa c14n
inclusiva en las referencias internas, lo que rompe la validación al ubicar la
firma dentro del documento).

La firma cumple:
  - ds:Signature dentro del 2º ext:UBLExtension/ext:ExtensionContent (enveloped)
  - 3 referencias, TODAS con c14n EXCLUSIVA explícita (digest independiente de la
    posición → la firma valida estando embebida):
      * documento (URI="") con transform enveloped + exc-c14n
      * KeyInfo (cadena de certificados)
      * SignedProperties (XAdES)
  - SignedSignatureProperties: SigningTime, SigningCertificate (3 Cert de la
    cadena, v1.3.2), SignaturePolicyIdentifier (política DIAN v2 con su hash real)
  - rsa-sha256 / sha256
"""

import base64
import hashlib
import uuid
from datetime import datetime, timezone, timedelta

from lxml import etree
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12, Encoding

# ── Namespaces ──────────────────────────────────────────────────────────────
NS_EXT   = 'urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2'
NS_DS    = 'http://www.w3.org/2000/09/xmldsig#'
NS_XADES = 'http://uri.etsi.org/01903/v1.3.2#'

# ── Algoritmos ──────────────────────────────────────────────────────────────
ALG_C14N_EXC  = 'http://www.w3.org/2001/10/xml-exc-c14n#'
ALG_ENVELOPED = 'http://www.w3.org/2000/09/xmldsig#enveloped-signature'
ALG_RSA_SHA256 = 'http://www.w3.org/2001/04/xmldsig-more#rsa-sha256'
ALG_SHA256 = 'http://www.w3.org/2001/04/xmlenc#sha256'

# ── Política de firma DIAN v2 (hash SHA-256 real del PDF) ────────────────────
DIAN_POLICY_ID = ('https://facturaelectronica.dian.gov.co/politicadefirma/v2/'
                  'politicadefirmav2.pdf')
DIAN_POLICY_DESC = ('Política de firma para facturas electrónicas de la '
                    'República de Colombia.')
DIAN_POLICY_DIGEST = 'dMoMvtcG5aIzgYo0tIsSQeVJBDnUnfSOfBpxXrmor0Y='

TZ_CO = timezone(timedelta(hours=-5))


def _ds(tag):
    return f'{{{NS_DS}}}{tag}'


def _xades(tag):
    return f'{{{NS_XADES}}}{tag}'


def _c14n(node) -> bytes:
    return etree.tostring(node, method='c14n', exclusive=True, with_comments=False)


def _sha256_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode('ascii')


class FirmadorDIAN:
    """Carga un .p12 y firma un documento UBL 2.1 con XAdES-EPES (DIAN)."""

    def __init__(self, cert_path: str, cert_password: str):
        with open(cert_path, 'rb') as f:
            p12_data = f.read()
        self.private_key, self.certificate, self.chain = (
            pkcs12.load_key_and_certificates(p12_data, cert_password.encode('utf-8'))
        )

    # ── API pública ───────────────────────────────────────────────────────────

    def firmar(self, xml_bytes: bytes) -> bytes:
        root = etree.fromstring(xml_bytes)

        # 2º ExtensionContent (el 1º lleva DianExtensions) — ahí va la firma.
        ext_contents = root.findall(
            f'{{{NS_EXT}}}UBLExtensions'
            f'/{{{NS_EXT}}}UBLExtension'
            f'/{{{NS_EXT}}}ExtensionContent')
        if len(ext_contents) < 2:
            raise ValueError("Se esperaban 2 ext:ExtensionContent (DianExtensions + firma)")
        sig_holder = ext_contents[-1]

        # Digest del documento (referencia enveloped). La firma aún NO está en el
        # árbol, así que c14n(root) == documento con la firma removida (que es lo
        # que hace la transformada enveloped del lado DIAN).
        doc_digest = _sha256_b64(_c14n(root))

        cadena = [self.certificate] + [c for c in (self.chain or [])]

        # IDs únicos
        uid          = uuid.uuid4().hex
        sig_id       = f"xmldsig-{uid}"
        ref_doc_id   = f"{sig_id}-ref0"
        keyinfo_id   = f"{sig_id}-keyinfo"
        signprops_id = f"{sig_id}-signedprops"

        # ── Construir ds:Signature (detached) ─────────────────────────────────
        sig = etree.Element(_ds('Signature'), nsmap={'ds': NS_DS, 'xades': NS_XADES})
        sig.set('Id', sig_id)

        signed_info = etree.SubElement(sig, _ds('SignedInfo'))
        etree.SubElement(signed_info, _ds('CanonicalizationMethod'), Algorithm=ALG_C14N_EXC)
        etree.SubElement(signed_info, _ds('SignatureMethod'), Algorithm=ALG_RSA_SHA256)

        # Ref 1 — documento (enveloped + exc-c14n)
        r_doc = etree.SubElement(signed_info, _ds('Reference'), Id=ref_doc_id, URI='')
        tr = etree.SubElement(r_doc, _ds('Transforms'))
        etree.SubElement(tr, _ds('Transform'), Algorithm=ALG_ENVELOPED)
        etree.SubElement(tr, _ds('Transform'), Algorithm=ALG_C14N_EXC)
        etree.SubElement(r_doc, _ds('DigestMethod'), Algorithm=ALG_SHA256)
        etree.SubElement(r_doc, _ds('DigestValue')).text = doc_digest

        # Ref 2 — KeyInfo
        r_ki = etree.SubElement(signed_info, _ds('Reference'), URI=f"#{keyinfo_id}")
        tr = etree.SubElement(r_ki, _ds('Transforms'))
        etree.SubElement(tr, _ds('Transform'), Algorithm=ALG_C14N_EXC)
        etree.SubElement(r_ki, _ds('DigestMethod'), Algorithm=ALG_SHA256)
        ki_digest_node = etree.SubElement(r_ki, _ds('DigestValue'))

        # Ref 3 — SignedProperties (XAdES)
        r_sp = etree.SubElement(signed_info, _ds('Reference'),
                                Type='http://uri.etsi.org/01903#SignedProperties',
                                URI=f"#{signprops_id}")
        tr = etree.SubElement(r_sp, _ds('Transforms'))
        etree.SubElement(tr, _ds('Transform'), Algorithm=ALG_C14N_EXC)
        etree.SubElement(r_sp, _ds('DigestMethod'), Algorithm=ALG_SHA256)
        sp_digest_node = etree.SubElement(r_sp, _ds('DigestValue'))

        # SignatureValue (se rellena tras firmar)
        sig_value_node = etree.SubElement(sig, _ds('SignatureValue'))

        # KeyInfo — cadena de certificados
        key_info = etree.SubElement(sig, _ds('KeyInfo'), Id=keyinfo_id)
        x509_data = etree.SubElement(key_info, _ds('X509Data'))
        for cert in cadena:
            der_b64 = base64.b64encode(cert.public_bytes(Encoding.DER)).decode('ascii')
            etree.SubElement(x509_data, _ds('X509Certificate')).text = der_b64

        # Object → QualifyingProperties → SignedProperties
        obj = etree.SubElement(sig, _ds('Object'))
        qp = etree.SubElement(obj, _xades('QualifyingProperties'), Target=f"#{sig_id}")
        sp = etree.SubElement(qp, _xades('SignedProperties'), Id=signprops_id)
        ssp = etree.SubElement(sp, _xades('SignedSignatureProperties'))

        etree.SubElement(ssp, _xades('SigningTime')).text = (
            datetime.now(TZ_CO).strftime('%Y-%m-%dT%H:%M:%S-05:00'))

        signing_cert = etree.SubElement(ssp, _xades('SigningCertificate'))
        for cert in cadena:
            der = cert.public_bytes(Encoding.DER)
            cert_el = etree.SubElement(signing_cert, _xades('Cert'))
            cdig = etree.SubElement(cert_el, _xades('CertDigest'))
            etree.SubElement(cdig, _ds('DigestMethod'), Algorithm=ALG_SHA256)
            etree.SubElement(cdig, _ds('DigestValue')).text = _sha256_b64(der)
            iss = etree.SubElement(cert_el, _xades('IssuerSerial'))
            etree.SubElement(iss, _ds('X509IssuerName')).text = cert.issuer.rfc4514_string()
            etree.SubElement(iss, _ds('X509SerialNumber')).text = str(cert.serial_number)

        spi = etree.SubElement(ssp, _xades('SignaturePolicyIdentifier'))
        spid = etree.SubElement(spi, _xades('SignaturePolicyId'))
        sp_id = etree.SubElement(spid, _xades('SigPolicyId'))
        etree.SubElement(sp_id, _xades('Identifier')).text = DIAN_POLICY_ID
        etree.SubElement(sp_id, _xades('Description')).text = DIAN_POLICY_DESC
        sp_hash = etree.SubElement(spid, _xades('SigPolicyHash'))
        etree.SubElement(sp_hash, _ds('DigestMethod'), Algorithm=ALG_SHA256)
        etree.SubElement(sp_hash, _ds('DigestValue')).text = DIAN_POLICY_DIGEST

        # ── Digests de KeyInfo y SignedProperties (c14n exclusiva) ────────────
        ki_digest_node.text = _sha256_b64(_c14n(key_info))
        sp_digest_node.text = _sha256_b64(_c14n(sp))

        # ── Firmar el SignedInfo ──────────────────────────────────────────────
        firma = self.private_key.sign(_c14n(signed_info), padding.PKCS1v15(), hashes.SHA256())
        sig_value_node.text = base64.b64encode(firma).decode('ascii')

        # ── Insertar la firma en el documento ─────────────────────────────────
        sig_holder.append(sig)

        return etree.tostring(root, xml_declaration=True, encoding='UTF-8', pretty_print=False)
