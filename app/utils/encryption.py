import os
from cryptography.fernet import Fernet


def _get_fernet() -> Fernet:
    key = os.getenv('FERNET_KEY', '')
    if not key:
        raise RuntimeError(
            "FERNET_KEY no configurada. "
            "Generar con: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode())


def cifrar(texto_plano: str) -> str:
    """Cifra un texto con Fernet. Retorna string base64 URL-safe."""
    return _get_fernet().encrypt(texto_plano.encode('utf-8')).decode('utf-8')


def descifrar(texto_cifrado: str) -> str:
    """Descifra un texto cifrado con Fernet."""
    return _get_fernet().decrypt(texto_cifrado.encode('utf-8')).decode('utf-8')
