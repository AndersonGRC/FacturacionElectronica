import os
from pathlib import Path
from dotenv import load_dotenv

# Cargar .env desde el mismo directorio que este archivo
load_dotenv(Path(__file__).parent / '.env')


class Config:
    # Flask
    SECRET_KEY = os.getenv('FLASK_SECRET_KEY', 'dev-insecure-key')
    JSON_SORT_KEYS = False

    # Sesión del panel UI
    SESSION_COOKIE_NAME     = 'dian_admin_session'
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SECURE   = True   # Solo HTTPS
    SESSION_COOKIE_SAMESITE = 'Lax'

    # Base de datos PostgreSQL
    DB_NAME     = os.getenv('DB_NAME', 'facturacion_dian')
    DB_USER     = os.getenv('DB_USER', 'postgres')
    DB_PASSWORD = os.getenv('DB_PASSWORD', '')
    DB_HOST     = os.getenv('DB_HOST', 'localhost')
    DB_PORT     = os.getenv('DB_PORT', '5432')

    # Seguridad
    MASTER_API_KEY = os.getenv('MASTER_API_KEY', '')   # Para rutas /admin
    FERNET_KEY     = os.getenv('FERNET_KEY', '')        # Para cifrar contraseñas de certificados

    # Celery / Redis
    CELERY_BROKER_URL     = os.getenv('CELERY_BROKER_URL',     'redis://localhost:6379/0')
    CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/1')

    # Almacenamiento de archivos (XML, response, PDF)
    STORAGE_BASE = os.getenv('STORAGE_BASE', str(Path(__file__).parent.parent / 'storage'))

    # DIAN — URLs por ambiente
    DIAN_URL_HABILITACION = 'https://vpfe-hab.dian.gov.co/WcfDianCustomerServices.svc'
    DIAN_URL_PRODUCCION   = 'https://vpfe.dian.gov.co/WcfDianCustomerServices.svc'

    # Timeout para llamadas HTTP a la DIAN
    DIAN_TIMEOUT = int(os.getenv('DIAN_TIMEOUT', '30'))

    # Plazo de gracia (minutos) para emisiones desde el portal: el documento queda
    # PENDIENTE y puede anularse antes de enviarse a la DIAN. Override por tenant
    # con la columna tenants.grace_minutos.
    GRACE_MINUTES = int(os.getenv('GRACE_MINUTES', '30'))

    # Política de reintentos Celery
    TASK_MAX_RETRIES  = 3
    TASK_RETRY_DELAYS = [60, 300, 900]   # segundos: 1 min, 5 min, 15 min
