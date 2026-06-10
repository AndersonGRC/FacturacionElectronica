from routes.facturas import facturas_bp
from routes.admin import admin_bp
from routes.ui import ui_bp


def register_blueprints(app):
    app.register_blueprint(facturas_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(ui_bp)
