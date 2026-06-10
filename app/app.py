import sys
import os
import logging
from pathlib import Path
from flask import Flask, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix

# Garantizar que app/ está en el path (necesario cuando Gunicorn arranca desde app/)
app_dir = Path(__file__).parent
if str(app_dir) not in sys.path:
    sys.path.insert(0, str(app_dir))

from config import Config
from routes import register_blueprints


def create_app(config_class=Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_class)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )

    # ── Blueprints ────────────────────────────────────────────────────────────
    register_blueprints(app)

    # ── Health check ──────────────────────────────────────────────────────────
    @app.route('/health')
    def health():
        return jsonify({"status": "ok", "service": "facturacion-dian"})

    # ── Manejo de errores ─────────────────────────────────────────────────────
    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "Endpoint no encontrado"}), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return jsonify({"error": "Método HTTP no permitido"}), 405

    @app.errorhandler(500)
    def internal_error(e):
        app.logger.error(f"Error interno: {e}")
        return jsonify({"error": "Error interno del servidor"}), 500

    return app


# Punto de entrada para Gunicorn: gunicorn app:app
app = create_app()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5003, debug=False)
