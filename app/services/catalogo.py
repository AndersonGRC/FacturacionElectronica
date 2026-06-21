"""
catalogo: trae el catálogo de productos de la tienda CyberShop del cliente.

Cada cliente tiene su PROPIA base de datos (modelo 1 DB por tenant). El tenant DIAN
guarda la URL base de su instancia CyberShop y su API key de sync; este servicio
llama al endpoint de sync de ESA instancia (que resuelve su propia DB) y normaliza
los productos para el formulario de emisión del portal.

Sin dependencias nuevas: urllib de la stdlib. Si el tenant no tiene la integración
configurada, retorna [] (la captura manual sigue funcionando).
"""

import json
import urllib.request
import urllib.error


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def obtener_productos(tenant_row: dict) -> list:
    base = (tenant_row.get('cybershop_base_url') or '').rstrip('/')
    key = tenant_row.get('cybershop_sync_key') or ''
    if not base or not key:
        return []

    url = f"{base}/api/v1/sync/products"
    req = urllib.request.Request(url, headers={'X-Sync-Key': key, 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode('utf-8') or '[]'
            data = json.loads(raw)
    except (urllib.error.URLError, OSError, ValueError):
        return []

    # El endpoint puede devolver una lista o {products|data|items: [...]}
    if isinstance(data, dict):
        data = data.get('products') or data.get('data') or data.get('items') or []
    if not isinstance(data, list):
        return []

    productos = []
    for p in data:
        if not isinstance(p, dict):
            continue
        desc = p.get('nombre') or p.get('descripcion') or p.get('name') or ''
        if not desc:
            continue
        productos.append({
            'codigo': str(p.get('codigo') or p.get('sku') or p.get('id') or ''),
            'descripcion': desc,
            'precio': _num(p.get('precio') if p.get('precio') is not None else
                           p.get('precio_unitario') or p.get('price')),
            'iva': _num(p.get('iva') if p.get('iva') is not None else
                        p.get('impuesto_iva') or p.get('porcentaje_iva')),
        })
    return productos
