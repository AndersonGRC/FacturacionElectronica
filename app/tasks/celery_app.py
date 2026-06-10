import os
import sys
from pathlib import Path
from celery import Celery
from dotenv import load_dotenv

# Garantizar que app/ esté en el path cuando el worker se inicia desde la raíz
app_dir = Path(__file__).parent.parent
if str(app_dir) not in sys.path:
    sys.path.insert(0, str(app_dir))

load_dotenv(app_dir / '.env')


def make_celery(app_name: str = 'facturacion_dian') -> Celery:
    celery = Celery(
        app_name,
        broker=os.getenv('CELERY_BROKER_URL',     'redis://localhost:6379/0'),
        backend=os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/1'),
        include=['tasks.facturacion'],
    )

    celery.conf.update(
        # Serialización
        task_serializer='json',
        result_serializer='json',
        accept_content=['json'],

        # Zona horaria
        timezone='America/Bogota',
        enable_utc=True,

        # Confiabilidad
        # ack_late: el mensaje NO se ack hasta que la tarea complete.
        # Si el worker muere, el mensaje vuelve a la cola y otro worker lo reintenta.
        task_acks_late=True,
        task_reject_on_worker_lost=True,

        # prefetch=1: el worker no toma otra tarea hasta terminar la actual.
        # Las tareas de facturación son pesadas (firma + HTTP DIAN).
        worker_prefetch_multiplier=1,

        # Visibilidad del estado
        task_track_started=True,

        # Resultados: expirar a las 24h (solo se usan para monitoreo)
        result_expires=86400,

        # Beat: revisar facturas listas para enviar cada 5 minutos
        beat_schedule={
            'verificar-facturas-programadas': {
                'task':     'tasks.facturacion.verificar_facturas_programadas',
                'schedule': 300.0,   # cada 5 minutos
            },
        },
    )

    return celery


celery_app = make_celery()
