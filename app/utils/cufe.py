"""
Cálculo del CUFE (Código Único de Factura Electrónica).

Fórmula según Anexo Técnico de Factura Electrónica DIAN v1.9, sección 7.3:

SHA384(
    NumFac + FecFac + HorFac +
    ValFac + CodImp1 + ValImp1 +
    CodImp2 + ValImp2 +
    CodImp3 + ValImp3 +
    ValTot + NitOFE + NumAdq +
    ClTec + TipoAmb
)

Donde:
  NumFac  = Número de factura (prefijo + consecutivo, ej: "SETT-1001")
  FecFac  = Fecha de emisión formato YYYY-MM-DD
  HorFac  = Hora de emisión formato HH:MM:SS
  ValFac  = Subtotal sin impuestos, exactamente 2 decimales
  CodImp1 = "01" (IVA)
  ValImp1 = Valor total del IVA, exactamente 2 decimales
  CodImp2 = "02" (Impuesto al consumo) — "00" si no aplica
  ValImp2 = Valor IC, exactamente 2 decimales — "0.00" si no aplica
  CodImp3 = "03" (ICA) — "00" si no aplica
  ValImp3 = Valor ICA, exactamente 2 decimales — "0.00" si no aplica
  ValTot  = Total con impuestos, exactamente 2 decimales
  NitOFE  = NIT del emisor sin dígito de verificación
  NumAdq  = Número de documento del comprador
  ClTec   = Clave técnica asignada por la DIAN al software
  TipoAmb = "2" habilitación | "1" producción
"""

import hashlib


def calcular_cufe(
    numero_factura: str,
    fecha_factura: str,
    hora_factura: str,
    valor_factura: float,
    cod_impuesto1: str,
    valor_impuesto1: float,
    cod_impuesto2: str,
    valor_impuesto2: float,
    cod_impuesto3: str,
    valor_impuesto3: float,
    valor_total: float,
    nit_emisor: str,
    num_doc_receptor: str,
    clave_tecnica: str,
    ambiente: str,
) -> str:
    """
    Calcula el CUFE como SHA384 en hexadecimal (96 caracteres).

    Los valores monetarios se formatean con exactamente 2 decimales.
    """
    cadena = (
        f"{numero_factura}"
        f"{fecha_factura}"
        f"{hora_factura}"
        f"{valor_factura:.2f}"
        f"{cod_impuesto1}"
        f"{valor_impuesto1:.2f}"
        f"{cod_impuesto2}"
        f"{valor_impuesto2:.2f}"
        f"{cod_impuesto3}"
        f"{valor_impuesto3:.2f}"
        f"{valor_total:.2f}"
        f"{nit_emisor}"
        f"{num_doc_receptor}"
        f"{clave_tecnica}"
        f"{ambiente}"
    )
    return hashlib.sha384(cadena.encode('utf-8')).hexdigest()


# Clave técnica de prueba para ambiente de habilitación DIAN
# Reemplazar con la clave real al registrar el software en el portal DIAN
CLAVE_TECNICA_HABILITACION = "fc8eac422eba16e22ffd8c6f94b3f40a6e38162c"
