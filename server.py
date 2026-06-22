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

def _token_valido():
    if not _tokens.get("access_token"):
        return False
    exp = _tokens.get("expires_at")
    if exp and datetime.now() >= exp - timedelta(minutes=5):
        _renovar_token()
    return bool(_tokens.get("access_token"))


def _ml_get(path, params=None):
    """GET autenticado a la API de ML."""
    headers = {"Authorization": f"Bearer {_tokens.get('access_token', '')}"}
    r = requests.get(f"{ML_API_URL}{path}", headers=headers, params=params, timeout=15)
    return r


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



    """
    Trae todos los pedidos pagados del vendedor usando /orders/search.
    SKU segun jerarquia oficial: variation seller_sku > variation seller_custom_field
    > item seller_sku > item seller_custom_field.
    """
    uid     = _tokens.get("user_id")
    pedidos = {}
    offset  = 0
    limit   = 50

    while True:
        r = _ml_get("/orders/search", params={
            "seller":       uid,
            "order.status": "paid",
            "offset":       offset,
            "limit":        limit,
            "sort":         "date_desc",
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

                # Jerarquia SKU segun documentacion oficial ML
                sku = ""
                for attr in var_attrs:
                    if attr.get("name","").lower() in ("sku","seller_sku"):
                        sku = str(attr.get("value_name","")).strip(); break
                if not sku:
                    sku = str(item_data.get("seller_sku") or
                              item_data.get("seller_custom_field") or "").strip()
                sku = sku.upper() if sku else ""

                color = next((a["value_name"] for a in var_attrs
                              if "color" in a.get("name","").lower()), "")
                talle = next((a["value_name"] for a in var_attrs
                              if a.get("name","").lower() in
                              ("talle","size","talha","talla")), "")
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
            }

        total  = data.get("paging",{}).get("total",0)
        offset += limit
        if offset >= total:
            break

    # Agrupar por pack_id igual que en _ml_get_all_orders_cuenta
    pedidos_agrupados = {}
    packs_vistos = {}
    for oid, ped in pedidos.items():
        pack_id = ped.get("pack_id", "")
        if not pack_id:
            pedidos_agrupados[oid] = ped
        elif pack_id not in packs_vistos:
            packs_vistos[pack_id] = oid
            pedidos_agrupados[oid] = ped
        else:
            oid_p = packs_vistos[pack_id]
            ped_p = pedidos_agrupados[oid_p]
            skus_ex = {it["sku"] for it in ped_p["items"]}
            for it in ped.get("items", []):
                if it["sku"] not in skus_ex:
                    ped_p["items"].append(it)
                    skus_ex.add(it["sku"])
            ped_p["total"] = ped_p.get("total",0) + ped.get("total",0)
    return pedidos_agrupados


def _enriquecer_skus(pedidos):
    """
    Para items sin SKU hace GET /items?ids=... (multiget de 20).
    Tambien obtiene estado de envio.
    """
    item_ids_sin_sku = {}
    for ped in pedidos.values():
        for it in ped["items"]:
            if not it["sku"] and it["item_id"]:
                item_ids_sin_sku[it["item_id"]] = True

    sku_map  = {}
    ids_list = list(item_ids_sin_sku.keys())
    for i in range(0, len(ids_list), 20):
        chunk = ids_list[i:i+20]
        r = _ml_get("/items", params={"ids": ",".join(chunk)})
        if r.status_code == 200:
            for entry in r.json():
                body = entry.get("body", {})
                iid  = body.get("id","")
                sku  = str(body.get("seller_sku") or
                           body.get("seller_custom_field") or "").strip().upper()
                if iid and sku:
                    sku_map[iid] = sku

    for ped in pedidos.values():
        for it in ped["items"]:
            if not it["sku"] and it["item_id"] in sku_map:
                it["sku"] = sku_map[it["item_id"]]
        if ped.get("shipping_id"):
            try:
                rs = _ml_get(f"/shipments/{ped['shipping_id']}")
                if rs.status_code == 200:
                    sd = rs.json()
                    log_new = (sd.get("logistic") or {}).get("type", "")
                    log_old = sd.get("logistic_type","")
                    logistica = log_new or log_old
                    status    = sd.get("status","")
                    substatus = sd.get("substatus","")
                    ped["logistica"]    = logistica
                    ped["estado_envio"] = status
                    ped["substatus"]    = substatus
                    ped["tipo"] = _calcular_tipo(
                        logistica, ped.get("tags",[]), ped.get("shipping_id",""))
                    # Sincronizar impreso desde substatus ML
                    ESTADOS_IMPRESOS = {
                        "printed","shipped","delivered",
                        "not_delivered","cancelled"
                    }
                    if substatus in ESTADOS_IMPRESOS or status in (
                            "shipped","delivered","not_delivered","cancelled"):
                        ped["impreso"] = True
                    elif substatus == "ready_to_print":
                        ped["impreso"] = False
                    elif status == "ready_to_ship" and not substatus:
                        ped["impreso"] = False
            except Exception:
                pass
        elif ped.get("logistica"):
            ped["tipo"] = _calcular_tipo(
                ped["logistica"], ped.get("tags",[]), ped.get("shipping_id",""))

    return pedidos


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


@app.route("/api/etiqueta/<order_id>")
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
        cached = _etiquetas_cache.get(order_id)
        snap   = dict(_pedidos_ml)

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

    # ── 2. Buscar pedido en memoria ───────────────────────────────────────────
    pedido   = snap.get(order_id)
    real_oid = order_id
    if not pedido and len(order_id) >= 8:
        sufijo = order_id[-8:]
        for k, v in snap.items():
            if k.endswith(sufijo) or k[:8] == order_id[:8]:
                pedido   = v
                real_oid = k
                break

    if not pedido:
        return _html_error(
            "Pedido no encontrado",
            f"El pedido #{order_id} no esta en la lista. "
            f"Hay {len(snap)} pedidos en memoria.<br>"
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
        return Response(
            pdf_content, status=200, mimetype="application/pdf",
            headers={"Content-Disposition": f"inline; filename=etiqueta_{shipping_id}.pdf",
                     "Content-Type": "application/pdf"})

    # PDF no disponible - consultar estado real
    estado = substatus = logistic = ""
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
    """Descarga directa redirigiendo a ML con token en URL."""
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

        url = (f"{ML_API_URL}/shipment_labels"
               f"?shipment_ids={shipping_id}"
               f"&response_type=pdf2"
               f"&access_token={at}")
        from flask import redirect as r2
        return r2(url)
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
    return render_template_string(HTML_APP)


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
    return render_template_string(HTML_MOVIL)


# ═══════════════════════════════════════════════════════════════════════════════
# HTML — APP PRINCIPAL (PC + tablet)
# ═══════════════════════════════════════════════════════════════════════════════

HTML_APP = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Logibot · Logibot · Sistema de Picking Pro</title>
<style>
:root{--bg:#111318;--panel:#1C2030;--card:#161B27;--border:#2A3352;
  --accent:#2563EB;--accent2:#1D4ED8;--success:#0EA5E9;--warning:#F59E0B;
  --danger:#EF4444;--hi:#F1F5F9;--mid:#94A3B8;--lo:#475569;--bar:#1E3A5F;
  --ml:#FFE600}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--hi);font-family:'Segoe UI',system-ui,sans-serif;height:100vh;display:flex;flex-direction:column}
/* TOPBAR */
.topbar{background:var(--panel);border-bottom:1px solid var(--border);
  padding:0 20px;height:56px;display:flex;align-items:center;gap:12px;flex-shrink:0}
.logo{font-size:22px}
.topbar-title{font-size:15px;font-weight:800;color:var(--hi)}
.topbar-sub{font-size:11px;color:var(--mid)}
.spacer{flex:1}
.user-chip{background:var(--card);border:1px solid var(--border);border-radius:20px;
  padding:4px 12px;font-size:12px;color:var(--mid);display:flex;align-items:center;gap:6px}
.ml-dot{width:8px;height:8px;border-radius:50%;background:var(--ml)}
.btn{border:none;border-radius:8px;padding:7px 16px;font-size:13px;font-weight:700;
  cursor:pointer;display:inline-flex;align-items:center;gap:6px}
.btn-primary{background:var(--accent);color:#fff}
.btn-primary:hover{background:#2563EB}
.btn-success{background:var(--success);color:#fff}
.btn-success:hover{background:#059669}
.btn-warn{background:var(--warning);color:#000}
.btn-danger{background:var(--danger);color:#fff}
.btn-ghost{background:var(--card);color:var(--mid);border:1px solid var(--border)}
.btn-ghost:hover{background:var(--border)}
.btn-ghost.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.btn-ml{background:var(--ml);color:#000}
.btn-ml:hover{background:#EAD700}
.btn:disabled{opacity:.4;cursor:not-allowed}
/* TABS */
.tabs{background:var(--panel);border-bottom:1px solid var(--border);
  display:flex;padding:0 20px;flex-shrink:0}
.tab{padding:12px 20px;font-size:13px;font-weight:700;color:var(--mid);
  cursor:pointer;border-bottom:3px solid transparent;transition:.15s}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab:hover:not(.active){color:var(--hi)}
/* CONTENT */
.content{flex:1;overflow:hidden;display:flex;flex-direction:column}
.tab-panel{display:none;flex:1;overflow:hidden}
.tab-panel.active{display:flex;flex-direction:column}
/* TOOLBAR */
.toolbar{padding:12px 20px;background:var(--panel);border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:10px;flex-wrap:wrap;flex-shrink:0}
.search-input{background:var(--card);border:1px solid var(--border);color:var(--hi);
  border-radius:8px;padding:7px 12px;font-size:13px;outline:none;width:260px}
.search-input:focus{border-color:var(--accent)}
.badge-count{background:var(--accent);color:#fff;font-size:11px;font-weight:700;
  padding:2px 8px;border-radius:12px}
/* TABLA PEDIDOS */
.tabla-wrap{flex:1;overflow-y:auto;padding:16px 20px}
.tabla{width:100%;border-collapse:collapse}
.tabla th{text-align:left;font-size:11px;font-weight:700;color:var(--lo);
  letter-spacing:.06em;padding:8px 12px;background:var(--bar);
  border-bottom:1px solid var(--border);position:sticky;top:0;z-index:10}
.tabla td{padding:10px 12px;border-bottom:1px solid rgba(51,65,85,.5);
  font-size:13px;vertical-align:middle}
.tabla tr:hover td{background:rgba(59,130,246,.05)}
.tabla tr.impreso td{opacity:.45}
.sku-chip{background:var(--card);border:1px solid var(--border);border-radius:4px;
  padding:1px 6px;font-family:monospace;font-size:11px;color:var(--accent);
  display:inline-block;margin:1px}
.estado-chip{border-radius:20px;padding:2px 10px;font-size:11px;font-weight:700;display:inline-block}
.estado-paid{background:rgba(16,185,129,.15);color:var(--success)}
.estado-ready{background:rgba(59,130,246,.15);color:var(--accent)}
.estado-pending{background:rgba(245,158,11,.15);color:var(--warning)}
/* PICKING PANEL */
.picking-wrap{display:flex;gap:0;flex:1;overflow:hidden}
.picking-left{width:340px;flex-shrink:0;overflow-y:auto;
  border-right:1px solid var(--border);background:var(--panel)}
.picking-center{flex:1;overflow-y:auto;padding:20px}
.picking-right{width:320px;flex-shrink:0;overflow-y:auto;
  border-left:1px solid var(--border);padding:16px;background:var(--panel)}
/* SKU GROUP */
.grupo-hdr{background:var(--bar);padding:8px 14px;
  display:flex;justify-content:space-between;align-items:center;
  cursor:pointer;border-bottom:1px solid var(--border)}
.grupo-nombre{font-size:12px;font-weight:800;color:var(--accent);text-transform:uppercase}
.grupo-prog{font-size:12px;font-weight:700}
.grupo-prog.done{color:var(--success)}
.sku-row{display:flex;align-items:center;gap:8px;padding:9px 14px;
  border-bottom:1px solid rgba(51,65,85,.4)}
.sku-row.done{opacity:.5}
.sku-chk{font-size:18px;width:24px;text-align:center;flex-shrink:0}
.sku-chk.ok{color:var(--success)}
.sku-body{flex:1;min-width:0}
.sku-name{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sku-meta{display:flex;align-items:center;gap:8px;margin-top:2px}
.sku-code{font-family:monospace;font-size:11px;color:var(--mid)}
.sku-cnt{font-size:12px;font-weight:700}
.sku-cnt.ok{color:var(--success)}
.sku-cnt.pend{color:var(--accent)}
.sku-loc{font-size:10px;color:var(--accent2);margin-top:1px}
/* SCAN BOX */
.scan-section{padding:14px;background:var(--card);border-radius:10px;margin-bottom:14px;
  border:1px solid var(--border)}
.scan-lbl{font-size:10px;font-weight:700;color:var(--lo);letter-spacing:.08em;margin-bottom:8px}
#scan-input{width:100%;background:var(--bg);border:2px solid var(--accent);color:var(--hi);
  font-size:18px;font-family:monospace;font-weight:700;padding:10px 14px;
  border-radius:8px;outline:none;text-align:center;text-transform:uppercase}
#scan-fb{margin-top:8px;min-height:34px;border-radius:8px;padding:7px 12px;
  font-size:13px;font-weight:600;display:flex;align-items:center;gap:8px}
#scan-fb.ok{background:rgba(16,185,129,.15);color:var(--success)}
#scan-fb.warn{background:rgba(245,158,11,.15);color:var(--warning)}
#scan-fb.err{background:rgba(239,68,68,.15);color:var(--danger)}
#scan-fb.neu{color:var(--mid)}
/* FASE BADGE */
.fase-badge{border-radius:20px;padding:4px 14px;font-size:12px;font-weight:800;display:inline-block}
.fase-1{background:rgba(59,130,246,.2);color:var(--accent)}
.fase-2{background:rgba(16,185,129,.2);color:var(--success)}
/* STATS */
.stats-row{display:flex;gap:12px;margin-bottom:14px;flex-wrap:wrap}
.stat{background:var(--card);border:1px solid var(--border);border-radius:8px;
  padding:10px 16px;min-width:110px}
.stat-n{font-size:22px;font-weight:800;color:var(--accent)}
.stat-l{font-size:10px;color:var(--lo);margin-top:2px}
/* MODAL */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:1000;
  display:flex;align-items:center;justify-content:center;display:none}
.modal-bg.open{display:flex}
.modal{background:var(--panel);border:1px solid var(--border);border-radius:12px;
  padding:24px;width:540px;max-width:95vw;max-height:90vh;overflow-y:auto}
.modal h3{font-size:16px;margin-bottom:14px}
/* SPINNER */
.spin{animation:spin .7s linear infinite;display:inline-block}
@keyframes spin{to{transform:rotate(360deg)}}
/* TOAST */
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(80px);
  background:var(--panel);border:1px solid var(--border);color:var(--hi);
  padding:10px 22px;border-radius:30px;font-size:14px;font-weight:600;z-index:9999;
  transition:transform .3s,opacity .3s;opacity:0;white-space:nowrap}
#toast.show{transform:translateX(-50%) translateY(0);opacity:1}
#toast.ok{border-color:var(--success);color:var(--success)}
#toast.err{border-color:var(--danger);color:var(--danger)}
/* EMPTY */
.empty{text-align:center;padding:60px 20px;color:var(--lo)}
.empty-i{font-size:48px;margin-bottom:12px}
</style>
</head>
<body>

<!-- TOPBAR -->
<div class="topbar">
  <span class="logo">⬡</span>
  <div><div class="topbar-title">Sistema de Picking</div>
    <div class="topbar-sub">MercadoLibre Uruguay</div></div>
  <div class="spacer"></div>
  <div class="user-chip">
    <div class="ml-dot"></div>
    <span id="nickname">Cargando…</span>
  </div>
  <button class="btn btn-ghost" onclick="location='/auth/logout'">Cerrar sesión</button>
</div>

<!-- TABS -->
<div class="tabs">
  <div class="tab active" onclick="showTab('pedidos')">📋 Pedidos ML</div>
  <div class="tab" onclick="showTab('picking')">📦 Picking</div>
  <div class="tab" onclick="showTab('skudb')">🗃 Base de SKUs</div>
  <div class="tab" onclick="showTab('movil-link')">📱 App Móvil</div>
</div>

<div class="content">

  <!-- ═══════════════════════════════════════ TAB PEDIDOS ══════════════════ -->
  <div class="tab-panel active" id="tab-pedidos">

    <!-- Filtros de fecha -->
    <div class="toolbar" style="background:var(--panel);border-bottom:1px solid var(--border);
         padding:8px 14px;gap:10px;flex-wrap:wrap">
      <span style="font-size:11px;font-weight:700;color:var(--lo);letter-spacing:.06em">
        PERIODO
      </span>
      <!-- Botones rápidos -->
      <div style="display:flex;gap:4px">
        <button class="btn btn-ghost fecha-rapida active" data-dias="0"
                onclick="setFechaRapida(0,this)">Hoy</button>
        <button class="btn btn-ghost fecha-rapida" data-dias="1"
                onclick="setFechaRapida(1,this)">Ayer</button>
        <button class="btn btn-ghost fecha-rapida" data-dias="7"
                onclick="setFechaRapida(7,this)">7 días</button>
        <button class="btn btn-ghost fecha-rapida" data-dias="14"
                onclick="setFechaRapida(14,this)">14 días</button>
        <button class="btn btn-ghost fecha-rapida" data-dias="30"
                onclick="setFechaRapida(30,this)">30 días</button>      </div>
      <!-- Rango personalizado -->
      <div style="display:flex;align-items:center;gap:6px">
        <span style="font-size:11px;color:var(--lo)">Desde</span>
        <input type="date" id="fecha-desde"
               style="background:var(--card);border:1px solid var(--border);
               color:var(--hi);padding:4px 8px;border-radius:6px;font-size:12px">
        <span style="font-size:11px;color:var(--lo)">Hasta</span>
        <input type="date" id="fecha-hasta"
               style="background:var(--card);border:1px solid var(--border);
               color:var(--hi);padding:4px 8px;border-radius:6px;font-size:12px">
        <button class="btn btn-ml" onclick="buscarConFecha()"
                style="padding:4px 12px;font-size:12px">
          Buscar
        </button>
      </div>
      <div class="spacer"></div>
      <span id="lbl-periodo" style="font-size:11px;color:var(--accent)"></span>
    </div>

    <div class="toolbar">
      <input class="search-input" type="text" id="search-pedidos"
             placeholder="🔍 Buscar por comprador, SKU, producto…"
             oninput="filtrarPedidos()">
      <span class="badge-count" id="badge-pedidos">0</span>
      <div class="spacer"></div>
      <!-- Resumen impresos vs pendientes -->
      <span id="resumen-pedidos" style="font-size:12px;margin-right:12px"></span>
      <button class="btn btn-ghost" onclick="refreshPedidos()" id="btn-refresh">
        🔄 Actualizar
      </button>
      <button class="btn btn-success" onclick="generarLotePicking()" id="btn-lote">
        ▶ Generar Lote de Picking
      </button>
    </div>
    <!-- Sub-tabs tipo logistica + filtro impresos -->
    <div style="display:flex;align-items:center;gap:4px;padding:6px 14px;
                background:var(--bg);border-bottom:1px solid var(--border);flex-wrap:wrap">
      <button class="btn btn-ghost tipo-tab active-tipo"
              onclick="setTipoFiltro('todos',this)"
              style="background:var(--accent);color:#fff;padding:4px 14px;font-size:12px">
        Todos
      </button>
      <button class="btn btn-ghost tipo-tab" onclick="setTipoFiltro('flex',this)"
              style="padding:4px 14px;font-size:12px">⚡ Flex</button>
      <button class="btn btn-ghost tipo-tab" onclick="setTipoFiltro('me2',this)"
              style="padding:4px 14px;font-size:12px">🚚 Mercado Envíos</button>
      <button class="btn btn-ghost tipo-tab" onclick="setTipoFiltro('me1',this)"
              style="padding:4px 14px;font-size:12px">📦 ME1</button>
      <div style="flex:1;min-width:12px"></div>
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;
                    color:var(--mid);cursor:pointer;white-space:nowrap">
        <input type="checkbox" id="chk-solo-pendientes"
               onchange="filtrarPedidos()"
               style="accent-color:var(--warning)">
        ⏳ Solo pendientes
      </label>
    </div>
    <div class="tabla-wrap">
      <table class="tabla" id="tabla-pedidos">
        <thead>
          <tr>
            <th>Tipo / Estado</th>
            <th>Pedido</th>
            <th>Comprador</th>
            <th style="width:38%">SKU · Nombre del Producto</th>
            <th>Estado Envío</th>
            <th>Acciones</th>
          </tr>
        </thead>
        <tbody id="tbody-pedidos">
          <tr><td colspan="6" class="empty">
            <div class="empty-i">⏳</div>
            <div>Cargando pedidos…</div>
          </td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- ═══════════════════════════════════════ TAB PICKING ══════════════════ -->
  <div class="tab-panel" id="tab-picking">
    <div class="toolbar">
      <span id="fase-badge" class="fase-badge fase-1">● FASE 1: COLECTA</span>
      <div class="spacer"></div>
      <button class="btn btn-ghost" id="btn-fase2" onclick="pasarFase2()" disabled>
        ▶ Pasar a Fase 2
      </button>
    </div>
    <div class="picking-wrap">

      <!-- Lista de SKUs por pasillo -->
      <div class="picking-left" id="picking-lista">
        <div class="empty" style="padding:40px 20px">
          <div class="empty-i">📋</div>
          <div style="font-size:13px">Generá un lote desde la pestaña Pedidos ML</div>
        </div>
      </div>

      <!-- Centro: escaneo -->
      <div class="picking-center">
        <div class="stats-row" id="stats-picking"></div>

        <div class="scan-section">
          <div class="scan-lbl">ESCANEAR CÓDIGO</div>
          <input id="scan-input" type="text" autocomplete="off"
                 autocorrect="off" autocapitalize="characters" spellcheck="false"
                 placeholder="SKU…" onkeydown="onScanKey(event)">
          <div id="scan-fb" class="neu">Listo para escanear</div>
        </div>

        <div id="picking-cajas"></div>
      </div>

      <!-- Panel derecho: info -->
      <div class="picking-right">
        <div style="font-size:12px;font-weight:800;color:var(--lo);letter-spacing:.06em;margin-bottom:12px">
          RESUMEN DE PEDIDOS
        </div>
        <div id="resumen-pedidos-picking"></div>
      </div>

    </div>
  </div>

  <!-- ═════════════════════════════════════ TAB SKU DB ════════════════════ -->
  <div class="tab-panel" id="tab-skudb">
    <div class="toolbar">
      <input class="search-input" type="text" id="search-skus"
             placeholder="🔍 Buscar por SKU, nombre o pasillo…"
             oninput="filtrarSkus()">
      <span class="badge-count" id="badge-skus">0</span>
      <div class="spacer"></div>
      <button class="btn btn-ghost" onclick="exportarSkus()">⬇ Exportar JSON</button>
      <button class="btn btn-ghost" onclick="document.getElementById('import-file').click()">
        ⬆ Importar JSON
      </button>
      <input type="file" id="import-file" accept=".json" style="display:none"
             onchange="importarSkus(event)">
      <button class="btn btn-success" onclick="abrirFormSku(null)">➕ Nuevo SKU</button>
    </div>

    <!-- Tabla -->
    <div class="tabla-wrap">
      <table class="tabla" id="tabla-skus">
        <thead>
          <tr>
            <th style="width:130px">SKU</th>
            <th>Nombre del Producto</th>
            <th style="width:180px">Pasillo</th>
            <th style="width:150px">Estantería</th>
            <th style="width:90px">Acciones</th>
          </tr>
        </thead>
        <tbody id="tbody-skus">
          <tr><td colspan="5" class="empty">
            <div class="empty-i">🗃</div>
            <div>Cargando base de SKUs…</div>
          </td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- ═════════════════════════════════════ TAB MÓVIL ══════════════════════ -->
  <div class="tab-panel" id="tab-movil-link">
    <div style="padding:40px;text-align:center">
      <div style="font-size:48px;margin-bottom:16px">📱</div>
      <div style="font-size:18px;font-weight:800;margin-bottom:8px">App Móvil para Operarios</div>
      <div style="color:var(--mid);margin-bottom:24px">Compartí este enlace con los operarios del depósito</div>
      <div id="movil-url" style="background:var(--card);border:1px solid var(--border);
        border-radius:10px;padding:16px 24px;font-size:16px;font-family:monospace;
        color:var(--accent);margin-bottom:20px;display:inline-block"></div>
      <br>
      <button class="btn btn-primary" onclick="copiarUrl()">📋 Copiar enlace</button>
      <a id="abrir-movil" href="/movil" target="_blank">
        <button class="btn btn-ghost" style="margin-left:8px">🔗 Abrir en nueva pestaña</button>
      </a>
    </div>
  </div>

</div><!-- /content -->

<!-- MODAL ETIQUETA -->
<div class="modal-bg" id="modal-etiqueta">
  <div class="modal">
    <h3>🏷️ Etiqueta de envío</h3>
    <div id="modal-etiqueta-body"></div>
    <div style="margin-top:16px;display:flex;gap:8px;justify-content:flex-end">
      <button class="btn btn-ghost" onclick="cerrarModal('modal-etiqueta')">Cerrar</button>
      <button class="btn btn-ml" id="btn-abrir-etiqueta" onclick="abrirEtiqueta()">
        🖨️ Abrir / Imprimir PDF
      </button>
    </div>
  </div>
</div>

<!-- MODAL FORM SKU -->
<div class="modal-bg" id="modal-sku">
  <div class="modal" style="width:480px">
    <h3 id="modal-sku-title">➕ Nuevo SKU</h3>
    <div style="display:flex;flex-direction:column;gap:12px">
      <div>
        <label style="font-size:11px;font-weight:700;color:var(--lo);letter-spacing:.06em;display:block;margin-bottom:4px">SKU *</label>
        <input id="form-sku" class="search-input" style="width:100%" type="text"
               placeholder="Ej: 7478" autocapitalize="characters">
      </div>
      <div>
        <label style="font-size:11px;font-weight:700;color:var(--lo);letter-spacing:.06em;display:block;margin-bottom:4px">NOMBRE DEL PRODUCTO *</label>
        <input id="form-nombre" class="search-input" style="width:100%" type="text"
               placeholder="Ej: Caja Organizadora De Cosméticos">
      </div>
      <div>
        <label style="font-size:11px;font-weight:700;color:var(--lo);letter-spacing:.06em;display:block;margin-bottom:4px">PASILLO</label>
        <input id="form-pasillo" class="search-input" style="width:100%" type="text"
               placeholder="Ej: Pasillo 1 · Belleza-Gym">
        <div id="pasillo-sugs" style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px"></div>
      </div>
      <div>
        <label style="font-size:11px;font-weight:700;color:var(--lo);letter-spacing:.06em;display:block;margin-bottom:4px">ESTANTERÍA / UBICACIÓN</label>
        <input id="form-estanteria" class="search-input" style="width:100%" type="text"
               placeholder="Ej: Estantería A4">
      </div>
      <div id="form-error" style="color:var(--danger);font-size:13px;display:none"></div>
    </div>
    <div style="margin-top:20px;display:flex;gap:8px;justify-content:flex-end">
      <button class="btn btn-ghost" onclick="cerrarModal('modal-sku')">Cancelar</button>
      <button class="btn btn-success" onclick="guardarSku()">✅ Guardar SKU</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
// ════════════════════════════════════════════════════════════
// ESTADO LOCAL
// ════════════════════════════════════════════════════════════
let PEDIDOS     = {};
let ESTADO_PK   = null;
let FASE_ACTUAL = 1;
let COLECTA     = {};
// Estado del monitor de Fase 2 (impresion automatica de etiquetas)
let _fase2Activo      = false;
let _fase2Cola        = [];        // order_ids pendientes de imprimir
let _fase2Imprimiendo = false;
let _fase2YaImpresas  = new Set();
let LOTE_IDS    = [];   // order_ids incluidos en el lote actual
let etiquetaUrlActual = "";

// ════════════════════════════════════════════════════════════
// INIT
// ════════════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
  cargarStatus();
  cargarPedidos();
  document.getElementById('movil-url').textContent = location.origin + '/movil';
  setInterval(cargarPedidosQuiet, 120000);  // auto-refresh cada 2 min
  setInterval(syncEstadoPicking, 2000);     // sync picking cada 2s (tiempo real)
});

async function cargarStatus() {
  const r = await fetch('/auth/status');
  const d = await r.json();
  document.getElementById('nickname').textContent = d.nickname || 'Usuario ML';
}

// ════════════════════════════════════════════════════════════
// TABS
// ════════════════════════════════════════════════════════════
function showTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelector(`.tab[onclick="showTab('${name}')"]`).classList.add('active');
  document.getElementById(`tab-${name}`).classList.add('active');
  if (name === 'picking') renderPicking();
}

// ════════════════════════════════════════════════════════════
// PEDIDOS
// ════════════════════════════════════════════════════════════
async function cargarPedidos() {
  try {
    const r = await fetch('/api/pedidos');
    const d = await r.json();
    if (d.ok) { PEDIDOS = {}; d.pedidos.forEach(p => PEDIDOS[p.order_id] = p); }
    renderPedidos();
  } catch(e) { toast('Error cargando pedidos', 'err'); }
}
async function cargarPedidosQuiet() {
  try {
    const r = await fetch('/api/pedidos'); const d = await r.json();
    if (d.ok) { PEDIDOS = {}; d.pedidos.forEach(p => PEDIDOS[p.order_id] = p); renderPedidos(); }
  } catch(e) {}
}
// ── Filtros de fecha ──────────────────────────────────────────────────────
let FECHA_DESDE = null;
let FECHA_HASTA = null;

function hoy() {
  return new Date().toISOString().slice(0,10);
}

function diasAtras(n) {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString().slice(0,10);
}

function setFechaRapida(dias, btn) {
  // Resaltar botón activo
  document.querySelectorAll('.fecha-rapida').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');

  if (dias === 0) {
    FECHA_DESDE = hoy();
    FECHA_HASTA = hoy();
  } else if (dias === 1) {
    FECHA_DESDE = diasAtras(1);
    FECHA_HASTA = diasAtras(1);
  } else {
    FECHA_DESDE = diasAtras(dias);
    FECHA_HASTA = hoy();
  }

  document.getElementById('fecha-desde').value = FECHA_DESDE;
  document.getElementById('fecha-hasta').value  = FECHA_HASTA;
  actualizarLblPeriodo();
  refreshPedidos();
}

function buscarConFecha() {
  FECHA_DESDE = document.getElementById('fecha-desde').value || null;
  FECHA_HASTA = document.getElementById('fecha-hasta').value || null;
  // Quitar resaltado de botones rápidos
  document.querySelectorAll('.fecha-rapida').forEach(b => b.classList.remove('active'));
  actualizarLblPeriodo();
  refreshPedidos();
}

function actualizarLblPeriodo() {
  const lbl = document.getElementById('lbl-periodo');
  if (!lbl) return;
  if (FECHA_DESDE === FECHA_HASTA && FECHA_DESDE === hoy()) {
    lbl.textContent = 'Mostrando: Hoy';
  } else if (FECHA_DESDE && FECHA_HASTA) {
    lbl.textContent = `Mostrando: ${FECHA_DESDE} → ${FECHA_HASTA}`;
  } else {
    lbl.textContent = '';
  }
}

async function refreshPedidos() {
  const btn = document.getElementById('btn-refresh');
  btn.innerHTML = '<span class="spin">🔄</span> Actualizando…';
  btn.disabled  = true;

  // Enviar filtros de fecha al servidor
  const body = {};
  if (FECHA_DESDE) body.fecha_desde = FECHA_DESDE;
  if (FECHA_HASTA) body.fecha_hasta = FECHA_HASTA;

  await fetch('/api/pedidos/refresh', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  await new Promise(r => setTimeout(r, 3000));
  await cargarPedidos();
  btn.innerHTML = '🔄 Actualizar';
  btn.disabled  = false;
  actualizarLblPeriodo();
  toast(`Pedidos actualizados${FECHA_DESDE ? ' ('+FECHA_DESDE+')' : ''}`, 'ok');
}

// Inicializar fechas al cargar: mostrar hoy por defecto
document.addEventListener('DOMContentLoaded', () => {
  FECHA_DESDE = diasAtras(7);
  FECHA_HASTA = hoy();
  document.getElementById('fecha-desde').value = FECHA_DESDE;
  document.getElementById('fecha-hasta').value  = FECHA_HASTA;
  // Marcar "7 días" como activo por defecto
  const btn7 = document.querySelector('.fecha-rapida[data-dias="7"]');
  if (btn7) btn7.classList.add('active');
  document.querySelector('.fecha-rapida[data-dias="0"]').classList.remove('active');
  actualizarLblPeriodo();
});

function tipoLogistica(p) {
  const log  = (p.logistica || '').toLowerCase().trim();
  const tags = (p.tags || []).map(t => t.toLowerCase()).join(' ');

  if (log === 'self_service')                                        return 'flex';
  if (['cross_docking','drop_off','xd_drop_off',
       'fulfillment','turbo','xd_same_day'].includes(log))          return 'me2';
  if (['default','custom','not_specified'].includes(log))           return 'me1';

  if (tags.includes('self_service') || tags.includes('flex'))      return 'flex';

  const tipo = (p.tipo || '').toLowerCase();
  if (['flex','me1','me2'].includes(tipo))                          return tipo;

  // Sin info confirmada → desconocido (NO asumir me2)
  return 'desconocido';
}

function renderPedidos(filtro='') {
  const tbody  = document.getElementById('tbody-pedidos');
  const tipoFiltro = window._tipoFiltroActivo || 'todos';
  const cuentaFiltro = window._cuentaFiltroActiva || 'todas';

  let peds = Object.values(PEDIDOS);

  // Filtrar por cuenta
  if (cuentaFiltro !== 'todas')
    peds = peds.filter(p => p._cuenta === cuentaFiltro);

  // Filtrar por tipo logistica
  if (tipoFiltro !== 'todos')
    peds = peds.filter(p => tipoLogistica(p) === tipoFiltro);

  // Filtrar solo pendientes si está marcado
  const soloPend = document.getElementById('chk-solo-pendientes');
  if (soloPend && soloPend.checked)
    peds = peds.filter(p => !p.impreso);

  // Filtrar por texto de búsqueda
  const q = filtro.toLowerCase();
  if (q) {
    peds = peds.filter(p =>
      p.comprador.toLowerCase().includes(q) ||
      p.order_id.includes(q) ||
      p.items.some(it => it.titulo.toLowerCase().includes(q) ||
                         it.sku.toLowerCase().includes(q)));
  }

  // Contar impresos vs pendientes
  const total     = peds.length;
  const impresos  = peds.filter(p => p.impreso).length;
  const pendientes= total - impresos;

  document.getElementById('badge-pedidos').textContent = total;

  // Actualizar resumen
  const resEl = document.getElementById('resumen-pedidos');
  if (resEl) {
    resEl.innerHTML =
      `<span style="color:var(--success)">✅ ${impresos} impresos</span>` +
      `<span style="margin:0 8px;color:var(--lo)">·</span>` +
      `<span style="color:var(--warning)">⏳ ${pendientes} pendientes</span>` +
      `<span style="margin:0 8px;color:var(--lo)">·</span>` +
      `<span style="color:var(--mid)">${total} total</span>`;
  }

  if (!peds.length) {
    tbody.innerHTML = `<tr><td colspan="6"><div class="empty">
      <div class="empty-i">📋</div>
      <div>Sin pedidos${filtro?' que coincidan':' en este filtro'}</div>
    </div></td></tr>`;
    return;
  }

  // Ordenar: pendientes primero, luego impresos
  peds.sort((a,b) => {
    if (a.impreso !== b.impreso) return a.impreso ? 1 : -1;
    return b.order_id.localeCompare(a.order_id);
  });

  tbody.innerHTML = peds.map(p => {
    const impreso = p.impreso;
    const tipo    = tipoLogistica(p);

    // Chip de tipo con color Y sub-tipo segun doc ML
    const TIPO_CFG = {
      flex:        {color:'#7C3AED', label:'⚡ FLEX'},
      me2:         {color:'#2563EB', label:'🚚 ME2'},
      me1:         {color:'#0891B2', label:'📦 ME1'},
      desconocido: {color:'#475569', label:'—'},
    };

    // Sub-tipo especifico dentro de ME2
    const SUBTIPO = {
      'self_service':  '⚡ Flex',
      'cross_docking': '🚚 Colecta',
      'xd_drop_off':   '📍 Places',
      'drop_off':      '🏪 Drop Off',
      'fulfillment':   '🏭 Full',
      'turbo':         '⚡ Turbo',
      'default':       '📦 ME1',
    };

    const tc     = TIPO_CFG[tipo] || TIPO_CFG.desconocido;
    const logRaw = (p.logistica || '').toLowerCase();
    const subLabel = SUBTIPO[logRaw] || tc.label;
    const tipoChip = `<span style="background:${tc.color};color:#fff;padding:2px 8px;
      border-radius:4px;font-size:10px;font-weight:700;white-space:nowrap">${subLabel}</span>`;

    // SKUs
    const skus = p.items.map(it => {
      const sku = it.sku ? `<span class="sku-chip">${it.sku}</span>` : '';
      const nom = it.titulo ? `<span style="color:${impreso?'var(--mid)':'var(--hi)'}">${it.titulo.substring(0,28)}</span>` : '';
      const qty = `<span style="color:var(--accent);font-size:11px"> ×${it.cantidad}</span>`;
      return `<div style="margin-bottom:2px">${sku} ${nom}${qty}</div>`;
    }).join('');

    // Estado envio con color
    const est = p.estado_envio || '';
    const estColor = est === 'ready_to_ship' ? 'var(--success)'
                   : est === 'delivered'     ? 'var(--mid)'
                   : est                     ? 'var(--warning)' : 'var(--lo)';
    const estChip = est
      ? `<span style="color:${estColor};font-size:11px;font-weight:600">${est}</span>`
      : '<span style="color:var(--lo)">—</span>';

    // Botón etiqueta — diferente según impreso
    const btnEtiq = impreso
      ? `<button class="btn" style="font-size:11px;padding:4px 10px;
           background:var(--card);color:var(--mid);border:1px solid var(--border)"
           onclick="verEtiqueta('${p.order_id}')">🔁 Reimprimir</button>`
      : `<button class="btn btn-ml" style="font-size:11px;padding:4px 10px"
           onclick="verEtiqueta('${p.order_id}')">🏷️ Etiqueta</button>`;

    // Indicador impreso
    const estadoRow = impreso
      ? `<span style="color:var(--success);font-size:10px;font-weight:700">✅ IMPRESO</span>`
      : `<span style="color:var(--warning);font-size:10px;font-weight:700">⏳ PENDIENTE</span>`;

    const rowBg = impreso ? 'opacity:.55;' : '';

    return `<tr style="${rowBg}">
      <td>${tipoChip}<br><small style="color:var(--lo);font-size:10px">${estadoRow}</small></td>
      <td><b style="color:${impreso?'var(--mid)':'var(--accent)'}">#${p.order_id}</b><br>
          <small style="color:var(--lo)">${p.fecha}</small></td>
      <td style="font-weight:600">${p.comprador}</td>
      <td>${skus}</td>
      <td>${estChip}</td>
      <td style="white-space:nowrap">${btnEtiq}</td>
    </tr>`;
  }).join('');
}

function filtrarPedidos() {
  renderPedidos(document.getElementById('search-pedidos').value);
}

function setTipoFiltro(tipo, btn) {
  window._tipoFiltroActivo = tipo;
  // Resaltar botón activo
  document.querySelectorAll('.tipo-tab').forEach(b => {
    b.style.background = '';
    b.style.color      = 'var(--mid)';
  });
  if (btn) {
    btn.style.background = 'var(--accent)';
    btn.style.color      = '#fff';
  }
  renderPedidos(document.getElementById('search-pedidos').value);
}

// ════════════════════════════════════════════════════════════
// ETIQUETAS
// ════════════════════════════════════════════════════════════
async function verEtiqueta(orderId) {
  const p = PEDIDOS[orderId];
  document.getElementById('modal-etiqueta-body').innerHTML =
    `<div style="margin-bottom:12px">
       <b>#${orderId}</b> — ${p.comprador}<br>
       <span style="font-size:12px;color:var(--mid)">
         ${p.items.map(it=>`${it.sku||'?'} · ${it.titulo.substring(0,30)}`).join(' | ')}
       </span>
     </div>
     <div style="display:flex;align-items:center;gap:8px;color:var(--mid)">
       <span class="spin">⏳</span> Obteniendo etiqueta…
     </div>`;
  document.getElementById('modal-etiqueta').classList.add('open');
  document.getElementById('btn-abrir-etiqueta').style.display = 'none';
  etiquetaUrlActual = '';

  try {
    const r = await fetch(`/api/etiqueta/${orderId}`);
    const ct = r.headers.get('Content-Type') || '';

    if (ct.includes('application/pdf')) {
      // PDF directo del proxy — mostrar inline
      const blob = await r.blob();
      const blobUrl = URL.createObjectURL(blob);
      etiquetaUrlActual = blobUrl;
      const p2 = PEDIDOS[orderId];
      document.getElementById('modal-etiqueta-body').innerHTML =
        `<div style="margin-bottom:10px">
           <b>#${orderId}</b> — ${p2.comprador}<br>
           <span style="font-size:11px;color:var(--mid)">
             ${p2.items.map(it=>`${it.sku||'?'} · ${it.titulo.substring(0,28)}`).join(' | ')}
           </span>
         </div>
         <div style="background:var(--bar);border-radius:6px;padding:8px 12px;
              font-size:11px;color:var(--success);margin-bottom:10px">
           ✅ Etiqueta lista — NO marcada como impresa en MercadoLibre
         </div>
         <iframe src="${blobUrl}" style="width:100%;height:400px;border:none;
         border-radius:8px;background:#fff"></iframe>`;
      document.getElementById('btn-abrir-etiqueta').style.display = '';
    } else {
      // JSON — puede ser error con diagnóstico
      const d = await r.json();
      const estado  = d.estado     ? `<br><b>Estado envío:</b> ${d.estado} / ${d.substatus||'—'}` : '';
      const logType = d.logistic_type ? `<br><b>Logística:</b> ${d.logistic_type}` : '';
      const hint    = d.hint
        ? `<div style="margin-top:10px;padding:10px;background:var(--bar);
           border-radius:6px;font-size:12px;color:var(--mid)">${d.hint}</div>` : '';
      document.getElementById('modal-etiqueta-body').innerHTML =
        `<div style="color:var(--danger);margin-bottom:8px">❌ ${d.msg}</div>
         <div style="font-size:12px;color:var(--mid)">${estado}${logType}</div>
         ${hint}`;
    }
  } catch(e) {
    document.getElementById('modal-etiqueta-body').innerHTML =
      `<div style="color:var(--danger)">❌ Error de conexión: ${e.message}</div>`;
  }
}
function abrirEtiqueta() { if (etiquetaUrlActual) window.open(etiquetaUrlActual,'_blank'); }
function cerrarModal(id) { document.getElementById(id).classList.remove('open'); }

// ════════════════════════════════════════════════════════════
// GENERAR LOTE DE PICKING
// ════════════════════════════════════════════════════════════
async function generarLotePicking() {
  const peds = Object.values(PEDIDOS).filter(p => !p.impreso);
  if (!peds.length) { toast('No hay pedidos pendientes', 'err'); return; }

  // Asegurarse de tener la BD de SKUs actualizada
  if (!Object.keys(SKU_DB).length) await cargarSkus();

  // Consolidar SKUs sumando cantidades
  const totalReq  = {};
  const skuNombre = {};
  peds.forEach(p => {
    p.items.forEach(it => {
      const sku = (it.sku || it.item_id).toUpperCase();
      totalReq[sku]  = (totalReq[sku]||0) + it.cantidad;
      if (!skuNombre[sku]) skuNombre[sku] = it.titulo;
    });
  });

  // Enriquecer con BD de SKUs y agrupar por pasillo
  const gruposDict = {};
  Object.entries(totalReq).sort().forEach(([sku, req]) => {
    const dbInfo   = SKU_DB[sku] || {};
    const nombre   = dbInfo.nombre   || skuNombre[sku] || sku;
    const pasillo  = dbInfo.pasillo  || 'Sin ubicación en BD';
    const estanteria = dbInfo.estanteria || '';
    if (!gruposDict[pasillo]) gruposDict[pasillo] = [];
    gruposDict[pasillo].push({sku, nombre, req, pasillo, estanteria});
  });

  // Ordenar pasillos: numerados primero
  const sortPasillo = n => { const m=n.match(/(\d+)/); return m?[0,+m[1],n]:[1,0,n]; };
  const grupos = Object.entries(gruposDict)
    .sort((a,b) => { const x=sortPasillo(a[0]),y=sortPasillo(b[0]); return x[0]-y[0]||x[1]-y[1]||a[0].localeCompare(b[0]); })
    .map(([pasillo,items]) => ({pasillo, items}));

  const totalSkus = Object.keys(totalReq).length;
  const totalUds  = Object.values(totalReq).reduce((a,b)=>a+b,0);
  const sinBD     = Object.keys(totalReq).filter(s=>!SKU_DB[s]).length;

  // Construir mapa de pedidos para Fase 2 (order_id → items con sku+req)
  const pedidosLote = {};
  peds.forEach(p => {
    const items = p.items.map(it => ({
      sku: (it.sku || it.item_id || '').toUpperCase(),
      req: it.cantidad || 1,
      titulo: it.titulo || ''
    }));
    pedidosLote[p.order_id] = {
      comprador:   p.comprador || '',
      shipping_id: p.shipping_id || '',
      _cuenta:     p._cuenta || 'cuenta_0',
      items:       items,
    };
  });

  const payload = {
    fase: 1, grupos, colecta: {}, colecta_completa: false,
    total_skus: totalSkus, total_uds: totalUds,
    pedidos: pedidosLote,
  };

  const r = await fetch('/api/subir_estado', {
    method:'POST',
    headers:{'Content-Type':'application/json','X-API-Key':'everest2024'},
    body: JSON.stringify(payload)
  });
  const d = await r.json();
  if (d.ok) {
    COLECTA = {}; FASE_ACTUAL = 1;
    LOTE_IDS = peds.map(p=>p.order_id);
    const msg = sinBD > 0
      ? `✅ Lote generado (${sinBD} SKUs sin info en BD — agregálos en 🗃 Base de SKUs)`
      : `✅ Lote generado: ${totalSkus} SKUs en ${grupos.length} pasillo(s)`;
    toast(msg, 'ok');
    showTab('picking');
    syncEstadoPicking();
  } else toast('Error: '+d.msg, 'err');
}

// ════════════════════════════════════════════════════════════
// PICKING
// ════════════════════════════════════════════════════════════
async function syncEstadoPicking() {
  try {
    const r = await fetch('/api/estado');
    const d = await r.json();
    if (!d.cargado) return;

    // Comparar totales colectados para detectar cambio real
    const totalAntes = Object.values(COLECTA).reduce((a,b)=>a+b,0);
    const totalNuevo = Object.values(d.colecta||{}).reduce((a,b)=>a+b,0);
    const faseAntes  = FASE_ACTUAL;

    ESTADO_PK   = d;
    COLECTA     = d.colecta || {};
    FASE_ACTUAL = d.fase || 1;

    // Siempre actualizar stats y badge
    renderPickingStats();
    _actualizarBadgePicking();

    // Re-renderizar lista si hubo escaneos nuevos o cambio de fase
    if (totalNuevo !== totalAntes || faseAntes !== FASE_ACTUAL) {
      renderPickingLista();
    }

    // Si pasamos a Fase 2 (por boton o automatico al completar) → activar monitor
    if (FASE_ACTUAL === 2) {
      if (!_fase2Activo) {
        if (faseAntes === 1) toast('🎉 Colecta completa → Fase 2: Armado', 'ok');
        iniciarMonitorFase2();
      } else {
        // Ya activo → seguir chequeando pedidos completos para imprimir
        chequearPedidosCompletos();
      }
    } else if (FASE_ACTUAL === 1 && _fase2Activo) {
      detenerMonitorFase2();
    }
  } catch(e) { console.error('[SYNC]', e); }
}

function _actualizarBadgePicking() {
  if (!ESTADO_PK) return;
  const grupos = ESTADO_PK.grupos || [];
  let tot = 0, done = 0;
  grupos.forEach(g => (g.items||[]).forEach(it => {
    tot++;
    if ((COLECTA[it.sku]||0) >= it.req) done++;
  }));
  // Actualizar el tab de picking con contador
  const tabPicking = document.querySelector('.tab[onclick="showTab('picking')"]');
  if (tabPicking) {
    const pct = tot > 0 ? Math.round(done/tot*100) : 0;
    const color = done === tot ? '#10B981' : done > 0 ? '#F59E0B' : '';
    tabPicking.innerHTML = `📦 Picking <span style="background:${color||'#475569'};color:#fff;
      font-size:10px;padding:1px 6px;border-radius:10px;margin-left:4px">${done}/${tot}</span>`;
  }
}

function renderPicking() { syncEstadoPicking(); }

function renderPickingStats() {
  if (!ESTADO_PK) return;
  const total = ESTADO_PK.total_skus||0;
  const done  = Object.values(COLECTA).reduce((a,b)=>a+b,0);
  document.getElementById('stats-picking').innerHTML = `
    <div class="stat"><div class="stat-n">${total}</div><div class="stat-l">SKUs distintos</div></div>
    <div class="stat"><div class="stat-n">${ESTADO_PK.total_uds||0}</div><div class="stat-l">Unidades totales</div></div>
    <div class="stat"><div class="stat-n" style="color:var(--success)">${done}</div><div class="stat-l">Colectadas</div></div>
  `;
  const fb = document.getElementById('fase-badge');
  fb.textContent = FASE_ACTUAL===1 ? '● FASE 1: COLECTA' : '● FASE 2: ARMADO';
  fb.className   = `fase-badge fase-${FASE_ACTUAL}`;
  document.getElementById('btn-fase2').disabled = FASE_ACTUAL === 2;
}

function renderPickingLista() {
  if (!ESTADO_PK) return;
  const grupos = ESTADO_PK.grupos||[];
  const lista  = document.getElementById('picking-lista');
  if (!grupos.length) {
    lista.innerHTML='<div class="empty" style="padding:40px 20px"><div class="empty-i">📋</div><div style="font-size:13px">Sin lote activo</div></div>';
    return;
  }

  // Recordar qué grupos estaban colapsados antes de re-renderizar
  const colapsados = new Set();
  lista.querySelectorAll('.grupo-hdr').forEach(hdr => {
    const body = hdr.nextElementSibling;
    if (body && body.classList.contains('hidden')) {
      colapsados.add(hdr.querySelector('.grupo-nombre')?.textContent?.trim());
    }
  });

  lista.innerHTML = grupos.map(g => {
    const col     = COLECTA;
    const done    = g.items.filter(it=>(col[it.sku]||0)>=it.req).length;
    const gDone   = done === g.items.length;
    const nombre  = '📦 ' + (g.pasillo||'Sin pasillo');
    const hidden  = colapsados.has(nombre) ? 'hidden' : '';
    return `<div>
      <div class="grupo-hdr" onclick="this.nextElementSibling.classList.toggle('hidden')">
        <div><div class="grupo-nombre">${nombre}</div>
          <div style="font-size:10px;color:var(--mid)">${g.items.length} SKUs</div></div>
        <div class="grupo-prog ${gDone?'done':''}">${done}/${g.items.length}</div>
      </div>
      <div class="${hidden}">${g.items.map(it=>{
        const c=col[it.sku]||0, ok=c>=it.req;
        return`<div class="sku-row ${ok?'done':''}" id="desk-sku-${it.sku}">
          <div class="sku-chk ${ok?'ok':''}">${ok?'✔':'○'}</div>
          <div class="sku-body">
            <div class="sku-name">${it.nombre||it.sku}</div>
            <div class="sku-meta">
              <span class="sku-code">${it.sku}</span>
              <span class="sku-cnt ${ok?'ok':'pend'}">${c} / ${it.req}</span>
            </div>
            ${it.estanteria?`<div class="sku-loc">🗂 ${it.estanteria}</div>`:''}
          </div></div>`;
      }).join('')}</div>
    </div>`;
  }).join('');
}

// SCAN
function onScanKey(e) {
  const inp = document.getElementById('scan-input');
  if (e.key === 'Enter') {
    const v = inp.value.trim();
    if (v) { procesarScan(v); inp.value=''; }
  }
}
document.addEventListener('DOMContentLoaded', () => {
  const inp = document.getElementById('scan-input');
  if(inp) inp.addEventListener('input', () => {
    clearTimeout(window._st);
    window._st = setTimeout(() => {
      const v = inp.value.trim();
      if (v.length >= 4) { procesarScan(v); inp.value=''; }
    }, 400);
  });
});

async function procesarScan(sku) {
  sku = sku.toUpperCase();
  const r    = await fetch('/api/escanear', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({sku})});
  const d    = await r.json();
  const fb   = document.getElementById('scan-fb');
  if (!d.ok) {
    fb.className='err'; fb.textContent=`❌ ${d.msg}`;
  } else if (d.tipo==='ya_completo') {
    fb.className='warn'; fb.textContent=`⚠ ${d.nombre||sku} ya completo`;
  } else {
    fb.className='ok';
    fb.textContent = d.tipo==='completo'
      ? `✔ ${d.nombre||sku} — ¡Completo!`
      : `${d.nombre||sku}  ${d.collected}/${d.req}`;
    if(!COLECTA) COLECTA={};
    COLECTA[sku] = d.collected;
    renderPickingLista();
    renderPickingStats();
    if (d.todo_completo) toast('🎉 ¡Colecta completa!', 'ok');
  }
}

async function pasarFase2() {
  // Pasar manualmente a Fase 2 (el boton). Tambien se pasa solo al completar colecta.
  const r = await fetch('/api/estado');
  const est = await r.json();
  if (!est.cargado) { toast('No hay lote activo', 'err'); return; }

  est.fase = 2;
  await fetch('/api/subir_estado', {
    method: 'POST',
    headers: {'Content-Type':'application/json','X-API-Key':'everest2024'},
    body: JSON.stringify(est)
  });

  FASE_ACTUAL = 2;
  ESTADO_PK   = est;
  COLECTA     = est.colecta || {};
  renderPickingStats();
  toast('✅ Fase 2 — armado de pedidos', 'ok');
  iniciarMonitorFase2();
}

// ── MONITOR DE FASE 2 — imprime etiquetas automaticamente ──────────────────
function iniciarMonitorFase2() {
  if (_fase2Activo) return;
  _fase2Activo = true;
  console.log('[FASE2] Monitor de impresion automatica iniciado');
  chequearPedidosCompletos();  // chequeo inmediato
}

function detenerMonitorFase2() {
  _fase2Activo = false;
  _fase2Cola = [];
  _fase2Imprimiendo = false;
}

async function chequearPedidosCompletos() {
  if (FASE_ACTUAL !== 2) return;
  try {
    const r = await fetch('/api/fase2/pedidos-completos');
    const d = await r.json();
    if (!d.ok) return;

    // Sincronizar las ya impresas del servidor
    (d.ya_impresas || []).forEach(oid => _fase2YaImpresas.add(oid));

    // Encolar los recien completados que aun no se imprimieron
    (d.completos || []).forEach(c => {
      if (c.recien_completado && !_fase2YaImpresas.has(c.order_id)
          && !_fase2Cola.includes(c.order_id)) {
        _fase2Cola.push(c.order_id);
        console.log('[FASE2] Pedido completo encolado:', c.order_id);
      }
    });

    // Procesar la cola
    procesarColaFase2();

    // Mostrar progreso de armado
    renderProgresoFase2(d.completos || []);
  } catch(e) { console.error('[FASE2]', e); }
}

async function procesarColaFase2() {
  if (_fase2Imprimiendo || !_fase2Cola.length) return;
  _fase2Imprimiendo = true;

  while (_fase2Cola.length) {
    const orderId = _fase2Cola.shift();
    if (_fase2YaImpresas.has(orderId)) continue;

    const p = PEDIDOS[orderId];
    const comprador = p ? p.comprador : orderId;
    toast(`🖨️ Imprimiendo etiqueta: ${comprador}`, 'ok');

    // Abrir la etiqueta automaticamente
    await imprimirEtiquetaAuto(orderId);

    // Marcar como impresa en el servidor
    try {
      await fetch(`/api/fase2/marcar-impresa/${orderId}`, {method:'POST'});
    } catch(e) {}
    _fase2YaImpresas.add(orderId);

    // Pequeno delay entre impresiones
    await new Promise(res => setTimeout(res, 1200));
  }

  _fase2Imprimiendo = false;
}

// Abrir e imprimir una etiqueta automaticamente (sin clic del usuario)
async function imprimirEtiquetaAuto(orderId) {
  try {
    const r = await fetch(`/api/etiqueta/${orderId}`);
    const ct = r.headers.get('Content-Type') || '';
    if (ct.includes('application/pdf')) {
      const blob = await r.blob();
      const url  = URL.createObjectURL(blob);
      // Abrir en ventana nueva para imprimir
      const win = window.open(url, '_blank');
      if (win) {
        // Intentar disparar impresion automatica
        win.addEventListener('load', () => {
          setTimeout(() => { try { win.print(); } catch(e) {} }, 700);
        });
      } else {
        // Popup bloqueado → mostrar en modal como fallback
        toast('⚠ Permití pop-ups para impresión automática', 'warn');
        verEtiqueta(orderId);
      }
    } else {
      // No hay PDF disponible — mostrar diagnostico
      console.warn('[FASE2] Etiqueta no disponible para', orderId);
      verEtiqueta(orderId);
    }
  } catch(e) {
    console.error('[FASE2] Error imprimiendo', orderId, e);
  }
}

// Mostrar progreso de armado en el panel derecho
function renderProgresoFase2(completos) {
  const cont = document.getElementById('resumen-pedidos-picking');
  if (!cont) return;
  const totalPedidos = Object.keys(ESTADO_PK?.pedidos || {}).length;
  const listos = completos.length;

  let html = `<div style="background:var(--card);border:1px solid var(--border);
    border-radius:8px;padding:12px;margin-bottom:12px">
    <div style="font-size:22px;font-weight:800;color:var(--success)">${listos}/${totalPedidos}</div>
    <div style="font-size:10px;color:var(--lo)">PEDIDOS ARMADOS</div>
  </div>`;

  const pedidos = ESTADO_PK?.pedidos || {};
  html += Object.entries(pedidos).map(([oid, ped]) => {
    const completo = completos.some(c => c.order_id === oid);
    const impreso  = _fase2YaImpresas.has(oid);
    const color = impreso ? 'var(--success)' : completo ? 'var(--warning)' : 'var(--lo)';
    const icon  = impreso ? '🖨️' : completo ? '✅' : '⏳';
    const estado = impreso ? 'Impreso' : completo ? 'Listo' : 'Armando';
    return `<div style="display:flex;justify-content:space-between;align-items:center;
      padding:8px 10px;border-bottom:1px solid var(--border);font-size:12px">
      <div>
        <div style="font-weight:600;color:${color}">${icon} ${ped.comprador||oid}</div>
        <div style="font-size:10px;color:var(--lo)">#${oid} · ${ped.items.length} item(s)</div>
      </div>
      <span style="color:${color};font-size:10px;font-weight:700">${estado}</span>
    </div>`;
  }).join('');

  cont.innerHTML = html;
}

// UTILS
function getCookie(n){const v=document.cookie.match('(^|;)\\s*'+n+'\\s*=\\s*([^;]+)');return v?v.pop():'';}
function toast(m,t=''){const el=document.getElementById('toast');el.textContent=m;el.className='show '+t;setTimeout(()=>el.className='',2800);}
function copiarUrl(){navigator.clipboard.writeText(location.origin+'/movil');toast('URL copiada','ok');}

// ════════════════════════════════════════════════════════════
// BASE DE SKUs
// ════════════════════════════════════════════════════════════
let SKU_DB      = {};   // cache local
let SKU_EDITANDO = null; // null = nuevo, string = SKU que se está editando

async function cargarSkus() {
  try {
    const r = await fetch('/api/skus');
    const d = await r.json();
    if (d.ok) { SKU_DB = d.skus; renderSkus(); }
  } catch(e) { toast('Error cargando SKUs', 'err'); }
}

function renderSkus(filtro='') {
  const tbody = document.getElementById('tbody-skus');
  const q     = filtro.toUpperCase();
  const items = Object.entries(SKU_DB).filter(([sku, v]) =>
    !q || sku.includes(q) || v.nombre.toUpperCase().includes(q) ||
    v.pasillo.toUpperCase().includes(q)
  ).sort((a,b) => a[0].localeCompare(b[0]));

  document.getElementById('badge-skus').textContent = Object.keys(SKU_DB).length;

  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="5"><div class="empty">
      <div class="empty-i">🗃</div>
      <div>${filtro ? 'Sin resultados' : 'No hay SKUs cargados aún.<br>Usá ➕ Nuevo SKU o ⬆ Importar JSON'}</div>
    </div></td></tr>`;
    return;
  }

  tbody.innerHTML = items.map(([sku, v]) => `
    <tr>
      <td><span class="sku-chip">${sku}</span></td>
      <td style="font-weight:600">${v.nombre}</td>
      <td style="color:var(--accent2)">${v.pasillo || '—'}</td>
      <td style="color:var(--mid);font-size:12px">${v.estanteria || '—'}</td>
      <td>
        <button class="btn btn-ghost" style="padding:4px 8px;font-size:11px"
          onclick="abrirFormSku('${sku}')">✏</button>
        <button class="btn btn-danger" style="padding:4px 8px;font-size:11px;margin-left:4px"
          onclick="eliminarSku('${sku}')">🗑</button>
      </td>
    </tr>`).join('');
}

function filtrarSkus() {
  renderSkus(document.getElementById('search-skus').value);
}

function abrirFormSku(skuOrig) {
  SKU_EDITANDO = skuOrig;
  const v = skuOrig ? (SKU_DB[skuOrig] || {}) : {};
  document.getElementById('modal-sku-title').textContent = skuOrig ? `✏ Editar: ${skuOrig}` : '➕ Nuevo SKU';
  document.getElementById('form-sku').value         = skuOrig || '';
  document.getElementById('form-sku').disabled      = !!skuOrig;
  document.getElementById('form-nombre').value      = v.nombre || '';
  document.getElementById('form-pasillo').value     = v.pasillo || '';
  document.getElementById('form-estanteria').value  = v.estanteria || '';
  document.getElementById('form-error').style.display = 'none';

  // Sugerencias de pasillos existentes
  const pasillos = [...new Set(Object.values(SKU_DB).map(x=>x.pasillo).filter(Boolean))];
  document.getElementById('pasillo-sugs').innerHTML = pasillos.map(p =>
    `<span style="background:var(--bar);border:1px solid var(--border);border-radius:4px;
      padding:2px 8px;font-size:11px;cursor:pointer;color:var(--accent)"
      onclick="document.getElementById('form-pasillo').value='${p}'">${p}</span>`
  ).join('');

  document.getElementById('modal-sku').classList.add('open');
  setTimeout(() => document.getElementById(skuOrig ? 'form-nombre' : 'form-sku').focus(), 100);
}

async function guardarSku() {
  const sku        = document.getElementById('form-sku').value.trim().toUpperCase();
  const nombre     = document.getElementById('form-nombre').value.trim();
  const pasillo    = document.getElementById('form-pasillo').value.trim();
  const estanteria = document.getElementById('form-estanteria').value.trim();
  const errEl      = document.getElementById('form-error');

  if (!sku)    { errEl.textContent='SKU obligatorio';    errEl.style.display='block'; return; }
  if (!nombre) { errEl.textContent='Nombre obligatorio'; errEl.style.display='block'; return; }
  errEl.style.display = 'none';

  // Si estamos editando y el SKU cambió, eliminar el viejo
  if (SKU_EDITANDO && SKU_EDITANDO !== sku) {
    await fetch(`/api/skus/${SKU_EDITANDO}`, {method:'DELETE'});
  }

  const r = await fetch('/api/skus', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({sku, nombre, pasillo, estanteria})
  });
  const d = await r.json();
  if (d.ok) {
    SKU_DB[sku] = {nombre, pasillo, estanteria};
    if (SKU_EDITANDO && SKU_EDITANDO !== sku) delete SKU_DB[SKU_EDITANDO];
    cerrarModal('modal-sku');
    renderSkus(document.getElementById('search-skus').value);
    document.getElementById('badge-skus').textContent = Object.keys(SKU_DB).length;
    toast(`✅ ${d.msg}`, 'ok');
  } else {
    errEl.textContent = d.msg; errEl.style.display = 'block';
  }
}

async function eliminarSku(sku) {
  if (!confirm(`¿Eliminar el SKU '${sku}'?`)) return;
  const r = await fetch(`/api/skus/${sku}`, {method:'DELETE'});
  const d = await r.json();
  if (d.ok) {
    delete SKU_DB[sku];
    renderSkus(document.getElementById('search-skus').value);
    toast(`🗑 ${sku} eliminado`, 'ok');
  } else toast('Error: '+d.msg, 'err');
}

function exportarSkus() {
  const blob = new Blob([JSON.stringify(SKU_DB, null, 2)], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'skus_db.json';
  a.click();
  toast('📥 JSON descargado', 'ok');
}

async function importarSkus(e) {
  const file = e.target.files[0];
  if (!file) return;
  const text = await file.text();
  let data;
  try { data = JSON.parse(text); } catch(err) { toast('JSON inválido', 'err'); return; }

  // Aceptar tanto { "SKU": {nombre,pasillo,estanteria} }
  // como { "skus": { "SKU": {...} } }
  const skus = data.skus || data;
  if (typeof skus !== 'object') { toast('Formato no reconocido', 'err'); return; }

  const r = await fetch('/api/skus/importar', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({skus})
  });
  const d = await r.json();
  if (d.ok) {
    await cargarSkus();
    toast(`✅ ${d.msg}`, 'ok');
  } else toast('Error: '+d.msg, 'err');
  e.target.value = '';
}

// Enter en cualquier campo del form guarda
document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && document.getElementById('modal-sku').classList.contains('open'))
    guardarSku();
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-bg.open').forEach(m => m.classList.remove('open'));
  }
});

// Al abrir tab SKU cargar datos
const _origShowTab = showTab;
function showTab(name) {
  _origShowTab(name);
  if (name === 'skudb') cargarSkus();
}
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# HTML MÓVIL (operarios en celular)
# ═══════════════════════════════════════════════════════════════════════════════

HTML_MOVIL = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#1E293B">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="manifest" href="/manifest.json">
<title>Logibot · Picking</title>
<style>
:root{--bg:#111318;--panel:#1C2030;--card:#161B27;--border:#2A3352;
  --accent:#2563EB;--accent2:#1D4ED8;--success:#0EA5E9;
  --warning:#F59E0B;--danger:#EF4444;--hi:#F1F5F9;--mid:#94A3B8;--lo:#475569;--bar:#1E3A5F}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{background:var(--bg);color:var(--hi);font-family:'Segoe UI',system-ui,sans-serif;
  min-height:100vh;display:flex;flex-direction:column}

/* TOPBAR */
.topbar{background:var(--panel);padding:10px 14px;display:flex;align-items:center;
  justify-content:space-between;border-bottom:1px solid var(--border);flex-shrink:0}
.t-left{display:flex;align-items:center;gap:8px}
.t-icon{font-size:20px}
.t-title{font-size:15px;font-weight:800}
.t-sub{font-size:10px;color:var(--mid)}
.badge{background:var(--accent);color:#fff;font-size:13px;font-weight:800;
  padding:5px 12px;border-radius:20px}
.badge.done{background:var(--success)}
.badge.warn{background:var(--warning);color:#000}

/* BANNER COMPLETADO */
#ban{display:none;background:var(--success);color:#fff;text-align:center;
  padding:14px;font-size:16px;font-weight:800;letter-spacing:.04em;flex-shrink:0}
#ban.show{display:block}

/* CAJA DE ESCANEO */
.scan-box{padding:10px 12px;background:var(--panel);border-bottom:1px solid var(--border);
  flex-shrink:0}
#sku{width:100%;background:var(--card);border:2px solid var(--accent);color:var(--hi);
  font-size:20px;font-family:monospace;font-weight:700;padding:12px 14px;
  border-radius:10px;outline:none;text-align:center;text-transform:uppercase;
  caret-color:var(--accent)}
#sku:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(59,130,246,.25)}

/* FEEDBACK — grande y claro para el depósito */
#fb{margin-top:8px;min-height:48px;border-radius:10px;padding:10px 14px;
  font-size:15px;font-weight:700;display:flex;align-items:center;gap:10px;
  transition:background .15s}
#fb.ok{background:rgba(16,185,129,.18);color:var(--success)}
#fb.warn{background:rgba(245,158,11,.18);color:var(--warning)}
#fb.err{background:rgba(239,68,68,.18);color:var(--danger)}
#fb.neu{color:var(--mid)}

/* LISTA POR PASILLO */
.content{flex:1;overflow-y:auto;padding:8px 8px 80px}
.grupo{margin-bottom:10px;border-radius:10px;overflow:hidden;border:1px solid var(--border)}
.g-hdr{background:var(--bar);padding:10px 14px;display:flex;align-items:center;
  justify-content:space-between;cursor:pointer;user-select:none}
.g-left{display:flex;align-items:center;gap:8px}
.g-name{font-size:13px;font-weight:800;color:var(--accent);text-transform:uppercase}
.g-stats{font-size:10px;color:var(--mid)}
.g-prog{font-size:14px;font-weight:800}
.g-prog.done{color:var(--success)}
.g-prog.pend{color:var(--accent)}
.g-items{background:var(--card)}
.g-items.col{display:none}

/* ITEM DE SKU */
.item{display:flex;align-items:center;gap:10px;padding:12px 14px;
  border-bottom:1px solid var(--border)}
.item:last-child{border-bottom:none}
.item.done{opacity:.45}
.item.recien{animation:flash_ok .6s ease}
@keyframes flash_ok{0%,100%{background:transparent}50%{background:rgba(16,185,129,.25)}}

.chk{font-size:22px;flex-shrink:0;width:28px;text-align:center}
.chk.ok{color:var(--success)}
.chk.pend{color:var(--lo)}
.ibody{flex:1;min-width:0}
.iname{font-size:14px;font-weight:600;color:#0F172A;white-space:normal;line-height:1.3;margin-top:3px}
.irow{display:flex;align-items:center;gap:10px;margin-top:3px}
.isku{font-family:monospace;font-size:12px;color:var(--mid)}
.icnt{font-size:14px;font-weight:800}
.icnt.ok{color:var(--success)}
.icnt.pend{color:var(--accent)}
.iest{font-size:11px;color:var(--accent2);margin-top:2px}

/* FLASH OVERLAY */
#flash{position:fixed;inset:0;pointer-events:none;opacity:0;transition:opacity .12s;z-index:200}
#flash.ok{background:rgba(16,185,129,.25)}
#flash.err{background:rgba(239,68,68,.25)}

/* CAMARA SCANNER */
#cam-overlay{position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:300;
  display:none;flex-direction:column;align-items:center;justify-content:center}
#cam-overlay.open{display:flex}
#cam-wrap{position:relative;width:min(92vw,400px);border-radius:16px;overflow:hidden;
  background:#000;border:2px solid var(--accent)}
#cam-video{width:100%;display:block;max-height:65vh;object-fit:cover}
#cam-guide{position:absolute;inset:0;pointer-events:none;display:flex;
  align-items:center;justify-content:center}
#cam-guide svg{width:72%;max-width:260px;opacity:.75}
#cam-hint{margin-top:14px;font-size:13px;color:rgba(255,255,255,.7);text-align:center}
#cam-result{margin-top:10px;font-size:15px;font-weight:700;color:var(--success);
  min-height:22px;text-align:center}
#btn-close-cam{margin-top:16px;padding:10px 32px;border-radius:30px;border:none;
  background:var(--danger);color:#fff;font-size:15px;font-weight:700;cursor:pointer}
#btn-cam{position:fixed;bottom:88px;right:16px;background:#7C3AED;color:#fff;border:none;
  border-radius:50%;width:52px;height:52px;font-size:22px;cursor:pointer;
  box-shadow:0 4px 20px rgba(0,0,0,.5);display:flex;align-items:center;
  justify-content:center;z-index:50;transition:transform .15s}
#btn-cam:active{transform:scale(.9)}
#cam-torch{margin-top:10px;padding:8px 20px;border-radius:20px;border:1px solid rgba(255,255,255,.3);
  background:transparent;color:#fff;font-size:13px;cursor:pointer;display:none}

/* FAB */
.fab{position:fixed;bottom:20px;right:16px;background:var(--accent);color:#fff;border:none;
  border-radius:50%;width:56px;height:56px;font-size:24px;cursor:pointer;
  box-shadow:0 4px 20px rgba(0,0,0,.5);display:flex;align-items:center;
  justify-content:center;z-index:50;transition:transform .15s}
.fab:active{transform:scale(.9)}
.spin{animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* TOAST */
#toast{position:fixed;bottom:90px;left:50%;transform:translateX(-50%) translateY(60px);
  background:var(--panel);border:1px solid var(--border);color:var(--hi);
  padding:12px 24px;border-radius:30px;font-size:14px;font-weight:700;z-index:999;
  transition:transform .25s,opacity .25s;opacity:0;white-space:nowrap;
  box-shadow:0 4px 20px rgba(0,0,0,.4)}
#toast.show{transform:translateX(-50%) translateY(0);opacity:1}
#toast.ok{border-color:var(--success);color:var(--success)}
#toast.err{border-color:var(--danger);color:var(--danger)}

/* EMPTY */
.empty{text-align:center;padding:60px 20px;color:var(--lo)}
.empty-i{font-size:52px;margin-bottom:12px}
.empty-t{font-size:15px;line-height:1.5}
</style>
</head>
<body>
<div id="flash"></div>

<!-- TOPBAR -->
<div class="topbar">
  <div class="t-left">
    <span class="t-icon">⬡</span>
    <div>
      <div class="t-title">PICKING · FASE 1</div>
      <div class="t-sub" id="t-sub">Colecta en depósito</div>
    </div>
  </div>
  <div id="badge" class="badge">— / —</div>
</div>

<!-- BANNER COMPLETADO -->
<div id="ban">🎉 ¡COLECTA COMPLETA! Avisá al supervisor.</div>

<!-- ESCANEO -->
<div class="scan-box">
  <div style="display:flex;gap:8px;margin-bottom:8px">
    <input id="sku" type="text"
           inputmode="none"
           autocomplete="off" autocorrect="off" autocapitalize="characters"
           spellcheck="false" enterkeyhint="send"
           placeholder="Apuntá el escáner aquí"
           style="flex:1">
    <button id="btn-confirmar"
            style="background:var(--success);color:#fff;border:none;border-radius:10px;
            padding:0 18px;font-size:20px;font-weight:800;cursor:pointer;flex-shrink:0"
            onclick="confirmarManual()">✓</button>
  </div>
  <div style="display:flex;align-items:center;gap:8px">
    <div id="fb" class="neu" style="flex:1">🔫 Listo para escanear</div>
    <button id="btn-teclado"
            onclick="toggleTeclado()"
            style="background:var(--panel);border:1px solid var(--border);color:var(--mid);
            border-radius:8px;padding:5px 10px;font-size:16px;cursor:pointer;flex-shrink:0"
            title="Abrir teclado manual">⌨</button>
  </div>
</div>

<!-- MODAL CAMARA -->
<div id="cam-overlay">
  <div id="cam-wrap">
    <video id="cam-video" autoplay playsinline muted></video>
    <div id="cam-guide">
      <svg viewBox="0 0 200 200" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="1" y="1" width="198" height="198" rx="8" stroke="white" stroke-width="1.5" stroke-dasharray="6 4"/>
        <path d="M1 40 L1 1 L40 1" stroke="#3B82F6" stroke-width="4" stroke-linecap="round"/>
        <path d="M160 1 L199 1 L199 40" stroke="#3B82F6" stroke-width="4" stroke-linecap="round"/>
        <path d="M1 160 L1 199 L40 199" stroke="#3B82F6" stroke-width="4" stroke-linecap="round"/>
        <path d="M160 199 L199 199 L199 160" stroke="#3B82F6" stroke-width="4" stroke-linecap="round"/>
        <line x1="1" y1="100" x2="199" y2="100" stroke="#3B82F6" stroke-width="1.5" stroke-dasharray="4 3" opacity="0.6"/>
      </svg>
    </div>
  </div>
  <div id="cam-hint">Apuntá al código de barras</div>
  <div id="cam-result"></div>
  <button id="cam-torch" onclick="toggleTorch()">🔦 Linterna</button>
  <button id="btn-close-cam" onclick="cerrarCamara()">✕ Cerrar</button>
</div>

<!-- Botón cámara flotante -->
<button id="btn-cam" onclick="abrirCamara()" title="Escanear con cámara">📷</button>

<!-- LISTA -->
<div class="content" id="content">
  <div class="empty">
    <div class="empty-i">📦</div>
    <div class="empty-t">Cargando…</div>
  </div>
</div>

<button class="fab" id="fab" title="Actualizar">🔄</button>
<div id="toast"></div>

<script>
const $ = id => document.getElementById(id);
let E = null, last = null;

// ── CARGA ──────────────────────────────────────────────────────────────────
async function load() {
  try {
    const r = await fetch('/api/estado');
    if (!r.ok) { fb('err', '❌ Error del servidor: ' + r.status); return; }
    E = await r.json();
    console.log('[MOVIL] cargado='+E.cargado+' grupos='+((E.grupos||[]).length));
    render();
  } catch(e) {
    console.error('[MOVIL] load error:', e);
    fb('err', '❌ Sin conexión al servidor');
  }
}

async function loadQ() {
  try {
    const r = await fetch('/api/estado');
    if (!r.ok) return;
    E = await r.json();
    render(true);
  } catch(e) {}
}

// ── RENDER ─────────────────────────────────────────────────────────────────
function render(quiet = false) {
  try {
    if (!E) return;
    const gs  = E.grupos  || [];
    const col = E.colecta || {};
    const cargado = E.cargado === true || E.cargado === 'true' || E.cargado === 1;

    const content = document.getElementById('content');
    const badge   = document.getElementById('badge');
    const tsub    = document.getElementById('t-sub');
    const ban     = document.getElementById('ban');

    if (!cargado || !gs.length) {
      if (content) content.innerHTML = `<div class="empty">
        <div class="empty-i">📋</div>
        <div class="empty-t">Esperando lote del supervisor…<br>
        <small style="color:var(--lo)">Cuando el supervisor genere el lote<br>aparecerá aquí automáticamente.</small>
        </div></div>`;
      if (badge) { badge.textContent = '— / —'; badge.className = 'badge'; }
      if (tsub)  tsub.textContent = 'Esperando lote…';
      return;
    }

    // Totales
    let tot = 0, done = 0, uds_done = 0, uds_tot = 0;
    gs.forEach(g => (g.items||[]).forEach(it => {
      const c = col[it.sku] || 0;
      tot++;
      uds_tot  += it.req || 0;
      uds_done += Math.min(c, it.req || 0);
      if (c >= it.req) done++;
    }));

    if (badge) {
      badge.textContent = `${done} / ${tot}`;
      badge.className   = done === tot ? 'badge done' : (done > 0 ? 'badge warn' : 'badge');
    }
    if (tsub) tsub.textContent = `${uds_done} / ${uds_tot} unidades`;
    if (ban)  ban.className    = E.colecta_completa ? 'show' : '';

    // Recordar grupos colapsados
    const colaps = new Set();
    document.querySelectorAll('.grupo').forEach(el => {
      if (el.querySelector('.g-items.col')) colaps.add(el.dataset.p);
    });

    if (content) content.innerHTML = gs.map(g => {
      const p     = g.pasillo || 'Sin pasillo';
      const its   = g.items || [];
      const d     = its.filter(it => (col[it.sku] || 0) >= it.req).length;
      const gDone = d === its.length;
      const cl    = colaps.has(p) ? 'col' : '';
      return `<div class="grupo" data-p="${p}">
        <div class="g-hdr" onclick="tog(this)">
          <div class="g-left">
            <span style="font-size:18px">📦</span>
            <div>
              <div class="g-name">${p}</div>
              <div class="g-stats">${its.length} SKU${its.length>1?'s':''} · ${its.reduce((s,i)=>s+(i.req||0),0)} ud.</div>
            </div>
          </div>
          <div class="g-prog ${gDone?'done':'pend'}">${d}/${its.length}</div>
        </div>
        <div class="g-items ${cl}">
          ${its.map(it => {
            const c  = col[it.sku] || 0;
            const ok = c >= it.req;
            return `<div class="item ${ok?'done':''}" id="item-${it.sku}">
              <div class="chk ${ok?'ok':'pend'}">${ok ? '✔' : '○'}</div>
              <div class="ibody">
                <span class="isku-badge">${it.sku}</span>
                <div class="iname">${it.nombre || '—'}</div>
                <div class="irow">
                  <span class="icnt ${ok?'ok':'pend'}">${c} / ${it.req}</span>
                  ${it.estanteria ? `<span class="iest">🗂 ${it.estanteria}</span>` : ''}
                </div>
              </div>
            </div>`;
          }).join('')}
        </div>
      </div>`;
    }).join('');
  } catch(err) {
    console.error('[MOVIL] render error:', err);
  }
}

function tog(h) { h.nextElementSibling.classList.toggle('col'); }

// ── ESCANEO ────────────────────────────────────────────────────────────────
async function scan(raw) {
  const sku = raw.trim().toUpperCase();
  if (!sku || sku.length < 2) return;

  // Anti-doble lectura 200ms
  if (last === sku) return;
  last = sku;
  setTimeout(() => last = null, 200);

  try {
    const r = await fetch('/api/escanear', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ sku })
    });
    const d = await r.json();

    if (!d.ok) {
      flash('err'); vib([200, 80, 200]);
      fb('err', `❌ ${d.msg}`);
    } else if (d.tipo === 'ya_completo') {
      vib([80]);
      fb('warn', `⚠ ${d.nombre || sku} — ya estaba completo`);
    } else {
      flash('ok'); vib([60]);
      const txt = d.tipo === 'completo'
        ? `✔  ${d.nombre || sku}  —  ¡Completo! (${d.req}/${d.req})`
        : `${d.nombre || sku}  ${d.collected}/${d.req}`;
      fb('ok', txt);

      if (!E.colecta) E.colecta = {};
      E.colecta[sku] = d.collected;

      if (d.todo_completo) {
        E.colecta_completa = true;
        toast('🎉 ¡Colecta completa!', 'ok');
      }

      render(true);

      // Resaltar el item escaneado y hacer scroll
      const el = document.getElementById(`item-${sku}`);
      if (el) {
        el.classList.add('recien');
        setTimeout(() => el.classList.remove('recien'), 700);
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    }
  } catch(e) {
    fb('err', '❌ Error de conexión');
  }
}

// ── HELPERS ────────────────────────────────────────────────────────────────
function fb(t, m) {
  const el = $('fb');
  el.className = t;
  el.textContent = m;
}

function toast(m, t = '') {
  const el = $('toast');
  el.textContent = m;
  el.className   = 'show ' + t;
  setTimeout(() => el.className = '', 3000);
}

function flash(t) {
  const el = $('flash');
  el.className   = t;
  el.style.opacity = '1';
  setTimeout(() => {
    el.style.opacity = '0';
    setTimeout(() => el.className = '', 300);
  }, 180);
}

function vib(p) { if (navigator.vibrate) navigator.vibrate(p); }

// ── INIT ───────────────────────────────────────────────────────────────────
let _tecladoVisible = false;
let _camaraActiva   = false;
let _zxingReader    = null;
let _torchTrack     = null;
let _torchOn        = false;

// ── CÁMARA ESCÁNER ─────────────────────────────────────────────────────────

async function abrirCamara() {
  const overlay = document.getElementById('cam-overlay');
  overlay.classList.add('open');
  _camaraActiva = true;

  // Cargar ZXing si no está cargado
  if (!window.ZXing) {
    document.getElementById('cam-hint').textContent = 'Cargando escáner…';
    await new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = 'https://unpkg.com/@zxing/library@0.19.1/umd/index.min.js';
      s.onload = res;
      s.onerror = rej;
      document.head.appendChild(s);
    });
  }

  try {
    const video  = document.getElementById('cam-video');
    const hint   = document.getElementById('cam-hint');
    const result = document.getElementById('cam-result');
    const torch  = document.getElementById('cam-torch');

    result.textContent = '';
    hint.textContent   = 'Iniciando cámara…';

    // Pedir cámara trasera
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: 'environment', width: {ideal:1280}, height: {ideal:720} }
    });
    video.srcObject = stream;

    // Detectar si la cámara soporta linterna
    const [track] = stream.getVideoTracks();
    _torchTrack = track;
    const caps = track.getCapabilities ? track.getCapabilities() : {};
    if (caps.torch) {
      torch.style.display = 'inline-block';
    }

    hint.textContent = 'Apuntá el código de barras al recuadro azul';

    // Iniciar ZXing
    _zxingReader = new ZXing.BrowserMultiFormatReader();
    _zxingReader.decodeFromVideoElement(video, (res, err) => {
      if (!_camaraActiva) return;
      if (res) {
        const codigo = res.getText();
        result.textContent = '✅ ' + codigo;
        // Vibrar si disponible
        if (navigator.vibrate) navigator.vibrate([60, 30, 60]);
        // Pequeño delay para que el usuario vea el resultado
        setTimeout(() => {
          cerrarCamara();
          // Procesar el código escaneado
          scan(codigo);
        }, 600);
      }
    });

  } catch (e) {
    document.getElementById('cam-hint').textContent =
      e.name === 'NotAllowedError'
        ? '❌ Permiso de cámara denegado. Habilitalo en la configuración del navegador.'
        : '❌ No se pudo acceder a la cámara: ' + e.message;
  }
}

function cerrarCamara() {
  _camaraActiva = false;
  const video = document.getElementById('cam-video');

  // Detener ZXing
  if (_zxingReader) {
    try { _zxingReader.reset(); } catch(e) {}
    _zxingReader = null;
  }

  // Apagar linterna si estaba encendida
  if (_torchTrack && _torchOn) {
    try { _torchTrack.applyConstraints({advanced:[{torch:false}]}); } catch(e) {}
    _torchOn = false;
  }

  // Detener stream de video
  if (video.srcObject) {
    video.srcObject.getTracks().forEach(t => t.stop());
    video.srcObject = null;
  }

  document.getElementById('cam-overlay').classList.remove('open');
  document.getElementById('cam-result').textContent = '';
  document.getElementById('cam-torch').style.display = 'none';

  // Restaurar foco al input
  const inp = $('sku');
  if (inp) setTimeout(() => inp.focus(), 100);
}

function toggleTorch() {
  if (!_torchTrack) return;
  _torchOn = !_torchOn;
  try {
    _torchTrack.applyConstraints({advanced: [{torch: _torchOn}]});
    document.getElementById('cam-torch').textContent =
      _torchOn ? '🔦 Linterna ON' : '🔦 Linterna';
  } catch(e) {
    console.warn('Linterna no disponible:', e);
  }
}

function confirmarManual() {
  const inp = $('sku');
  const v   = inp.value.trim();
  if (v) { scan(v); inp.value = ''; inp.focus(); }
  else   { inp.focus(); }
}

function toggleTeclado() {
  const inp = $('sku');
  const btn = $('btn-teclado');
  _tecladoVisible = !_tecladoVisible;

  if (_tecladoVisible) {
    // Modo teclado manual — abre teclado virtual
    inp.setAttribute('inputmode', 'text');
    inp.focus(); inp.click();
    if (btn) { btn.style.background='var(--accent)'; btn.style.color='#fff'; }
    const f = $('fb');
    if (f) { f.textContent='⌨ Escribí el SKU y tocá ✓'; f.className='neu'; }
  } else {
    // Modo escáner láser — no abre teclado virtual
    inp.setAttribute('inputmode', 'none');
    inp.value = '';
    inp.blur();
    setTimeout(() => inp.focus(), 80);
    if (btn) { btn.style.background='var(--panel)'; btn.style.color='var(--mid)'; }
    const f = $('fb');
    if (f) { f.textContent='🔫 Listo para escanear'; f.className='neu'; }
  }
}

document.addEventListener('DOMContentLoaded', () => {
  load();
  const inp = $('sku');

  // ── ESCÁNER LÁSER HARDWARE ─────────────────────────────────────────────
  // El escáner manda el código como si fuera un teclado físico + Enter al final.
  // inputmode="none" = no abre teclado virtual pero SÍ recibe input del escáner.

  let _scanTimer = null;

  // Enter → procesar INMEDIATAMENTE sin esperar nada
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.keyCode === 13) {
      const v = inp.value.trim();
      if (v) {
        e.preventDefault();  // evitar submit solo si hay valor
        clearTimeout(_scanTimer);
        scan(v);
        inp.value = '';
        inp.focus();
      }
    }
  });

  // Fallback: escáneres que NO mandan Enter → procesar al dejar de escribir (50ms)
  // Los escáneres escriben muy rápido (<30ms total) — 50ms es más que suficiente
  inp.addEventListener('input', () => {
    clearTimeout(_scanTimer);
    const v = inp.value.trim();
    if (!v) return;
    if (_tecladoVisible) return;  // modo teclado manual: esperar Enter o botón ✓
    _scanTimer = setTimeout(() => {
      const v2 = inp.value.trim();
      if (v2.length >= 2) {
        scan(v2);
        inp.value = '';
        inp.focus();
      }
    }, 50);
  });

  // Edge case Android: si el input pierde foco con valor → procesar igual
  inp.addEventListener('blur', () => {
    if (!_tecladoVisible && !_camaraActiva) {
      const v = inp.value.trim();
      if (v.length >= 2) {
        clearTimeout(_scanTimer);
        scan(v);
        inp.value = '';
        setTimeout(() => inp.focus(), 80);
      }
    }
  });

  // Clic en contenido → foco (excepto botones interactivos)
  document.addEventListener('click', e => {
    if (!e.target.closest('#btn-teclado') &&
        !e.target.closest('#btn-confirmar') &&
        !e.target.closest('#btn-cam') &&
        !e.target.closest('#cam-overlay') &&
        !e.target.closest('.g-hdr')) {
      if (!_tecladoVisible && !_camaraActiva) inp.focus();
    }
  });

  // Foco inicial
  inp.focus();

  // Botón cámara: solo en dispositivos táctiles
  const btnCam = document.getElementById('btn-cam');
  if (btnCam) {
    const esMovil = ('ontouchstart' in window) || navigator.maxTouchPoints > 0;
    btnCam.style.display = esMovil ? 'flex' : 'none';
  }

  // Botón refresh
  $('fab').addEventListener('click', () => {
    $('fab').classList.add('spin');
    load().finally(() => setTimeout(() => $('fab').classList.remove('spin'), 500));
  });

  // Auto-refresh del estado cada 5 segundos (silencioso)
  setInterval(loadQ, 5000);

  // Mantener foco en el input cada 3s — asegura que el escáner siempre funcione
  // Solo si el teclado manual y la cámara no están activos
  setInterval(() => {
    if (!_tecladoVisible && !_camaraActiva && document.activeElement !== inp) {
      inp.focus();
    }
  }, 3000);
});
</script>
</body>
</html>"""


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