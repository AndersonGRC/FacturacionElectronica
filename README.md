# FacturacionDIAN — Microservicio de Facturación Electrónica

Microservicio independiente del ecosistema CyberShop para emitir facturación
electrónica ante la DIAN (Colombia): construcción del XML UBL, firma digital,
CUFE, envío y consulta de estado.

## Stack
- **Flask** (API + UI admin en `app/`) + **Celery** (worker asíncrono, broker Redis)
- **PostgreSQL** (BD `facturacion_dian`, ver `schema.sql`)
- Certificados digitales por tenant en `storage/certificates/` (NUNCA en git)

## Estructura
| Ruta | Qué es |
|---|---|
| `app/app.py` / `app/routes/` | API y UI (dashboard, facturas, tenants) |
| `app/services/` | `xml_builder` (UBL), `signer` (firma), `dian_client` (envío), `storage` |
| `app/tasks/` | Tareas Celery (emisión asíncrona con reintentos) |
| `app/utils/` | `cufe` (cálculo CUFE), `encryption` (Fernet para certs) |
| `celery_worker.py` | Entry del worker |
| `schema.sql` | Esquema de la BD |
| `*.service` | Unidades systemd (API + worker) para producción |
| `docker-compose.yml` | Redis local |

## Configuración
Copiar `app/.env.example` → `app/.env` y llenar valores (BD, `MASTER_API_KEY`,
`FERNET_KEY`, broker Redis, certificado de pruebas). **El `.env`, los
certificados (`storage/`) y los logs están excluidos de git.**

## Pruebas
`tests/` corre contra una base PostgreSQL de **prueba** (el nombre debe terminar
en `_test`; si no, se saltan) y con la DIAN y la firma simuladas: ninguna prueba
se conecta a la DIAN.

```
DB_NAME=facturacion_dian_test DB_USER=... DB_PASSWORD=... python -m pytest tests
```

Cubren: un reintento reenvía el mismo XML (mismo número, fecha y CUFE); la
Regla 90 («procesado anteriormente») se resuelve consultando por CUFE; no se
numera fuera del rango ni con la resolución vencida; producción exige los datos
reales del emisor; el set de pruebas solo funciona en habilitación; todo POST
del portal exige token; la entrega al comprador (contenedor AttachedDocument,
asunto y .zip del Anexo 1.9 numeral 9.1); el comprador con NIT lleva su DV; un
subtotal de IVA por tarifa.

## Entrega al comprador
Al quedar ACEPTADO en producción, el documento se le envía al comprador desde el
correo del propio negocio (portal → «Correo de entrega»; la contraseña se guarda
cifrada con `FERNET_KEY`). Va un .zip con el contenedor electrónico
(`services/contenedor.py`: el XML validado + la respuesta de la DIAN, firmado) y
el PDF. En habilitación nunca se le escribe al comprador. Si el correo falla, el
documento sigue aceptado: queda el evento y se reenvía desde su detalle.

Requiere la columna `tenants.correo_entrega` (está en `schema.sql`):

```
sudo -u postgres psql -d facturacion_dian -c "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS correo_entrega JSONB DEFAULT '{}'::jsonb;"
```

## Producción
Corre en el mismo VPS del ecosistema (`/var/www/FacturacionDIAN`) con los
servicios `facturacion-dian` (gunicorn) y `facturacion-dian-worker` (celery).
La app CyberShop lo consume vía `routes/factura_electronica.py`.

> Visión general del ecosistema: repo CyberShop → `app/docs/ECOSISTEMA.md`.
