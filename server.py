"""
server.py — Sistema de Picking integrado con MercadoLibre (Uruguay)
====================================================================
Deploy en Railway. Variables de entorno requeridas:
  ML_APP_ID      → App ID de tu aplicación ML
  ML_SECRET_KEY  → Secret Key de tu aplicación ML
  APP_SECRET_KEY → Clave para sesiones Flask (cualquier string largo)
  PICKING_API_KEY → Clave interna para sincronización con app_deposito.py

Flujo OAuth2 ML:
  1. Usuario va a /auth/login  →  redirige a ML
  2. ML redirige a /auth/callback con ?code=XXX
  3. Se intercambia por access_token + refresh_token
  4. Los tokens se guardan en sesión y se auto-renuevan
"""

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

# ── Estado en memoria ─────────────────────────────────────────────────────────
# tokens: { access_token, refresh_token, expires_at, user_id, nickname }
_tokens = {}
# pedidos ML: { order_id: { ...datos... } }
_pedidos_ml = {}
# estado picking (colecta/fase)
_estado = {
    "fase": 1, "grupos": [], "colecta": {}, "colecta_completa": False,
    "total_skus": 0, "total_uds": 0, "ultima_actualizacion": "", "cargado": False,
}
# cache de etiquetas PDF (order_id → url)
_etiquetas_cache = {}
# timestamp último refresh automático
_ultimo_refresh_pedidos = None
# Base de datos de SKUs: { SKU: {nombre, pasillo, estanteria} }
_sku_db = {}
# Archivo de persistencia de SKUs (Railway tiene disco efímero,
# pero sirve dentro de una sesión; para persistencia real usar variable de entorno)
_SKU_DB_ENV_KEY = "SKU_DB_JSON"   # Variable de entorno opcional para persistir


def _ts():
    return datetime.now().strftime("%d/%m %H:%M:%S")


def _cargar_sku_db():
    """Carga la BD de SKUs desde variable de entorno si existe."""
    global _sku_db
    raw = os.environ.get(_SKU_DB_ENV_KEY, "")
    if raw:
        try:
            _sku_db = json.loads(raw)
        except Exception:
            _sku_db = {}


def _sku_info(sku):
    """Devuelve {nombre, pasillo, estanteria} para un SKU, o defaults vacíos."""
    return _sku_db.get(sku.upper(), {"nombre": "", "pasillo": "", "estanteria": ""})


_cargar_sku_db()

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


def _ml_get_all_orders():
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


def _refresh_pedidos_worker():
    """Trae pedidos en background thread."""
    global _pedidos_ml, _ultimo_refresh_pedidos
    if not _token_valido():
        return
    try:
        pedidos = _ml_get_all_orders()
        pedidos = _enriquecer_skus(pedidos)
        with _lock:
            # Preservar flag 'impreso' de pedidos ya existentes
            for oid, p in pedidos.items():
                if oid in _pedidos_ml:
                    p["impreso"] = _pedidos_ml[oid].get("impreso", False)
            _pedidos_ml = pedidos
            _ultimo_refresh_pedidos = datetime.now()
    except Exception as e:
        print(f"Error refresh pedidos: {e}")


def _auto_refresh_loop():
    """Hilo que refresca pedidos cada 5 minutos automáticamente."""
    while True:
        time.sleep(300)  # 5 minutos
        if _token_valido() and _pedidos_ml:
            _refresh_pedidos_worker()


threading.Thread(target=_auto_refresh_loop, daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════════════
# DECORADORES
# ═══════════════════════════════════════════════════════════════════════════════

def requiere_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _token_valido():
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "msg": "No autenticado con ML", "login": "/auth/login"}), 401
            return redirect("/auth/login")
        return f(*args, **kwargs)
    return decorated

def requiere_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        k = request.headers.get("X-API-Key") or request.args.get("key")
        if k != API_KEY:
            return jsonify({"ok": False, "msg": "Clave inválida"}), 401
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
    import secrets
    state    = secrets.token_urlsafe(16)
    verifier, challenge = _generar_pkce()
    _pkce_store[state] = verifier   # guardar para el callback

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
            f"Redirect URI configurado: <code>{ML_REDIRECT}</code>"
        )

    verifier = _pkce_store.pop(state, None)

    # Construir payload — con o sin code_verifier según PKCE
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
            headers={
                "Accept":       "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=payload,
            timeout=15
        )

        if r.status_code != 200:
            try:
                err_detail = json.dumps(r.json(), indent=2, ensure_ascii=False)
            except Exception:
                err_detail = r.text
            return _html_error(
                f"Error obteniendo token ({r.status_code})",
                f"<pre style='background:#0F172A;padding:12px;border-radius:6px;"
                f"color:#94A3B8;white-space:pre-wrap;font-size:12px'>{err_detail}</pre>"
                f"<p style='color:#475569;font-size:12px'>"
                f"App ID: {ML_APP_ID} | PKCE: {'SI' if verifier else 'NO'}</p>"
            )

        d = r.json()
        _tokens["access_token"]  = d["access_token"]
        _tokens["refresh_token"] = d.get("refresh_token", "")
        _tokens["expires_at"]    = datetime.now() + timedelta(
            seconds=d.get("expires_in", 21600))

        # Obtener datos del vendedor
        me = _ml_get("/users/me").json()
        _tokens["user_id"]  = str(me.get("id", ""))
        _tokens["nickname"] = me.get("nickname", "")

        # Traer pedidos en background
        threading.Thread(target=_refresh_pedidos_worker, daemon=True).start()

        return _html_ok(
            f"Conectado como <b>{_tokens['nickname']}</b>",
            "Cerrá esta pestaña y volvé a la app de picking."
        )

    except Exception as e:
        return _html_error("Error inesperado", str(e))


def _html_error(titulo, detalle=""):
    return f"""<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
    <title>Error ML</title></head>
    <body style="font-family:Arial,sans-serif;background:#0F172A;color:#F1F5F9;
    padding:40px;max-width:700px;margin:auto">
    <div style="background:#1E293B;border:1px solid #EF4444;border-radius:12px;padding:32px">
    <h2 style="color:#EF4444;margin:0 0 16px">❌ {titulo}</h2>
    <div style="color:#94A3B8">{detalle}</div>
    <a href="/auth/login" style="display:inline-block;margin-top:24px;background:#3B82F6;
    color:white;padding:10px 24px;border-radius:8px;text-decoration:none;font-weight:bold">
    🔄 Reintentar</a></div></body></html>"""


def _html_ok(titulo, subtitulo=""):
    return f"""<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
    <title>Conectado ML</title></head>
    <body style="font-family:Arial,sans-serif;background:#0F172A;color:#F1F5F9;
    padding:40px;max-width:600px;margin:auto">
    <div style="background:#1E293B;border:1px solid #10B981;border-radius:12px;padding:32px;
    text-align:center">
    <div style="font-size:52px;margin-bottom:16px">✅</div>
    <h2 style="color:#10B981;margin:0 0 12px">{titulo}</h2>
    <p style="color:#94A3B8">{subtitulo}</p>
    <a href="/" style="display:inline-block;margin-top:24px;background:#10B981;
    color:white;padding:10px 24px;border-radius:8px;text-decoration:none;font-weight:bold">
    Ir al panel →</a></div></body></html>"""


@app.route("/auth/logout")
def auth_logout():
    _tokens.clear()
    return redirect("/auth/login")


@app.route("/auth/status")
def auth_status():
    return jsonify({
        "autenticado":    _token_valido(),
        "nickname":       _tokens.get("nickname", ""),
        "user_id":        _tokens.get("user_id", ""),
        "pedidos":        len(_pedidos_ml),
        "ultimo_refresh": _ultimo_refresh_pedidos.strftime("%d/%m %H:%M:%S")
                          if _ultimo_refresh_pedidos else "—",
    })


# ═══════════════════════════════════════════════════════════════════════════════
# API PEDIDOS ML
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/pedidos")
@requiere_auth
def api_pedidos():
    with _lock:
        return jsonify({
            "ok":      True,
            "pedidos": list(_pedidos_ml.values()),
            "total":   len(_pedidos_ml),
            "ts":      _ultimo_refresh_pedidos.strftime("%d/%m %H:%M:%S") if _ultimo_refresh_pedidos else "—",
        })


@app.route("/api/pedidos/refresh", methods=["POST"])
@requiere_auth
def api_refresh():
    threading.Thread(target=_refresh_pedidos_worker, daemon=True).start()
    return jsonify({"ok": True, "msg": "Actualizando pedidos en segundo plano…"})


@app.route("/api/etiqueta/<order_id>")
@requiere_auth
def api_etiqueta(order_id):
    """Devuelve la URL del PDF de etiqueta para un pedido."""
    with _lock:
        pedido = _pedidos_ml.get(order_id)
    if not pedido:
        return jsonify({"ok": False, "msg": "Pedido no encontrado"}), 404

    shipping_id = pedido.get("shipping_id")
    if not shipping_id:
        return jsonify({"ok": False, "msg": "Este pedido no tiene envío"}), 400

    # Obtener label de ML
    r = _ml_get(f"/shipments/{shipping_id}/labels",
                params={"response_type": "zpl2", "caller.id": _tokens.get("user_id","")})

    # ML también permite PDF directo
    r_pdf = _ml_get(f"/shipments/{shipping_id}/labels",
                    params={"response_type": "pdf2"})

    if r_pdf.status_code == 200:
        # Devolver URL directa al PDF de ML
        label_url = f"{ML_API_URL}/shipments/{shipping_id}/labels?response_type=pdf2&access_token={_tokens['access_token']}"
        return jsonify({"ok": True, "url": label_url, "shipping_id": shipping_id})

    return jsonify({"ok": False, "msg": f"No se pudo obtener etiqueta (status {r_pdf.status_code})"}), 400


@app.route("/api/pedidos/marcar_impreso/<order_id>", methods=["POST"])
@requiere_auth
def marcar_impreso(order_id):
    with _lock:
        if order_id in _pedidos_ml:
            _pedidos_ml[order_id]["impreso"] = True
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════════
# API PICKING (sync con app_deposito.py)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/ping")
def ping():
    return jsonify({
        "ok": True, "ts": _ts(),
        "cargado":    _estado["cargado"],
        "skus":       _estado["total_skus"],
        "autenticado": _token_valido(),
        "pedidos_ml": len(_pedidos_ml),
    })


@app.route("/api/debug_config")
def debug_config():
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
            <th>Productos / SKUs</th>
            <th>Total</th>
            <th>Envío</th>
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
async function refreshPedidos() {
  const btn = document.getElementById('btn-refresh');
  btn.innerHTML = '<span class="spin">🔄</span> Actualizando…';
  btn.disabled = true;
  await fetch('/api/pedidos/refresh', {method:'POST'});
  await new Promise(r => setTimeout(r, 3000));
  await cargarPedidos();
  btn.innerHTML = '🔄 Actualizar';
  btn.disabled = false;
  toast('Pedidos actualizados', 'ok');
}

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
    const skus = p.items.map(it =>
      `<span class="sku-chip">${it.sku||'?'}</span> ${it.titulo.substring(0,25)}… ×${it.cantidad}`
    ).join('<br>');
    const envChip = p.estado_envio ?
      `<span class="estado-chip estado-ready">${p.estado_envio}</span>` : '—';
    const btnEtiq = p.shipping_id ?
      `<button class="btn btn-ml" style="font-size:11px;padding:4px 10px"
        onclick="verEtiqueta('${p.order_id}')">🏷️ Etiqueta</button>` : '';
    return `<tr class="${p.impreso?'impreso':''}">
      <td><b style="color:var(--accent)">#${p.order_id}</b></td>
      <td style="color:var(--mid);font-size:12px">${p.fecha}</td>
      <td style="font-weight:600">${p.comprador}</td>
      <td>${skus}</td>
      <td style="color:var(--success);font-weight:700">${p.moneda} ${p.total}</td>
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
    `<div>📦 Pedido #${orderId} — ${p.comprador}<br>
     <span class="spin">⏳</span> Obteniendo etiqueta…</div>`;
  document.getElementById('modal-etiqueta').classList.add('open');
  etiquetaUrlActual = '';

  const r = await fetch(`/api/etiqueta/${orderId}`);
  const d = await r.json();
  if (d.ok) {
    etiquetaUrlActual = d.url;
    document.getElementById('modal-etiqueta-body').innerHTML =
      `<div style="margin-bottom:8px">📦 <b>#${orderId}</b> — ${p.comprador}</div>
       <iframe src="${d.url}" style="width:100%;height:400px;border:none;border-radius:8px;
       background:#fff"></iframe>`;
    await fetch(`/api/pedidos/marcar_impreso/${orderId}`, {method:'POST'});
    PEDIDOS[orderId].impreso = true;
    renderPedidos(document.getElementById('search-pedidos').value);
  } else {
    document.getElementById('modal-etiqueta-body').innerHTML =
      `<div style="color:var(--danger)">❌ ${d.msg}</div>
       <div style="color:var(--mid);font-size:12px;margin-top:8px">
       Verificá que el pedido tenga envío activo en ML.</div>`;
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
<title>Picking</title>
<style>
:root{--bg:#0F172A;--panel:#1E293B;--card:#162032;--border:#334155;--accent:#3B82F6;--accent2:#6366F1;--success:#10B981;--warning:#F59E0B;--danger:#EF4444;--hi:#F1F5F9;--mid:#94A3B8;--lo:#475569;--bar:#1E3A5F}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{background:var(--bg);color:var(--hi);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}
.topbar{background:var(--panel);padding:12px 16px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;border-bottom:1px solid var(--border)}
.t-title{font-size:14px;font-weight:800}.t-sub{font-size:10px;color:var(--mid)}
.badge{background:var(--accent);color:#fff;font-size:12px;font-weight:700;padding:4px 10px;border-radius:20px}
.badge.done{background:var(--success)}
.scan-box{padding:12px 14px;background:var(--panel);border-bottom:1px solid var(--border)}
.scan-lbl{font-size:10px;font-weight:700;color:var(--lo);letter-spacing:.08em;margin-bottom:6px}
.input-row{display:flex;gap:8px}
#sku{flex:1;background:var(--card);border:2px solid var(--accent);color:var(--hi);font-size:16px;font-family:monospace;font-weight:700;padding:10px 14px;border-radius:10px;outline:none;text-align:center;text-transform:uppercase}
.btn-cam{background:var(--accent);border:none;border-radius:10px;color:#fff;font-size:22px;width:48px;height:48px;display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0}
.btn-cam.on{background:var(--danger)}
#fb{margin-top:8px;min-height:36px;border-radius:8px;padding:8px 12px;font-size:13px;font-weight:600;display:flex;align-items:center;gap:8px}
#fb.ok{background:rgba(16,185,129,.15);color:var(--success)}
#fb.warn{background:rgba(245,158,11,.15);color:var(--warning)}
#fb.err{background:rgba(239,68,68,.15);color:var(--danger)}
#fb.neu{color:var(--mid)}
#cam-wrap{display:none;background:#000;position:relative}
#cam-wrap.open{display:block}
#vid{width:100%;max-height:220px;object-fit:cover;display:block}
.cam-over{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none}
.cam-rect{width:60%;height:55px;border:2px solid var(--accent);border-radius:6px;box-shadow:0 0 0 9999px rgba(0,0,0,.45)}
.content{padding:10px 10px 90px}
.grupo{margin-bottom:10px;border-radius:10px;overflow:hidden;border:1px solid var(--border)}
.g-hdr{background:var(--bar);padding:10px 14px;display:flex;align-items:center;justify-content:space-between;cursor:pointer;user-select:none}
.g-name{font-size:13px;font-weight:800;color:var(--accent);text-transform:uppercase}
.g-stats{font-size:10px;color:var(--mid)}
.g-prog{font-size:13px;font-weight:700}
.g-prog.done{color:var(--success)}.g-prog.pend{color:var(--accent)}
.g-items{background:var(--card)}
.g-items.col{display:none}
.item{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--border)}
.item:last-child{border-bottom:none}.item.done{opacity:.5}
.chk{font-size:20px;flex-shrink:0;width:26px;text-align:center}
.chk.ok{color:var(--success)}.chk.pend{color:var(--lo)}
.ibody{flex:1;min-width:0}
.iname{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.irow{display:flex;align-items:center;gap:8px;margin-top:2px}
.isku{font-family:monospace;font-size:11px;color:var(--mid)}
.icnt{font-size:12px;font-weight:700}
.icnt.ok{color:var(--success)}.icnt.pend{color:var(--accent)}
.iest{font-size:10px;color:var(--accent2);margin-top:2px}
#toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(80px);background:var(--panel);border:1px solid var(--border);color:var(--hi);padding:12px 22px;border-radius:30px;font-size:14px;font-weight:600;z-index:999;transition:transform .3s,opacity .3s;opacity:0;white-space:nowrap}
#toast.show{transform:translateX(-50%) translateY(0);opacity:1}
#toast.ok{border-color:var(--success);color:var(--success)}
#toast.err{border-color:var(--danger);color:var(--danger)}
#flash{position:fixed;inset:0;pointer-events:none;opacity:0;transition:opacity .15s;z-index:200}
#flash.ok{background:rgba(16,185,129,.2)}#flash.err{background:rgba(239,68,68,.2)}
.fab{position:fixed;bottom:20px;right:20px;background:var(--accent);color:#fff;border:none;border-radius:50%;width:52px;height:52px;font-size:22px;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:50}
.spin{animation:spin .7s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
#ban{display:none;background:var(--success);color:#fff;text-align:center;padding:12px;font-size:14px;font-weight:800}
#ban.show{display:block}
.empty{text-align:center;padding:60px 20px;color:var(--lo)}
</style>
</head>
<body>
<div id="flash"></div>
<div class="topbar">
  <div style="display:flex;align-items:center;gap:10px">
    <span style="font-size:20px">⬡</span>
    <div><div class="t-title">PICKING · FASE 1</div><div class="t-sub">Colecta en depósito</div></div>
  </div>
  <div id="badge" class="badge">— / —</div>
</div>
<div id="ban">🎉 ¡COLECTA COMPLETA!</div>
<div class="scan-box">
  <div class="scan-lbl">ESCANEAR CÓDIGO</div>
  <div class="input-row">
    <input id="sku" type="text" inputmode="text" autocomplete="off" autocorrect="off"
           autocapitalize="characters" spellcheck="false" placeholder="Escaneá o escribí">
    <button class="btn-cam" id="btn-cam">📷</button>
  </div>
  <div id="fb" class="neu">Listo para escanear</div>
</div>
<div id="cam-wrap">
  <video id="vid" autoplay playsinline muted></video>
  <div class="cam-over"><div class="cam-rect"></div></div>
</div>
<div class="content" id="content">
  <div class="empty"><div style="font-size:48px">📦</div><div>Cargando…</div></div>
</div>
<button class="fab" id="fab">🔄</button>
<div id="toast"></div>
<script>
let E=null,camOn=false,stream=null,bd=null,loop=null,last=null;
const $=id=>document.getElementById(id);
async function load(){try{const r=await fetch('/api/estado');E=await r.json();render();}catch(e){fb('err','❌ Sin conexión');}}
async function loadQ(){try{const r=await fetch('/api/estado');E=await r.json();render(true);}catch(e){}}
function render(q=false){
  if(!E||!E.cargado){$('content').innerHTML='<div class="empty"><div style="font-size:48px">📋</div><div>Esperando lote del supervisor…</div></div>';return;}
  const gs=E.grupos||[],col=E.colecta||{};
  let tot=0,done=0;gs.forEach(g=>g.items.forEach(it=>{tot++;if((col[it.sku]||0)>=it.req)done++;}));
  const b=$('badge');b.textContent=`${done}/${tot}`;b.className=done===tot?'badge done':'badge';
  $('ban').className=E.colecta_completa?'show':'';
  const prev=new Set();document.querySelectorAll('.grupo').forEach(el=>{if(el.querySelector('.g-items.col'))prev.add(el.dataset.p);});
  $('content').innerHTML=gs.map(g=>{
    const p=g.pasillo||'Sin pasillo',its=g.items;
    const d=its.filter(it=>(col[it.sku]||0)>=it.req).length;
    return`<div class="grupo" data-p="${p}">
<div class="g-hdr" onclick="tog(this)">
  <div><div class="g-name">📦 ${p}</div><div class="g-stats">${its.length} SKUs</div></div>
  <div class="g-prog ${d===its.length?'done':'pend'}">${d}/${its.length}</div>
</div>
<div class="g-items ${prev.has(p)?'col':''}">${its.map(it=>{
  const c=col[it.sku]||0,ok=c>=it.req;
  return`<div class="item ${ok?'done':''}">
<div class="chk ${ok?'ok':'pend'}">${ok?'✔':'○'}</div>
<div class="ibody">
  <div class="iname">${it.nombre||it.sku}</div>
  <div class="irow"><span class="isku">${it.sku}</span><span class="icnt ${ok?'ok':'pend'}">${c}/${it.req}</span></div>
  ${it.estanteria?`<div class="iest">🗂 ${it.estanteria}</div>`:''}
</div></div>`;}).join('')}</div></div>`;}).join('');
}
function tog(h){h.nextElementSibling.classList.toggle('col');}
async function scan(raw){
  const sku=raw.trim().toUpperCase();if(!sku||sku.length<2)return;
  if(last===sku)return;last=sku;setTimeout(()=>last=null,150);
  try{
    const r=await fetch('/api/escanear',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sku})});
    const d=await r.json();
    if(!d.ok){flash('err');vib([200,80,200]);fb('err',`❌ ${d.msg}`);}
    else if(d.tipo==='ya_completo'){fb('warn',`⚠ ${d.nombre||sku} ya completo`);}
    else{
      flash('ok');vib([60]);
      fb('ok',d.tipo==='completo'?`✔ ${d.nombre||sku} ¡Listo!`:`${d.nombre||sku} ${d.collected}/${d.req}`);
      if(!E.colecta)E.colecta={};E.colecta[sku]=d.collected;
      if(d.todo_completo){E.colecta_completa=true;toast('🎉 ¡Colecta completa!','ok');}
      render(true);
    }
  }catch(e){fb('err','❌ Error');}
}
function fb(t,m){const el=$('fb');el.className=t;el.textContent=m;}
function toast(m,t=''){const el=$('toast');el.textContent=m;el.className='show '+t;setTimeout(()=>el.className='',2800);}
function flash(t){const el=$('flash');el.className=t;el.style.opacity='1';setTimeout(()=>{el.style.opacity='0';setTimeout(()=>el.className='',300);},160);}
function vib(p){if(navigator.vibrate)navigator.vibrate(p);}
async function camTog(){camOn?camStop():camStart();}
async function camStart(){
  try{
    stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'environment'}});
    $('vid').srcObject=stream;$('cam-wrap').classList.add('open');$('btn-cam').classList.add('on');$('btn-cam').textContent='⏹';camOn=true;
    if('BarcodeDetector' in window){bd=new BarcodeDetector({formats:['code_128','code_39','ean_13','ean_8','qr_code','upc_a','upc_e','itf']});loop=requestAnimationFrame(detect);}
    else fb('warn','⚠ Usá el lector físico.');
  }catch(e){fb('err','❌ '+e.message);}
}
async function detect(){
  if(!camOn||!bd)return;
  const v=$('vid');
  if(v.readyState===v.HAVE_ENOUGH_DATA){try{const bs=await bd.detect(v);if(bs.length){scan(bs[0].rawValue);await new Promise(r=>setTimeout(r,1500));}}catch(e){}}
  if(camOn)loop=requestAnimationFrame(detect);
}
function camStop(){
  if(stream)stream.getTracks().forEach(t=>t.stop());stream=null;
  if(loop){cancelAnimationFrame(loop);loop=null;}
  $('cam-wrap').classList.remove('open');$('btn-cam').classList.remove('on');$('btn-cam').textContent='📷';camOn=false;
}
document.addEventListener('DOMContentLoaded',()=>{
  load();
  const inp=$('sku');
  inp.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();const v=inp.value.trim();if(v){scan(v);inp.value='';}}}); 
  inp.addEventListener('input',()=>{clearTimeout(window._t);window._t=setTimeout(()=>{const v=inp.value.trim();if(v.length>=4){scan(v);inp.value='';}},400);});
  $('btn-cam').addEventListener('click',camTog);
  $('fab').addEventListener('click',()=>{$('fab').classList.add('spin');load().finally(()=>setTimeout(()=>$('fab').classList.remove('spin'),500));});
  document.addEventListener('click',e=>{if(!e.target.closest('#cam-wrap')&&!e.target.closest('#btn-cam'))inp.focus();});
  inp.focus();setInterval(loadQ,6000);
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