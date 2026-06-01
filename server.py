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
                   session, redirect, url_for)
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
}
_colectores          = {}
_etiquetas_cache     = {}
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
    Guarda los tokens en Railway via la API GraphQL.
    Solo funciona si RAILWAY_API_TOKEN, PROJECT_ID y ENVIRONMENT_ID
    estan configurados en las variables de entorno.
    """
    if not RAILWAY_API_TOKEN or not RAILWAY_PROJECT_ID:
        # Sin credenciales de Railway API, guardar en archivo local como fallback
        _persistir_tokens_local()
        return

    try:
        tokens_json = _serializar_cuentas()
        # Railway GraphQL API para actualizar variables
        query = """
        mutation variableUpsert($input: VariableUpsertInput!) {
          variableUpsert(input: $input)
        }
        """
        variables = {
            "input": {
                "projectId":     RAILWAY_PROJECT_ID,
                "environmentId": RAILWAY_ENV_ID,
                "serviceId":     RAILWAY_SERVICE_ID,
                "name":          ML_TOKENS_ENV_KEY,
                "value":         tokens_json,
            }
        }
        r = requests.post(
            "https://backboard.railway.app/graphql/v2",
            headers={
                "Authorization": f"Bearer {RAILWAY_API_TOKEN}",
                "Content-Type":  "application/json",
            },
            json={"query": query, "variables": variables},
            timeout=10)
        if r.status_code == 200 and not r.json().get("errors"):
            print(f"[TOKEN] Tokens persistidos en Railway OK")
        else:
            print(f"[TOKEN] Error persistiendo en Railway: {r.text[:200]}")
            _persistir_tokens_local()
    except Exception as e:
        print(f"[TOKEN] Error Railway API: {e}")
        _persistir_tokens_local()


def _persistir_tokens_local():
    """Fallback: guarda tokens en un archivo local (para dev o si no hay Railway API)."""
    try:
        path = "/tmp/ml_tokens.json"
        with open(path, "w") as f:
            f.write(_serializar_cuentas())
        print(f"[TOKEN] Tokens guardados localmente en {path}")
    except Exception as e:
        print(f"[TOKEN] Error guardando local: {e}")


def _cargar_tokens_local():
    """Carga tokens del archivo local (fallback)."""
    global _cuentas, _tokens
    try:
        path = "/tmp/ml_tokens.json"
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
    if exp and datetime.now() >= exp - timedelta(seconds=300):
        _renovar_token_cuenta(cid)
    return bool(_cuentas.get(cid, {}).get("access_token"))

def _token_valido():
    """Compatibilidad: verifica la cuenta_0."""
    return _token_valido_cuenta("cuenta_0")

def _renovar_token_cuenta(cuenta_id):
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
            tok["expires_at"]    = datetime.now() + timedelta(seconds=d.get("expires_in", 21600))
            _cuentas[cuenta_id]  = tok
            if cuenta_id == "cuenta_0":
                _tokens.update(tok)
            # Persistir tokens despues de cada renovacion
            _guardar_tokens()
            print(f"[TOKEN] Renovado OK para {cuenta_id}")
        else:
            print(f"[TOKEN] Error renovando {cuenta_id}: {r.status_code} {r.text[:100]}")
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


def _renovar_token():
    """Renueva el access_token usando el refresh_token."""
    try:
        payload = {
            "grant_type":    "refresh_token",
            "client_id":     ML_APP_ID,
            "client_secret": ML_SECRET_KEY,
            "refresh_token": _tokens.get("refresh_token", ""),
        }
        # Intentar con endpoint global primero (más confiable)
        r = requests.post("https://api.mercadolibre.com/oauth/token",
                          data=payload, timeout=10)
        if r.status_code != 200:
            r = requests.post(f"{ML_AUTH_URL}/oauth/token",
                              data=payload, timeout=10)
        if r.status_code == 200:
            d = r.json()
            _tokens["access_token"]  = d["access_token"]
            _tokens["refresh_token"] = d.get("refresh_token", _tokens["refresh_token"])
            _tokens["expires_at"]    = datetime.now() + timedelta(seconds=d.get("expires_in", 21600))
    except Exception:
        pass


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
                "logistica":    "",
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
    return pedidos


def _enriquecer_skus_cuenta(pedidos, cuenta_id):
    """Enriquece SKUs y estado de envío usando los tokens de una cuenta."""
    item_ids_sin_sku = {}
    for ped in pedidos.values():
        for it in ped["items"]:
            if not it["sku"] and it["item_id"]:
                item_ids_sin_sku[it["item_id"]] = True
    sku_map  = {}
    ids_list = list(item_ids_sin_sku.keys())
    for i in range(0, len(ids_list), 20):
        chunk = ids_list[i:i+20]
        r = _ml_get_cuenta("/items", cuenta_id, params={"ids": ",".join(chunk)})
        if r.status_code == 200:
            for entry in r.json():
                body = entry.get("body", {})
                iid  = body.get("id","")
                sku  = str(body.get("seller_sku") or body.get("seller_custom_field") or "").strip().upper()
                if iid and sku:
                    sku_map[iid] = sku
    for ped in pedidos.values():
        for it in ped["items"]:
            if not it["sku"] and it["item_id"] in sku_map:
                it["sku"] = sku_map[it["item_id"]]
        if ped.get("shipping_id"):
            try:
                rs = _ml_get_cuenta(f"/shipments/{ped['shipping_id']}", cuenta_id)
                if rs.status_code == 200:
                    sd = rs.json()
                    # Nuevo formato ML: logistic.type
                    log_new = (sd.get("logistic") or {}).get("type", "")
                    # Formato antiguo: logistic_type directo
                    log_old = sd.get("logistic_type", "")
                    ped["logistica"]    = log_new or log_old
                    ped["estado_envio"] = sd.get("status", "")
                    ped["substatus"]    = sd.get("substatus", "")
            except Exception:
                pass
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
                "logistica":    "",
                "estado_envio": "",
                "impreso":      False,
                "tags":         order.get("tags", []),
            }

        total  = data.get("paging",{}).get("total",0)
        offset += limit
        if offset >= total:
            break

    return pedidos


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
                    ped["logistica"]    = sd.get("logistic_type","")
                    ped["estado_envio"] = sd.get("status","")
            except Exception:
                pass

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
    except Exception as e:
        print(f"Error refresh cuenta {cuenta_id}: {e}")


def _refresh_pedidos_worker():
    """Compatibilidad: refresca todas las cuentas."""
    for cid in list(_cuentas.keys()):
        _refresh_pedidos_worker_cuenta(cid)


def _auto_refresh_loop():
    """Refresca pedidos de todas las cuentas cada 5 minutos."""
    while True:
        time.sleep(300)
        if _cuentas:
            _refresh_pedidos_worker()


threading.Thread(target=_auto_refresh_loop, daemon=True).start()


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

    entry      = _pkce_store.pop(state, None) or {}
    verifier   = entry.get("verifier") if isinstance(entry, dict) else entry
    cuenta_id  = entry.get("cuenta_id", "cuenta_0") if isinstance(entry, dict) else "cuenta_0"

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
        return jsonify({
            "ok":      True,
            "pedidos": list(_pedidos_ml.values()),
            "total":   len(_pedidos_ml),
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

    # Buscar pedido en memoria
    with _lock:
        snap = dict(_pedidos_ml)

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
    """
    Endpoint alternativo: redirige a ML con el token en la URL
    para forzar la descarga del PDF.
    """
    with _lock:
        snap = dict(_pedidos_ml)

    pedido = snap.get(order_id)
    if not pedido and len(order_id) >= 8:
        for k, v in snap.items():
            if k.endswith(order_id[-8:]):
                pedido = v; order_id = k; break

    if not pedido:
        return _html_error("Pedido no encontrado", f"#{order_id} no esta en memoria."), 404

    shipping_id = pedido.get("shipping_id","")
    if not shipping_id:
        return _html_error("Sin envio", "Este pedido no tiene envio ML asignado."), 400

    cuenta_id = pedido.get("_cuenta", "cuenta_0")
    at = _cuentas.get(cuenta_id, {}).get("access_token","")
    if not at:
        return _html_error("Token no disponible", "Reconecta la cuenta ML."), 401

    # URL directa a ML con el token — fuerza la descarga
    url = (f"{ML_API_URL}/shipment_labels"
           f"?shipment_ids={shipping_id}"
           f"&response_type=pdf2"
           f"&access_token={at}")
    from flask import redirect as r2
    return r2(url)

@app.route("/api/pedidos/marcar_impreso/<order_id>", methods=["POST"])
@requiere_auth
def marcar_impreso(order_id):
    """Marca el pedido como procesado SOLO en nuestro sistema interno, nunca en ML."""
    with _lock:
        if order_id in _pedidos_ml:
            _pedidos_ml[order_id]["impreso"] = True
    return jsonify({"ok": True, "nota": "Marcado solo localmente, sin afectar ML"})


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
    })


@app.route("/api/token_status")
def api_token_status():
    """Muestra el estado de los tokens de todas las cuentas."""
    result = []
    for cid, tok in _cuentas.items():
        exp = tok.get("expires_at")
        if isinstance(exp, datetime):
            diff = exp - datetime.now()
            horas = diff.total_seconds() / 3600
            estado_exp = f"Vence en {horas:.1f}h" if horas > 0 else "EXPIRADO"
        else:
            estado_exp = "Sin fecha"
        result.append({
            "cuenta_id":   cid,
            "nickname":    tok.get("nickname", "?"),
            "tiene_token": bool(tok.get("access_token")),
            "tiene_refresh": bool(tok.get("refresh_token")),
            "token_estado": estado_exp,
            "user_id":     tok.get("user_id",""),
        })
    return jsonify({
        "ok":           True,
        "cuentas":      result,
        "total":        len(result),
        "railway_api":  bool(RAILWAY_API_TOKEN),
        "tokens_json_guardado": bool(os.environ.get(ML_TOKENS_ENV_KEY)),
        "ts":           _ts(),
    })



def debug_logistica():
    """Muestra los valores de logistica de los primeros 20 pedidos para diagnosticar."""
    sample = []
    for oid, p in list(_pedidos_ml.items())[:20]:
        sample.append({
            "order_id":    oid,
            "shipping_id": p.get("shipping_id",""),
            "logistica":   p.get("logistica","(vacio)"),
            "estado_envio": p.get("estado_envio",""),
            "tags":        p.get("tags",[]),
        })
    tipos = {}
    for p in _pedidos_ml.values():
        log = p.get("logistica","(vacio)") or "(vacio)"
        tipos[log] = tipos.get(log, 0) + 1
    return jsonify({
        "total_pedidos": len(_pedidos_ml),
        "tipos_encontrados": tipos,
        "muestra": sample
    })



    """Muestra la config exacta que lee el servidor — solo para diagnosticar."""
    app_id = os.environ.get("ML_APP_ID", "NO_DEFINIDO")
    secret = os.environ.get("ML_SECRET_KEY", "NO_DEFINIDO")
    return jsonify({
        "ML_APP_ID":        app_id,
        "ML_APP_ID_len":    len(app_id),
        "ML_APP_ID_repr":   repr(app_id),
        "ML_SECRET_len":    len(secret),
        "ML_SECRET_first4": secret[:4] if secret else "",
        "ML_SECRET_last4":  secret[-4:] if secret else "",
        "ML_SECRET_repr":   repr(secret[:5]) + "...",
        "ML_REDIRECT":      ML_REDIRECT,
        "PICKING_API_KEY":  API_KEY,
        "ML_AUTH_URL":      ML_AUTH_URL,
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
        _estado.update({
            "fase":             data.get("fase", 1),
            "grupos":           data.get("grupos", []),
            "total_skus":       data.get("total_skus", 0),
            "total_uds":        data.get("total_uds", 0),
            "colecta":          data.get("colecta", {}),
            "colecta_completa": data.get("colecta_completa", False),
            "ultima_actualizacion": _ts(),
            "cargado":          True,
        })
    return jsonify({"ok": True, "msg": f"Estado cargado: {_estado['total_skus']} SKUs"})


@app.route("/api/estado")
def get_estado():
    with _lock:
        return jsonify(dict(_estado))


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
        nuevo = colecta[sku]
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
    """
    Inicializa el servidor al arrancar.
    Carga tokens persistidos y SKUs para sobrevivir reinicios de Railway.
    Se llama DESPUES de que todas las funciones esten definidas.
    """
    # Cargar base de datos de SKUs
    _cargar_sku_db()

    # Cargar tokens de ML desde variable de entorno (persiste entre redeploys)
    _cargar_tokens_persistidos()

    # Fallback: archivo /tmp (persiste entre reinicios normales)
    if not _cuentas:
        _cargar_tokens_local()

    if _cuentas:
        nicks = [t.get("nickname", "?") for t in _cuentas.values()]
        print(f"[STARTUP] ✅ Tokens cargados para: {nicks}")
        # Convertir expires_at de string a datetime si es necesario
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
        # Traer pedidos en background
        threading.Thread(target=_refresh_pedidos_worker, daemon=True).start()
    else:
        print("[STARTUP] ⚠ Sin tokens guardados. Conectar ML desde la app.")

_startup()


@app.route("/")
def index():
    if not _token_valido():
        return redirect("/auth/login")
    return render_template_string(HTML_APP)


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
<title>Sistema de Picking — MercadoLibre</title>
<style>
:root{--bg:#0F172A;--panel:#1E293B;--card:#162032;--border:#334155;
  --accent:#3B82F6;--accent2:#6366F1;--success:#10B981;--warning:#F59E0B;
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
                onclick="setFechaRapida(30,this)">30 días</button>
      </div>
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
      <button class="btn btn-ghost" onclick="refreshPedidos()" id="btn-refresh">
        🔄 Actualizar
      </button>
      <button class="btn btn-success" onclick="generarLotePicking()" id="btn-lote">
        ▶ Generar Lote de Picking
      </button>
    </div>
    <div class="tabla-wrap">
      <table class="tabla" id="tabla-pedidos">
        <thead>
          <tr>
            <th>Pedido</th>
            <th>Fecha</th>
            <th>Comprador</th>
            <th style="width:40%">SKU · Nombre del Producto</th>
            <th>Estado Envío</th>
            <th>Acciones</th>
          </tr>
        </thead>
        <tbody id="tbody-pedidos">
          <tr><td colspan="7" class="empty">
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
  setInterval(syncEstadoPicking, 5000);     // sync picking cada 5s
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

function renderPedidos(filtro='') {
  const tbody = document.getElementById('tbody-pedidos');
  const peds  = Object.values(PEDIDOS).filter(p => {
    if (!filtro) return true;
    const q = filtro.toLowerCase();
    return p.comprador.toLowerCase().includes(q) ||
           p.order_id.includes(q) ||
           p.items.some(it => it.titulo.toLowerCase().includes(q) ||
                               it.sku.toLowerCase().includes(q));
  });

  document.getElementById('badge-pedidos').textContent = peds.length;

  if (!peds.length) {
    tbody.innerHTML = `<tr><td colspan="7"><div class="empty">
      <div class="empty-i">📋</div><div>Sin pedidos${filtro?' que coincidan':''}</div>
    </div></td></tr>`;
    return;
  }

  tbody.innerHTML = peds.map(p => {
    // SKU + nombre del producto — mostrar todos los items del pedido
    const skus = p.items.map(it => {
      const sku  = it.sku  ? `<span class="sku-chip">${it.sku}</span>` : '';
      const nom  = it.titulo ? `<span style="color:var(--hi)">${it.titulo.substring(0,32)}</span>` : '';
      const qty  = `<span style="color:var(--accent);font-size:11px"> ×${it.cantidad}</span>`;
      return `<div style="margin-bottom:3px">${sku} ${nom}${qty}</div>`;
    }).join('');

    const envChip = p.estado_envio
      ? `<span class="estado-chip estado-ready">${p.estado_envio}</span>` : '—';

    // Botón etiqueta — siempre visible, con aviso si no tiene shipping_id
    const btnEtiq = `<button class="btn btn-ml" style="font-size:11px;padding:4px 10px"
      onclick="verEtiqueta('${p.order_id}')">🏷️ Etiqueta</button>`;

    return `<tr class="${p.impreso?'impreso':''}">
      <td><b style="color:var(--accent)">#${p.order_id}</b></td>
      <td style="color:var(--mid);font-size:12px">${p.fecha}</td>
      <td style="font-weight:600">${p.comprador}</td>
      <td>${skus}</td>
      <td>${envChip}</td>
      <td style="white-space:nowrap">${btnEtiq}</td>
    </tr>`;
  }).join('');
}

function filtrarPedidos() {
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

  const r = await fetch(`/api/etiqueta/${orderId}`);
  const d = await r.json();
  if (d.ok !== undefined && !d.ok) {
    // Error con diagnóstico mejorado
    const estado   = d.estado    ? `<br><b>Estado envío:</b> ${d.estado} / ${d.substatus||'—'}` : '';
    const logType  = d.logistic_type ? `<br><b>Logística:</b> ${d.logistic_type}` : '';
    const hint     = d.hint ? `<div style="margin-top:10px;padding:10px;background:var(--bar);
      border-radius:6px;font-size:12px;color:var(--mid)">${d.hint}</div>` : '';
    document.getElementById('modal-etiqueta-body').innerHTML =
      `<div style="color:var(--danger);margin-bottom:8px">❌ ${d.msg}</div>
       <div style="font-size:12px;color:var(--mid)">${estado}${logType}</div>
       ${hint}`;
    document.getElementById('btn-abrir-etiqueta').style.display = 'none';
  } else {
    // Éxito — el proxy devuelve el PDF directamente
    // Usar la URL del proxy que ya tiene el token embebido en el servidor
    const pdfUrl = `/api/etiqueta/${orderId}`;
    etiquetaUrlActual = pdfUrl;
    const p = PEDIDOS[orderId];
    document.getElementById('modal-etiqueta-body').innerHTML =
      `<div style="margin-bottom:10px">
         <b>#${orderId}</b> — ${p.comprador}<br>
         <span style="font-size:11px;color:var(--mid)">
           ${p.items.map(it=>`${it.sku||'?'} · ${it.titulo.substring(0,28)}`).join(' | ')}
         </span>
       </div>
       <div style="background:var(--bar);border-radius:6px;padding:8px 12px;
            font-size:11px;color:var(--mid);margin-bottom:10px">
         ✅ La etiqueta NO se marca como impresa en MercadoLibre
       </div>
       <iframe src="${pdfUrl}" style="width:100%;height:400px;border:none;
       border-radius:8px;background:#fff"></iframe>`;
    document.getElementById('btn-abrir-etiqueta').style.display = '';
    // NO llamar a marcar_impreso
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

  const payload = {
    fase: 1, grupos, colecta: {}, colecta_completa: false,
    total_skus: totalSkus, total_uds: totalUds,
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
    ESTADO_PK = d; COLECTA = d.colecta||{}; FASE_ACTUAL = d.fase||1;
    renderPickingStats();
    renderPickingLista();
  } catch(e) {}
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
  lista.innerHTML = grupos.map(g => {
    const col   = COLECTA;
    const done  = g.items.filter(it=>(col[it.sku]||0)>=it.req).length;
    const gDone = done === g.items.length;
    return `<div>
      <div class="grupo-hdr" onclick="this.nextElementSibling.classList.toggle('hidden')">
        <div><div class="grupo-nombre">📦 ${g.pasillo||'Sin pasillo'}</div>
          <div style="font-size:10px;color:var(--mid)">${g.items.length} SKUs</div></div>
        <div class="grupo-prog ${gDone?'done':''}">${done}/${g.items.length}</div>
      </div>
      <div>${g.items.map(it=>{
        const c=col[it.sku]||0, ok=c>=it.req;
        return`<div class="sku-row ${ok?'done':''}">
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
  if (!confirm('¿Pasar a Fase 2 (Armado de pedidos)?')) return;
  FASE_ACTUAL = 2;
  const est = await (await fetch('/api/estado')).json();
  est.fase = 2;
  await fetch('/api/subir_estado', {method:'POST',
    headers:{'Content-Type':'application/json','X-API-Key':'everest2024'},
    body:JSON.stringify(est)});
  renderPickingStats();
  toast('✅ Pasado a Fase 2', 'ok');
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
<title>Picking · Fase 1</title>
<style>
:root{--bg:#0F172A;--panel:#1E293B;--card:#162032;--border:#334155;
  --accent:#3B82F6;--accent2:#6366F1;--success:#10B981;
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
.iname{font-size:14px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
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
    <input id="sku" type="text" inputmode="text"
           autocomplete="off" autocorrect="off"
           autocapitalize="characters" spellcheck="false"
           placeholder="Escaneá o escribí el SKU"
           style="flex:1">
    <button id="btn-confirmar"
            style="background:var(--success);color:#fff;border:none;border-radius:10px;
            padding:0 18px;font-size:20px;font-weight:800;cursor:pointer;flex-shrink:0"
            onclick="confirmarManual()">✓</button>
  </div>
  <div style="display:flex;align-items:center;gap:8px">
    <div id="fb" class="neu" style="flex:1">Listo para escanear</div>
    <button id="btn-teclado"
            onclick="toggleTeclado()"
            style="background:var(--panel);border:1px solid var(--border);color:var(--mid);
            border-radius:8px;padding:5px 10px;font-size:16px;cursor:pointer;flex-shrink:0"
            title="Abrir/cerrar teclado">⌨</button>
  </div>
</div>

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
    E = await r.json();
    render();
  } catch(e) {
    fb('err', '❌ Sin conexión al servidor');
  }
}

async function loadQ() {
  try {
    const r = await fetch('/api/estado');
    E = await r.json();
    render(true);
  } catch(e) {}
}

// ── RENDER ─────────────────────────────────────────────────────────────────
function render(quiet = false) {
  if (!E) return;
  const gs  = E.grupos  || [];
  const col = E.colecta || {};

  if (!gs.length || !E.cargado) {
    $('content').innerHTML = `<div class="empty">
      <div class="empty-i">📋</div>
      <div class="empty-t">Esperando lote del supervisor…<br>
      <small style="color:var(--lo)">Cuando el supervisor genere el lote<br>aparecerá aquí automáticamente.</small>
      </div></div>`;
    $('badge').textContent = '— / —';
    $('badge').className   = 'badge';
    $('t-sub').textContent = 'Esperando lote…';
    return;
  }

  // Totales
  let tot = 0, done = 0, uds_done = 0, uds_tot = 0;
  gs.forEach(g => g.items.forEach(it => {
    const c = col[it.sku] || 0;
    tot++;
    uds_tot += it.req;
    uds_done += Math.min(c, it.req);
    if (c >= it.req) done++;
  }));

  const b = $('badge');
  b.textContent = `${done} / ${tot}`;
  b.className   = done === tot ? 'badge done' : (done > 0 ? 'badge warn' : 'badge');
  $('t-sub').textContent = `${uds_done} / ${uds_tot} unidades`;
  $('ban').className = E.colecta_completa ? 'show' : '';

  // Recordar grupos colapsados
  const colaps = new Set();
  document.querySelectorAll('.grupo').forEach(el => {
    if (el.querySelector('.g-items.col')) colaps.add(el.dataset.p);
  });

  $('content').innerHTML = gs.map(g => {
    const p    = g.pasillo || 'Sin pasillo';
    const its  = g.items;
    const d    = its.filter(it => (col[it.sku] || 0) >= it.req).length;
    const gDone = d === its.length;
    const cl   = colaps.has(p) ? 'col' : '';

    return `<div class="grupo" data-p="${p}">
      <div class="g-hdr" onclick="tog(this)">
        <div class="g-left">
          <span style="font-size:18px">📦</span>
          <div>
            <div class="g-name">${p}</div>
            <div class="g-stats">${its.length} SKU${its.length>1?'s':''} · ${its.reduce((s,i)=>s+i.req,0)} ud.</div>
          </div>
        </div>
        <div class="g-prog ${gDone?'done':'pend'}">${d}/${its.length}</div>
      </div>
      <div class="g-items ${cl}">
        ${its.map(it => {
          const c   = col[it.sku] || 0;
          const ok  = c >= it.req;
          return `<div class="item ${ok?'done':''}" id="item-${it.sku}">
            <div class="chk ${ok?'ok':'pend'}">${ok ? '✔' : '○'}</div>
            <div class="ibody">
              <div class="iname">${it.nombre || it.sku}</div>
              <div class="irow">
                <span class="isku">${it.sku}</span>
                <span class="icnt ${ok?'ok':'pend'}">${c} / ${it.req}</span>
              </div>
              ${it.estanteria ? `<div class="iest">🗂 ${it.estanteria}</div>` : ''}
            </div>
          </div>`;
        }).join('')}
      </div>
    </div>`;
  }).join('');
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
    inp.setAttribute('inputmode', 'text');
    inp.focus(); inp.click();
    btn.style.background = 'var(--accent)';
    btn.style.color      = '#fff';
    btn.title            = 'Cerrar teclado';
    fb('neu', 'Escribí el SKU y tocá ✓');
  } else {
    inp.setAttribute('inputmode', 'none');
    inp.blur();
    setTimeout(() => inp.focus(), 80);
    btn.style.background = 'var(--panel)';
    btn.style.color      = 'var(--mid)';
    btn.title            = 'Abrir teclado';
    fb('neu', 'Listo para escanear');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  load();
  const inp = $('sku');

  // Enter → procesar
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const v = inp.value.trim();
      if (v) { scan(v); inp.value = ''; }
    }
  });

  // Fallback 300ms (para lectores sin Enter)
  inp.addEventListener('input', () => {
    clearTimeout(window._t);
    window._t = setTimeout(() => {
      const v = inp.value.trim();
      if (v.length >= 4 && !_tecladoVisible) { scan(v); inp.value = ''; }
    }, 300);
  });

  // Clic en contenido → foco (excepto botones de teclado y confirmar)
  document.addEventListener('click', e => {
    if (!e.target.closest('#btn-teclado') &&
        !e.target.closest('#btn-confirmar') &&
        !e.target.closest('.g-hdr')) {
      if (!_tecladoVisible) inp.focus();
    }
  });

  inp.focus();

  $('fab').addEventListener('click', () => {
    $('fab').classList.add('spin');
    load().finally(() => setTimeout(() => $('fab').classList.remove('spin'), 500));
  });

  setInterval(loadQ, 5000);

  // Mantener foco cada 2s SOLO si el teclado manual no está activo
  setInterval(() => { if (!_tecladoVisible) inp.focus(); }, 2000);
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