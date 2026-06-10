"""
StorageManager: gestión de archivos del microservicio DIAN.

Los XMLs firmados, ApplicationResponses y PDFs (Fase 2) se guardan en
el sistema de archivos — NO como blobs en PostgreSQL — para preservar
la memoria de la base de datos y optimizar las consultas SQL.

Estructura de directorios:
  storage/documentos/{tenant_id}/{año}/{mes}/{numero_factura}_firmado.xml
  storage/documentos/{tenant_id}/{año}/{mes}/{numero_factura}_response.xml
  storage/certificates/{tenant_id}/cert.p12
"""

import os
from pathlib import Path
from datetime import datetime


class StorageManager:
    def __init__(self, tenant_id: str, base_path: str = None):
        self.tenant_id = tenant_id
        self.base      = Path(base_path or os.getenv('STORAGE_BASE', '/var/www/FacturacionDIAN/storage'))
        self._now      = datetime.now()

    def _get_doc_dir(self) -> Path:
        """Retorna (y crea si no existe) el directorio para documentos del mes actual."""
        directory = (
            self.base / 'documentos' / self.tenant_id /
            str(self._now.year) / f"{self._now.month:02d}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def get_cert_dir(self) -> Path:
        """Retorna (y crea si no existe) el directorio de certificados del tenant."""
        cert_dir = self.base / 'certificates' / self.tenant_id
        cert_dir.mkdir(parents=True, exist_ok=True)
        return cert_dir

    def guardar_xml(self, xml_bytes: bytes, numero_factura: str) -> str:
        """Guarda el XML UBL 2.1 firmado. Retorna la ruta absoluta como string."""
        path = self._get_doc_dir() / f"{numero_factura}_firmado.xml"
        path.write_bytes(xml_bytes)
        return str(path)

    def guardar_response(self, response_bytes: bytes, numero_factura: str) -> str:
        """Guarda el ApplicationResponse de la DIAN. Retorna la ruta absoluta."""
        path = self._get_doc_dir() / f"{numero_factura}_response.xml"
        path.write_bytes(response_bytes)
        return str(path)

    def guardar_pdf(self, pdf_bytes: bytes, numero_factura: str) -> str:
        """Guarda el PDF de representación gráfica. Retorna la ruta absoluta. (Fase 2)"""
        path = self._get_doc_dir() / f"{numero_factura}.pdf"
        path.write_bytes(pdf_bytes)
        return str(path)

    def leer_xml(self, xml_path: str) -> bytes:
        return Path(xml_path).read_bytes()

    def leer_response(self, response_path: str) -> bytes:
        return Path(response_path).read_bytes()
