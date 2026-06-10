"""
XMLBuilder: construye el XML UBL 2.1 para factura electrónica colombiana.

Referencia: Anexo Técnico de Factura Electrónica de Venta DIAN v1.9
Tipo de documento: Factura de Venta (InvoiceTypeCode = "01")
"""

from lxml import etree
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP


# ── Namespaces UBL 2.1 colombianos ────────────────────────────────────────────
NSMAP = {
    None:    'urn:oasis:names:specification:ubl:schema:xsd:Invoice-2',
    'cac':   'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
    'cbc':   'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
    'ext':   'urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2',
    'sts':   'http://www.dian.gov.co/contratos/facturaelectronica/v1/Structures',
    'xades': 'http://uri.etsi.org/01903/v1.3.2#',
    'ds':    'http://www.w3.org/2000/09/xmldsig#',
    'xsi':   'http://www.w3.org/2001/XMLSchema-instance',
}

NS_CBC  = NSMAP['cbc']
NS_CAC  = NSMAP['cac']
NS_EXT  = NSMAP['ext']

# Tipos de documento de identidad → schemeID DIAN
TIPO_DOC_SCHEME = {
    'CC':  '13',   # Cédula de ciudadanía
    'CE':  '22',   # Cédula de extranjería
    'NIT': '31',   # NIT
    'TI':  '12',   # Tarjeta de identidad
    'PA':  '41',   # Pasaporte
    'RC':  '11',   # Registro civil
}

TIPO_DOC_NOMBRE = {
    'CC':  'Cédula de ciudadanía',
    'CE':  'Cédula de extranjería',
    'NIT': 'NIT',
    'TI':  'Tarjeta de identidad',
    'PA':  'Pasaporte',
    'RC':  'Registro civil',
}


def _d(val) -> Decimal:
    """Convierte a Decimal con 2 decimales."""
    return Decimal(str(val)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _cbc(parent, tag: str, text: str = None, **attrs):
    """Crea un elemento cbc:Tag con texto y atributos opcionales."""
    el = etree.SubElement(parent, f'{{{NS_CBC}}}{tag}', **attrs)
    if text is not None:
        el.text = str(text)
    return el


def _cac(parent, tag: str):
    """Crea un elemento cac:Tag vacío."""
    return etree.SubElement(parent, f'{{{NS_CAC}}}{tag}')


def _ext(parent, tag: str):
    """Crea un elemento ext:Tag vacío."""
    return etree.SubElement(parent, f'{{{NS_EXT}}}{tag}')


class XMLBuilder:
    """
    Construye el XML UBL 2.1 de una factura electrónica colombiana.

    Args:
        tenant:         Dict con la configuración del emisor (tenant de la BD).
        datos:          Dict con el payload JSON del cliente.
        numero_factura: Número asignado (prefijo + consecutivo, ej: "SETT-1001").
        cufe:           CUFE calculado (SHA384, 96 chars hex).
    """

    def __init__(self, tenant: dict, datos: dict, numero_factura: str, cufe: str):
        self.tenant  = tenant
        self.datos   = datos
        self.numero  = numero_factura
        self.cufe    = cufe
        self.now     = datetime.now()
        self.totales = self._calcular_totales()

    # ── Entrada pública ───────────────────────────────────────────────────────

    def build(self) -> bytes:
        """Construye el XML completo. Retorna bytes UTF-8."""
        root = etree.Element('Invoice', nsmap=NSMAP)
        self._add_ubl_extensions(root)
        self._add_header(root)
        self._add_supplier(root)
        self._add_customer(root)
        self._add_payment_means(root)
        self._add_tax_totals(root)
        self._add_legal_totals(root)
        self._add_lines(root)
        return etree.tostring(
            root,
            xml_declaration=True,
            encoding='UTF-8',
            pretty_print=True
        )

    # ── Cálculos internos ─────────────────────────────────────────────────────

    def _calcular_totales(self) -> dict:
        items        = self.datos.get('items', [])
        subtotal     = Decimal('0')
        total_iva    = Decimal('0')
        total_ic     = Decimal('0')
        descuentos   = Decimal('0')

        for item in items:
            cant     = _d(item.get('cantidad', 1))
            precio   = _d(item.get('precio_unitario', 0))
            desc     = _d(item.get('descuento', 0))
            iva_pct  = _d(item.get('impuesto_iva', 19))

            base_item  = (cant * precio) - desc
            iva_item   = (base_item * iva_pct / Decimal('100'))
            subtotal  += base_item
            total_iva += iva_item
            descuentos += desc

        total = subtotal + total_iva + total_ic
        return {
            'subtotal':   subtotal,
            'total_iva':  total_iva,
            'total_ic':   total_ic,
            'descuentos': descuentos,
            'total':      total,
        }

    # ── Secciones del XML ────────────────────────────────────────────────────

    def _add_ubl_extensions(self, root):
        """Nodo reservado para la firma digital (lo rellena FirmadorDIAN)."""
        ubl_exts = _ext(root, 'UBLExtensions')
        ubl_ext  = _ext(ubl_exts, 'UBLExtension')
        _ext(ubl_ext, 'ExtensionContent')

    def _add_header(self, root):
        ambiente = self.tenant.get('ambiente', 'habilitacion')
        profile_exec_id = '2' if ambiente == 'habilitacion' else '1'

        _cbc(root, 'UBLVersionID',        'UBL 2.1')
        _cbc(root, 'CustomizationID',     '10')
        _cbc(root, 'ProfileID',           'DIAN 2.1')
        _cbc(root, 'ProfileExecutionID',  profile_exec_id)
        _cbc(root, 'ID',                  self.numero)
        _cbc(root, 'UUID',                self.cufe,
             schemeID=profile_exec_id,
             schemeName='CUFE-SHA384')
        _cbc(root, 'IssueDate',           self.now.strftime('%Y-%m-%d'))
        _cbc(root, 'IssueTime',           self.now.strftime('%H:%M:%S') + '-05:00')
        _cbc(root, 'DueDate',             self.now.strftime('%Y-%m-%d'))
        _cbc(root, 'InvoiceTypeCode',     '01',
             listAgencyID='6',
             listAgencyName='United Nations Economic Commission for Europe',
             listID='UN/ECE 1001 Invoice Status Code')
        _cbc(root, 'Note',                self.datos.get('notas', 'Factura electrónica'),
             languageID='es')
        _cbc(root, 'DocumentCurrencyCode', self.datos.get('moneda', 'COP'))
        _cbc(root, 'LineCountNumeric',    str(len(self.datos.get('items', []))))

    def _add_supplier(self, root):
        """AccountingSupplierParty — emisor de la factura (datos del tenant)."""
        supplier = _cac(root, 'AccountingSupplierParty')
        tipo_persona = self.tenant.get('tipo_persona_emisor', 'juridica')
        additional_id = '2' if tipo_persona == 'natural' else '1'
        _cbc(supplier, 'AdditionalAccountID', additional_id)

        party = _cac(supplier, 'Party')

        party_name = _cac(party, 'PartyName')
        _cbc(party_name, 'Name', self.tenant.get('razon_social', ''))

        # Dirección (simplificada — extender con datos reales del tenant si se tienen)
        phys_loc  = _cac(party, 'PhysicalLocation')
        address   = _cac(phys_loc, 'Address')
        _cbc(address, 'ID',                  '11001')  # Código DIVIPOLA Bogotá
        _cbc(address, 'CityName',            'Bogotá')
        _cbc(address, 'CountrySubentity',    'Cundinamarca')
        _cbc(address, 'CountrySubentityCode','11')
        country  = _cac(address, 'Country')
        _cbc(country, 'IdentificationCode',  'CO')
        _cbc(country, 'Name',               'Colombia', languageID='es')

        # Identificación fiscal (NIT)
        tax_scheme = _cac(party, 'PartyTaxScheme')
        _cbc(tax_scheme, 'RegistrationName', self.tenant.get('razon_social', ''))
        _cbc(tax_scheme, 'CompanyID',
             self.tenant.get('nit', ''),
             schemeAgencyID='195',
             schemeAgencyName='CO, DIAN (Dirección de Impuestos y Aduanas Nacionales)',
             schemeID=str(self.tenant.get('digito_verificacion', '0')),
             schemeName='31')
        _cbc(tax_scheme, 'TaxLevelCode', 'O-13', listName='48')  # Responsable de IVA

        reg_addr  = _cac(tax_scheme, 'RegistrationAddress')
        _cbc(reg_addr, 'ID',       '11001')
        _cbc(reg_addr, 'CityName', 'Bogotá')
        reg_country = _cac(reg_addr, 'Country')
        _cbc(reg_country, 'IdentificationCode', 'CO')
        _cbc(reg_country, 'Name', 'Colombia', languageID='es')

        # PartyLegalEntity solo para personas jurídicas
        if tipo_persona != 'natural':
            legal = _cac(party, 'PartyLegalEntity')
            _cbc(legal, 'RegistrationName', self.tenant.get('razon_social', ''))
            _cbc(legal, 'CompanyID',
                 self.tenant.get('nit', ''),
                 schemeAgencyID='195',
                 schemeAgencyName='CO, DIAN (Dirección de Impuestos y Aduanas Nacionales)',
                 schemeID=str(self.tenant.get('digito_verificacion', '0')),
                 schemeName='31')

    def _add_customer(self, root):
        """AccountingCustomerParty — comprador."""
        cliente = self.datos.get('cliente', {})
        customer = _cac(root, 'AccountingCustomerParty')

        tipo_persona = cliente.get('tipo_persona', 'natural')
        additional_account = '2' if tipo_persona == 'natural' else '1'
        _cbc(customer, 'AdditionalAccountID', additional_account)

        party    = _cac(customer, 'Party')
        party_id = _cac(party, 'PartyIdentification')

        tipo_doc = cliente.get('tipo_documento', 'CC').upper()
        scheme_id   = TIPO_DOC_SCHEME.get(tipo_doc, '13')
        scheme_name = TIPO_DOC_NOMBRE.get(tipo_doc, 'Cédula de ciudadanía')

        _cbc(party_id, 'ID',
             cliente.get('numero_documento', ''),
             schemeAgencyID='195',
             schemeAgencyName='CO, DIAN (Dirección de Impuestos y Aduanas Nacionales)',
             schemeID=scheme_id,
             schemeName=scheme_name)

        party_name = _cac(party, 'PartyName')
        _cbc(party_name, 'Name', cliente.get('nombre', ''))

        phys_loc = _cac(party, 'PhysicalLocation')
        address  = _cac(phys_loc, 'Address')
        _cbc(address, 'ID',                  cliente.get('municipio_codigo', '11001'))
        _cbc(address, 'AddressLine',         cliente.get('direccion', ''))
        _cbc(address, 'CountrySubentityCode','11')
        country = _cac(address, 'Country')
        _cbc(country, 'IdentificationCode', 'CO')
        _cbc(country, 'Name', 'Colombia', languageID='es')

        tax_scheme = _cac(party, 'PartyTaxScheme')
        _cbc(tax_scheme, 'RegistrationName', cliente.get('nombre', ''))
        _cbc(tax_scheme, 'CompanyID',
             cliente.get('numero_documento', ''),
             schemeAgencyID='195',
             schemeAgencyName='CO, DIAN (Dirección de Impuestos y Aduanas Nacionales)',
             schemeID=scheme_id,
             schemeName=scheme_name)
        _cbc(tax_scheme, 'TaxLevelCode', 'R-99-PN', listName='48')

        contact = _cac(party, 'Contact')
        _cbc(contact, 'Telephone',       cliente.get('telefono', ''))
        _cbc(contact, 'ElectronicMail',  cliente.get('email', ''))

        if tipo_persona == 'natural':
            nombre_split = cliente.get('nombre', '').split(' ', 1)
            person = _cac(party, 'Person')
            _cbc(person, 'FirstName',   nombre_split[0] if nombre_split else '')
            _cbc(person, 'FamilyName',  nombre_split[1] if len(nombre_split) > 1 else '')

    def _add_payment_means(self, root):
        payment = _cac(root, 'PaymentMeans')
        _cbc(payment, 'ID',               '1')
        _cbc(payment, 'PaymentMeansCode', self.datos.get('metodo_pago', '10'))
        _cbc(payment, 'PaymentDueDate',   self.now.strftime('%Y-%m-%d'))

    def _add_tax_totals(self, root):
        """Agrega TaxTotal por cada tipo de impuesto aplicado."""
        t = self.totales

        # IVA (CodImp = "01")
        if t['total_iva'] > 0:
            self._add_tax_total_node(
                root,
                tax_amount=t['total_iva'],
                taxable_amount=t['subtotal'],
                tax_percent=Decimal('19.00'),   # Porcentaje estándar IVA
                tax_id='01',
                tax_name='IVA',
            )

        # Impuesto al Consumo (CodImp = "02") — agregar si aplica
        if t['total_ic'] > 0:
            self._add_tax_total_node(
                root,
                tax_amount=t['total_ic'],
                taxable_amount=t['subtotal'],
                tax_percent=Decimal('8.00'),
                tax_id='02',
                tax_name='IC',
            )

    def _add_tax_total_node(self, parent, tax_amount: Decimal, taxable_amount: Decimal,
                             tax_percent: Decimal, tax_id: str, tax_name: str):
        tax_total = _cac(parent, 'TaxTotal')
        _cbc(tax_total, 'TaxAmount', str(tax_amount), currencyID='COP')

        tax_sub = _cac(tax_total, 'TaxSubtotal')
        _cbc(tax_sub, 'TaxableAmount', str(taxable_amount), currencyID='COP')
        _cbc(tax_sub, 'TaxAmount',     str(tax_amount),     currencyID='COP')

        tax_cat = _cac(tax_sub, 'TaxCategory')
        _cbc(tax_cat, 'Percent',    str(tax_percent))

        tax_scheme = _cac(tax_cat, 'TaxScheme')
        _cbc(tax_scheme, 'ID',   tax_id)
        _cbc(tax_scheme, 'Name', tax_name)

    def _add_legal_totals(self, root):
        t = self.totales
        legal = _cac(root, 'LegalMonetaryTotal')
        _cbc(legal, 'LineExtensionAmount', str(t['subtotal']),   currencyID='COP')
        _cbc(legal, 'TaxExclusiveAmount',  str(t['subtotal']),   currencyID='COP')
        _cbc(legal, 'TaxInclusiveAmount',  str(t['total']),      currencyID='COP')
        _cbc(legal, 'AllowanceTotalAmount',str(t['descuentos']), currencyID='COP')
        _cbc(legal, 'ChargeTotalAmount',   '0.00',               currencyID='COP')
        _cbc(legal, 'PayableAmount',       str(t['total']),      currencyID='COP')

    def _add_lines(self, root):
        for idx, item in enumerate(self.datos.get('items', []), start=1):
            cant    = _d(item.get('cantidad', 1))
            precio  = _d(item.get('precio_unitario', 0))
            desc    = _d(item.get('descuento', 0))
            iva_pct = _d(item.get('impuesto_iva', 19))
            unidad  = item.get('codigo_unidad', 'EA')
            subtotal = (cant * precio) - desc

            line = _cac(root, 'InvoiceLine')
            _cbc(line, 'ID',               str(idx))
            _cbc(line, 'InvoicedQuantity', str(cant), unitCode=unidad)
            _cbc(line, 'LineExtensionAmount', str(subtotal), currencyID='COP')

            item_el  = _cac(line, 'Item')
            _cbc(item_el, 'Description', item.get('descripcion', ''))

            sell_id = _cac(item_el, 'SellersItemIdentification')
            _cbc(sell_id, 'ID', item.get('codigo', f'ITEM-{idx:03d}'))

            cls_tax = _cac(item_el, 'ClassifiedTaxCategory')
            _cbc(cls_tax, 'Percent', str(iva_pct))
            tax_sch = _cac(cls_tax, 'TaxScheme')
            _cbc(tax_sch, 'ID',   '01')
            _cbc(tax_sch, 'Name', 'IVA')

            price_el = _cac(line, 'Price')
            _cbc(price_el, 'PriceAmount',   str(precio), currencyID='COP')
            _cbc(price_el, 'BaseQuantity',  '1',         unitCode=unidad)
