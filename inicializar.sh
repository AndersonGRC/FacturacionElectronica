#!/bin/bash
# ============================================================
# inicializar.sh — Script de puesta en marcha del microservicio
# Ejecutar UNA SOLA VEZ después de clonar/desplegar
# ============================================================

set -e

BASE_DIR="/var/www/FacturacionDIAN"
cd "$BASE_DIR"

echo ""
echo "=== Microservicio Facturación Electrónica DIAN ==="
echo ""

# 1. Verificar .env
if grep -q "CAMBIAR_" app/.env; then
    echo "[ERROR] El archivo app/.env tiene valores por defecto."
    echo "        Edita app/.env y reemplaza todos los valores 'CAMBIAR_*' antes de continuar."
    echo ""
    echo "  Generar FLASK_SECRET_KEY:"
    echo "    python3 -c \"import secrets; print(secrets.token_hex(32))\""
    echo ""
    echo "  Generar MASTER_API_KEY:"
    echo "    python3 -c \"import secrets; print(secrets.token_hex(32))\""
    echo ""
    echo "  Generar FERNET_KEY:"
    echo "    python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    echo ""
    exit 1
fi

# 2. Crear base de datos PostgreSQL
echo "[1/5] Creando base de datos PostgreSQL..."
source app/.env
createdb -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME" 2>/dev/null || true
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f schema.sql
echo "      OK — Base de datos '$DB_NAME' inicializada"

# 3. Levantar Redis con Docker
echo "[2/5] Levantando Redis..."
docker-compose up -d redis
echo "      OK — Redis en puerto 6379"

# 4. Generar certificado de prueba para habilitación (si no existe)
echo "[3/5] Verificando certificado de prueba..."
CERT_TEST_DIR="$BASE_DIR/storage/certificates/prueba"
mkdir -p "$CERT_TEST_DIR"
if [ ! -f "$CERT_TEST_DIR/cert.p12" ]; then
    openssl req -x509 -newkey rsa:2048 \
        -keyout "$CERT_TEST_DIR/key.pem" \
        -out "$CERT_TEST_DIR/cert.pem" \
        -days 365 -nodes \
        -subj "/CN=PruebaHabilitacion/O=MiEmpresa/C=CO" 2>/dev/null
    openssl pkcs12 -export \
        -out "$CERT_TEST_DIR/cert.p12" \
        -inkey "$CERT_TEST_DIR/key.pem" \
        -in "$CERT_TEST_DIR/cert.pem" \
        -passout pass:prueba123 2>/dev/null
    echo "      OK — Certificado de prueba en $CERT_TEST_DIR/cert.p12 (pass: prueba123)"
else
    echo "      OK — Certificado de prueba ya existe"
fi

# 5. Instalar servicios systemd
echo "[4/5] Instalando servicios systemd..."
cp facturacion-dian.service /etc/systemd/system/
cp facturacion-dian-worker.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable facturacion-dian facturacion-dian-worker
echo "      OK — Servicios instalados"

# 6. Iniciar servicios
echo "[5/5] Iniciando servicios..."
systemctl start facturacion-dian
systemctl start facturacion-dian-worker
sleep 2
systemctl status facturacion-dian --no-pager -l
systemctl status facturacion-dian-worker --no-pager -l

echo ""
echo "=== Instalación completada ==="
echo ""
echo "Próximos pasos:"
echo "  1. Registrar tenant de prueba:"
echo "     curl -X POST http://localhost:5003/api/v1/admin/tenants \\"
echo "       -H 'X-Master-Key: \$MASTER_API_KEY' \\"
echo "       -H 'Content-Type: application/json' \\"
echo "       -d '{\"nombre\":\"Mi Empresa\",\"nit\":\"900123456\",\"digito_verificacion\":7,\"razon_social\":\"Mi Empresa SAS\"}'"
echo ""
echo "  2. Subir certificado de prueba:"
echo "     curl -X POST http://localhost:5003/api/v1/admin/tenants/{id}/certificado \\"
echo "       -H 'X-Master-Key: \$MASTER_API_KEY' \\"
echo "       -F 'certificado=@$CERT_TEST_DIR/cert.p12' \\"
echo "       -F 'password=prueba123'"
echo ""
echo "  3. Enviar factura de prueba con el api_key retornado en el paso 1"
echo ""
