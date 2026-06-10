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

## Producción
Corre en el mismo VPS del ecosistema (`/var/www/FacturacionDIAN`) con los
servicios `facturacion-dian` (gunicorn) y `facturacion-dian-worker` (celery).
La app CyberShop lo consume vía `routes/factura_electronica.py`.

> Visión general del ecosistema: repo CyberShop → `app/docs/ECOSISTEMA.md`.
