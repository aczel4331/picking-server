"""
server.py — Sistema de Picking integrado con MercadoLibre (Uruguay)
====================================================================
VERSION: 2.1.0 — Auto-token + Persistencia Railway
Deploy en Railway. Variables de entorno requeridas:
  ML_APP_ID      → App ID de tu aplicación ML
  ML_SECRET_KEY  → Secret Key de tu aplicación ML
  APP_SECRET_KEY → Clave para sesiones Flask (cualquier string largo)
  PICKING_API_KEY → Clave interna para sincronización con app_deposito.py

Flujo OAuth2 ML:
  1. Usuario va a /auth/login  →  redirige a ML
  2. ML redirige a /auth/callback con ?code=XXX
  3. Se intercambia por access_token + refresh_token
  4. Los tokens se guardan y se auto-renuevan cada 6 horas
"""

SERVER_VERSION = "2.1.0"

import os, json, threading, time, requests
from datetime import datetime, timedelta
from flask import (Flask, jsonify, request, render_template_string,
                   session, redirect, url_for, Response)
from functools import wraps

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET_KEY", "picking-dev-secret-2024")
_lock = threading.Lock()

# ── Templates HTML (servidos desde la carpeta templates/) ─────────────────────
# Los HTML viven en archivos separados para mantener el server.py limpio.
# Se cachean en memoria tras la primera lectura. Para forzar recarga en
# desarrollo, definir la variable de entorno TEMPLATES_NO_CACHE=1.
_TEMPLATES_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
_template_cache  = {}
_TEMPLATES_NO_CACHE = os.environ.get("TEMPLATES_NO_CACHE", "").strip() == "1"

def _leer_template(nombre):
    """Lee un archivo HTML de templates/ y lo devuelve como respuesta."""
    if not _TEMPLATES_NO_CACHE and nombre in _template_cache:
        return _template_cache[nombre]
    ruta = os.path.join(_TEMPLATES_DIR, nombre)
    try:
        with open(ruta, encoding="utf-8") as f:
            html = f.read()
        if not _TEMPLATES_NO_CACHE:
            _template_cache[nombre] = html
        return html
    except FileNotFoundError:
        return (f"<h1>Error: template '{nombre}' no encontrado</h1>"
                f"<p>Asegurate de que la carpeta <code>templates/</code> esté "
                f"en el repositorio junto a server.py.</p>"), 500


# ── Config ML ─────────────────────────────────────────────────────────────────
ML_APP_ID     = os.environ.get("ML_APP_ID", "")
ML_SECRET_KEY = os.environ.get("ML_SECRET_KEY", "")
ML_SITE_ID    = "MLU"   # Uruguay
ML_AUTH_URL   = "https://auth.mercadolibre.com.uy"
ML_API_URL    = "https://api.mercadolibre.com"
# Redirect URI fijo — debe coincidir EXACTAMENTE con el configurado en ML Developers
ML_REDIRECT   = os.environ.get(
    "ML_REDIRECT_URI",
    "https://picking-server-production.up.railway.app/auth/callback"
)

# ── Clave interna (sync con app_deposito.py) ──────────────────────────────────
API_KEY = os.environ.get("PICKING_API_KEY", "everest2024")

# ── Directorio de datos persistente ───────────────────────────────────────────
# En Railway, montar un Volume y definir la variable DATA_DIR con su ruta
# (ej: /data). Los archivos guardados ahí SOBREVIVEN redeploys y reinicios.
# Si no se define DATA_DIR, usa /tmp (VOLÁTIL — se borra en cada redeploy).
def _resolver_data_dir():
    candidato = os.environ.get("DATA_DIR", "").strip()

    if not candidato:
        print("[DATA] ⚠ DATA_DIR no definido — usando /tmp (VOLÁTIL, se pierde en redeploy)")
        print("[DATA] ⚠ Para persistencia real: montá un Railway Volume y definí DATA_DIR=/data")
        os.makedirs("/tmp", exist_ok=True)
        return "/tmp"

    try:
        os.makedirs(candidato, exist_ok=True)
        # Probar que se puede escribir
        test = os.path.join(candidato, ".write_test")
        with open(test, "w") as f:
            f.write("ok")
        os.remove(test)
        print(f"[DATA] ✅ Persistencia activa en: {candidato}")
        return candidato
    except Exception as e:
        print(f"[DATA] ⚠ No se pudo usar '{candidato}' ({e}) — usando /tmp (VOLÁTIL)")
        os.makedirs("/tmp", exist_ok=True)
        return "/tmp"

DATA_DIR        = _resolver_data_dir()
LOTE_PATH       = os.path.join(DATA_DIR, "lote_estado.json")
ML_TOKENS_PATH  = os.path.join(DATA_DIR, "ml_tokens.json")

# ── Clave Railway API para persistir tokens ───────────────────────────────────
# Railway permite actualizar variables de entorno via su API REST
RAILWAY_API_TOKEN = os.environ.get("RAILWAY_API_TOKEN", "")
RAILWAY_PROJECT_ID = os.environ.get("RAILWAY_PROJECT_ID", "")
RAILWAY_SERVICE_ID = os.environ.get("RAILWAY_SERVICE_ID", "")
RAILWAY_ENV_ID     = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")
# Nombre de la variable donde guardamos los tokens
ML_TOKENS_ENV_KEY  = "ML_TOKENS_JSON"

# ── Estado en memoria ─────────────────────────────────────────────────────────
_cuentas        = {}
_tokens         = {}
_cuenta_activa  = "cuenta_0"
_pedidos_ml     = {}
_estado = {
    "fase": 1, "grupos": [], "colecta": {}, "colecta_completa": False,
    "total_skus": 0, "total_uds": 0, "ultima_actualizacion": "", "cargado": False,
    # Fase 2: pedidos del lote { order_id: {comprador, shipping_id, items:[{sku,req}], _cuenta} }
    "pedidos": {},
    # order_ids cuya etiqueta ya fue marcada como impresa en esta sesion de fase 2
    "etiquetas_impresas": [],
}
_colectores          = {}
# Cache de etiquetas PDF: { order_id: { pdf: bytes, ts: str, shipping_id: str } }
_etiquetas_cache     = {}
# Cola de pedidos pendientes de descarga de etiqueta
_cola_etiquetas      = []   # [ order_id, ... ]
_ultimo_refresh_pedidos = None
_sku_db              = {}
_SKU_DB_ENV_KEY      = "SKU_DB_JSON"
_pkce_store          = {}


def _ts():
    return datetime.now().strftime("%d/%m %H:%M:%S")


# ── Persistencia de tokens ─────────────────────────────────────────────────────

def _cargar_tokens_persistidos():
    """
    Carga los tokens guardados en la variable de entorno ML_TOKENS_JSON.
    Esto permite que el servidor sobreviva reinicios de Railway sin perder
    la sesion de MercadoLibre.
    """
    global _cuentas, _tokens
    raw = os.environ.get(ML_TOKENS_ENV_KEY, "").strip()
    if not raw:
        return
    try:
        data = json.loads(raw)
        for cid, tok in data.items():
            # Convertir expires_at de string ISO a datetime
            if tok.get("expires_at") and isinstance(tok["expires_at"], str):
                try:
                    tok["expires_at"] = datetime.fromisoformat(tok["expires_at"])
                except Exception:
                    tok["expires_at"] = datetime.now() + timedelta(hours=1)
            _cuentas[cid] = tok
        if "cuenta_0" in _cuentas:
            _tokens.update(_cuentas["cuenta_0"])
        print(f"[TOKEN] Tokens cargados: {list(_cuentas.keys())}")
    except Exception as e:
        print(f"[TOKEN] Error cargando tokens: {e}")


def _serializar_cuentas():
    """Serializa _cuentas a JSON, convirtiendo datetime a ISO string."""
    data = {}
    for cid, tok in _cuentas.items():
        t = dict(tok)
        if isinstance(t.get("expires_at"), datetime):
            t["expires_at"] = t["expires_at"].isoformat()
        data[cid] = t
    return json.dumps(data, ensure_ascii=False)


def _persistir_tokens_railway():
    """
    Guarda los tokens en el directorio de datos persistente (DATA_DIR).
    Si hay un Railway Volume montado, sobreviven redeploys y reinicios.
    """
    _persistir_tokens_local()


def _persistir_tokens_local():
    """Guarda tokens en el archivo persistente (DATA_DIR o /tmp)."""
    try:
        data = _serializar_cuentas()
        with open(ML_TOKENS_PATH, "w") as f:
            f.write(data)
        print(f"[TOKEN] Tokens guardados en {ML_TOKENS_PATH}")
        # Tambien actualizar os.environ para que _cargar_tokens_persistidos()
        # los encuentre si se llama de nuevo en la misma sesion
        os.environ[ML_TOKENS_ENV_KEY] = data
    except Exception as e:
        print(f"[TOKEN] Error guardando: {e}")


def _cargar_tokens_local():
    """Carga tokens del archivo persistente (fallback)."""
    global _cuentas, _tokens
    try:
        path = ML_TOKENS_PATH
        if os.path.exists(path):
            with open(path) as f:
                data = json.loads(f.read())
            for cid, tok in data.items():
                if tok.get("expires_at") and isinstance(tok["expires_at"], str):
                    try:
                        tok["expires_at"] = datetime.fromisoformat(tok["expires_at"])
                    except Exception:
                        tok["expires_at"] = datetime.now() + timedelta(hours=1)
                _cuentas[cid] = tok
            if "cuenta_0" in _cuentas:
                _tokens.update(_cuentas["cuenta_0"])
            print(f"[TOKEN] Tokens cargados desde archivo local: {list(_cuentas.keys())}")
    except Exception as e:
        print(f"[TOKEN] Error cargando local: {e}")


def _guardar_tokens():
    """Guarda tokens en Railway y/o archivo local."""
    threading.Thread(target=_persistir_tokens_railway, daemon=True).start()




# ── Descarga automatica de etiquetas ─────────────────────────────────────────

def _descargar_etiqueta_bg(order_id, reintentos=3):
    """
    Descarga el PDF de una etiqueta en background y lo guarda en cache.
    Se llama automaticamente al recibir una nueva venta via webhook ML.
    """
    import time as _time

    for intento in range(reintentos):
        try:
            with _lock:
                pedido = _pedidos_ml.get(order_id)

            if not pedido:
                print(f"[ETIQUETA] Pedido {order_id} no encontrado, esperando...")
                _time.sleep(5)
                continue

            shipping_id = pedido.get("shipping_id","")
            if not shipping_id:
                print(f"[ETIQUETA] Pedido {order_id} sin shipping_id aun, esperando...")
                _time.sleep(8)
                continue

            cuenta_id = pedido.get("_cuenta", "cuenta_0")
            at = _cuentas.get(cuenta_id, {}).get("access_token","")
            if not at:
                print(f"[ETIQUETA] Sin token para {cuenta_id} — no hay sesion ML activa")
                # Remover de la cola para no reintentar sin token
                if order_id in _cola_etiquetas:
                    _cola_etiquetas.remove(order_id)
                return

            # Verificar estado del envio
            r_ship = requests.get(
                f"{ML_API_URL}/shipments/{shipping_id}",
                headers={"Authorization": f"Bearer {at}"},
                timeout=10)

            if r_ship.status_code != 200:
                print(f"[ETIQUETA] No se pudo obtener shipment {shipping_id}: {r_ship.status_code}")
                _time.sleep(10)
                continue

            ship_data  = r_ship.json()
            estado     = ship_data.get("status","")
            substatus  = ship_data.get("substatus","")
            logistic   = ship_data.get("logistic_type","") or                          (ship_data.get("logistic") or {}).get("type","")

            # Actualizar logistica y estado en el pedido
            with _lock:
                if order_id in _pedidos_ml:
                    _pedidos_ml[order_id]["logistica"]    = logistic
                    _pedidos_ml[order_id]["estado_envio"] = estado

            # Solo intentar descargar si esta en estado correcto
            if estado not in ("ready_to_ship", "handling", "pending", ""):
                print(f"[ETIQUETA] Pedido {order_id} en estado '{estado}' — no disponible aun")
                if estado in ("delivered", "shipped", "cancelled", "not_delivered"):
                    return  # No reintentar
                _time.sleep(15)
                continue

            # Intentar descargar el PDF
            pdf_content = None
            for rtype in ["pdf2", "pdf"]:
                r = requests.get(
                    f"{ML_API_URL}/shipment_labels",
                    headers={"Authorization": f"Bearer {at}"},
                    params={"shipment_ids": shipping_id, "response_type": rtype},
                    timeout=15)
                if r.status_code == 200 and len(r.content) > 500:
                    pdf_content = r.content
                    print(f"[ETIQUETA] ✅ PDF descargado para {order_id} ({len(pdf_content)} bytes)")
                    break

            if pdf_content:
                with _lock:
                    _etiquetas_cache[order_id] = {
                        "pdf":         pdf_content,
                        "shipping_id": shipping_id,
                        "logistica":   logistic,
                        "estado":      estado,
                        "ts":          datetime.now().strftime("%d/%m %H:%M:%S"),
                        "intentos":    intento + 1,
                    }
                # Remover de la cola
                if order_id in _cola_etiquetas:
                    _cola_etiquetas.remove(order_id)
                return  # Exito

            print(f"[ETIQUETA] Intento {intento+1}/{reintentos} fallido para {order_id} (estado: {estado})")
            _time.sleep(20 * (intento + 1))  # Espera creciente

        except Exception as e:
            print(f"[ETIQUETA] Error en intento {intento+1} para {order_id}: {e}")
            _time.sleep(10)

    print(f"[ETIQUETA] ❌ No se pudo descargar etiqueta para {order_id} despues de {reintentos} intentos")


def _encolar_descarga_etiqueta(order_id):
    """Encola la descarga de una etiqueta en un thread separado."""
    if order_id not in _etiquetas_cache and order_id not in _cola_etiquetas:
        _cola_etiquetas.append(order_id)
        threading.Thread(
            target=_descargar_etiqueta_bg,
            args=(order_id,),
            daemon=True).start()
        print(f"[ETIQUETA] Encolada descarga para {order_id}")


def _descargar_etiquetas_lote(order_ids):
    """Descarga etiquetas de multiples pedidos en paralelo (max 5 simultaneos)."""
    from concurrent.futures import ThreadPoolExecutor
    ids_sin_cache = [oid for oid in order_ids if oid not in _etiquetas_cache]
    if not ids_sin_cache:
        print(f"[ETIQUETA] Todas las etiquetas ya estan en cache")
        return
    print(f"[ETIQUETA] Descargando {len(ids_sin_cache)} etiquetas en paralelo...")
    with ThreadPoolExecutor(max_workers=5) as ex:
        ex.map(_descargar_etiqueta_bg, ids_sin_cache)


# ─── Helpers multi-cuenta ────────────────────────────────────────────────────

def _cargar_sku_db():
    """Carga la BD de SKUs desde variable de entorno."""
    global _sku_db
    raw = os.environ.get(_SKU_DB_ENV_KEY, "").strip()
    if raw:
        try:
            _sku_db = json.loads(raw)
            print(f"[STARTUP] SKUs cargados: {len(_sku_db)}")
        except Exception as e:
            print(f"[STARTUP] Error cargando SKU_DB_JSON: {e}")
            _sku_db = {}


def _sku_info(sku):
    """Devuelve info de un SKU desde la BD interna."""
    return _sku_db.get(str(sku).upper(), {"nombre": "", "pasillo": "", "estanteria": ""})


def _html_ok(titulo, detalle=""):
    """Pagina HTML de exito."""
    return render_template_string("""
<!DOCTYPE html><html lang="es">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OK</title>
<style>
body{font-family:'Segoe UI',sans-serif;background:#0F172A;color:#F1F5F9;
  display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.box{background:#1E293B;border-radius:16px;padding:40px;max-width:500px;width:90%;
  text-align:center;border:1px solid #334155}
.icon{font-size:56px;margin-bottom:16px}
h1{color:#10B981;font-size:22px;margin:0 0 12px}
p{color:#94A3B8;font-size:14px;line-height:1.6}
.btn{display:inline-block;margin-top:24px;padding:10px 24px;background:#10B981;
  color:#fff;border-radius:8px;text-decoration:none;font-weight:600;
  font-size:14px;cursor:pointer;border:none}
</style></head>
<body><div class="box">
  <div class="icon">✅</div>
  <h1>{{ titulo }}</h1>
  <p>{{ detalle|safe }}</p>
  <button class="btn" onclick="window.close()">Cerrar esta pestaña</button>
</div></body></html>""", titulo=titulo, detalle=detalle)


def _html_error(titulo, detalle=""):
    """Pagina HTML de error."""
    return render_template_string("""
<!DOCTYPE html><html lang="es">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Error</title>
<style>
body{font-family:'Segoe UI',sans-serif;background:#0F172A;color:#F1F5F9;
  display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:16px}
.box{background:#1E293B;border-radius:16px;padding:36px;max-width:560px;width:100%;
  border:1px solid #334155}
.icon{font-size:48px;margin-bottom:12px;text-align:center;display:block}
h1{color:#EF4444;font-size:20px;margin:0 0 16px;text-align:center}
.detail{background:#0F172A;border-radius:8px;padding:14px;color:#94A3B8;
  font-size:13px;line-height:1.7;border:1px solid #334155}
.btn{display:inline-block;margin-top:20px;padding:8px 20px;background:#334155;
  color:#F1F5F9;border-radius:8px;font-size:13px;cursor:pointer;border:none}
</style></head>
<body><div class="box">
  <span class="icon">❌</span>
  <h1>{{ titulo }}</h1>
  <div class="detail">{{ detalle|safe }}</div>
  <button class="btn" onclick="history.back()">← Volver</button>
</div></body></html>""", titulo=titulo, detalle=detalle)


def _calcular_tipo(logistica, tags_order=None, shipping_id=""):
    """
    Clasifica el tipo de logistica segun documentacion oficial ML.
    SOLO clasifica con certeza si hay logistic_type o tags claros.
    Si no hay info suficiente, devuelve 'desconocido' (NO asume me2).

    ME2 (Mercado Envios 2):
      - self_service  -> Flex
      - cross_docking -> Colectas
      - xd_drop_off   -> Places
      - drop_off      -> Drop Off
      - fulfillment   -> Full
      - turbo         -> Turbo
    ME1:
      - default       -> logistica propia
    """
    log = (logistica or "").lower().strip()

    # Clasificacion definitiva por logistic_type
    if log == "self_service":
        return "flex"
    if log in ("cross_docking", "drop_off", "xd_drop_off",
               "fulfillment", "turbo", "xd_same_day"):
        return "me2"
    if log == "default":
        return "me1"
    if log in ("custom", "not_specified"):
        return "me1"

    # Fallback por tags del order
    tags_str = " ".join(t.lower() for t in (tags_order or []))
    if "self_service" in tags_str or "flex" in tags_str:
        return "flex"

    # Sin logistic_type confirmado → desconocido (no asumir me2)
    return "desconocido"


def _tokens_de(cuenta_id):
    """Devuelve el dict de tokens de una cuenta, o {} si no existe."""
    return _cuentas.get(cuenta_id, {})

def _token_valido_cuenta(cuenta_id=None):
    """True si la cuenta tiene un token activo."""
    cid = cuenta_id or _cuenta_activa
    tok = _cuentas.get(cid, {})
    if not tok.get("access_token"):
        return False
    exp = tok.get("expires_at")
    if exp and isinstance(exp, datetime):
        if datetime.now() >= exp - timedelta(seconds=300):
            _renovar_token_cuenta(cid)
    return bool(_cuentas.get(cid, {}).get("access_token"))


def _token_valido():
    return _token_valido_cuenta("cuenta_0")


def _renovar_token_cuenta(cuenta_id):
    """Renueva el access_token usando el refresh_token."""
    tok = _cuentas.get(cuenta_id, {})
    if not tok.get("refresh_token"):
        return
    try:
        r = requests.post(
            "https://api.mercadolibre.com/oauth/token",
            headers={"Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type":    "refresh_token",
                "client_id":     ML_APP_ID,
                "client_secret": ML_SECRET_KEY,
                "refresh_token": tok["refresh_token"],
            }, timeout=10)
        if r.status_code == 200:
            d = r.json()
            tok["access_token"]  = d["access_token"]
            tok["refresh_token"] = d.get("refresh_token", tok["refresh_token"])
            tok["expires_at"]    = datetime.now() + timedelta(
                seconds=d.get("expires_in", 21600))
            _cuentas[cuenta_id] = tok
            if cuenta_id == "cuenta_0":
                _tokens.update(tok)
            _guardar_tokens()
            print(f"[TOKEN] Renovado OK para {cuenta_id}")
        else:
            print(f"[TOKEN] Error renovando {cuenta_id}: {r.status_code}")
    except Exception as e:
        print(f"[TOKEN] Excepcion renovando {cuenta_id}: {e}")


def _renovar_token():
    _renovar_token_cuenta("cuenta_0")



def _ml_get_cuenta(ruta, cuenta_id, params=None):
    """GET a la API ML usando los tokens de una cuenta específica."""
    tok = _cuentas.get(cuenta_id, {})
    at  = tok.get("access_token", "")
    r   = requests.get(
        ML_API_URL + ruta,
        headers={"Authorization": f"Bearer {at}"},
        params=params or {}, timeout=12)
    return r

def _ml_get(ruta, params=None):
    """GET usando la cuenta_0 (compatibilidad)."""
    return _ml_get_cuenta(ruta, "cuenta_0", params)

def _cuentas_info():
    """Lista de cuentas conectadas con info básica."""
    return [
        {
            "cuenta_id": cid,
            "nickname":  tok.get("nickname", cid),
            "user_id":   tok.get("user_id", ""),
            "activa":    bool(tok.get("access_token")),
        }
        for cid, tok in _cuentas.items()
        if tok.get("access_token")
    ]

# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS ML
# ═══════════════════════════════════════════════════════════════════════════════

def _ml_get_all_orders_cuenta(cuenta_id, fecha_desde=None, fecha_hasta=None):
    """
    Trae pedidos pagados de una cuenta.
    fecha_desde / fecha_hasta: strings ISO 'YYYY-MM-DD' (default: últimos 7 días).
    """
    tok = _cuentas.get(cuenta_id, {})
    uid = tok.get("user_id")
    if not uid:
        return {}

    # Por defecto: últimos 7 días
    if not fecha_desde:
        fecha_desde = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00.000-00:00")
    else:
        fecha_desde = f"{fecha_desde}T00:00:00.000-00:00"

    if not fecha_hasta:
        fecha_hasta = datetime.now().strftime("%Y-%m-%dT23:59:59.000-00:00")
    else:
        fecha_hasta = f"{fecha_hasta}T23:59:59.000-00:00"

    pedidos = {}
    offset  = 0
    limit   = 50
    while True:
        r = _ml_get_cuenta("/orders/search", cuenta_id, params={
            "seller":                   uid,
            "order.status":             "paid",
            "order.date_created.from":  fecha_desde,
            "order.date_created.to":    fecha_hasta,
            "offset":                   offset,
            "limit":                    limit,
            "sort":                     "date_desc",
        })
        if r.status_code != 200:
            break
        data    = r.json()
        results = data.get("results", [])
        if not results:
            break
        for order in results:
            oid   = str(order["id"])
            items = []
            for it in order.get("order_items", []):
                item_data = it.get("item", {})
                var_attrs = item_data.get("variation_attributes", [])
                sku = ""
                for attr in var_attrs:
                    if attr.get("name","").lower() in ("sku","seller_sku"):
                        sku = str(attr.get("value_name","")).strip(); break
                if not sku:
                    sku = str(item_data.get("seller_sku") or
                              item_data.get("seller_custom_field") or "").strip()
                sku   = sku.upper() if sku else ""
                color = next((a["value_name"] for a in var_attrs
                              if "color" in a.get("name","").lower()), "")
                talle = next((a["value_name"] for a in var_attrs
                              if a.get("name","").lower() in ("talle","size","talha","talla")), "")
                items.append({
                    "item_id":    item_data.get("id",""),
                    "titulo":     item_data.get("title",""),
                    "sku":        sku,
                    "cantidad":   it.get("quantity", 1),
                    "color":      color,
                    "talle":      talle,
                    "unit_price": it.get("unit_price", 0),
                })
            ship = order.get("shipping") or {}
            log_type = (ship.get("logistic_type") or "").lower()
            tipo_calculado = _calcular_tipo(
                log_type, order.get("tags",[]),
                str(ship.get("id","")) if ship.get("id") else "")

            pedidos[oid] = {
                "order_id":     oid,
                "pack_id":      str(order.get("pack_id","")) if order.get("pack_id") else "",
                "fecha":        order.get("date_created","")[:10],
                "fecha_cierre": order.get("date_closed","")[:10],
                "comprador":    (order.get("buyer") or {}).get("nickname","")
                                or str((order.get("buyer") or {}).get("id","")),
                "total":        order.get("total_amount", 0),
                "moneda":       order.get("currency_id","UYU"),
                "items":        items,
                "shipping_id":  str(ship.get("id","")) if ship.get("id") else "",
                "logistica":    log_type,
                "tipo":         tipo_calculado,
                "estado_envio": "",
                "impreso":      False,
                "tags":         order.get("tags", []),
                "_cuenta":      cuenta_id,
                "_nickname":    tok.get("nickname", cuenta_id),
            }
        total  = data.get("paging",{}).get("total",0)
        offset += limit
        if offset >= total:
            break

    # ── Agrupar por pack_id ─────────────────────────────────────────────────
    # Si varios order_ids comparten el mismo pack_id → van en un solo paquete.
    # Consolidar en una sola entrada usando el primer order_id del pack.
    pedidos_agrupados = {}
    packs_vistos = {}  # pack_id → order_id principal

    for oid, ped in pedidos.items():
        pack_id = ped.get("pack_id", "")
        if not pack_id:
            # Sin pack → entrada individual
            pedidos_agrupados[oid] = ped
        elif pack_id not in packs_vistos:
            # Primer orden del pack → es la entrada principal
            packs_vistos[pack_id] = oid
            pedidos_agrupados[oid] = ped
        else:
            # Orden adicional del mismo pack → agregar sus items al principal
            oid_principal = packs_vistos[pack_id]
            ped_principal = pedidos_agrupados[oid_principal]
            # Agregar items que no estén ya
            skus_existentes = {it["sku"] for it in ped_principal["items"]}
            for it in ped.get("items", []):
                if it["sku"] not in skus_existentes:
                    ped_principal["items"].append(it)
                    skus_existentes.add(it["sku"])
                else:
                    # SKU ya existe — sumar cantidad si es diferente item_id
                    for ex in ped_principal["items"]:
                        if ex["sku"] == it["sku"] and ex["item_id"] != it["item_id"]:
                            ex["cantidad"] += it["cantidad"]
                            break
            # Sumar total
            ped_principal["total"] = (ped_principal.get("total", 0)
                                      + ped.get("total", 0))
            print(f"[PACK] Agrupado {oid} → {oid_principal} (pack {pack_id})")

    return pedidos_agrupados


def _enriquecer_skus_cuenta(pedidos, cuenta_id):
    """
    Enriquece SKUs y estado de envio usando los tokens de una cuenta.
    Las llamadas a /shipments se hacen en paralelo para mayor velocidad.
    """
    # 1. Enriquecer SKUs faltantes (multiget de 20)
    item_ids_sin_sku = {}
    for ped in pedidos.values():
        for it in ped["items"]:
            if not it["sku"] and it["item_id"]:
                item_ids_sin_sku[it["item_id"]] = True

    sku_map  = {}
    ids_list = list(item_ids_sin_sku.keys())
    for i in range(0, len(ids_list), 20):
        chunk = ids_list[i:i+20]
        try:
            r = _ml_get_cuenta("/items", cuenta_id, params={"ids": ",".join(chunk)})
            if r.status_code == 200:
                for entry in r.json():
                    body = entry.get("body", {})
                    iid  = body.get("id","")
                    sku  = str(body.get("seller_sku") or
                               body.get("seller_custom_field") or "").strip().upper()
                    if iid and sku:
                        sku_map[iid] = sku
        except Exception:
            pass

    for ped in pedidos.values():
        for it in ped["items"]:
            if not it["sku"] and it["item_id"] in sku_map:
                it["sku"] = sku_map[it["item_id"]]

    # 2. Enriquecer shipments EN PARALELO (max 12 threads simultaneos)
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch_shipment(oid):
        ped = pedidos.get(oid)
        if not ped or not ped.get("shipping_id"):
            return oid, None
        # Reintentar hasta 2 veces si falla
        for intento in range(2):
            try:
                rs = _ml_get_cuenta(f"/shipments/{ped['shipping_id']}", cuenta_id)
                if rs.status_code == 200:
                    return oid, rs.json()
            except Exception:
                pass
        return oid, None

    oids_con_shipping = [oid for oid, p in pedidos.items() if p.get("shipping_id")]

    if oids_con_shipping:
        with ThreadPoolExecutor(max_workers=12) as ex:
            futures = {ex.submit(_fetch_shipment, oid): oid
                       for oid in oids_con_shipping}
            # Sin timeout global — esperar a que TODOS terminen
            for future in as_completed(futures):
                try:
                    oid, sd = future.result()
                    if sd and oid in pedidos:
                        ped = pedidos[oid]
                        log_new   = (sd.get("logistic") or {}).get("type", "")
                        log_old   = sd.get("logistic_type", "")
                        logistica = log_new or log_old
                        status    = sd.get("status", "")
                        substatus = sd.get("substatus", "")

                        ped["logistica"]    = logistica
                        ped["estado_envio"] = status
                        ped["substatus"]    = substatus
                        ped["tipo"] = _calcular_tipo(
                            logistica, ped.get("tags",[]),
                            ped.get("shipping_id",""))

                        # Sincronizar campo impreso desde ML
                        # printed = etiqueta ya fue impresa
                        # ready_to_print = falta imprimir
                        # shipped/delivered = ya enviado (también "impreso")
                        ESTADOS_IMPRESOS = {
                            "printed", "shipped", "delivered",
                            "not_delivered", "cancelled"
                        }
                        if substatus in ESTADOS_IMPRESOS or status in (
                                "shipped", "delivered", "not_delivered", "cancelled"):
                            ped["impreso"] = True
                        elif substatus == "ready_to_print":
                            ped["impreso"] = False
                        # Si substatus vacío y status es ready_to_ship → pendiente
                        elif status == "ready_to_ship" and not substatus:
                            ped["impreso"] = False

                except Exception:
                    pass

    # 3. Para pedidos sin shipment o sin logistica, asegurar tipo por fallback
    for ped in pedidos.values():
        if not ped.get("tipo") or ped.get("tipo") == "desconocido":
            ped["tipo"] = _calcular_tipo(
                ped.get("logistica",""),
                ped.get("tags",[]),
                ped.get("shipping_id",""))

    return pedidos


def _ml_get_all_orders():
    """Compatibilidad: trae pedidos de cuenta_0."""
    return _ml_get_all_orders_cuenta("cuenta_0")


def _enriquecer_skus(pedidos):
    """Compatibilidad: usa cuenta_0."""
    return _enriquecer_skus_cuenta(pedidos, "cuenta_0")


def _refresh_pedidos_worker_cuenta(cuenta_id, fecha_desde=None, fecha_hasta=None):
    """Trae pedidos de UNA cuenta en background, con filtro de fecha."""
    global _ultimo_refresh_pedidos
    if not _token_valido_cuenta(cuenta_id):
        return
    try:
        pedidos = _ml_get_all_orders_cuenta(cuenta_id, fecha_desde, fecha_hasta)
        pedidos = _enriquecer_skus_cuenta(pedidos, cuenta_id)
        for p in pedidos.values():
            p["_cuenta"] = cuenta_id
        with _lock:
            to_del = [oid for oid, p in _pedidos_ml.items()
                      if p.get("_cuenta") == cuenta_id]
            for oid in to_del:
                del _pedidos_ml[oid]
            for oid, p in pedidos.items():
                if oid in _pedidos_ml:
                    p["impreso"] = _pedidos_ml[oid].get("impreso", False)
            _pedidos_ml.update(pedidos)
            _ultimo_refresh_pedidos = datetime.now()
        # NO auto-descargar etiquetas — descargar /shipment_labels marca el envio
        # como 'printed' en ML. Solo se descarga cuando el usuario hace clic
        # en el boton Etiqueta (bajo demanda).
        # (auto-descarga desactivada intencionalmente)
    except Exception as e:
        print(f"Error refresh cuenta {cuenta_id}: {e}")


def _refresh_pedidos_worker():
    """Compatibilidad: refresca todas las cuentas."""
    for cid in list(_cuentas.keys()):
        _refresh_pedidos_worker_cuenta(cid)


def _auto_refresh_loop():
    """Refresca pedidos de todas las cuentas cada 2 minutos."""
    while True:
        time.sleep(120)
        if _cuentas:
            _refresh_pedidos_worker()


def _cache_cleanup_loop():
    """
    Limpieza periodica del cache de etiquetas cada hora.
    Borra PDFs que llevan mas de 24 horas en cache.
    Esto da tiempo suficiente para el picking/packing del dia
    sin ocupar memoria indefinidamente.
    """
    while True:
        time.sleep(3600)  # Revisar cada 1 hora
        try:
            ahora    = datetime.now()
            borrados = 0
            kb_total = 0
            with _lock:
                for oid in list(_etiquetas_cache.keys()):
                    data = _etiquetas_cache[oid]
                    if not data.get("pdf"):
                        continue
                    try:
                        ts_str = data.get("ts","")
                        if ts_str:
                            ts     = datetime.strptime(ts_str, "%d/%m %H:%M:%S")
                            ts     = ts.replace(year=ahora.year)
                            horas  = (ahora - ts).total_seconds() / 3600
                            if horas >= 24:
                                kb = round(len(data["pdf"]) / 1024, 1)
                                kb_total += kb
                                data["pdf"]      = None
                                data["expirado"] = True
                                borrados        += 1
                                print(f"[CACHE] Expirado: {oid} ({kb} KB, {horas:.1f}h)")
                    except Exception:
                        pass
            if borrados:
                print(f"[CACHE] Limpieza 24h: {borrados} PDFs eliminados ({kb_total:.1f} KB)")
        except Exception as e:
            print(f"[CACHE] Error en limpieza: {e}")


threading.Thread(target=_auto_refresh_loop,  daemon=True).start()
threading.Thread(target=_cache_cleanup_loop, daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════════════
# DECORADORES
# ═══════════════════════════════════════════════════════════════════════════════

def requiere_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _cuentas:  # Al menos una cuenta conectada
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "msg": "No autenticado con ML", "login": "/auth/login"}), 401
            return redirect("/auth/login")
        return f(*args, **kwargs)
    return decorated

def requiere_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        k = (request.headers.get("X-API-Key") or
             request.args.get("key") or
             request.args.get("api_key") or "")
        k = k.strip()
        # Acepta la key de Railway O el fallback hardcodeado
        if k not in (API_KEY, "everest2024", "everest2025"):
            return jsonify({
                "ok":  False,
                "msg": f"Clave invalida. Recibida: '{k[:8]}...' Esperada: '{API_KEY[:4]}...'"
            }), 401
        return f(*args, **kwargs)
    return decorated


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH ROUTES  — OAuth2 con PKCE (requerido por ML cuando está activado)
# ═══════════════════════════════════════════════════════════════════════════════

# Almacena el code_verifier temporalmente hasta que llegue el callback
_pkce_store = {}   # { state: code_verifier }


def _generar_pkce():
    """Genera code_verifier y code_challenge (S256) según spec RFC 7636."""
    import hashlib, base64, secrets
    verifier  = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b'=').decode()
    return verifier, challenge


@app.route("/auth/login")
def auth_login():
    cuenta_id = request.args.get("cuenta", "cuenta_0")
    import secrets
    state     = secrets.token_urlsafe(16)
    verifier, challenge = _generar_pkce()
    _pkce_store[state]  = {"verifier": verifier, "cuenta_id": cuenta_id}
    url = (
        f"{ML_AUTH_URL}/authorization"
        f"?response_type=code"
        f"&client_id={ML_APP_ID}"
        f"&redirect_uri={ML_REDIRECT}"
        f"&scope=read%20write%20offline_access"
        f"&state={state}"
        f"&code_challenge={challenge}"
        f"&code_challenge_method=S256"
    )
    return redirect(url)


@app.route("/auth/callback")
def auth_callback():
    code  = request.args.get("code")
    error = request.args.get("error")
    state = request.args.get("state", "")

    if error or not code:
        return _html_error(
            f"Error de autorizacion: {error or 'sin codigo'}",
            f"Redirect URI: <code>{ML_REDIRECT}</code>")

    entry     = _pkce_store.pop(state, None) or {}
    verifier  = entry.get("verifier") if isinstance(entry, dict) else entry
    cuenta_id = entry.get("cuenta_id", "cuenta_0") if isinstance(entry, dict) else "cuenta_0"

    # Si el state no existe, el servidor pudo haberse reiniciado entre el login y callback.
    # Intentar igual sin PKCE (el code sigue siendo válido).
    if not entry:
        print(f"[AUTH] State '{state}' no encontrado en pkce_store — posible reinicio del servidor")
        verifier  = None
        cuenta_id = "cuenta_0"

    payload = {
        "grant_type":    "authorization_code",
        "client_id":     ML_APP_ID,
        "client_secret": ML_SECRET_KEY,
        "code":          code,
        "redirect_uri":  ML_REDIRECT,
    }
    if verifier:
        payload["code_verifier"] = verifier

    try:
        r = requests.post(
            "https://api.mercadolibre.com/oauth/token",
            headers={"Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data=payload, timeout=15)

        if r.status_code != 200:
            try:
                err_detail = json.dumps(r.json(), indent=2, ensure_ascii=False)
            except Exception:
                err_detail = r.text
            return _html_error(
                f"Error obteniendo token ({r.status_code})",
                f"<pre style='background:#0F172A;padding:12px;border-radius:4px;"
                f"color:#94A3B8;white-space:pre-wrap'>{err_detail}</pre>"
                f"<small style='color:#475569'>App ID: {ML_APP_ID} | PKCE: {'SI' if verifier else 'NO'}</small>")

        d   = r.json()
        tok = {
            "access_token":  d["access_token"],
            "refresh_token": d.get("refresh_token", ""),
            "expires_at":    datetime.now() + timedelta(seconds=d.get("expires_in", 21600)),
        }
        me_r = requests.get(ML_API_URL + "/users/me",
                            headers={"Authorization": f"Bearer {tok['access_token']}"},
                            timeout=8)
        if me_r.status_code == 200:
            me = me_r.json()
            tok["user_id"]  = str(me.get("id", ""))
            tok["nickname"] = me.get("nickname", cuenta_id)
        else:
            tok["user_id"]  = ""
            tok["nickname"] = cuenta_id

        _cuentas[cuenta_id] = tok
        if cuenta_id == "cuenta_0":
            _tokens.update(tok)

        # Persistir tokens inmediatamente despues de conectar
        _guardar_tokens()
        print(f"[TOKEN] Conectado y persistido: {tok.get('nickname','?')} -> {cuenta_id}")

        threading.Thread(
            target=_refresh_pedidos_worker_cuenta,
            args=(cuenta_id,), daemon=True).start()

        return _html_ok(
            f"Conectado como <b>{tok['nickname']}</b>",
            f"Cuenta registrada como <b>{cuenta_id}</b>. Podés cerrar esta pestaña.")

    except Exception as e:
        return _html_error("Error inesperado", str(e))


@app.route("/auth/logout")
def auth_logout():
    cuenta_id = request.args.get("cuenta", "cuenta_0")
    if cuenta_id in _cuentas:
        del _cuentas[cuenta_id]
        to_del = [oid for oid, p in list(_pedidos_ml.items())
                  if p.get("_cuenta") == cuenta_id]
        for oid in to_del:
            _pedidos_ml.pop(oid, None)
    if cuenta_id == "cuenta_0":
        _tokens.clear()
    return redirect("/")


@app.route("/auth/status")
def auth_status():
    return jsonify({
        "autenticado": bool(_cuentas),
        "nickname":    _cuentas.get("cuenta_0", {}).get("nickname", ""),
        "user_id":     _cuentas.get("cuenta_0", {}).get("user_id", ""),
        "pedidos":     len(_pedidos_ml),
        "cuentas":     _cuentas_info(),
        "ultimo_refresh": _ultimo_refresh_pedidos.strftime("%d/%m %H:%M:%S")
                          if _ultimo_refresh_pedidos else "-",
    })


@app.route("/api/cuentas")
def api_cuentas():
    return jsonify({"ok": True, "cuentas": _cuentas_info()})


@app.route("/api/cuentas/<cuenta_id>/logout", methods=["POST"])
@requiere_api_key
def api_cuenta_logout(cuenta_id):
    """Desconecta una cuenta ML. Requiere API Key, no OAuth."""
    with _lock:
        if cuenta_id in _cuentas:
            del _cuentas[cuenta_id]
            to_del = [oid for oid, p in list(_pedidos_ml.items())
                      if p.get("_cuenta") == cuenta_id]
            for oid in to_del:
                _pedidos_ml.pop(oid, None)
    # Persistir el estado (sin la cuenta eliminada)
    _guardar_tokens()
    return jsonify({"ok": True, "msg": f"Cuenta {cuenta_id} desvinculada"})


# ═══════════════════════════════════════════════════════════════════════════════
# API PEDIDOS ML
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/pedidos")
def api_pedidos():
    """Devuelve los pedidos en memoria. No requiere OAuth."""
    if not _cuentas:
        return jsonify({"ok": False, "msg": "login", "pedidos": []}), 200
    with _lock:
        pedidos = list(_pedidos_ml.values())

    # Garantizar que tipo sea siempre correcto antes de enviar
    for p in pedidos:
        log = p.get("logistica","")
        if log and (not p.get("tipo") or p.get("tipo") == "desconocido"):
            p["tipo"] = _calcular_tipo(log, p.get("tags",[]), p.get("shipping_id",""))

    return jsonify({
        "ok":      True,
        "pedidos": pedidos,
        "total":   len(pedidos),
        "ts":      _ultimo_refresh_pedidos.strftime("%d/%m %H:%M:%S")
                   if _ultimo_refresh_pedidos else "—",
    })


@app.route("/api/pedidos/refresh", methods=["POST"])
def api_refresh():
    """
    Refresca pedidos. No requiere OAuth — usa X-API-Key o funciona si hay cuentas activas.
    Body JSON opcional: { "fecha_desde": "YYYY-MM-DD", "fecha_hasta": "YYYY-MM-DD" }
    """
    if not _cuentas:
        return jsonify({"ok": False, "msg": "No hay cuentas ML conectadas"}), 400

    body    = request.get_json(silent=True) or {}
    f_desde = body.get("fecha_desde")
    f_hasta = body.get("fecha_hasta")

    for cid in list(_cuentas.keys()):
        threading.Thread(
            target=_refresh_pedidos_worker_cuenta,
            args=(cid, f_desde, f_hasta), daemon=True).start()

    return jsonify({
        "ok":  True,
        "msg": f"Actualizando pedidos ({f_desde or 'ultimos 7 dias'} a {f_hasta or 'hoy'})"
    })


# ═══════════════════════════════════════════════════════════════════════════════
# ETIQUETA PERSONALIZADA — Logo + Texto
# ═══════════════════════════════════════════════════════════════════════════════

def _aplicar_personalizacion_etiqueta(pdf_bytes, config):
    """
    Aplica logo y texto personalizado a un PDF de etiqueta.
    El logo puede venir en base64 (desde la app) o como URL.
    """
    if not config:
        return pdf_bytes
    
    # Si no hay personalización, devolver PDF original
    if not config.get("etiqueta_logo_b64") and not config.get("etiqueta_logo_url") and not config.get("etiqueta_texto"):
        return pdf_bytes
    
    try:
        import io
        import base64
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader
        from PIL import Image
        
        # 1. Cargar el PDF original
        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError:
            import PyPDF2
            PdfReader = PyPDF2.PdfReader
            PdfWriter  = PyPDF2.PdfWriter

        reader = PdfReader(io.BytesIO(pdf_bytes))
        if not reader.pages:
            return pdf_bytes
        
        first_page = reader.pages[0]
        page_width = float(first_page.mediabox.width)
        page_height = float(first_page.mediabox.height)
        
        # 2. Crear overlay
        overlay_buffer = io.BytesIO()
        c = canvas.Canvas(overlay_buffer, pagesize=(page_width, page_height))
        
        # 3. Agregar logo
        logo_data = None
        
        # Preferir base64 (desde archivo local) sobre URL
        if config.get("etiqueta_logo_b64"):
            try:
                logo_bytes = base64.b64decode(config.get("etiqueta_logo_b64"))
                logo_data = logo_bytes
            except Exception as e:
                print(f"[ETIQUETA] Error decodificando logo base64: {e}")
        elif config.get("etiqueta_logo_url"):
            try:
                r = requests.get(config.get("etiqueta_logo_url"), timeout=5)
                if r.status_code == 200:
                    logo_data = r.content
            except Exception as e:
                print(f"[ETIQUETA] Error descargando logo URL: {e}")
        
        if logo_data:
            try:
                logo_size = int(config.get("etiqueta_logo_size", 15))
                logo_size = max(5, min(50, logo_size))
                
                img_pil = Image.open(io.BytesIO(logo_data))
                new_width = (page_width * logo_size) / 100
                ratio = img_pil.height / img_pil.width if img_pil.width > 0 else 1
                new_height = new_width * ratio
                
                img_pil.thumbnail((int(new_width), int(new_height)), Image.Resampling.LANCZOS)
                
                # Posición
                pos = config.get("etiqueta_logo_pos", "superior_izq")
                if pos == "superior_izq":
                    x, y = 10, page_height - img_pil.height - 10
                elif pos == "superior_der":
                    x, y = page_width - img_pil.width - 10, page_height - img_pil.height - 10
                else:  # borde = lateral izquierdo
                    x, y = 5, (page_height - img_pil.height) / 2
                
                img_buffer = io.BytesIO()
                img_pil.save(img_buffer, format='PNG')
                img_buffer.seek(0)
                
                c.drawImage(ImageReader(img_buffer), x, y,
                           width=img_pil.width, height=img_pil.height)
                print(f"[ETIQUETA] Logo aplicado: {pos} ({logo_size}%)")
            except Exception as e:
                print(f"[ETIQUETA] Error aplicando logo: {e}")
        
        # 4. Agregar texto
        texto = config.get("etiqueta_texto", "").strip()
        if texto:
            from reportlab.lib import colors
            c.setFont("Helvetica-Bold", 9)
            c.setFillColor(colors.black)
            
            pos_txt = config.get("etiqueta_texto_pos", "abajo")
            if pos_txt == "arriba":
                c.drawString(15, page_height - 40, texto[:80])
            elif pos_txt == "lateral":
                c.rotate(90)
                c.drawString(15, -page_width + 15, texto[:80])
                c.rotate(-90)
            else:  # abajo
                c.drawString(15, 15, texto[:80])
            print(f"[ETIQUETA] Texto agregado: {pos_txt}")
        
        c.save()
        overlay_buffer.seek(0)
        
        # 5. Mezclar PDF original con overlay
        try:
            from pypdf import PdfReader as PR2, PdfWriter as PW2
        except ImportError:
            import PyPDF2
            PR2 = PyPDF2.PdfReader
            PW2 = PyPDF2.PdfWriter

        overlay_reader = PR2(overlay_buffer)
        writer = PW2()
        
        for i, page in enumerate(reader.pages):
            if i == 0 and overlay_reader.pages:
                page.merge_page(overlay_reader.pages[0])
            writer.add_page(page)
        
        output = io.BytesIO()
        writer.write(output)
        output.seek(0)
        print(f"[ETIQUETA] PDF personalizado generado ({len(output.getvalue())} bytes)")
        return output.getvalue()
    
    except Exception as e:
        print(f"[ETIQUETA] Error en personalización: {e}")
        import traceback
        traceback.print_exc()
        return pdf_bytes


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINT: Descargar etiqueta de envío (ML integrado)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/etiqueta/<order_id>", methods=["GET", "POST"])
def api_etiqueta(order_id):
    """Proxy de etiqueta ML - sirve PDF directo sin marcar como impresa en ML."""
    try:
        return _api_etiqueta_impl(order_id)
    except Exception as e:
        import traceback
        print(f"[ETIQUETA] Error inesperado para {order_id}: {e}")
        traceback.print_exc()
        return _html_error(
            "Error interno al obtener etiqueta",
            f"Pedido #{order_id}<br><br>"
            f"Error: {str(e)[:200]}<br><br>"
            f"Intentá de nuevo en unos segundos o actualizá los pedidos."), 200


def _api_etiqueta_impl(order_id):

    # ── 1. Servir desde cache si ya fue descargada via webhook ────────────────
    with _lock:
        cached    = _etiquetas_cache.get(order_id)
        snap      = dict(_pedidos_ml)
        snap_lote = dict(_estado.get("pedidos", {}))   # pedidos del lote actual

    if cached and cached.get("pdf"):
        print(f"[ETIQUETA] Sirviendo desde cache para {order_id}")
        pdf_bytes = cached["pdf"]
        shid      = cached.get("shipping_id","label")
        ts        = cached.get("ts","")
        return Response(
            pdf_bytes,
            status=200,
            mimetype="application/pdf",
            headers={
                "Content-Disposition": f"inline; filename=etiqueta_{shid}.pdf",
                "Content-Type":        "application/pdf",
                "X-Cache":             "HIT",
                "X-Cached-At":         ts,
            })

    # ── 2. Buscar pedido — _pedidos_ml (str y int) + lote ────────────────────
    pedido   = snap.get(order_id) or snap.get(str(order_id)) or snap.get(int(order_id) if order_id.isdigit() else order_id)
    real_oid = order_id

    # Búsqueda por sufijo/prefijo en _pedidos_ml
    if not pedido and len(order_id) >= 8:
        sufijo = order_id[-8:]
        for k, v in snap.items():
            if str(k).endswith(sufijo) or str(k)[:8] == order_id[:8]:
                pedido   = v
                real_oid = str(k)
                break

    # Buscar en _estado["pedidos"] (lote subido desde la app desktop)
    if not pedido:
        for p_num, p_data in snap_lote.items():
            oid_lote = str(p_data.get("_order_id", ""))
            if oid_lote == order_id:
                pedido = {
                    "shipping_id": str(p_data.get("_shipping_id", "")),
                    "comprador":   p_data.get("comprador", ""),
                    "_cuenta":     p_data.get("_cuenta", "cuenta_0"),
                    "items":       p_data.get("items", []),
                }
                real_oid = order_id
                print(f"[ETIQUETA] #{order_id} encontrado en lote (p={p_num}), shid={pedido['shipping_id']}")
                break

    if not pedido:
        print(f"[ETIQUETA] #{order_id} NO encontrado. _pedidos_ml={len(snap)}, lote={len(snap_lote)}")
        return _html_error(
            "Pedido no encontrado",
            f"El pedido #{order_id} no esta en la lista. "
            f"Hay {len(snap)} pedidos ML y {len(snap_lote)} en el lote.<br>"
            "Actualizá los pedidos e intentá de nuevo."), 404

    shipping_id = pedido.get("shipping_id", "")
    comprador   = pedido.get("comprador", "")
    items_txt   = " | ".join(
        f"{it.get('sku','?')} x{it.get('cantidad',1)}"
        for it in pedido.get("items", [])[:3])

    if not shipping_id:
        return _html_error(
            f"Pedido #{real_oid} sin envio ML",
            f"<b>Comprador:</b> {comprador}<br>"
            f"<b>Productos:</b> {items_txt}<br><br>"
            "Este pedido no tiene envio de ML asignado. "
            "Puede ser un pedido de tipo 'acordar con vendedor'."), 200

    cuenta_id = pedido.get("_cuenta", "cuenta_0")
    tok       = _cuentas.get(cuenta_id, {})
    at        = tok.get("access_token", "")

    if not at:
        return _html_error("Token ML no disponible",
                           "Reconecta la cuenta MercadoLibre."), 401

    # GET puro - nunca POST - no marca como impresa
    pdf_content = None
    last_status = 0
    for rtype in ["pdf2", "pdf"]:
        try:
            r = requests.get(
                f"{ML_API_URL}/shipment_labels",
                headers={"Authorization": f"Bearer {at}"},
                params={"shipment_ids": shipping_id, "response_type": rtype},
                timeout=15)
            last_status = r.status_code
            if r.status_code == 200 and len(r.content) > 200:
                pdf_content = r.content
                break
        except Exception:
            continue

    if pdf_content:
        # Leer config de personalización:
        # - POST → body JSON  (logo en base64, puede ser grande)
        # - GET  → query param ?config=... (fallback, sin logo)
        config = {}
        try:
            if request.method == "POST" and request.content_type == "application/json":
                config = request.get_json(silent=True) or {}
                print(f"[ETIQUETA] Config POST recibida: logo={bool(config.get('etiqueta_logo_b64'))}, texto={bool(config.get('etiqueta_texto'))}, pos={config.get('etiqueta_logo_pos')}")
            else:
                config_str = request.args.get("config", "{}")
                config = json.loads(config_str) if config_str else {}
        except Exception as e:
            print(f"[ETIQUETA] Error leyendo config: {e}")
            config = {}

        # Aplicar personalización (logo + texto sobre el PDF de ML)
        pdf_final = _aplicar_personalizacion_etiqueta(pdf_content, config)

        return Response(
            pdf_final, status=200, mimetype="application/pdf",
            headers={
                "Content-Disposition": f"inline; filename=etiqueta_{shipping_id}.pdf",
                "Content-Type": "application/pdf"
            })
    try:
        ri = requests.get(f"{ML_API_URL}/shipments/{shipping_id}",
                          headers={"Authorization": f"Bearer {at}"}, timeout=8)
        if ri.status_code == 200:
            sd        = ri.json()
            estado    = sd.get("status", "")
            substatus = sd.get("substatus", "")
            logistic  = sd.get("logistic_type","") or (sd.get("logistic") or {}).get("type","")
    except Exception:
        pass

    MSGS = {
        "delivered":    ("Pedido ya entregado",
                         "Este pedido fue entregado. La etiqueta ya no esta disponible en ML."),
        "shipped":      ("Pedido en camino",
                         "El pedido esta en camino. La etiqueta fue usada anteriormente."),
        "cancelled":    ("Cancelado", "El envio fue cancelado."),
        "ready_to_ship":("Listo para enviar",
                         "Intenta de nuevo en unos segundos."),
        "handling":     ("En preparacion",
                         "La etiqueta estara disponible cuando pase a ready_to_ship."),
        "pending":      ("Pendiente", "El envio esta pendiente de procesamiento por ML."),
    }
    tit, desc = MSGS.get(estado, ("Sin etiqueta disponible",
                                   f"Estado: {estado or 'desconocido'} / {substatus}"))

    # Boton de descarga directa si el estado es ready_to_ship
    btn_alt = ""
    if estado == "ready_to_ship" or not estado:
        btn_alt = (
            f'<br><br>'
            f'<a href="/api/etiqueta/{real_oid}/descargar" target="_blank"'
            f'   style="display:inline-block;background:#3B82F6;color:#fff;'
            f'   padding:12px 24px;border-radius:8px;text-decoration:none;'
            f'   font-weight:bold;font-size:15px;margin-top:8px">'
            f'📥 Descargar etiqueta (metodo alternativo)</a>'
            f'<br><small style="color:#6B7280;margin-top:8px;display:block">'
            f'Si el boton abre el PDF, guardarlo con Ctrl+S o clic derecho → Guardar como.</small>'
        )

    return _html_error(
        tit,
        f"<b>Pedido:</b> #{real_oid} — {comprador}<br>"
        f"<b>Productos:</b> {items_txt}<br>"
        f"<b>Shipping ID:</b> {shipping_id}<br>"
        f"<b>Logistica:</b> {logistic or 'no disponible'}<br>"
        f"<b>Estado ML:</b> {estado} / {substatus or '—'}<br>"
        f"<b>Codigo HTTP:</b> {last_status}<br><br>{desc}{btn_alt}"), 200



@app.route("/api/etiqueta/<order_id>/descargar")
def api_etiqueta_descargar(order_id):
    """
    Descarga la etiqueta como archivo (attachment).
    El servidor obtiene el PDF de ML usando el token en el HEADER Authorization
    y lo sirve directamente — el token NUNCA viaja en la URL.
    """
    try:
        with _lock:
            snap = dict(_pedidos_ml)

        pedido = snap.get(order_id)
        if not pedido and len(order_id) >= 8:
            for k, v in snap.items():
                if k.endswith(order_id[-8:]):
                    pedido = v; order_id = k; break

        if not pedido:
            return _html_error("Pedido no encontrado",
                               f"#{order_id} no esta en memoria. Actualizá los pedidos."), 404

        shipping_id = pedido.get("shipping_id","")
        if not shipping_id:
            return _html_error("Sin envio ML",
                               "Este pedido no tiene envio asignado."), 400

        cuenta_id = pedido.get("_cuenta","cuenta_0")
        at = _cuentas.get(cuenta_id,{}).get("access_token","")
        if not at:
            return _html_error("Token no disponible", "Reconectá la cuenta ML."), 401

        # Descargar el PDF en el servidor — token en HEADER, nunca en la URL
        pdf_content = None
        last_status = 0
        for rtype in ["pdf2", "pdf"]:
            try:
                r = requests.get(
                    f"{ML_API_URL}/shipment_labels",
                    headers={"Authorization": f"Bearer {at}"},
                    params={"shipment_ids": shipping_id, "response_type": rtype},
                    timeout=15)
                last_status = r.status_code
                if r.status_code == 200 and len(r.content) > 200:
                    pdf_content = r.content
                    break
            except Exception:
                continue

        if pdf_content:
            return Response(
                pdf_content, status=200, mimetype="application/pdf",
                headers={
                    "Content-Disposition": f"attachment; filename=etiqueta_{shipping_id}.pdf",
                    "Content-Type": "application/pdf",
                })

        return _html_error(
            "Etiqueta no disponible",
            f"No se pudo obtener la etiqueta del pedido #{order_id}.<br>"
            f"Código ML: {last_status}<br>Intentá de nuevo en unos segundos."), 200

    except Exception as e:
        return _html_error("Error al descargar", str(e)[:200]), 200


# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOK MERCADOLIBRE — Notificaciones en tiempo real
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/webhook/ml", methods=["POST", "GET"])
def webhook_ml():
    """
    Recibe notificaciones de MercadoLibre cuando hay una nueva venta.
    ML hace POST aqui con el topic y el resource_id.
    Railway URL: https://picking-server-production.up.railway.app/webhook/ml

    Configurar en ML Developers > Notificaciones:
      URL: https://picking-server-production.up.railway.app/webhook/ml
      Topicos: orders_v2, shipments
    """
    # ML hace GET para verificar la URL al configurar
    if request.method == "GET":
        return jsonify({"ok": True, "msg": "Webhook ML activo"}), 200

    try:
        data    = request.get_json(silent=True) or {}
        topic   = data.get("topic", data.get("type", ""))
        res_id  = data.get("resource", "")
        user_id = str(data.get("user_id", ""))

        print(f"[WEBHOOK] Recibido: topic={topic} resource={res_id} user={user_id}")

        # Responder 200 inmediatamente (ML requiere respuesta rapida)
        # El procesamiento se hace en background
        if topic in ("orders_v2", "orders"):
            # Extraer order_id del resource (formato: /orders/2000012345)
            order_id = res_id.strip("/").split("/")[-1]
            threading.Thread(
                target=_procesar_notificacion_orden,
                args=(order_id, user_id),
                daemon=True).start()

        elif topic == "shipments":
            # Extraer shipment_id y buscar el order relacionado
            shipment_id = res_id.strip("/").split("/")[-1]
            threading.Thread(
                target=_procesar_notificacion_shipment,
                args=(shipment_id, user_id),
                daemon=True).start()

        return jsonify({"ok": True}), 200

    except Exception as e:
        print(f"[WEBHOOK] Error: {e}")
        return jsonify({"ok": True}), 200  # Siempre 200 para que ML no reintente


def _procesar_notificacion_orden(order_id, user_id):
    """
    Procesa una notificacion de orden nueva de ML.
    1. Busca la cuenta que corresponde al user_id
    2. Trae los datos de la orden
    3. La agrega a _pedidos_ml
    4. Descarga la etiqueta automaticamente
    """
    import time as _time
    _time.sleep(2)  # Esperar que ML procese la orden completamente

    # Encontrar la cuenta que corresponde a este user_id
    cuenta_id = None
    for cid, tok in _cuentas.items():
        if str(tok.get("user_id","")) == str(user_id):
            cuenta_id = cid
            break

    if not cuenta_id and _cuentas:
        # Fallback: usar la primera cuenta disponible
        cuenta_id = list(_cuentas.keys())[0]

    if not cuenta_id:
        print(f"[WEBHOOK] No hay cuentas conectadas para user_id={user_id}")
        return

    at = _cuentas.get(cuenta_id, {}).get("access_token","")
    if not at:
        print(f"[WEBHOOK] Sin token para cuenta {cuenta_id}")
        return

    try:
        # Traer datos de la orden
        r = requests.get(
            f"{ML_API_URL}/orders/{order_id}",
            headers={"Authorization": f"Bearer {at}"},
            timeout=10)

        if r.status_code != 200:
            print(f"[WEBHOOK] Error trayendo orden {order_id}: {r.status_code}")
            return

        order = r.json()

        # Solo procesar ordenes pagadas
        if order.get("status") not in ("paid",):
            print(f"[WEBHOOK] Orden {order_id} en estado '{order.get('status')}' — ignorada")
            return

        # Construir el pedido
        items = []
        for it in order.get("order_items", []):
            item_data = it.get("item", {})
            var_attrs = item_data.get("variation_attributes", [])
            sku = ""
            for attr in var_attrs:
                if attr.get("name","").lower() in ("sku","seller_sku"):
                    sku = str(attr.get("value_name","")).strip(); break
            if not sku:
                sku = str(item_data.get("seller_sku") or
                          item_data.get("seller_custom_field") or "").strip()
            sku = sku.upper() if sku else ""
            items.append({
                "item_id":    item_data.get("id",""),
                "titulo":     item_data.get("title",""),
                "sku":        sku,
                "cantidad":   it.get("quantity", 1),
                "unit_price": it.get("unit_price", 0),
            })

        ship     = order.get("shipping") or {}
        ship_id  = str(ship.get("id","")) if ship.get("id") else ""
        nick     = _cuentas.get(cuenta_id, {}).get("nickname", cuenta_id)

        pedido = {
            "order_id":     str(order_id),
            "pack_id":      str(order.get("pack_id","")) if order.get("pack_id") else "",
            "fecha":        order.get("date_created","")[:10],
            "fecha_cierre": order.get("date_closed","")[:10],
            "comprador":    (order.get("buyer") or {}).get("nickname",""),
            "total":        order.get("total_amount", 0),
            "moneda":       order.get("currency_id","UYU"),
            "items":        items,
            "shipping_id":  ship_id,
            "logistica":    "",
            "estado_envio": "",
            "impreso":      False,
            "tags":         order.get("tags", []),
            "_cuenta":      cuenta_id,
            "_nickname":    nick,
            "_via_webhook": True,
        }

        with _lock:
            _pedidos_ml[str(order_id)] = pedido

        print(f"[WEBHOOK] ✅ Orden {order_id} agregada ({nick}) — {len(items)} items")
        # NO descargar etiqueta automaticamente — marca como printed en ML.
        # La etiqueta se descarga solo cuando el operario hace clic en el boton.

    except Exception as e:
        print(f"[WEBHOOK] Error procesando orden {order_id}: {e}")


def _procesar_notificacion_shipment(shipment_id, user_id):
    """
    Procesa una notificacion de cambio de estado de envio.
    Si pasa a ready_to_ship, descarga la etiqueta.
    """
    import time as _time

    # Buscar la cuenta
    cuenta_id = None
    for cid, tok in _cuentas.items():
        if str(tok.get("user_id","")) == str(user_id):
            cuenta_id = cid
            break
    if not cuenta_id and _cuentas:
        cuenta_id = list(_cuentas.keys())[0]
    if not cuenta_id:
        return

    at = _cuentas.get(cuenta_id, {}).get("access_token","")
    if not at:
        return

    try:
        r = requests.get(
            f"{ML_API_URL}/shipments/{shipment_id}",
            headers={"Authorization": f"Bearer {at}"},
            timeout=10)

        if r.status_code != 200:
            return

        ship_data  = r.json()
        estado     = ship_data.get("status","")
        order_id   = str(ship_data.get("order_id",""))
        logistic   = ship_data.get("logistic_type","") or                      (ship_data.get("logistic") or {}).get("type","")

        print(f"[WEBHOOK] Shipment {shipment_id} → estado={estado} order={order_id}")

        # Actualizar estado en el pedido si existe
        if order_id and order_id in _pedidos_ml:
            with _lock:
                _pedidos_ml[order_id]["estado_envio"] = estado
                _pedidos_ml[order_id]["logistica"]    = logistic
                _pedidos_ml[order_id]["shipping_id"]  = str(shipment_id)

        # NO auto-descargar — marca como printed en ML.
        # El estado ready_to_ship solo se registra, la etiqueta se baja bajo demanda.
        if estado == "ready_to_ship" and order_id:
            print(f"[WEBHOOK] Pedido {order_id} ready_to_ship — listo para imprimir")

    except Exception as e:
        print(f"[WEBHOOK] Error procesando shipment {shipment_id}: {e}")


@app.route("/api/etiquetas_cache")
def api_etiquetas_cache():
    """Estado del cache de etiquetas — muestra memoria usada y liberada."""
    with _lock:
        resultado   = []
        total_kb    = 0
        pdfs_activos = 0
        for oid, data in _etiquetas_cache.items():
            ped  = _pedidos_ml.get(oid, {})
            kb   = round(len(data.get("pdf") or b"") / 1024, 1)
            total_kb += kb
            tiene_pdf = data.get("pdf") is not None
            if tiene_pdf:
                pdfs_activos += 1
            resultado.append({
                "order_id":    oid,
                "comprador":   ped.get("comprador",""),
                "shipping_id": data.get("shipping_id",""),
                "logistica":   data.get("logistica",""),
                "estado":      data.get("estado",""),
                "tiene_pdf":   tiene_pdf,
                "tamanio_kb":  kb,
                "guardado_en": data.get("ts",""),
                "impreso":     data.get("impreso", False),
                "impreso_en":  data.get("impreso_ts",""),
            })
    return jsonify({
        "ok":              True,
        "total_registros": len(_etiquetas_cache),
        "pdfs_en_memoria": pdfs_activos,
        "en_cola_descarga": len(_cola_etiquetas),
        "memoria_total_kb": round(total_kb, 1),
        "politica":        "PDF disponible por 24 horas — se borra al cumplir 24h o al marcar impreso",
        "etiquetas":       resultado,
    })

@app.route("/api/pedidos/marcar_impreso/<order_id>", methods=["POST"])
@requiere_auth
def marcar_impreso(order_id):
    """
    Marca el pedido como impreso en nuestro sistema.
    Borra el PDF del cache para liberar memoria.
    """
    with _lock:
        if order_id in _pedidos_ml:
            _pedidos_ml[order_id]["impreso"] = True

        # Borrar PDF del cache para liberar memoria
        borrado_kb = 0
        if order_id in _etiquetas_cache:
            pdf_size = len(_etiquetas_cache[order_id].get("pdf", b""))
            borrado_kb = round(pdf_size / 1024, 1)
            # Mantener metadata pero borrar el PDF binario
            _etiquetas_cache[order_id]["pdf"]      = None
            _etiquetas_cache[order_id]["impreso"]  = True
            _etiquetas_cache[order_id]["impreso_ts"] = datetime.now().strftime("%d/%m %H:%M:%S")

    print(f"[CACHE] PDF borrado para {order_id} ({borrado_kb} KB liberados)")
    return jsonify({
        "ok":          True,
        "liberado_kb": borrado_kb,
        "nota":        "PDF borrado del cache. El pedido fue marcado como impreso."
    })


# ═══════════════════════════════════════════════════════════════════════════════
# API PICKING (sync con app_deposito.py)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/ping")
def ping():
    return jsonify({
        "ok":          True,
        "version":     SERVER_VERSION,
        "ts":          _ts(),
        "cargado":     _estado["cargado"],
        "skus":        len(_sku_db),
        "autenticado": bool(_cuentas),
        "cuentas":     [t.get("nickname","?") for t in _cuentas.values()],
        "pedidos_ml":  len(_pedidos_ml),
        "data_dir":    DATA_DIR,
        "persistente": DATA_DIR != "/tmp",
    })


@app.route("/api/tokens_export")
@requiere_api_key
def api_tokens_export():
    """
    Exporta los tokens actuales en formato listo para pegar
    en la variable ML_TOKENS_JSON de Railway.
    Usar despues de conectar ML para persistir entre redeploys.
    """
    if not _cuentas:
        return jsonify({
            "ok":  False,
            "msg": "No hay cuentas conectadas. Conecta ML primero."
        }), 400

    tokens_json = _serializar_cuentas()
    cuentas_info = []
    for cid, tok in _cuentas.items():
        exp = tok.get("expires_at")
        if isinstance(exp, datetime):
            horas = (exp - datetime.now()).total_seconds() / 3600
            exp_str = f"vence en {horas:.1f}h"
        else:
            exp_str = "desconocido"
        cuentas_info.append({
            "cuenta_id": cid,
            "nickname":  tok.get("nickname", "?"),
            "expira":    exp_str,
        })

    return jsonify({
        "ok":            True,
        "instrucciones": [
            "1. Copiar el valor de 'ML_TOKENS_JSON' de abajo",
            "2. Ir a Railway → tu proyecto → picking-server → Variables",
            "3. Crear o editar la variable 'ML_TOKENS_JSON'",
            "4. Pegar el valor copiado",
            "5. Listo — los tokens sobreviven redeploys"
        ],
        "variable_name":  "ML_TOKENS_JSON",
        "ML_TOKENS_JSON": tokens_json,
        "cuentas":        cuentas_info,
    })



@app.route("/api/token_status")
def api_token_status():
    """Estado de los tokens de todas las cuentas."""
    result = []
    for cid, tok in _cuentas.items():
        exp = tok.get("expires_at")
        if isinstance(exp, datetime):
            horas = (exp - datetime.now()).total_seconds() / 3600
            estado = f"Vence en {horas:.1f}h" if horas > 0 else "EXPIRADO"
        else:
            estado = "Sin fecha"
        result.append({
            "cuenta_id":     cid,
            "nickname":      tok.get("nickname","?"),
            "tiene_token":   bool(tok.get("access_token")),
            "tiene_refresh": bool(tok.get("refresh_token")),
            "token_estado":  estado,
        })
    return jsonify({
        "ok":     True,
        "cuentas": result,
        "total":   len(result),
        "ts":      _ts(),
    })



@app.route("/api/debug_logistica")
def debug_logistica():
    """Muestra distribucion de tipos y ejemplos — para diagnosticar filtros."""
    with _lock:
        snap = dict(_pedidos_ml)

    conteo_tipo = {}
    conteo_log  = {}
    ejemplos    = {}

    for oid, p in list(snap.items())[:100]:
        log  = p.get("logistica","") or "(vacio)"
        tipo = p.get("tipo","")     or "(sin_tipo)"
        # Aplicar _calcular_tipo en vivo para comparar
        tipo_calculado = _calcular_tipo(log, p.get("tags",[]), p.get("shipping_id",""))

        conteo_log[log]   = conteo_log.get(log, 0) + 1
        conteo_tipo[tipo] = conteo_tipo.get(tipo, 0) + 1

        if log not in ejemplos:
            ejemplos[log] = {
                "order_id":       oid,
                "logistica":      log,
                "tipo_guardado":  tipo,
                "tipo_calculado": tipo_calculado,
                "shipping_id":    p.get("shipping_id",""),
                "tags":           p.get("tags",[])[:3],
                "estado":         p.get("estado_envio",""),
                "coinciden":      tipo == tipo_calculado,
            }

    return jsonify({
        "total_pedidos":     len(snap),
        "por_tipo_guardado": conteo_tipo,
        "por_logistica":     conteo_log,
        "ejemplos_por_log":  ejemplos,
        "clasificacion": {
            "self_service":  "→ flex ⚡",
            "xd_drop_off":   "→ me2 🚚",
            "cross_docking": "→ me2 🚚",
            "drop_off":      "→ me2 🚚",
            "fulfillment":   "→ me2 🚚",
            "turbo":         "→ me2 🚚",
            "default":       "→ me1 📦",
        }
    })




@app.route("/api/test_credentials")
def test_credentials():
    """
    Prueba las credenciales ML directamente con Client Credentials grant.
    No necesita flujo OAuth. Acceder a:
    https://picking-server-production.up.railway.app/api/test_credentials
    """
    try:
        r = requests.post(
            "https://api.mercadolibre.com/oauth/token",
            headers={
                "Accept":       "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type":    "client_credentials",
                "client_id":     ML_APP_ID,
                "client_secret": ML_SECRET_KEY,
            },
            timeout=10
        )
        return jsonify({
            "status":    r.status_code,
            "app_id_usado": ML_APP_ID,
            "respuesta": r.json() if r.headers.get("content-type","").startswith("application/json") else r.text,
            "credenciales_ok": r.status_code == 200,
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/subir_estado", methods=["POST"])
@requiere_api_key
def subir_estado():
    data = request.get_json(force=True)
    if not data or "grupos" not in data:
        return jsonify({"ok": False, "msg": "Datos inválidos"}), 400
    with _lock:
        nueva_fase = data.get("fase", 1)
        _estado.update({
            "fase":             nueva_fase,
            "grupos":           data.get("grupos", []),
            "total_skus":       data.get("total_skus", 0),
            "total_uds":        data.get("total_uds", 0),
            "colecta":          data.get("colecta", {}),
            "colecta_completa": data.get("colecta_completa", False),
            "ultima_actualizacion": _ts(),
            "cargado":          True,
        })
        # Guardar pedidos del lote si vienen (para Fase 2 — impresion por pedido)
        if "pedidos" in data and isinstance(data["pedidos"], dict):
            _estado["pedidos"] = data["pedidos"]
        # Si es un lote nuevo (fase 1 desde cero), resetear etiquetas impresas
        if nueva_fase == 1 and not data.get("colecta"):
            _estado["etiquetas_impresas"] = []
    # Persistir en /tmp para sobrevivir reinicios de Railway
    try:
        import json as _j
        with open(LOTE_PATH, "w") as f:
            _j.dump(dict(_estado), f)
    except Exception as e:
        print(f"[LOTE] Error persistiendo estado: {e}")
    return jsonify({"ok": True, "msg": f"Estado cargado: {_estado['total_skus']} SKUs"})


@app.route("/api/estado")
def get_estado():
    """Estado del lote para la app móvil."""
    with _lock:
        estado = dict(_estado)
    # Asegurar que cargado sea boolean, no string
    estado["cargado"] = bool(estado.get("cargado", False))
    return jsonify(estado)


def _pedidos_completos_segun_colecta():
    """
    Calcula que pedidos del lote estan completos segun la colecta actual.

    Criterio correcto para deposito con pedidos individuales:
    la colecta global de cada SKU se REPARTE entre los pedidos que lo necesitan,
    en orden. Un pedido esta completo cuando para CADA uno de sus SKUs hay
    stock colectado suficiente asignado a el.

    Ejemplo: 3 clientes piden SKU "6104" (1 c/u). req_total = 3.
      - Colecta 1 unidad → pedido #1 listo (los otros 2 esperan)
      - Colecta 2 unidades → pedidos #1 y #2 listos
      - Colecta 3 unidades → los 3 listos

    Devuelve lista de dicts:
      [{order_id, comprador, shipping_id, _cuenta, items, recien_completado}]
    """
    pedidos = _estado.get("pedidos", {})
    colecta = dict(_estado.get("colecta", {}))  # copia para ir descontando
    impresas = set(_estado.get("etiquetas_impresas", []))

    # Stock disponible por SKU (lo que se colecto)
    disponible = {}
    for s, c in colecta.items():
        disponible[str(s).upper()] = int(c)

    resultado = []
    # Procesar pedidos en orden estable (por order_id) para reparto consistente
    for oid in sorted(pedidos.keys()):
        ped = pedidos[oid]
        items = ped.get("items", [])
        if not items:
            continue

        # Verificar si hay stock suficiente para TODOS los SKUs de este pedido
        completo = True
        for it in items:
            s    = str(it.get("sku","")).upper()
            need = int(it.get("req", it.get("cantidad", 1)))
            if disponible.get(s, 0) < need:
                completo = False
                break

        if completo:
            # Descontar el stock asignado a este pedido
            for it in items:
                s    = str(it.get("sku","")).upper()
                need = int(it.get("req", it.get("cantidad", 1)))
                disponible[s] = disponible.get(s, 0) - need

            recien = oid not in impresas
            resultado.append({
                "order_id":    oid,
                "comprador":   ped.get("comprador",""),
                "shipping_id": ped.get("shipping_id",""),
                "_cuenta":     ped.get("_cuenta","cuenta_0"),
                "items":       items,
                "recien_completado": recien,
            })
    return resultado


@app.route("/api/fase2/pedidos-completos", methods=["GET"])
def fase2_pedidos_completos():
    """
    Devuelve los pedidos completos segun la colecta y cuales son nuevos
    (para imprimir su etiqueta automaticamente).
    La app desktop hace polling aqui durante la Fase 2.
    """
    with _lock:
        completos = _pedidos_completos_segun_colecta()
        fase = _estado.get("fase", 1)
        impresas = list(_estado.get("etiquetas_impresas", []))
    return jsonify({
        "ok": True,
        "fase": fase,
        "completos": completos,
        "ya_impresas": impresas,
    })


@app.route("/api/fase2/marcar-impresa/<order_id>", methods=["POST"])
def fase2_marcar_impresa(order_id):
    """Marca un pedido como ya impreso en Fase 2 para no reimprimir."""
    with _lock:
        if order_id not in _estado.get("etiquetas_impresas", []):
            _estado.setdefault("etiquetas_impresas", []).append(order_id)
        # Tambien marcar el pedido ML como impreso si existe
        if order_id in _pedidos_ml:
            _pedidos_ml[order_id]["impreso"] = True
        try:
            import json as _j
            with open(LOTE_PATH, "w") as f:
                _j.dump(dict(_estado), f)
        except Exception:
            pass
    return jsonify({"ok": True, "order_id": order_id})


@app.route("/api/escanear", methods=["POST"])
def escanear():
    data = request.get_json(force=True)
    sku  = str(data.get("sku","")).strip().upper()
    if not sku:
        return jsonify({"ok": False, "msg": "SKU vacío"})
    with _lock:
        if not _estado["cargado"]:
            return jsonify({"ok": False, "msg": "No hay lote cargado"})
        colecta  = _estado["colecta"]
        sku_info = next((it for g in _estado["grupos"]
                         for it in g.get("items",[]) if it["sku"]==sku), None)
        if not sku_info:
            return jsonify({"ok": False, "tipo": "no_encontrado",
                            "msg": f"'{sku}' no está en ningún pedido"})
        req = sku_info["req"]
        col = colecta.get(sku, 0)
        if col >= req:
            return jsonify({"ok": True, "tipo": "ya_completo",
                            "msg": f"'{sku}' ya completo ({req}/{req})",
                            "collected": col, "req": req})
        colecta[sku] = col + 1
        _estado["ultima_actualizacion"] = _ts()
        todo = all(colecta.get(it["sku"],0) >= it["req"]
                   for g in _estado["grupos"] for it in g["items"])
        _estado["colecta_completa"] = todo
        # Si se completo toda la colecta → pasar automaticamente a Fase 2
        if todo and _estado.get("fase", 1) == 1:
            _estado["fase"] = 2
            print("[FASE] Colecta completa → pasando a Fase 2 automaticamente")
        nuevo = colecta[sku]
    # Persistir colecta actualizada
    try:
        import json as _j
        with open(LOTE_PATH, "w") as f:
            _j.dump(dict(_estado), f)
    except Exception:
        pass
    return jsonify({"ok": True,
                    "tipo":        "completo" if nuevo >= req else "parcial",
                    "sku":         sku,
                    "nombre":      sku_info.get("nombre",""),
                    "pasillo":     sku_info.get("pasillo",""),
                    "estanteria":  sku_info.get("estanteria",""),
                    "collected":   nuevo, "req": req,
                    "todo_completo": todo,
                    "msg": f"✔ {nuevo}/{req}" if nuevo >= req else f"{nuevo}/{req}"})


@app.route("/api/reset_sku", methods=["POST"])
def reset_sku():
    data = request.get_json(force=True)
    sku  = str(data.get("sku","")).strip().upper()
    with _lock:
        col = _estado["colecta"]
        if sku in col and col[sku] > 0:
            col[sku] -= 1
            if col[sku] == 0: del col[sku]
            _estado["colecta_completa"] = False
            _estado["ultima_actualizacion"] = _ts()
            return jsonify({"ok": True, "msg": f"Deshecho: {sku}"})
    return jsonify({"ok": False, "msg": "Nada que deshacer"})


@app.route("/api/limpiar", methods=["POST"])
@requiere_api_key
def limpiar():
    with _lock:
        _estado.update({"grupos":[],"colecta":{},"colecta_completa":False,
                         "cargado":False,"total_skus":0,"total_uds":0,
                         "ultima_actualizacion":_ts()})
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════════
# API BASE DE SKUs
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/skus")
@requiere_auth
def api_skus_list():
    """Devuelve toda la BD de SKUs."""
    q = request.args.get("q", "").strip().upper()
    if q:
        resultado = {k: v for k, v in _sku_db.items()
                     if q in k or q in v.get("nombre","").upper()
                     or q in v.get("pasillo","").upper()}
    else:
        resultado = _sku_db
    return jsonify({"ok": True, "skus": resultado, "total": len(_sku_db)})


@app.route("/api/skus", methods=["POST"])
@requiere_auth
def api_sku_guardar():
    """Crea o actualiza un SKU."""
    data = request.get_json(force=True)
    sku  = str(data.get("sku", "")).strip().upper()
    if not sku:
        return jsonify({"ok": False, "msg": "SKU vacío"}), 400
    nombre     = str(data.get("nombre", "")).strip()
    pasillo    = str(data.get("pasillo", "")).strip()
    estanteria = str(data.get("estanteria", "")).strip()
    if not nombre:
        return jsonify({"ok": False, "msg": "Nombre obligatorio"}), 400
    with _lock:
        _sku_db[sku] = {"nombre": nombre, "pasillo": pasillo, "estanteria": estanteria}
    return jsonify({"ok": True, "sku": sku,
                    "msg": f"SKU '{sku}' guardado correctamente"})


@app.route("/api/skus/<sku>", methods=["DELETE"])
@requiere_auth
def api_sku_eliminar(sku):
    sku = sku.strip().upper()
    with _lock:
        if sku in _sku_db:
            del _sku_db[sku]
            return jsonify({"ok": True, "msg": f"'{sku}' eliminado"})
    return jsonify({"ok": False, "msg": "SKU no encontrado"}), 404


@app.route("/api/skus/importar", methods=["POST"])
@requiere_auth
def api_skus_importar():
    """
    Importa múltiples SKUs desde JSON.
    Body: { "skus": { "SKU1": {nombre, pasillo, estanteria}, ... } }
    Solo agrega los que no existen (no sobreescribe).
    """
    data = request.get_json(force=True)
    nuevos = data.get("skus", {})
    if not isinstance(nuevos, dict):
        return jsonify({"ok": False, "msg": "Formato inválido"}), 400
    agregados = 0
    with _lock:
        for sku, info in nuevos.items():
            k = sku.strip().upper()
            if k and k not in _sku_db:
                _sku_db[k] = {
                    "nombre":     str(info.get("nombre","")).strip(),
                    "pasillo":    str(info.get("pasillo","")).strip(),
                    "estanteria": str(info.get("estanteria","")).strip(),
                }
                agregados += 1
    return jsonify({"ok": True, "agregados": agregados,
                    "total": len(_sku_db),
                    "msg": f"{agregados} SKUs agregados ({len(_sku_db)} en total)"})


@app.route("/api/skus/exportar")
@requiere_auth
def api_skus_exportar():
    """Exporta toda la BD como JSON descargable."""
    resp = app.response_class(
        response=json.dumps(_sku_db, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=skus_db.json"}
    )
    return resp


@app.route("/api/skus/limpiar_todo", methods=["POST"])
@requiere_auth
def api_skus_limpiar():
    with _lock:
        _sku_db.clear()
    return jsonify({"ok": True, "msg": "BD de SKUs limpiada"})


@app.route("/api/imagen-sku/<sku>")
def api_imagen_sku(sku):
    """
    Obtiene la imagen del producto desde MercadoLibre usando el item_id del lote.
    
    Estrategia:
    1. Busca el SKU en los pedidos del lote actual (mejor opción, tiene item_id)
    2. Si no, busca en los grupos del lote
    3. Obtiene item_id asociado
    4. Hace GET /items/{item_id} a MercadoLibre (sin necesidad de token)
    5. Extrae nombre, imagen, precio, disponibilidad
    
    Respuesta:
      {
        "ok": true,
        "existe": true,
        "sku": "8677",
        "item_id": "MLAU123456789",
        "titulo": "Protectores Anti Arrugas...",
        "imagen_url": "https://mli.s3.amazonaws.com/...",
        "precio": 1500,
        "disponible": 10
      }
    """
    sku = str(sku).strip().upper()
    if not sku:
        return jsonify({
            "ok": False, "existe": False, "sku": sku,
            "msg": "SKU vacío", "imagen_url": None
        }), 400

    try:
        item_id = None
        nombre_local = None
        cuenta_id = "cuenta_0"
        
        with _lock:
            # 1. Buscar primero en pedidos (tiene item_id directamente)
            pedidos = _estado.get("pedidos", {})
            for oid, ped in pedidos.items():
                items = ped.get("items", [])
                for item in items:
                    if str(item.get("sku", "")).strip().upper() == sku:
                        item_id = item.get("item_id", "")
                        nombre_local = item.get("nombre", "")
                        cuenta_id = ped.get("_cuenta", "cuenta_0")
                        if item_id:
                            break
                if item_id:
                    break
            
            # 2. Si no encontré en pedidos, buscar en grupos
            if not item_id:
                grupos = _estado.get("grupos", [])
                for grupo in grupos:
                    for item in grupo.get("items", []):
                        if item.get("sku", "").upper() == sku:
                            item_id = item.get("item_id", "")
                            nombre_local = item.get("nombre", "")
                            break
                    if item_id:
                        break
        
        if not item_id:
            return jsonify({
                "ok": True, "existe": False, "sku": sku,
                "imagen_url": None,
                "msg": "SKU no encontrado en el lote actual (falta item_id)"
            }), 200

        # 3. GET /items/{item_id} a MercadoLibre CON TOKEN.
        #    ML ahora exige Authorization en TODOS los recursos, incluso /items.
        #    Probar con la cuenta del pedido; si falla, probar las demás cuentas.
        r_item = None
        cuentas_a_probar = [cuenta_id] + [c for c in _cuentas.keys() if c != cuenta_id]
        for cid in cuentas_a_probar:
            # Asegurar token vigente (renueva si está por expirar)
            try:
                _token_valido_cuenta(cid)
            except Exception:
                pass
            at = _cuentas.get(cid, {}).get("access_token", "")
            if not at:
                continue
            try:
                r_item = requests.get(
                    f"{ML_API_URL}/items/{item_id}",
                    headers={"Authorization": f"Bearer {at}"},
                    timeout=10
                )
                if r_item.status_code == 200:
                    break
            except Exception:
                continue

        if r_item is None or r_item.status_code != 200:
            status = r_item.status_code if r_item is not None else "sin token"
            return jsonify({
                "ok": True, "existe": False, "sku": sku,
                "imagen_url": None,
                "msg": f"No se pudo obtener item de ML (status {status})",
                "item_id": item_id
            }), 200

        item_data = r_item.json()
        titulo = item_data.get("title", nombre_local or sku)
        precio = item_data.get("price", 0)
        disponible = item_data.get("available_quantity", 0)

        # 4. Obtener imagen. ML devuelve varias URLs; SIEMPRE preferir las
        #    seguras (https), porque el móvil corre en https y bloquea
        #    contenido mixto (imágenes http).
        def _https(u):
            if not u:
                return ""
            return u.replace("http://", "https://") if u.startswith("http://") else u

        imagen_url = ""
        pictures = item_data.get("pictures", [])
        if pictures:
            pic = pictures[0]
            # Preferir secure_url > url
            imagen_url = _https(pic.get("secure_url") or pic.get("url") or "")
        if not imagen_url:
            imagen_url = _https(item_data.get("secure_thumbnail")
                                or item_data.get("thumbnail") or "")

        if not imagen_url:
            return jsonify({
                "ok": True, "existe": True, "sku": sku,
                "item_id": item_id, "titulo": titulo[:100],
                "imagen_url": None,
                "msg": "Item encontrado pero sin imagen disponible"
            }), 200

        return jsonify({
            "ok": True,
            "existe": True,
            "sku": sku,
            "item_id": item_id,
            "titulo": titulo[:100],
            "imagen_url": imagen_url,
            "precio": precio,
            "disponible": disponible,
            "ml_url": f"https://articulo.mercadolibre.com.uy/{item_id}"
        }), 200

    except requests.Timeout:
        return jsonify({
            "ok": True, "existe": False, "sku": sku,
            "imagen_url": None,
            "msg": "Timeout descargando desde MercadoLibre (>10s)"
        }), 200
    except Exception as e:
        print(f"[IMAGEN-SKU] Error para {sku}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "ok": True, "existe": False, "sku": sku,
            "imagen_url": None,
            "msg": f"Error interno: {str(e)[:100]}"
        }), 200


@app.route("/api/diag-imagen/<sku>")
def api_diag_imagen(sku):
    """
    DIAGNÓSTICO paso a paso de por qué un SKU no muestra imagen.
    Devuelve un JSON con cada etapa para identificar dónde se corta.
    """
    sku = str(sku).strip().upper()
    diag = {"sku": sku, "pasos": []}

    def paso(nombre, ok, detalle=""):
        diag["pasos"].append({"paso": nombre, "ok": ok, "detalle": str(detalle)[:300]})

    # 1. ¿Hay lote cargado?
    with _lock:
        cargado = bool(_estado.get("cargado"))
        n_grupos = len(_estado.get("grupos", []))
        n_pedidos = len(_estado.get("pedidos", {}))
    paso("lote_cargado", cargado,
         f"grupos={n_grupos}, pedidos={n_pedidos}")

    # 2. ¿Está el SKU en el lote? ¿Tiene item_id?
    item_id = None
    cuenta_id = "cuenta_0"
    origen = None
    with _lock:
        for oid, ped in _estado.get("pedidos", {}).items():
            for item in ped.get("items", []):
                if str(item.get("sku", "")).strip().upper() == sku:
                    item_id = item.get("item_id", "")
                    cuenta_id = ped.get("_cuenta", "cuenta_0")
                    origen = "pedidos"
                    break
            if item_id:
                break
        if not item_id:
            for grupo in _estado.get("grupos", []):
                for item in grupo.get("items", []):
                    if str(item.get("sku", "")).strip().upper() == sku:
                        item_id = item.get("item_id", "")
                        origen = "grupos"
                        break
                if item_id:
                    break
    paso("sku_en_lote", bool(item_id),
         f"item_id={item_id or '(vacío)'}, origen={origen}, cuenta={cuenta_id}")

    if not item_id:
        diag["conclusion"] = ("El SKU no tiene item_id en el lote. "
                              "Hay que regenerar el lote con la versión nueva "
                              "de app_deposito.py que incluye item_id.")
        return jsonify(diag), 200

    # 3. ¿Hay cuentas con token?
    cuentas_con_token = [c for c, t in _cuentas.items() if t.get("access_token")]
    paso("cuentas_con_token", bool(cuentas_con_token),
         f"cuentas={cuentas_con_token}")

    if not cuentas_con_token:
        diag["conclusion"] = ("No hay ninguna cuenta ML con token activo. "
                              "Reconectá MercadoLibre en /auth/login.")
        return jsonify(diag), 200

    # 4. Llamar a ML /items/{id} con token
    cuentas_a_probar = [cuenta_id] + [c for c in _cuentas.keys() if c != cuenta_id]
    item_data = None
    for cid in cuentas_a_probar:
        try:
            _token_valido_cuenta(cid)
        except Exception:
            pass
        at = _cuentas.get(cid, {}).get("access_token", "")
        if not at:
            continue
        try:
            r = requests.get(f"{ML_API_URL}/items/{item_id}",
                             headers={"Authorization": f"Bearer {at}"},
                             timeout=10)
            paso(f"ml_items_{cid}", r.status_code == 200,
                 f"status={r.status_code}, body={r.text[:150]}")
            if r.status_code == 200:
                item_data = r.json()
                break
        except Exception as e:
            paso(f"ml_items_{cid}", False, f"excepción: {e}")

    if not item_data:
        diag["conclusion"] = ("ML rechazó la llamada a /items/{id}. "
                              "Revisá el status arriba (401=token inválido, "
                              "403=sin permiso, 404=item no existe).")
        return jsonify(diag), 200

    # 5. Extraer imagen
    pics = item_data.get("pictures", [])
    thumb = item_data.get("secure_thumbnail") or item_data.get("thumbnail")
    img = ""
    if pics:
        img = pics[0].get("secure_url") or pics[0].get("url") or ""
    if not img:
        img = thumb or ""
    paso("imagen_extraida", bool(img),
         f"n_pictures={len(pics)}, url={img[:120]}")

    diag["resultado"] = {
        "titulo": item_data.get("title", ""),
        "imagen_url": img,
        "precio": item_data.get("price", 0),
        "disponible": item_data.get("available_quantity", 0),
    }
    diag["conclusion"] = ("✅ Todo OK" if img else
                          "El item existe pero no tiene imágenes en ML.")
    return jsonify(diag), 200


# ═══════════════════════════════════════════════════════════════════════════════
# FRONTEND — página principal (requiere auth)
# ═══════════════════════════════════════════════════════════════════════════════

# ── Inicializar al arrancar ───────────────────────────────────────────────────
def _startup():
    """Inicializa el servidor al arrancar."""
    _cargar_sku_db()
    _cargar_tokens_persistidos()
    if not _cuentas:
        _cargar_tokens_local()

    if _cuentas:
        nicks = [t.get("nickname","?") for t in _cuentas.values()]
        print(f"[STARTUP] ✅ Tokens cargados: {nicks}")
        for cid in list(_cuentas.keys()):
            tok = _cuentas[cid]
            exp = tok.get("expires_at")
            if exp and isinstance(exp, str):
                try:
                    tok["expires_at"] = datetime.fromisoformat(exp)
                    _cuentas[cid] = tok
                except Exception:
                    tok["expires_at"] = datetime.now() + timedelta(hours=1)
                    _cuentas[cid] = tok
        threading.Thread(target=_refresh_pedidos_worker, daemon=True).start()
    else:
        print("[STARTUP] ⚠ Sin tokens. Conectar ML desde la app.")

    # Restaurar el último lote enviado por el supervisor
    try:
        import json as _j
        with open(LOTE_PATH) as f:
            data = _j.load(f)
        if data.get("cargado") and data.get("grupos"):
            _estado.update(data)
            print(f"[STARTUP] ✅ Lote restaurado: {data.get('total_skus',0)} SKUs")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[STARTUP] Error restaurando lote: {e}")

_startup()


@app.route("/")
def index():
    if not _token_valido():
        return redirect("/auth/login")
    return _leer_template("app.html")


@app.route("/manifest.json")
def manifest():
    """PWA manifest para la app móvil."""
    return jsonify({
        "name": "Picking App",
        "short_name": "Picking",
        "start_url": "/movil",
        "display": "standalone",
        "background_color": "#0F172A",
        "theme_color": "#1E293B",
        "icons": [
            {"src": "/static/icon.png", "sizes": "192x192", "type": "image/png"}
        ]
    })


@app.route("/movil")
def movil():
    return _leer_template("movil.html")


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("DEBUG", "false").lower() == "true"
    if debug:
        app.run(host="0.0.0.0", port=port, debug=True)
    else:
        try:
            from waitress import serve
            print(f"✅ Servidor iniciado (waitress) en http://0.0.0.0:{port}")
            serve(app, host="0.0.0.0", port=port, threads=8)
        except ImportError:
            print(f"✅ Servidor iniciado (flask dev) en http://0.0.0.0:{port}")
            app.run(host="0.0.0.0", port=port, debug=False)
            app.run(host="0.0.0.0", port=port, debug=False)