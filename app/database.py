import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / '.env')


def get_db_connection():
    return psycopg2.connect(
        dbname=os.getenv('DB_NAME',     'facturacion_dian'),
        user=os.getenv('DB_USER',       'postgres'),
        password=os.getenv('DB_PASSWORD', ''),
        host=os.getenv('DB_HOST',       'localhost'),
        port=os.getenv('DB_PORT',       '5432'),
    )


@contextmanager
def get_db_cursor(dict_cursor=False):
    conn = get_db_connection()
    try:
        cursor_factory = psycopg2.extras.RealDictCursor if dict_cursor else None
        cur = conn.cursor(cursor_factory=cursor_factory)
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    finally:
        conn.close()
