"""
XMLBuilder: construye el XML UBL 2.1 para la DIAN (Colombia).

Soporta los 3 documentos electrónicos:
  - factura       → <Invoice>      (InvoiceTypeCode 01, CUFE)
  - nota_credito  → <CreditNote>   (CreditNoteTypeCode 91, CUDE, referencia factura)
  - nota_debito   → <DebitNote>    (DebitNoteTypeCode 92, CUDE, referencia factura)

DISEÑO ESCALABLE / MULTI-TENANT: TODO se lee del dict `tenant` (fila de la tabla
tenants) y del payload `datos`. Nada quemado a un cliente. Maneja responsable y
no-responsable de IVA, persona natural y jurídica.
"""

import hashlib
from lxml import etree
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

# ── Namespaces UBL 2.1 ────────────────────────────────────────────────────────
NS_CAC  = 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2'
NS_CBC  = 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'
NS_EXT  = 'urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2'
NS_STS  = 'dian:gov:co:facturaelectronica:Structures-2-1'
NS_DS   = 'http://www.w3.org/2000/09/xmldsig#'

DIAN_AGENCY = 'CO, DIAN (Dirección de Impuestos y Aduanas Nacionales)'
DIAN_NIT    = '800197268'
DIAN_NIT_DV = '4'

TIPO_DOC_SCHEME = {'CC': '13', 'CE': '22', 'NIT': '31', 'TI': '12', 'PA': '41', 'RC': '11'}

# Config por tipo de documento
TIPO_CFG = {
    'factura': dict(
        ns='urn:oasis:names:specification:ubl:schema:xsd:Invoice-2', tag='Invoice',
        tcode='01', tcode_elem='InvoiceTypeCode', tcode_uri='InvoiceType-2.1',
        cust='10', profile='DIAN 2.1: Factura Electrónica de Venta',
        uuid_scheme='CUFE-SHA384', line='InvoiceLine', qty='InvoicedQuantity',
        total_elem='LegalMonetaryTotal', note='Factura electrónica de venta'),
    'nota_credito': dict(
        ns='urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2', tag='CreditNote',
        tcode='91', tcode_elem='CreditNoteTypeCode', tcode_uri='CreditNoteType-2.1',
        cust='20', profile='DIAN 2.1: Nota Crédito de Factura Electrónica de Venta',
        uuid_scheme='CUDE-SHA384', line='CreditNoteLine', qty='CreditedQuantity',
        total_elem='LegalMonetaryTotal', note='Nota crédito electrónica'),
    'nota_debito': dict(
        ns='urn:oasis:names:specification:ubl:schema:xsd:DebitNote-2', tag='DebitNote',
        tcode='92', tcode_elem=None, tcode_uri='DebitNoteType-2.1',   # DebitNote NO lleva TypeCode
        cust='30', profile='DIAN 2.1: Nota Débito de Factura Electrónica de Venta',
        uuid_scheme='CUDE-SHA384', line='DebitNoteLine', qty='DebitedQuantity',
        total_elem='RequestedMonetaryTotal', note='Nota débito electrónica'),
}


def _d(val) -> Decimal:
    return Decimal(str(val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _el(parent, ns, tag, text=None, **attrs):
    e = etree.SubElement(parent, f'{{{ns}}}{tag}', **attrs)
    if text is not None:
        e.text = str(text)
    return e


def _cbc(parent, tag, text=None, **attrs):
    return _el(parent, NS_CBC, tag, text, **attrs)


def _cac(parent, tag):
    return _el(parent, NS_CAC, tag)


def _ext(parent, tag):
    return _el(parent, NS_EXT, tag)


def _sts(parent, tag):
    return _el(parent, NS_STS, tag)


class XMLBuilder:
    """
    Args:
        tenant: config del emisor (fila tenants).
        datos:  payload (incluye tipo_documento, documento_referencia, concepto_nota).
        numero: número del documento (prefijo + consecutivo).
        cufe:   CUFE (factura) o CUDE (notas), SHA384.
        fecha/hora: emisión (la MISMA usada en el CUFE/CUDE).
    """

    def __init__(self, tenant, datos, numero_factura, cufe, fecha=None, hora=None):
        self.tenant = tenant
        self.datos  = datos
        self.numero = numero_factura
        self.cufe   = cufe
        _now = datetime.now()
        self.fecha = fecha or _now.strftime('%Y-%m-%d')
        self.hora  = hora  or (_now.strftime('%H:%M:%S') + '-05:00')
        self.tipo  = datos.get('tipo_documento', 'factura')
        if self.tipo not in TIPO_CFG:
            self.tipo = 'factura'
        self.cfg = TIPO_CFG[self.tipo]
        self.doc_ref  = datos.get('documento_referencia', {}) or {}
        self.concepto = datos.get('concepto_nota', {}) or {}
        self.responsable_iva = str(tenant.get('regimen_codigo') or '48') == '48'
        self.totales = self._calcular_totales()

    # ── Entrada pública ───────────────────────────────────────────────────────

    def build(self) -> bytes:
        nsmap = {None: self.cfg['ns'], 'cac': NS_CAC, 'cbc': NS_CBC, 'ext': NS_EXT,
                 'sts': NS_STS, 'ds': NS_DS}
        root = etree.Element(self.cfg['tag'], nsmap=nsmap)
        self._add_ubl_extensions(root)
        self._add_header(root)
        if self.tipo != 'factura':
            self._add_billing_reference(root)
        self._add_supplier(root)
        self._add_customer(root)
        self._add_payment_means(root)
        self._add_tax_totals(root)
        self._add_legal_totals(root)
        self._add_lines(root)
        return etree.tostring(root, xml_declaration=True, encoding='UTF-8', pretty_print=False)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _t(self, key, default=''):
        v = self.tenant.get(key)
        return default if v in (None, '') else v

    @property
    def _es_natural(self):
        return self._t('tipo_persona_emisor', 'juridica') == 'natural'

    def _calcular_totales(self):
        subtotal = Decimal('0'); total_iva = Decimal('0'); descuentos = Decimal('0')
        for item in self.datos.get('items', []):
            cant = _d(item.get('cantidad', 1)); precio = _d(item.get('precio_unitario', 0))
            desc = _d(item.get('descuento', 0))
            iva_pct = _d(item.get('impuesto_iva', 19)) if self.responsable_iva else Decimal('0')
            base = _d((cant * precio) - desc)
            subtotal += base
            total_iva += _d(base * iva_pct / Decimal('100'))
            descuentos += desc
        total = subtotal + total_iva
        return {'subtotal': _d(subtotal), 'total_iva': _d(total_iva),
                'descuentos': _d(descuentos), 'total': _d(total)}

    def _software_security_code(self):
        cadena = f"{self._t('software_id')}{self._t('software_pin')}{self.numero}"
        return hashlib.sha384(cadena.encode('utf-8')).hexdigest()

    def _qr_url(self):
        base = ('https://catalogo-vpfe-hab.dian.gov.co'
                if self._t('ambiente', 'habilitacion') == 'habilitacion'
                else 'https://catalogo-vpfe.dian.gov.co')
        return f"{base}/document/searchqr?documentkey={self.cufe}"

    # ── UBLExtensions ─────────────────────────────────────────────────────────

    def _add_ubl_extensions(self, root):
        ubl_exts = _ext(root, 'UBLExtensions')
        ext1 = _ext(ubl_exts, 'UBLExtension')
        dian = _sts(_ext(ext1, 'ExtensionContent'), 'DianExtensions')

        # InvoiceControl (resolución/rango) SOLO para factura — las notas no llevan.
        if self.tipo == 'factura':
            inv_ctrl = _sts(dian, 'InvoiceControl')
            _sts(inv_ctrl, 'InvoiceAuthorization').text = str(self._t('resolucion_dian'))
            period = _sts(inv_ctrl, 'AuthorizationPeriod')
            _cbc(period, 'StartDate', str(self._t('resolucion_fecha')))
            _cbc(period, 'EndDate', str(self._t('resolucion_vigencia')))
            auth = _sts(inv_ctrl, 'AuthorizedInvoices')
            _sts(auth, 'Prefix').text = str(self._t('prefijo'))
            _sts(auth, 'From').text = str(self._t('resolucion_desde'))
            _sts(auth, 'To').text = str(self._t('resolucion_hasta'))

        src = _sts(dian, 'InvoiceSource')
        _cbc(src, 'IdentificationCode', 'CO', listAgencyID='6',
             listAgencyName='United Nations Economic Commission for Europe',
             listSchemeURI='urn:oasis:names:specification:ubl:codelist:gc:CountryIdentificationCode-2.1')

        sw = _sts(dian, 'SoftwareProvider')
        pid = _sts(sw, 'ProviderID'); pid.text = self._t('nit')
        pid.set('schemeAgencyID', '195'); pid.set('schemeAgencyName', DIAN_AGENCY)
        pid.set('schemeID', str(self._t('digito_verificacion', '0'))); pid.set('schemeName', '31')
        sid = _sts(sw, 'SoftwareID'); sid.text = self._t('software_id')
        sid.set('schemeAgencyID', '195'); sid.set('schemeAgencyName', DIAN_AGENCY)

        ssc = _sts(dian, 'SoftwareSecurityCode')
        ssc.set('schemeAgencyID', '195'); ssc.set('schemeAgencyName', DIAN_AGENCY)
        ssc.text = self._software_security_code()

        ap = _sts(dian, 'AuthorizationProvider')
        apid = _sts(ap, 'AuthorizationProviderID'); apid.text = DIAN_NIT
        apid.set('schemeAgencyID', '195'); apid.set('schemeAgencyName', DIAN_AGENCY)
        apid.set('schemeID', DIAN_NIT_DV); apid.set('schemeName', '31')

        _sts(dian, 'QRCode').text = self._qr_url()

        ext2 = _ext(ubl_exts, 'UBLExtension')
        _ext(ext2, 'ExtensionContent')   # hueco para la firma

    # ── Cabecera ──────────────────────────────────────────────────────────────

    def _add_header(self, root):
        prof_exec = '2' if self._t('ambiente', 'habilitacion') == 'habilitacion' else '1'
        _cbc(root, 'UBLVersionID', 'UBL 2.1')
        _cbc(root, 'CustomizationID', self.cfg['cust'])
        _cbc(root, 'ProfileID', self.cfg['profile'])
        _cbc(root, 'ProfileExecutionID', prof_exec)
        _cbc(root, 'ID', self.numero)
        _cbc(root, 'UUID', self.cufe, schemeID=prof_exec, schemeName=self.cfg['uuid_scheme'])
        _cbc(root, 'IssueDate', self.fecha)
        _cbc(root, 'IssueTime', self.hora)
        # DebitNote (UBL 2.1) NO tiene elemento TypeCode; factura y nota crédito sí.
        if self.cfg['tcode_elem']:
            _cbc(root, self.cfg['tcode_elem'], self.cfg['tcode'],
                 listAgencyID='195', listAgencyName=DIAN_AGENCY,
                 listSchemeURI=f"urn:oasis:names:specification:ubl:codelist:gc:{self.cfg['tcode_uri']}")
        _cbc(root, 'Note', self.datos.get('notas', self.cfg['note']))
        _cbc(root, 'DocumentCurrencyCode', self.datos.get('moneda', 'COP'),
             listAgencyID='6', listAgencyName='United Nations Economic Commission for Europe',
             listID='ISO 4217 Alpha')
        _cbc(root, 'LineCountNumeric', str(len(self.datos.get('items', []))))

    # ── Referencia a la factura (solo notas) ──────────────────────────────────

    def _add_billing_reference(self, root):
        disc = _cac(root, 'DiscrepancyResponse')
        _cbc(disc, 'ReferenceID', self.doc_ref.get('numero', ''))
        _cbc(disc, 'ResponseCode', str(self.concepto.get('codigo', '2')))
        _cbc(disc, 'Description', self.concepto.get('descripcion', 'Ajuste'))

        bill = _cac(root, 'BillingReference')
        idr = _cac(bill, 'InvoiceDocumentReference')
        _cbc(idr, 'ID', self.doc_ref.get('numero', ''))
        _cbc(idr, 'UUID', self.doc_ref.get('cufe', ''), schemeName='CUFE-SHA384')
        _cbc(idr, 'IssueDate', self.doc_ref.get('fecha', self.fecha))

    # ── Direcciones / Partes ──────────────────────────────────────────────────

    def _add_party_address(self, parent, tag, muni_cod, muni_nom, dep_cod, dep_nom, linea):
        addr = _cac(parent, tag)
        _cbc(addr, 'ID', muni_cod)
        _cbc(addr, 'CityName', muni_nom)
        _cbc(addr, 'CountrySubentity', dep_nom)
        _cbc(addr, 'CountrySubentityCode', dep_cod)
        _cbc(_cac(addr, 'AddressLine'), 'Line', linea or 'No informado')
        c = _cac(addr, 'Country')
        _cbc(c, 'IdentificationCode', 'CO')
        _cbc(c, 'Name', 'Colombia', languageID='es')

    def _add_supplier(self, root):
        supplier = _cac(root, 'AccountingSupplierParty')
        _cbc(supplier, 'AdditionalAccountID', '2' if self._es_natural else '1')
        party = _cac(supplier, 'Party')

        muni_cod = self._t('municipio_codigo', '11001'); muni_nom = self._t('municipio_nombre', 'Bogotá, D.C.')
        dep_cod = self._t('departamento_codigo', '11'); dep_nom = self._t('departamento_nombre', 'Bogotá')
        direccion = self._t('direccion', 'No informado')
        razon = self._t('razon_social'); nit = self._t('nit'); dv = str(self._t('digito_verificacion', '0'))
        responsabilidad = self._t('responsabilidad_fiscal', 'O-13'); regimen = self._t('regimen_codigo', '48')

        _cbc(_cac(party, 'PartyName'), 'Name', razon)
        self._add_party_address(_cac(party, 'PhysicalLocation'), 'Address',
                                muni_cod, muni_nom, dep_cod, dep_nom, direccion)

        pts = _cac(party, 'PartyTaxScheme')
        _cbc(pts, 'RegistrationName', razon)
        _cbc(pts, 'CompanyID', nit, schemeAgencyID='195', schemeAgencyName=DIAN_AGENCY,
             schemeID=dv, schemeName='31')
        _cbc(pts, 'TaxLevelCode', responsabilidad, listName=regimen)
        self._add_party_address(pts, 'RegistrationAddress', muni_cod, muni_nom, dep_cod, dep_nom, direccion)
        ts = _cac(pts, 'TaxScheme'); _cbc(ts, 'ID', '01'); _cbc(ts, 'Name', 'IVA')

        ple = _cac(party, 'PartyLegalEntity')
        _cbc(ple, 'RegistrationName', razon)
        _cbc(ple, 'CompanyID', nit, schemeAgencyID='195', schemeAgencyName=DIAN_AGENCY,
             schemeID=dv, schemeName='31')
        _cbc(_cac(ple, 'CorporateRegistrationScheme'), 'ID', self._t('prefijo'))

        contact = _cac(party, 'Contact')
        _cbc(contact, 'Telephone', self._t('telefono', '0000000'))
        _cbc(contact, 'ElectronicMail', self._t('email', 'facturacion@empresa.co'))

    def _add_customer(self, root):
        cliente = self.datos.get('cliente', {})
        customer = _cac(root, 'AccountingCustomerParty')
        tipo_persona = cliente.get('tipo_persona', 'natural')
        _cbc(customer, 'AdditionalAccountID', '2' if tipo_persona == 'natural' else '1')
        party = _cac(customer, 'Party')

        tipo_doc = str(cliente.get('tipo_documento', 'CC')).upper()
        doc_code = TIPO_DOC_SCHEME.get(tipo_doc, '13')
        id_attrs = {'schemeAgencyID': '195', 'schemeAgencyName': DIAN_AGENCY, 'schemeName': doc_code}

        _cbc(_cac(party, 'PartyIdentification'), 'ID', cliente.get('numero_documento', ''), **id_attrs)
        _cbc(_cac(party, 'PartyName'), 'Name', cliente.get('nombre', 'Consumidor Final'))
        self._add_party_address(_cac(party, 'PhysicalLocation'), 'Address',
                                cliente.get('municipio_codigo', '11001'), 'Bogotá, D.C.', '11', 'Bogotá',
                                cliente.get('direccion', 'No informado'))

        pts = _cac(party, 'PartyTaxScheme')
        _cbc(pts, 'RegistrationName', cliente.get('nombre', 'Consumidor Final'))
        _cbc(pts, 'CompanyID', cliente.get('numero_documento', ''), **id_attrs)
        _cbc(pts, 'TaxLevelCode', 'R-99-PN', listName='49')
        ts = _cac(pts, 'TaxScheme'); _cbc(ts, 'ID', 'ZZ'); _cbc(ts, 'Name', 'No aplica')

        ple = _cac(party, 'PartyLegalEntity')
        _cbc(ple, 'RegistrationName', cliente.get('nombre', 'Consumidor Final'))
        _cbc(ple, 'CompanyID', cliente.get('numero_documento', ''), **id_attrs)

        contact = _cac(party, 'Contact')
        _cbc(contact, 'Telephone', cliente.get('telefono', '0000000'))
        _cbc(contact, 'ElectronicMail', cliente.get('email', ''))

    def _add_payment_means(self, root):
        p = _cac(root, 'PaymentMeans')
        _cbc(p, 'ID', '1')
        _cbc(p, 'PaymentMeansCode', str(self.datos.get('metodo_pago', '10')))
        _cbc(p, 'PaymentDueDate', self.fecha)

    # ── Impuestos / Totales ───────────────────────────────────────────────────

    def _add_tax_totals(self, root):
        t = self.totales
        percent = Decimal('19.00') if self.responsable_iva else Decimal('0.00')
        self._tax_total_node(root, t['total_iva'], t['subtotal'], percent, '01', 'IVA')

    def _tax_total_node(self, parent, tax_amount, taxable, percent, tax_id, tax_name):
        tt = _cac(parent, 'TaxTotal')
        _cbc(tt, 'TaxAmount', str(_d(tax_amount)), currencyID='COP')
        sub = _cac(tt, 'TaxSubtotal')
        _cbc(sub, 'TaxableAmount', str(_d(taxable)), currencyID='COP')
        _cbc(sub, 'TaxAmount', str(_d(tax_amount)), currencyID='COP')
        cat = _cac(sub, 'TaxCategory')
        _cbc(cat, 'Percent', str(percent))
        sch = _cac(cat, 'TaxScheme'); _cbc(sch, 'ID', tax_id); _cbc(sch, 'Name', tax_name)

    def _add_legal_totals(self, root):
        t = self.totales
        # DebitNote usa RequestedMonetaryTotal; factura y nota crédito LegalMonetaryTotal.
        legal = _cac(root, self.cfg['total_elem'])
        _cbc(legal, 'LineExtensionAmount', str(t['subtotal']), currencyID='COP')
        _cbc(legal, 'TaxExclusiveAmount', str(t['subtotal']), currencyID='COP')
        _cbc(legal, 'TaxInclusiveAmount', str(t['total']), currencyID='COP')
        _cbc(legal, 'AllowanceTotalAmount', str(t['descuentos']), currencyID='COP')
        _cbc(legal, 'ChargeTotalAmount', '0.00', currencyID='COP')
        _cbc(legal, 'PayableAmount', str(t['total']), currencyID='COP')

    # ── Líneas (Invoice/CreditNote/DebitNote) ─────────────────────────────────

    def _add_lines(self, root):
        for idx, item in enumerate(self.datos.get('items', []), start=1):
            cant = _d(item.get('cantidad', 1)); precio = _d(item.get('precio_unitario', 0))
            desc = _d(item.get('descuento', 0))
            iva_pct = _d(item.get('impuesto_iva', 19)) if self.responsable_iva else Decimal('0')
            unidad = item.get('codigo_unidad', 'EA')
            base = (cant * precio) - desc
            iva_val = (base * iva_pct / Decimal('100'))

            line = _cac(root, self.cfg['line'])
            _cbc(line, 'ID', str(idx))
            _cbc(line, self.cfg['qty'], str(cant), unitCode=unidad)
            _cbc(line, 'LineExtensionAmount', str(_d(base)), currencyID='COP')
            self._tax_total_node(line, iva_val, base, iva_pct, '01', 'IVA')

            item_el = _cac(line, 'Item')
            _cbc(item_el, 'Description', item.get('descripcion', ''))
            _cbc(_cac(item_el, 'SellersItemIdentification'), 'ID', item.get('codigo', f'ITEM-{idx:03d}'))

            price = _cac(line, 'Price')
            _cbc(price, 'PriceAmount', str(precio), currencyID='COP')
            _cbc(price, 'BaseQuantity', str(cant), unitCode=unidad)
