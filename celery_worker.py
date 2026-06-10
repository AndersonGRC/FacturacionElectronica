"""
Punto de entrada para el worker Celery.

Uso:
  celery -A celery_worker.celery_app worker --loglevel=info --concurrency=2

Desde la raíz del proyecto (/var/www/FacturacionDIAN/):
  el worker añade app/ al sys.path para que los imports funcionen correctamente.
"""

import sys
import os
from pathlib import Path

# Añadir app/ al path
app_dir = Path(__file__).parent / 'app'
if str(app_dir) not in sys.path:
    sys.path.insert(0, str(app_dir))

from tasks.celery_app import celery_app

if __name__ == '__main__':
    celery_app.start()
