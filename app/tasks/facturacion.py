"""
Task Celery principal: procesar_factura

Orquesta el ciclo completo de una factura electrónica:
  1. Marcar PROCESANDO (UPDATE atómico anti-doble-procesamiento)
  2. Cargar configuración del tenant
  3. Obtener consecutivo atómico (función SQL)
  4. Calcular CUFE
  5. Construir XML UBL 2.1
  6. Firmar digitalmente
  7. Guardar XML firmado en storage/
  8. Enviar a DIAN
  9. Guardar ApplicationResponse en storage/
 10. Actualizar estado final en BD
 11. Registrar eventos en audit log
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Zona horaria de Colombia (UTC-5) — el servidor corre en UTC
TZ_CO = timezone(timedelta(hours=-5))

# Asegurar que app/ esté en el path
app_dir = Path(__file__).parent.parent
if str(app_dir) not in sys.path:
    sys.path.insert(0, str(app_dir))

from tasks.celery_app import celery_app
from database import get_db_cursor
from services.xml_builder import XMLBuilder, desglosar_linea
from services.signer import FirmadorDIAN
from services.dian_client import DIANClient
from services.storage import StorageManager
from utils.cufe import calcular_cufe, CLAVE_TECNICA_HABILITACION
from utils.encryption import descifrar

logger = logging.getLogger(__name__)

# Backoff entre reintentos: 1 min, 5 min, 15 min
RETRY_DELAYS = [60, 300, 900]


@celery_app.task(
    bind=True,
    max_retries=3,
    name='facturacion.procesar_factura',
    acks_late=True,
    ignore_result=False,
)
def procesar_factura(self, factura_id: str):
    """
    Procesa una factura electrónica de punta a punta.

    El UPDATE atómico en el paso 1 previene que dos workers procesen
    la misma factura simultáneamente (puede ocurrir si Celery reencola
    una tarea tras un crash de worker).
    """
    retry_num = self.request.retries

    try:
        # ── Paso 1: Marcar PROCESANDO (atómico) ──────────────────────────────
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute(
                """UPDATE facturas
                   SET estado = 'PROCESANDO',
                       intentos = intentos + 1
                   WHERE id = %s
                     AND estado IN ('PENDIENTE', 'ERROR')
                   RETURNING tenant_id, datos_json, intentos, numero_factura""",
                (factura_id,)
            )
            row = cur.fetchone()

        if not row:
            # Ya fue procesada por otro worker o no existe — salir silenciosamente
            logger.warning(f"Factura {factura_id} no disponible para procesar")
            return {'status': 'skipped', 'factura_id': factura_id}

        tenant_id  = str(row['tenant_id'])
        datos      = dict(row['datos_json'])
        intentos   = row['intentos']

        _registrar_evento(factura_id, 'PROCESANDO', f"Intento #{intentos}")
        logger.info(f"Procesando factura {factura_id}, intento #{intentos}")

        # ── Paso 2: Cargar tenant ──────────────────────────────────────────────
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute("SELECT * FROM tenants WHERE id = %s AND activo = TRUE",
                        (tenant_id,))
            tenant = cur.fetchone()

        if not tenant:
            raise ValueError(f"Tenant {tenant_id} no encontrado o inactivo")

        tenant = dict(tenant)

        if not tenant.get('cert_path') or not tenant.get('cert_password_enc'):
            raise ValueError(
                f"Tenant {tenant_id} no tiene certificado configurado. "
                "Suba el certificado .p12 mediante POST /api/v1/admin/tenants/{id}/certificado"
            )

        cert_password = descifrar(tenant['cert_password_enc'])

        # ── Paso 3: Consecutivo atómico (una sola vez por factura) ─────────────
        # Un reintento NO puede tomar otro consecutivo: dejaría un hueco en el
        # rango autorizado por la DIAN y, si el envío anterior sí llegó, crearía
        # un DUPLICADO con distinto número. Se persiste apenas se obtiene, así
        # que cualquier intento posterior reutiliza el mismo. Si la DIAN ya lo
        # recibió, rechazará el número repetido — que es el resultado seguro:
        # el problema se hace visible en vez de facturar dos veces.
        numero_factura = (row.get('numero_factura') or '').strip()
        if not numero_factura:
            with get_db_cursor() as cur:
                cur.execute("SELECT siguiente_consecutivo(%s)", (tenant_id,))
                consecutivo = cur.fetchone()[0]
            prefijo        = tenant.get('prefijo') or ''
            numero_factura = f"{prefijo}{consecutivo}"
            with get_db_cursor() as cur:
                cur.execute(
                    """UPDATE facturas SET numero_factura = %s
                       WHERE id = %s AND numero_factura IS NULL""",
                    (numero_factura, factura_id)
                )
            _registrar_evento(factura_id, 'NUMERADA',
                              f"Consecutivo asignado: {numero_factura}")
        else:
            logger.info(f"Factura {factura_id} reutiliza consecutivo {numero_factura}")

        # ── Paso 4: Fecha de emisión y CUFE ────────────────────────────────────
        now            = datetime.now(TZ_CO)
        fecha_emision  = now.strftime('%Y-%m-%d')
        hora_emision   = now.strftime('%H:%M:%S-05:00')

        # Calcular totales para el CUFE
        from decimal import Decimal, ROUND_HALF_UP

        def _d(v):
            return Decimal(str(v)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        # Responsable de IVA (regimen 48) vs No responsable (49). El desglose se
        # delega a xml_builder.desglosar_linea: era una copia paralela de la
        # misma fórmula y cualquier divergencia hace que el CUFE no calce con el
        # XML (rechazo de la DIAN).
        responsable_iva = str(tenant.get('regimen_codigo') or '48') == '48'
        subtotal  = Decimal('0')
        total_iva = Decimal('0')
        for item in datos.get('items', []):
            ln = desglosar_linea(item, responsable_iva)
            subtotal  += ln['base']
            total_iva += ln['iva']

        total = subtotal + total_iva

        ambiente       = tenant.get('ambiente', 'habilitacion')
        clave_tecnica  = tenant.get('clave_tecnica') or CLAVE_TECNICA_HABILITACION
        tipo_amb       = '2' if ambiente == 'habilitacion' else '1'
        nit_emisor     = tenant.get('nit', '')
        num_doc_recep  = datos.get('cliente', {}).get('numero_documento', '0')

        # CUFE (factura) usa la CLAVE TÉCNICA; CUDE (notas) usa el PIN del software.
        tipo_doc_actual = datos.get('tipo_documento', 'factura')
        clave_cufe = (tenant.get('software_pin') or clave_tecnica
                      if tipo_doc_actual in ('nota_credito', 'nota_debito')
                      else clave_tecnica)

        cufe = calcular_cufe(
            numero_factura  = numero_factura,
            fecha_factura   = fecha_emision,
            hora_factura    = hora_emision,
            valor_factura   = float(subtotal),
            cod_impuesto1   = '01',
            valor_impuesto1 = float(total_iva),
            cod_impuesto2   = '04',   # INC (impuesto nacional al consumo)
            valor_impuesto2 = 0.0,
            cod_impuesto3   = '03',   # ICA
            valor_impuesto3 = 0.0,
            valor_total     = float(total),
            nit_emisor      = nit_emisor,
            num_doc_receptor= num_doc_recep,
            clave_tecnica   = clave_cufe,
            ambiente        = tipo_amb,
        )

        logger.info(f"Número de factura: {numero_factura}, CUFE: {cufe[:20]}...")

        # ── Paso 5: Construir XML ──────────────────────────────────────────────
        builder   = XMLBuilder(tenant, datos, numero_factura, cufe,
                               fecha=fecha_emision, hora=hora_emision)
        xml_bytes = builder.build()

        # ── Paso 6: Firmar ─────────────────────────────────────────────────────
        firmador    = FirmadorDIAN(tenant['cert_path'], cert_password)
        xml_firmado = firmador.firmar(xml_bytes)

        _registrar_evento(factura_id, 'FIRMADA', f"XML firmado, {len(xml_firmado)} bytes")
        logger.info(f"XML firmado para {factura_id}")

        # ── Paso 7: Guardar XML ────────────────────────────────────────────────
        storage  = StorageManager(tenant_id)
        xml_path = storage.guardar_xml(xml_firmado, numero_factura)

        # ── Paso 8: Enviar a DIAN ─────────────────────────────────────────────
        _registrar_evento(factura_id, 'ENVIADA',
                          f"Enviando a DIAN ({ambiente})")

        dian    = DIANClient(
            ambiente      = ambiente,
            nit_emisor    = nit_emisor,
            clave_tecnica = clave_tecnica,
            software_id   = tenant.get('software_id'),
            timeout       = int(os.getenv('DIAN_TIMEOUT', '30')),
            cert_path     = tenant['cert_path'],
            cert_password = cert_password,
        )
        test_set_id = tenant.get('test_set_id')
        if ambiente == 'habilitacion' and test_set_id:
            # SET DE PRUEBAS: SendTestSetAsync con el TestSetId (la DIAN solo cuenta
            # los documentos del set si se envían así). Es asíncrono → ZipKey y luego
            # GetStatusZip para el veredicto.
            envio   = dian.enviar_test_set(xml_firmado, numero_factura, test_set_id)
            zip_key = envio.get('zip_key')
            with get_db_cursor() as cur:
                cur.execute("UPDATE facturas SET zip_key=%s WHERE id=%s", (zip_key, factura_id))
            _registrar_evento(factura_id, 'ENVIADA',
                              f"Set de pruebas (TestSetId). ZipKey={zip_key or '—'}")
            if not zip_key:
                raise ValueError("DIAN no devolvió ZipKey en SendTestSetAsync: "
                                 + '; '.join(envio.get('errors') or ['sin detalle']))
            resultado = None
            for _ in range(10):
                time.sleep(6)
                r = dian.obtener_estado_documento(zip_key)
                if r.get('is_valid') or (r.get('status_code') or '').strip():
                    resultado = r
                    break
            if resultado is None:
                # Aún en proceso en la DIAN — dejar PROCESANDO; se puede reconsultar luego
                with get_db_cursor() as cur:
                    cur.execute("""UPDATE facturas SET estado='PROCESANDO', numero_factura=%s,
                                   cufe=%s, xml_path=%s, zip_key=%s,
                                   error_mensaje='En proceso en la DIAN (set de pruebas)',
                                   actualizado_en=NOW() WHERE id=%s""",
                                (numero_factura, cufe, xml_path, zip_key, factura_id))
                _registrar_evento(factura_id, 'PROCESANDO',
                                  f"En proceso en la DIAN. ZipKey={zip_key}")
                return {'status': 'PROCESANDO', 'factura_id': factura_id, 'zip_key': zip_key}
        else:
            resultado = dian.enviar_factura(
                xml_firmado    = xml_firmado,
                numero_factura = numero_factura,
                token_dian     = tenant.get('token_dian'),
            )

        # ── Paso 9: Guardar ApplicationResponse ────────────────────────────────
        response_path = storage.guardar_response(
            resultado['raw_xml'], numero_factura
        )

        # ── Paso 10: Actualizar BD ─────────────────────────────────────────────
        estado_final = 'ACEPTADA' if resultado['is_valid'] else 'RECHAZADA'
        error_msg    = '; '.join(resultado['errors']) if resultado['errors'] else None
        cufe_final   = resultado.get('cufe_dian') or cufe  # DIAN puede confirmar el CUFE

        with get_db_cursor() as cur:
            cur.execute(
                """UPDATE facturas
                   SET estado          = %s,
                       numero_factura  = %s,
                       cufe            = %s,
                       xml_path        = %s,
                       response_path   = %s,
                       error_mensaje   = %s,
                       actualizado_en  = NOW()
                   WHERE id = %s""",
                (estado_final, numero_factura, cufe_final,
                 xml_path, response_path, error_msg, factura_id)
            )

        _registrar_evento(
            factura_id, estado_final,
            resultado.get('description') or f"CUFE: {cufe_final[:30]}..."
        )

        # ── Paso 11: Representación gráfica (PDF) — no debe romper el flujo ─────
        try:
            from services.pdf_builder import generar_pdf
            pdf_bytes = generar_pdf(tenant, datos, numero_factura, cufe_final,
                                    fecha_emision, hora_emision, estado_final)
            pdf_path = storage.guardar_pdf(pdf_bytes, numero_factura)
            with get_db_cursor() as cur:
                cur.execute("UPDATE facturas SET pdf_path = %s WHERE id = %s",
                            (pdf_path, factura_id))
        except Exception as e:
            logger.warning(f"No se pudo generar el PDF para {factura_id}: {e}")

        logger.info(f"Factura {factura_id} → {estado_final} ({numero_factura})")
        return {
            'status':          estado_final,
            'factura_id':      factura_id,
            'numero_factura':  numero_factura,
            'cufe':            cufe_final,
        }

    except Exception as exc:
        logger.error(f"Error procesando factura {factura_id} (intento {retry_num}): {exc}")
        _registrar_evento(factura_id, 'ERROR', str(exc)[:500])

        # Actualizar estado a ERROR con el mensaje
        try:
            with get_db_cursor() as cur:
                cur.execute(
                    """UPDATE facturas
                       SET estado         = 'ERROR',
                           error_mensaje  = %s,
                           actualizado_en = NOW()
                       WHERE id = %s""",
                    (str(exc)[:500], factura_id)
                )
        except Exception as db_err:
            logger.error(f"No se pudo actualizar estado ERROR: {db_err}")

        if retry_num < 3:
            countdown = RETRY_DELAYS[retry_num]
            logger.info(f"Reintentando factura {factura_id} en {countdown}s")
            _registrar_evento(factura_id, 'REINTENTO',
                              f"Reintento {retry_num + 1}/3 en {countdown}s: {exc}")
            raise self.retry(exc=exc, countdown=countdown)

        # Reintentos agotados — queda en ERROR permanente
        logger.error(f"Factura {factura_id} agotó 3 reintentos. Error final: {exc}")
        _registrar_evento(factura_id, 'ERROR',
                          f"Agotados 3 reintentos. Error final: {exc}")
        return {'status': 'error', 'factura_id': factura_id, 'error': str(exc)}


def _registrar_evento(factura_id: str, evento: str, detalle: str = None):
    """Inserta una entrada en el audit log. Falla silenciosamente."""
    try:
        with get_db_cursor() as cur:
            cur.execute(
                """INSERT INTO factura_eventos (factura_id, evento, detalle)
                   VALUES (%s, %s, %s)""",
                (factura_id, evento, detalle)
            )
    except Exception as e:
        logger.warning(f"No se pudo registrar evento '{evento}' para {factura_id}: {e}")


@celery_app.task(name='tasks.facturacion.verificar_facturas_programadas')
def verificar_facturas_programadas():
    """
    Tarea periódica (beat, cada 5 min).
    Busca facturas PENDIENTE cuya fecha procesar_en ya llegó y las encola.
    """
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("""
            SELECT id FROM facturas
            WHERE estado    = 'PENDIENTE'
              AND procesar_en <= NOW()
              AND intentos   < 3
              AND requiere_aprobacion = FALSE
        """)
        listas = cur.fetchall()

    if not listas:
        return {'encoladas': 0}

    for row in listas:
        factura_id = str(row['id'])
        task = procesar_factura.delay(factura_id)
        try:
            with get_db_cursor() as cur:
                cur.execute(
                    "UPDATE facturas SET celery_task_id = %s WHERE id = %s",
                    (task.id, factura_id)
                )
        except Exception:
            pass
        _registrar_evento(factura_id, 'ENCOLADA',
                          f"Enviada a DIAN por tarea programada. task_id={task.id}")

    logger.info(f"verificar_facturas_programadas: {len(listas)} factura(s) encolada(s)")
    return {'encoladas': len(listas)}


# Minutos que debe llevar una factura en PROCESANDO para considerarla trabada.
RECONCILIAR_UMBRAL_MIN = int(os.getenv('FE_RECONCILIAR_UMBRAL_MIN', '30'))
# Apagado por defecto: consultar el estado en la DIAN es tráfico saliente y se
# habilita solo con autorización explícita del dueño.
RECONCILIAR_ENABLED = os.getenv('FE_RECONCILIAR_ENABLED', 'false').lower() == 'true'


@celery_app.task(name='tasks.facturacion.reconciliar_facturas_procesando')
def reconciliar_facturas_procesando():
    """Rescata facturas atascadas en PROCESANDO.

    Antes no existía: `verificar_facturas_programadas` solo mira PENDIENTE, así
    que si el worker moría —o si el set de pruebas agotaba su espera de 60 s—
    el documento quedaba en PROCESANDO para siempre, sin reintento ni consulta.

    La regla que evita duplicar es distinguir tres casos:
      · sin numero_factura  → nunca se numeró ni se envió  → vuelve a PENDIENTE.
      · con zip_key         → sí salió; se RECONSULTA su estado (nunca se
                              reenvía) y se cierra con el veredicto de la DIAN.
      · con numero pero sin zip_key → ambiguo: pudo haber salido. NO se toca;
                              se marca para revisión manual, porque reenviar
                              aquí es exactamente lo que genera un duplicado.
    """
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("""
            SELECT id, tenant_id, numero_factura, zip_key
            FROM facturas
            WHERE estado = 'PROCESANDO'
              AND actualizado_en < NOW() - (%s * INTERVAL '1 minute')
            ORDER BY actualizado_en
            LIMIT 50
        """, (RECONCILIAR_UMBRAL_MIN,))
        trabadas = cur.fetchall()

    reencoladas = consultadas = revision = 0

    for row in trabadas:
        factura_id = str(row['id'])
        numero     = (row['numero_factura'] or '').strip()
        zip_key    = (row['zip_key'] or '').strip()

        if not numero:
            # Nunca se numeró → no pudo haber llegado a la DIAN. Seguro.
            with get_db_cursor() as cur:
                cur.execute(
                    """UPDATE facturas
                       SET estado = 'PENDIENTE', actualizado_en = NOW()
                       WHERE id = %s AND estado = 'PROCESANDO'""",
                    (factura_id,)
                )
            _registrar_evento(factura_id, 'PENDIENTE',
                              'Rescatada: estaba en PROCESANDO sin numerar')
            reencoladas += 1
            continue

        if zip_key and RECONCILIAR_ENABLED:
            consultadas += _reconsultar_zip(factura_id, str(row['tenant_id']), zip_key)
            continue

        _registrar_evento(
            factura_id, 'REVISION',
            f"Trabada en PROCESANDO con número {numero} y "
            f"{'zip_key ' + zip_key if zip_key else 'sin zip_key'}. "
            "Requiere revisión manual: reenviar podría duplicar."
        )
        revision += 1

    if trabadas:
        logger.info(
            "reconciliar_facturas_procesando: %d trabada(s) — %d reencolada(s), "
            "%d consultada(s), %d para revisión",
            len(trabadas), reencoladas, consultadas, revision
        )
    return {'trabadas': len(trabadas), 'reencoladas': reencoladas,
            'consultadas': consultadas, 'revision': revision}


def _reconsultar_zip(factura_id: str, tenant_id: str, zip_key: str) -> int:
    """Pregunta a la DIAN por el veredicto de un envío ya realizado.

    Es una consulta de estado (GetStatusZip), nunca un reenvío: no puede crear
    un documento nuevo. Aun así queda tras `FE_RECONCILIAR_ENABLED`.
    """
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute("SELECT * FROM tenants WHERE id = %s", (tenant_id,))
            tenant = cur.fetchone()
        if not tenant:
            return 0
        tenant = dict(tenant)

        dian = DIANClient(
            ambiente      = tenant.get('ambiente', 'habilitacion'),
            nit_emisor    = tenant.get('nit', ''),
            clave_tecnica = tenant.get('clave_tecnica') or CLAVE_TECNICA_HABILITACION,
            software_id   = tenant.get('software_id'),
            timeout       = int(os.getenv('DIAN_TIMEOUT', '30')),
            cert_path     = tenant.get('cert_path'),
            cert_password = descifrar(tenant['cert_password_enc']) if tenant.get('cert_password_enc') else None,
        )
        r = dian.obtener_estado_documento(zip_key)

        if not (r.get('is_valid') or (r.get('status_code') or '').strip()):
            return 0   # sigue en trámite; se reintenta en el próximo barrido

        estado_final = 'ACEPTADA' if r.get('is_valid') else 'RECHAZADA'
        mensaje = '; '.join(r.get('errors') or [])[:500] or None
        with get_db_cursor() as cur:
            cur.execute(
                """UPDATE facturas
                   SET estado = %s, error_mensaje = %s, actualizado_en = NOW()
                   WHERE id = %s AND estado = 'PROCESANDO'""",
                (estado_final, mensaje, factura_id)
            )
        _registrar_evento(factura_id, estado_final,
                          f"Reconsulta por ZipKey={zip_key}: {mensaje or 'sin observaciones'}")
        return 1
    except Exception as exc:
        logger.warning(f"No se pudo reconsultar {factura_id} (zip {zip_key}): {exc}")
        return 0
