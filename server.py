"""
server.py — Sistema de Picking integrado con MercadoLibre (Uruguay)
====================================================================
VERSION: 2.1.0 — Auto-token + Persistencia Railway
"""

SERVER_VERSION = "2.1.0"

import os, json, threading, time, requests, logging
from datetime import datetime, timedelta
from flask import (Flask, jsonify, request, render_template_string,
                   session, redirect, url_for, Response)
from functools import wraps

# ── Logging centralizado ──────────────────────────────────────────────────────
# En produccion: solo INFO y superior — elimina el spam de DEBUG
# Para activar DEBUG temporalmente: variable de entorno LOG_LEVEL=DEBUG
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%d/%m %H:%M:%S"
)
logger = logging.getLogger("server")
# Silenciar loggers ruidosos de librerias externas
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("waitress").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET_KEY", os.urandom(24).hex())
_lock = threading.Lock()

# ── Templates HTML ─────────────────────────────────────────────────────────────
_TEMPLATES_DIR      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
_template_cache     = {}
_TEMPLATES_NO_CACHE = os.environ.get("TEMPLATES_NO_CACHE", "").strip() == "1"

def _leer_template(nombre):
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
                f"<p>Asegurate de que la carpeta <code>templates/</code> este "
                f"en el repositorio junto a server.py.</p>"), 500

# ── Config ML ─────────────────────────────────────────────────────────────────
ML_APP_ID     = os.environ.get("ML_APP_ID", "")
ML_SECRET_KEY = os.environ.get("ML_SECRET_KEY", "")
ML_SITE_ID    = "MLU"
ML_AUTH_URL   = "https://auth.mercadolibre.com.uy"
ML_API_URL    = "https://api.mercadolibre.com"
ML_REDIRECT   = os.environ.get("ML_REDIRECT_URI", "")
if not ML_REDIRECT:
    # Construir desde RAILWAY_PUBLIC_DOMAIN si está disponible
    _domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if _domain:
        ML_REDIRECT = f"https://{_domain}/auth/callback"

API_KEY           = os.environ.get("PICKING_API_KEY", "")
if not API_KEY:
    raise RuntimeError(
        "[STARTUP] Variable PICKING_API_KEY no definida en Railway. "
        "Agrégala en Settings → Variables.")
ML_TOKENS_ENV_KEY = "ML_TOKENS_JSON"
_SKU_DB_ENV_KEY   = "SKU_DB_JSON"

# ── Directorio de datos persistente ───────────────────────────────────────────
def _resolver_data_dir():
    candidato = os.environ.get("DATA_DIR", "").strip()
    if not candidato:
        logger.warning("[DATA] DATA_DIR no definido — usando /tmp (VOLATIL)")
        os.makedirs("/tmp", exist_ok=True)
        return "/tmp"
    try:
        os.makedirs(candidato, exist_ok=True)
        test = os.path.join(candidato, ".write_test")
        with open(test, "w") as f:
            f.write("ok")
        os.remove(test)
        logger.info(f"[DATA] Persistencia activa en: {candidato}")
        return candidato
    except Exception as e:
        logger.warning(f"[DATA] No se pudo usar '{candidato}' ({e}) — usando /tmp")
        os.makedirs("/tmp", exist_ok=True)
        return "/tmp"

DATA_DIR       = _resolver_data_dir()
LOTE_PATH      = os.path.join(DATA_DIR, "lote_estado.json")
ML_TOKENS_PATH = os.path.join(DATA_DIR, "ml_tokens.json")
USUARIOS_PATH  = os.path.join(DATA_DIR, "usuarios.json")

RAILWAY_API_TOKEN  = os.environ.get("RAILWAY_API_TOKEN", "")
RAILWAY_PROJECT_ID = os.environ.get("RAILWAY_PROJECT_ID", "")
RAILWAY_SERVICE_ID = os.environ.get("RAILWAY_SERVICE_ID", "")
RAILWAY_ENV_ID     = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")

# ── Estado en memoria ─────────────────────────────────────────────────────────
_cuentas       = {}
_tokens        = {}
_cuenta_activa = "cuenta_0"
_pedidos_ml    = {}

def _estado_vacio():
    return {
        "fase": 1, "grupos": [], "colecta": {}, "colecta_completa": False,
        "total_skus": 0, "total_uds": 0, "ultima_actualizacion": "",
        "cargado": False, "pedidos": {}, "etiquetas_impresas": [],
    }

_estados_canal = {
    "flex":    _estado_vacio(),
    "colecta": _estado_vacio(),
}
_estado = _estados_canal["colecta"]

# ── Control inteligente de sync ────────────────────────────────────────────────
UMBRAL_REACTIVAR   = 2
_sync_estado_canal = {"flex": "idle", "colecta": "idle"}

def _actualizar_sync_estado(canal: str):
    est = _get_estado(canal)
    if not est.get("cargado"):
        _sync_estado_canal[canal] = "idle"; return
    pedidos = est.get("pedidos", {})
    if not pedidos:
        _sync_estado_canal[canal] = "idle"; return
    impresas_set = set(est.get("etiquetas_impresas", []))
    pendientes   = sum(1 for oid in pedidos if oid not in impresas_set)
    _sync_estado_canal[canal] = "casi_listo" if pendientes <= UMBRAL_REACTIVAR else "en_lote"

def _hora_uruguay():
    from datetime import timezone
    return datetime.now(timezone(timedelta(hours=-3)))

def _sync_pausado() -> bool:
    ahora    = _hora_uruguay()
    hora     = ahora.hour
    mins     = ahora.minute
    es_noche = hora >= 19 or hora < 6 or (hora == 6 and mins < 45)
    if es_noche:
        return True
    if hora == 6 and mins >= 45:
        return False
    activos = [c for c, e in _estados_canal.items() if e.get("cargado")]
    if not activos:
        return False
    return all(_sync_estado_canal.get(c, "idle") == "en_lote" for c in activos)

def _get_estado(canal=None):
    if not canal or canal == "default":
        return _estado
    canal = canal.lower().strip()
    if canal not in _estados_canal:
        _estados_canal[canal] = _estado_vacio()
    return _estados_canal[canal]

_colectores          = {}
_etiquetas_cache     = {}
_cola_etiquetas      = []
_ultimo_refresh_pedidos = None
_sku_db              = {}
_usuarios            = []   # lista de usuarios cargada desde /data/usuarios.json
_pkce_store          = {}

def _ts():
    return datetime.now().strftime("%d/%m %H:%M:%S")

# ── Persistencia de tokens ─────────────────────────────────────────────────────

def _cargar_tokens_persistidos():
    global _cuentas, _tokens
    raw = os.environ.get(ML_TOKENS_ENV_KEY, "").strip()
    if not raw:
        return
    try:
        data = json.loads(raw)
        for cid, tok in data.items():
            if tok.get("expires_at") and isinstance(tok["expires_at"], str):
                try:
                    tok["expires_at"] = datetime.fromisoformat(tok["expires_at"])
                except Exception:
                    tok["expires_at"] = datetime.now() + timedelta(hours=1)
            _cuentas[cid] = tok
        if "cuenta_0" in _cuentas:
            _tokens.update(_cuentas["cuenta_0"])
        logger.info(f"[TOKEN] Tokens cargados: {list(_cuentas.keys())}")
    except Exception as e:
        logger.error(f"[TOKEN] Error cargando tokens: {e}")

def _serializar_cuentas():
    data = {}
    for cid, tok in _cuentas.items():
        t = dict(tok)
        if isinstance(t.get("expires_at"), datetime):
            t["expires_at"] = t["expires_at"].isoformat()
        data[cid] = t
    return json.dumps(data, ensure_ascii=False)

def _persistir_tokens_local():
    try:
        data = _serializar_cuentas()
        with open(ML_TOKENS_PATH, "w") as f:
            f.write(data)
        logger.info(f"[TOKEN] Tokens guardados en {ML_TOKENS_PATH}")
        os.environ[ML_TOKENS_ENV_KEY] = data
    except Exception as e:
        logger.error(f"[TOKEN] Error guardando: {e}")

def _persistir_tokens_railway():
    _persistir_tokens_local()

def _cargar_tokens_local():
    global _cuentas, _tokens
    try:
        if os.path.exists(ML_TOKENS_PATH):
            with open(ML_TOKENS_PATH) as f:
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
            logger.info(f"[TOKEN] Tokens cargados desde archivo local: {list(_cuentas.keys())}")
    except Exception as e:
        logger.error(f"[TOKEN] Error cargando local: {e}")

def _guardar_tokens():
    threading.Thread(target=_persistir_tokens_railway, daemon=True).start()

# ── Tokens y cuentas ──────────────────────────────────────────────────────────

def _tokens_de(cuenta_id):
    return _cuentas.get(cuenta_id, {})

def _token_valido_cuenta(cuenta_id=None):
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
    tok = _cuentas.get(cuenta_id, {})
    if not tok.get("refresh_token"):
        return
    try:
        r = requests.post(
            "https://api.mercadolibre.com/oauth/token",
            headers={"Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "client_id": ML_APP_ID,
                  "client_secret": ML_SECRET_KEY, "refresh_token": tok["refresh_token"]},
            timeout=10)
        if r.status_code == 200:
            d = r.json()
            tok["access_token"]  = d["access_token"]
            tok["refresh_token"] = d.get("refresh_token", tok["refresh_token"])
            tok["expires_at"]    = datetime.now() + timedelta(seconds=d.get("expires_in", 21600))
            _cuentas[cuenta_id] = tok
            if cuenta_id == "cuenta_0":
                _tokens.update(tok)
            _guardar_tokens()
            logger.info(f"[TOKEN] Renovado OK para {cuenta_id}")
        else:
            logger.warning(f"[TOKEN] Error renovando {cuenta_id}: {r.status_code}")
    except Exception as e:
        logger.error(f"[TOKEN] Excepcion renovando {cuenta_id}: {e}")

def _renovar_token():
    _renovar_token_cuenta("cuenta_0")

def _ml_get_cuenta(ruta, cuenta_id, params=None):
    tok = _cuentas.get(cuenta_id, {})
    at  = tok.get("access_token", "")
    return requests.get(ML_API_URL + ruta,
                        headers={"Authorization": f"Bearer {at}"},
                        params=params or {}, timeout=12)

def _ml_get(ruta, params=None):
    return _ml_get_cuenta(ruta, "cuenta_0", params)

def _cuentas_info():
    return [
        {"cuenta_id": cid, "nickname": tok.get("nickname", cid),
         "user_id": tok.get("user_id", ""), "activa": bool(tok.get("access_token"))}
        for cid, tok in _cuentas.items() if tok.get("access_token")
    ]

# ══════════════════════════════════════════════════════════════════════════════
# GESTIÓN DE USUARIOS — persiste en /data/usuarios.json (Railway Volume)
# ══════════════════════════════════════════════════════════════════════════════

def _cargar_usuarios():
    """Carga usuarios desde /data/usuarios.json al arrancar."""
    global _usuarios
    try:
        if os.path.exists(USUARIOS_PATH):
            with open(USUARIOS_PATH, encoding="utf-8") as f:
                _usuarios = json.load(f)
            logger.info(f"[USUARIOS] Cargados: {[u.get('usuario') for u in _usuarios]}")
        else:
            # Primera vez: crear usuario admin por defecto
            _usuarios = [{
                "usuario":    "admin",
                "clave":      "1234",
                "nombre":     "Administrador",
                "cuenta_id":  "todas",
                "rol":        "supervisor",
            }]
            _guardar_usuarios()
            logger.info("[USUARIOS] Archivo creado con usuario admin por defecto")
    except Exception as e:
        logger.error(f"[USUARIOS] Error cargando: {e}")
        _usuarios = []


def _guardar_usuarios():
    """Persiste usuarios en /data/usuarios.json."""
    try:
        with open(USUARIOS_PATH, "w", encoding="utf-8") as f:
            json.dump(_usuarios, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[USUARIOS] Error guardando: {e}")


def _verificar_credenciales(usuario: str, clave: str):
    """
    Verifica credenciales. Devuelve el dict del usuario si son correctas,
    o None si son incorrectas.
    No guarda contraseñas en texto plano en logs.
    """
    usuario = usuario.strip().lower()
    for u in _usuarios:
        if u.get("usuario","").lower() == usuario and u.get("clave","") == clave:
            return {
                "usuario":   u.get("usuario"),
                "nombre":    u.get("nombre", u.get("usuario")),
                "cuenta_id": u.get("cuenta_id", "todas"),
                "rol":       u.get("rol", "operario"),
            }
    return None





def _cargar_sku_db():
    global _sku_db
    raw = os.environ.get(_SKU_DB_ENV_KEY, "").strip()
    if raw:
        try:
            _sku_db = json.loads(raw)
            logger.info(f"[STARTUP] SKUs cargados: {len(_sku_db)}")
        except Exception as e:
            logger.error(f"[STARTUP] Error cargando SKU_DB_JSON: {e}")
            _sku_db = {}

def _sku_info(sku):
    return _sku_db.get(str(sku).upper(), {"nombre": "", "pasillo": "", "estanteria": ""})

# ── HTML helpers ──────────────────────────────────────────────────────────────

def _html_ok(titulo, detalle=""):
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
  color:#fff;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;cursor:pointer;border:none}
</style></head>
<body><div class="box">
  <div class="icon">&#x2705;</div>
  <h1>{{ titulo }}</h1>
  <p>{{ detalle|safe }}</p>
  <button class="btn" onclick="window.close()">Cerrar esta pestana</button>
</div></body></html>""", titulo=titulo, detalle=detalle)

def _html_error(titulo, detalle=""):
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
  <span class="icon">&#x274C;</span>
  <h1>{{ titulo }}</h1>
  <div class="detail">{{ detalle|safe }}</div>
  <button class="btn" onclick="history.back()">&#x2190; Volver</button>
</div></body></html>""", titulo=titulo, detalle=detalle)

# ── Clasificacion logistica ────────────────────────────────────────────────────

def _calcular_tipo(logistica, tags_order=None, shipping_id=""):
    log = (logistica or "").lower().strip()
    if log == "self_service":
        return "flex"
    if log in ("cross_docking", "xd_drop_off", "drop_off", "turbo", "xd_same_day"):
        return "colecta"
    if log == "fulfillment":
        return "full"
    if log in ("default", "custom", "not_specified"):
        return "me1"
    tags_str = " ".join(t.lower() for t in (tags_order or []))
    if "self_service" in tags_str or "flex" in tags_str:
        return "flex"
    if "cross_docking" in tags_str or "xd_drop_off" in tags_str:
        return "colecta"
    return "desconocido"

# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS ML — Pedidos, SKUs, Shipments
# ═══════════════════════════════════════════════════════════════════════════════

def _ml_get_all_orders_cuenta(cuenta_id, fecha_desde=None, fecha_hasta=None):
    tok = _cuentas.get(cuenta_id, {})
    uid = tok.get("user_id")
    if not uid:
        return {}
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
            "seller": uid, "order.status": "paid",
            "order.date_created.from": fecha_desde, "order.date_created.to": fecha_hasta,
            "offset": offset, "limit": limit, "sort": "date_desc",
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
                    sku = str(item_data.get("seller_sku") or item_data.get("seller_custom_field") or "").strip()
                sku   = sku.upper() if sku else ""
                color = next((a["value_name"] for a in var_attrs if "color" in a.get("name","").lower()), "")
                talle = next((a["value_name"] for a in var_attrs
                              if a.get("name","").lower() in ("talle","size","talha","talla")), "")
                items.append({
                    "item_id": item_data.get("id",""), "titulo": item_data.get("title",""),
                    "sku": sku, "cantidad": it.get("quantity", 1),
                    "color": color, "talle": talle, "unit_price": it.get("unit_price", 0),
                })
            ship          = order.get("shipping") or {}
            log_type      = (ship.get("logistic_type") or "").lower()
            tipo_calculado = _calcular_tipo(log_type, order.get("tags",[]),
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
                "logistica":    log_type, "tipo": tipo_calculado,
                "estado_envio": "", "impreso": False,
                "tags":         order.get("tags", []),
                "_cuenta":      cuenta_id, "_nickname": tok.get("nickname", cuenta_id),
            }
        total  = data.get("paging",{}).get("total",0)
        offset += limit
        if offset >= total:
            break

    # Agrupar por pack_id
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
            oid_principal = packs_vistos[pack_id]
            ped_principal = pedidos_agrupados[oid_principal]
            skus_existentes = {it["sku"] for it in ped_principal["items"]}
            for it in ped.get("items", []):
                if it["sku"] not in skus_existentes:
                    ped_principal["items"].append(it)
                    skus_existentes.add(it["sku"])
                else:
                    for ex in ped_principal["items"]:
                        if ex["sku"] == it["sku"] and ex["item_id"] != it["item_id"]:
                            ex["cantidad"] += it["cantidad"]; break
            ped_principal["total"] = ped_principal.get("total", 0) + ped.get("total", 0)
            logger.debug(f"[PACK] Agrupado {oid} -> {oid_principal} (pack {pack_id})")
    return pedidos_agrupados


def _enriquecer_skus_cuenta(pedidos, cuenta_id):
    # 1. Enriquecer SKUs faltantes
    item_ids_sin_sku = {it["item_id"]: True
                        for ped in pedidos.values()
                        for it in ped["items"]
                        if not it["sku"] and it["item_id"]}
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
                    sku  = str(body.get("seller_sku") or body.get("seller_custom_field") or "").strip().upper()
                    if iid and sku:
                        sku_map[iid] = sku
        except Exception:
            pass
    for ped in pedidos.values():
        for it in ped["items"]:
            if not it["sku"] and it["item_id"] in sku_map:
                it["sku"] = sku_map[it["item_id"]]

    # 2. Enriquecer shipments en paralelo
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch_shipment(oid):
        ped = pedidos.get(oid)
        if not ped or not ped.get("shipping_id"):
            return oid, None
        for _ in range(2):
            try:
                rs = _ml_get_cuenta(f"/shipments/{ped['shipping_id']}", cuenta_id)
                if rs.status_code == 200:
                    return oid, rs.json()
            except Exception:
                pass
        return oid, None

    oids_con_shipping = [oid for oid, p in pedidos.items() if p.get("shipping_id")]
    if oids_con_shipping:
        # max_workers=4: suficiente para velocidad sin saturar Waitress ni ML
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(_fetch_shipment, oid): oid for oid in oids_con_shipping}
            for future in as_completed(futures):
                try:
                    oid, sd = future.result()
                    if sd and oid in pedidos:
                        ped       = pedidos[oid]
                        log_new   = (sd.get("logistic") or {}).get("type", "")
                        log_old   = sd.get("logistic_type", "")
                        logistica = log_new or log_old
                        status    = sd.get("status", "")
                        substatus = sd.get("substatus", "")
                        ped["logistica"] = logistica
                        ped["estado_envio"] = status
                        ped["substatus"] = substatus
                        ped["tipo"] = _calcular_tipo(logistica, ped.get("tags",[]), ped.get("shipping_id",""))
                        IMPRESOS = {"printed","shipped","delivered","not_delivered","cancelled"}
                        if substatus in IMPRESOS or status in ("shipped","delivered","not_delivered","cancelled"):
                            ped["impreso"] = True
                        elif substatus == "ready_to_print":
                            ped["impreso"] = False
                        elif status == "ready_to_ship" and not substatus:
                            ped["impreso"] = False
                except Exception:
                    pass

    for ped in pedidos.values():
        if not ped.get("tipo") or ped.get("tipo") == "desconocido":
            ped["tipo"] = _calcular_tipo(ped.get("logistica",""), ped.get("tags",[]), ped.get("shipping_id",""))
    return pedidos


def _ml_get_all_orders():
    return _ml_get_all_orders_cuenta("cuenta_0")

def _enriquecer_skus(pedidos):
    return _enriquecer_skus_cuenta(pedidos, "cuenta_0")

def _refresh_pedidos_worker_cuenta(cuenta_id, fecha_desde=None, fecha_hasta=None):
    global _ultimo_refresh_pedidos
    if not _token_valido_cuenta(cuenta_id):
        return
    try:
        pedidos = _ml_get_all_orders_cuenta(cuenta_id, fecha_desde, fecha_hasta)
        pedidos = _enriquecer_skus_cuenta(pedidos, cuenta_id)
        for p in pedidos.values():
            p["_cuenta"] = cuenta_id
        with _lock:
            to_del = [oid for oid, p in _pedidos_ml.items() if p.get("_cuenta") == cuenta_id]
            for oid in to_del:
                del _pedidos_ml[oid]
            for oid, p in pedidos.items():
                if oid in _pedidos_ml:
                    p["impreso"] = _pedidos_ml[oid].get("impreso", False)
            _pedidos_ml.update(pedidos)
            _ultimo_refresh_pedidos = datetime.now()
    except Exception as e:
        logger.error(f"[REFRESH] Error cuenta {cuenta_id}: {e}")

def _refresh_pedidos_worker():
    for cid in list(_cuentas.keys()):
        _refresh_pedidos_worker_cuenta(cid)

# ── Loops de background ────────────────────────────────────────────────────────

def _warmup_matutino():
    """Pre-calentamiento 6:45AM Uruguay."""
    while True:
        ahora = _hora_uruguay()
        if ahora.hour == 6 and ahora.minute >= 45:
            logger.info("[WARMUP] 6:45 AM Uruguay — pre-calentamiento...")
            try:
                threading.Thread(target=_refresh_pedidos_worker, daemon=True).start()
                time.sleep(60)
                with _lock:
                    pendientes = [p for p in _pedidos_ml.values()
                                  if p.get("shipping_id","") and not p.get("impreso", False)]
                for i in range(0, min(len(pendientes), 60), 15):
                    _refrescar_estado_pedidos_bg(pendientes[i:i+15], limite=15)
                    time.sleep(20)
                logger.info("[WARMUP] Listo para las 7:00 AM")
            except Exception as e:
                logger.error(f"[WARMUP] Error: {e}")
            time.sleep(23 * 3600)
        else:
            time.sleep(300)


_refresh_lock = threading.Semaphore(1)  # solo 1 refresh completo a la vez

def _auto_refresh_loop():
    """
    Refresca lista completa de pedidos ML cada 8 minutos.
    Usa semáforo para evitar dos refresh simultáneos que saturen Waitress.
    """
    while True:
        time.sleep(480)  # 8 minutos — antes era 2, demasiado frecuente
        if not _cuentas or _sync_pausado():
            continue
        if _refresh_lock.acquire(blocking=False):
            try:
                _refresh_pedidos_worker()
            finally:
                _refresh_lock.release()


def _cache_cleanup_loop():
    """Limpieza periodica del cache de etiquetas cada hora. Borra PDFs > 24h."""
    while True:
        time.sleep(3600)
        try:
            ahora    = datetime.now()
            borrados = 0
            kb_total = 0.0
            with _lock:
                for oid in list(_etiquetas_cache.keys()):
                    data = _etiquetas_cache[oid]
                    if not data.get("pdf"):
                        continue
                    try:
                        ts_str = data.get("ts","")
                        if ts_str:
                            ts    = datetime.strptime(ts_str, "%d/%m %H:%M:%S").replace(year=ahora.year)
                            horas = (ahora - ts).total_seconds() / 3600
                            if horas >= 24:
                                kb_total += round(len(data["pdf"]) / 1024, 1)
                                data["pdf"]      = None
                                data["expirado"] = True
                                borrados        += 1
                    except Exception:
                        pass
            if borrados:
                logger.info(f"[CACHE] Limpieza 24h: {borrados} PDFs eliminados ({kb_total:.1f} KB)")
        except Exception as e:
            logger.error(f"[CACHE] Error en limpieza: {e}")


def _refrescar_estado_pedidos_bg(pedidos_lista, limite=20):
    """
    Consulta shipment de cada pedido en ML y actualiza estado.
    Pausa 0.5s entre requests para no generar burst que sature Waitress.
    """
    SUBSTATUS_IMPRESOS = (
        "printed","ready_for_pickup","in_packing_list",
        "in_hub","shipped","delivered","ready_to_ship_wt_route","to_be_agreed",
    )
    procesados = marcados_impresos = 0
    for i, p in enumerate(pedidos_lista[:limite]):
        # Pausa entre requests: evita burst que llena la cola de Waitress
        if i > 0:
            time.sleep(0.5)
        try:
            ship_id = p.get("shipping_id","")
            cuenta  = p.get("_cuenta","cuenta_0")
            if not ship_id:
                continue
            rs = _ml_get_cuenta(f"/shipments/{ship_id}", cuenta)
            if not rs or rs.status_code != 200:
                continue
            sd        = rs.json()
            log_new   = (sd.get("logistic") or {}).get("type", "")
            log_old   = sd.get("logistic_type", "")
            logistica = log_new or log_old
            status    = sd.get("status", "")
            substatus = sd.get("substatus", "")
            with _lock:
                oid = str(p.get("order_id",""))
                pp  = _pedidos_ml.get(oid)
                if not pp:
                    continue
                pp["logistica"]    = logistica
                pp["estado_envio"] = status
                pp["substatus"]    = substatus
                pp["tipo"]         = _calcular_tipo(logistica, pp.get("tags",[]), ship_id)
                antes = pp.get("impreso", False)
                if substatus in SUBSTATUS_IMPRESOS or status in ("shipped","delivered","not_delivered","cancelled"):
                    pp["impreso"] = True
                elif substatus == "ready_to_print":
                    pp["impreso"] = False
                elif status == "ready_to_ship" and not substatus:
                    pp["impreso"] = False
                if not antes and pp.get("impreso"):
                    marcados_impresos += 1
                    logger.info(f"[PEDIDOS] #{oid} -> impreso (sub={substatus})")
            procesados += 1
        except Exception:
            pass
    if marcados_impresos:
        logger.info(f"[PEDIDOS] {marcados_impresos} nuevos impresos de {procesados} consultados")


def _sync_pedidos_ml_periodico():
    """
    Sincroniza estado de envíos con ML.
    - Ciclo: 120s (antes 90s)
    - Límite por ciclo: 20 pedidos (antes 60) — evita cola profunda en Waitress
    - Pausa de 1s entre cada request individual para no saturar
    - No corre si hay un refresh completo en curso (_refresh_lock)
    """
    time.sleep(45)
    ciclo = 0
    while True:
        try:
            if _sync_pausado():
                if ciclo % 30 == 0:
                    logger.debug(f"[SYNC] Pausado (horario/lote activo)")
            elif _refresh_lock.acquire(blocking=False):
                # Adquirir el semáforo para no correr junto con _auto_refresh_loop
                try:
                    with _lock:
                        pendientes = [p for p in _pedidos_ml.values()
                                      if p.get("shipping_id","") and not p.get("impreso", False)]
                    if pendientes:
                        if ciclo % 15 == 0:
                            logger.debug(f"[SYNC] Ciclo #{ciclo}: {len(pendientes)} pendientes")
                        # Límite 20 por ciclo, con pausa entre requests
                        _refrescar_estado_pedidos_bg(pendientes, limite=20)
                finally:
                    _refresh_lock.release()
        except Exception as e:
            logger.error(f"[SYNC] Error: {e}")
        ciclo += 1
        time.sleep(120)


threading.Thread(target=_auto_refresh_loop,  daemon=True).start()
threading.Thread(target=_cache_cleanup_loop, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════════════════
# DECORADORES
# ═══════════════════════════════════════════════════════════════════════════════

def requiere_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _cuentas:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "msg": "No autenticado con ML", "login": "/auth/login"}), 401
            return redirect("/auth/login")
        return f(*args, **kwargs)
    return decorated

def requiere_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        k = (request.headers.get("X-API-Key") or
             request.args.get("key") or request.args.get("api_key") or "").strip()
        # Si API_KEY no está configurada, bloquear todo
        if not API_KEY:
            return jsonify({"ok": False, "msg": "Servidor no configurado correctamente."}), 503
        if k != API_KEY:
            return jsonify({"ok": False, "msg": "Clave invalida."}), 401
        return f(*args, **kwargs)
    return decorated


# ═══════════════════════════════════════════════════════════════════════════════
# API USUARIOS — login centralizado en Railway
# ═══════════════════════════════════════════════════════════════════════════════

# ── API de usuarios ───────────────────────────────────────────────────────────

@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    """
    Endpoint de login para la app de escritorio.
    Body JSON: {"usuario": "everest", "clave": "xxxx"}
    Respuesta: {"ok": true, "usuario": {...}} o {"ok": false, "msg": "..."}
    NO requiere API key — es el punto de entrada antes de autenticarse.
    """
    data    = request.get_json(silent=True) or {}
    usuario = str(data.get("usuario", "")).strip()
    clave   = str(data.get("clave",   "")).strip()

    if not usuario or not clave:
        return jsonify({"ok": False, "msg": "Falta usuario o clave"}), 400

    resultado = _verificar_credenciales(usuario, clave)
    if resultado:
        logger.info(f"[AUTH] Login OK: {usuario} (rol={resultado['rol']})")
        return jsonify({"ok": True, "usuario": resultado})
    else:
        logger.warning(f"[AUTH] Login FALLIDO: {usuario}")
        return jsonify({"ok": False, "msg": "Usuario o clave incorrectos"}), 401


@app.route("/api/auth/usuarios", methods=["GET"])
@requiere_api_key
def api_usuarios_lista():
    """Lista todos los usuarios (sin mostrar claves). Solo supervisores."""
    return jsonify({
        "ok":      True,
        "usuarios": [
            {k: v for k, v in u.items() if k != "clave"}
            for u in _usuarios
        ]
    })


@app.route("/api/auth/usuarios", methods=["POST"])
@requiere_api_key
def api_usuarios_crear():
    """
    Crea un nuevo usuario.
    Body: {"usuario","clave","nombre","cuenta_id","rol"}
    """
    data    = request.get_json(silent=True) or {}
    usuario = str(data.get("usuario","")).strip().lower()
    clave   = str(data.get("clave","")).strip()
    nombre  = str(data.get("nombre", usuario)).strip()
    cuenta  = str(data.get("cuenta_id","todas")).strip()
    rol     = str(data.get("rol","operario")).strip()

    if not usuario or not clave:
        return jsonify({"ok": False, "msg": "Falta usuario o clave"}), 400
    if any(u.get("usuario","").lower() == usuario for u in _usuarios):
        return jsonify({"ok": False, "msg": f"El usuario '{usuario}' ya existe"}), 409
    if rol not in ("operario", "supervisor"):
        return jsonify({"ok": False, "msg": "Rol debe ser 'operario' o 'supervisor'"}), 400

    nuevo = {"usuario": usuario, "clave": clave,
             "nombre": nombre, "cuenta_id": cuenta, "rol": rol}
    _usuarios.append(nuevo)
    _guardar_usuarios()
    logger.info(f"[USUARIOS] Creado: {usuario} (cuenta={cuenta}, rol={rol})")
    return jsonify({"ok": True, "msg": f"Usuario '{usuario}' creado", "usuario": {
        k: v for k, v in nuevo.items() if k != "clave"
    }})


@app.route("/api/auth/usuarios/<usuario_id>", methods=["PUT"])
@requiere_api_key
def api_usuarios_editar(usuario_id):
    """Edita un usuario existente (clave, nombre, cuenta_id, rol)."""
    u = next((x for x in _usuarios if x.get("usuario","").lower() == usuario_id.lower()), None)
    if not u:
        return jsonify({"ok": False, "msg": "Usuario no encontrado"}), 404
    data = request.get_json(silent=True) or {}
    if data.get("clave"):
        u["clave"]     = data["clave"].strip()
    if data.get("nombre"):
        u["nombre"]    = data["nombre"].strip()
    if data.get("cuenta_id"):
        u["cuenta_id"] = data["cuenta_id"].strip()
    if data.get("rol") in ("operario","supervisor"):
        u["rol"]       = data["rol"]
    _guardar_usuarios()
    logger.info(f"[USUARIOS] Editado: {usuario_id}")
    return jsonify({"ok": True, "msg": f"Usuario '{usuario_id}' actualizado"})


@app.route("/api/auth/usuarios/<usuario_id>", methods=["DELETE"])
@requiere_api_key
def api_usuarios_eliminar(usuario_id):
    """Elimina un usuario. No se puede eliminar el último supervisor."""
    global _usuarios
    u = next((x for x in _usuarios if x.get("usuario","").lower() == usuario_id.lower()), None)
    if not u:
        return jsonify({"ok": False, "msg": "Usuario no encontrado"}), 404
    supervisores = [x for x in _usuarios if x.get("rol") == "supervisor"]
    if u.get("rol") == "supervisor" and len(supervisores) <= 1:
        return jsonify({"ok": False, "msg": "No podés eliminar el único supervisor"}), 400
    _usuarios = [x for x in _usuarios if x.get("usuario","").lower() != usuario_id.lower()]
    _guardar_usuarios()
    logger.info(f"[USUARIOS] Eliminado: {usuario_id}")
    return jsonify({"ok": True, "msg": f"Usuario '{usuario_id}' eliminado"})


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

def _generar_pkce():
    import hashlib, base64, secrets
    verifier  = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@app.route("/auth/login")
def auth_login():
    import secrets
    cuenta_id           = request.args.get("cuenta", "cuenta_0")
    state               = secrets.token_urlsafe(16)
    verifier, challenge = _generar_pkce()
    _pkce_store[state]  = {"verifier": verifier, "cuenta_id": cuenta_id}
    url = (f"{ML_AUTH_URL}/authorization?response_type=code"
           f"&client_id={ML_APP_ID}&redirect_uri={ML_REDIRECT}"
           f"&scope=read%20write%20offline_access&state={state}"
           f"&code_challenge={challenge}&code_challenge_method=S256")
    return redirect(url)


@app.route("/auth/callback")
def auth_callback():
    code  = request.args.get("code")
    error = request.args.get("error")
    state = request.args.get("state", "")
    if error or not code:
        return _html_error(f"Error de autorizacion: {error or 'sin codigo'}",
                           f"Redirect URI: <code>{ML_REDIRECT}</code>")
    entry     = _pkce_store.pop(state, None) or {}
    verifier  = entry.get("verifier") if isinstance(entry, dict) else entry
    cuenta_id = entry.get("cuenta_id", "cuenta_0") if isinstance(entry, dict) else "cuenta_0"
    if not entry:
        logger.warning(f"[AUTH] State '{state}' no encontrado — posible reinicio")
        verifier = None; cuenta_id = "cuenta_0"
    payload = {"grant_type": "authorization_code", "client_id": ML_APP_ID,
               "client_secret": ML_SECRET_KEY, "code": code, "redirect_uri": ML_REDIRECT}
    if verifier:
        payload["code_verifier"] = verifier
    try:
        r = requests.post("https://api.mercadolibre.com/oauth/token",
                          headers={"Accept": "application/json",
                                   "Content-Type": "application/x-www-form-urlencoded"},
                          data=payload, timeout=15)
        if r.status_code != 200:
            try:
                err_detail = json.dumps(r.json(), indent=2, ensure_ascii=False)
            except Exception:
                err_detail = r.text
            return _html_error(f"Error obteniendo token ({r.status_code})",
                               f"<pre>{err_detail}</pre>")
        d   = r.json()
        tok = {"access_token": d["access_token"], "refresh_token": d.get("refresh_token",""),
               "expires_at": datetime.now() + timedelta(seconds=d.get("expires_in", 21600))}
        me_r = requests.get(ML_API_URL + "/users/me",
                            headers={"Authorization": f"Bearer {tok['access_token']}"},
                            timeout=8)
        if me_r.status_code == 200:
            me = me_r.json()
            tok["user_id"]  = str(me.get("id",""))
            tok["nickname"] = me.get("nickname", cuenta_id)
        else:
            tok["user_id"] = ""; tok["nickname"] = cuenta_id
        _cuentas[cuenta_id] = tok
        if cuenta_id == "cuenta_0":
            _tokens.update(tok)
        _guardar_tokens()
        logger.info(f"[TOKEN] Conectado: {tok.get('nickname','?')} -> {cuenta_id}")
        threading.Thread(target=_refresh_pedidos_worker_cuenta, args=(cuenta_id,), daemon=True).start()
        return _html_ok(f"Conectado como <b>{tok['nickname']}</b>",
                        f"Cuenta registrada como <b>{cuenta_id}</b>.")
    except Exception as e:
        return _html_error("Error inesperado", str(e))


@app.route("/auth/logout")
def auth_logout():
    cuenta_id = request.args.get("cuenta", "cuenta_0")
    if cuenta_id in _cuentas:
        del _cuentas[cuenta_id]
        for oid in [o for o, p in list(_pedidos_ml.items()) if p.get("_cuenta") == cuenta_id]:
            _pedidos_ml.pop(oid, None)
    if cuenta_id == "cuenta_0":
        _tokens.clear()
    return redirect("/")


@app.route("/auth/status")
def auth_status():
    return jsonify({
        "autenticado": bool(_cuentas),
        "nickname":    _cuentas.get("cuenta_0",{}).get("nickname",""),
        "user_id":     _cuentas.get("cuenta_0",{}).get("user_id",""),
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
    with _lock:
        if cuenta_id in _cuentas:
            del _cuentas[cuenta_id]
            for oid in [o for o, p in list(_pedidos_ml.items()) if p.get("_cuenta") == cuenta_id]:
                _pedidos_ml.pop(oid, None)
    _guardar_tokens()
    return jsonify({"ok": True, "msg": f"Cuenta {cuenta_id} desvinculada"})

# ═══════════════════════════════════════════════════════════════════════════════
# API PEDIDOS ML
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/pedidos")
def api_pedidos():
    if not _cuentas:
        return jsonify({"ok": False, "msg": "login", "pedidos": []}), 200
    with _lock:
        pedidos = list(_pedidos_ml.values())
    for p in pedidos:
        log = p.get("logistica","")
        if log and (not p.get("tipo") or p.get("tipo") == "desconocido"):
            p["tipo"] = _calcular_tipo(log, p.get("tags",[]), p.get("shipping_id",""))
    # NO lanzar refresh por cada llamada a /api/pedidos —
    # el sync periódico (_sync_pedidos_ml_periodico) ya lo maneja cada 120s.
    # Lanzarlo aquí multiplicaba la carga en Waitress con cada poll del cliente.
    return jsonify({
        "ok": True, "pedidos": pedidos, "total": len(pedidos),
        "sync_pausado": _sync_pausado(), "sync_estados": dict(_sync_estado_canal),
        "ts": _ultimo_refresh_pedidos.strftime("%d/%m %H:%M:%S") if _ultimo_refresh_pedidos else "-",
    })


@app.route("/api/pedidos/refresh", methods=["POST"])
def api_refresh():
    if not _cuentas:
        return jsonify({"ok": False, "msg": "No hay cuentas ML conectadas"}), 400
    body    = request.get_json(silent=True) or {}
    f_desde = body.get("fecha_desde")
    f_hasta = body.get("fecha_hasta")
    for cid in list(_cuentas.keys()):
        threading.Thread(target=_refresh_pedidos_worker_cuenta,
                         args=(cid, f_desde, f_hasta), daemon=True).start()
    return jsonify({"ok": True, "msg": f"Actualizando ({f_desde or 'ultimos 7 dias'} a {f_hasta or 'hoy'})"})


@app.route("/api/diag-colecta")
def api_diag_colecta():
    with _lock:
        pedidos = list(_pedidos_ml.values())
    LOGS_COLECTA = ("cross_docking","xd_drop_off","xd_same_day","drop_off")
    SUBS_IMPRESOS = {"printed","ready_for_pickup","in_packing_list","in_hub","shipped","delivered","ready_to_ship_wt_route"}
    colecta_pendientes = []; colecta_impresos = []; sin_logistica = []
    for p in pedidos:
        log      = (p.get("logistica","") or "").lower()
        substatus = (p.get("substatus","") or "").lower()
        status   = (p.get("estado_envio","") or "").lower()
        impreso  = p.get("impreso", False)
        info = {"order_id": str(p.get("order_id","")), "logistica": log or "(vacio)",
                "estado": status, "substatus": substatus, "impreso_flag": impreso,
                "shipping_id": p.get("shipping_id","")}
        if not log:
            sin_logistica.append(info); continue
        if log not in LOGS_COLECTA:
            continue
        if impreso or substatus in SUBS_IMPRESOS or status in ("shipped","delivered","not_delivered","cancelled"):
            colecta_impresos.append(info)
        else:
            colecta_pendientes.append(info)
    return jsonify({"total_pedidos": len(pedidos), "sin_logistica": len(sin_logistica),
                    "colecta_pendientes": len(colecta_pendientes), "colecta_impresos": len(colecta_impresos),
                    "detalle_pendientes": colecta_pendientes, "detalle_impresos": colecta_impresos[:20],
                    "detalle_sin_logistica": sin_logistica[:20]})


@app.route("/api/diag-sku/<sku>")
def api_diag_sku(sku):
    sku = sku.strip().upper()
    result = {"sku_consultado": sku, "en_lote": {}, "en_pedidos_ml": [], "pedidos_sin_sku": []}
    for canal, est in _estados_canal.items():
        encontrado = None
        for g in est.get("grupos",[]):
            for it in g.get("items",[]):
                if str(it.get("sku","")).upper() == sku:
                    encontrado = {"pasillo": it.get("pasillo",""), "vertical": it.get("vertical",""),
                                  "nombre": it.get("nombre",""), "req": it.get("req",0)}; break
            if encontrado: break
        result["en_lote"][canal] = encontrado or "no encontrado"
    with _lock:
        pedidos_snap = list(_pedidos_ml.values())
    for ped in pedidos_snap[:300]:
        for it in ped.get("items",[]):
            if str(it.get("sku","")).upper() == sku:
                result["en_pedidos_ml"].append({"order_id": str(ped.get("order_id","")),
                    "sku_en_ml": it.get("sku","(VACIO)"), "titulo": it.get("titulo","")[:60],
                    "cantidad": it.get("cantidad",1)})
            if not it.get("sku","") and it.get("item_id",""):
                result["pedidos_sin_sku"].append({"order_id": str(ped.get("order_id","")),
                    "item_id": it.get("item_id",""), "titulo": it.get("titulo","")[:60]})
        if len(result["en_pedidos_ml"]) >= 5: break
    result["pedidos_sin_sku"] = result["pedidos_sin_sku"][:10]
    en_lote_ok = any(v != "no encontrado" for v in result["en_lote"].values())
    result["diagnostico"] = ("SKU no encontrado en ningun lote activo." if not en_lote_ok
                              else "OK SKU encontrado en el lote")
    return jsonify(result)


@app.route("/api/debug_logistica")
def debug_logistica():
    with _lock:
        snap = dict(_pedidos_ml)
    conteo_tipo = {}; conteo_log = {}; ejemplos = {}
    for oid, p in list(snap.items())[:100]:
        log  = p.get("logistica","") or "(vacio)"
        tipo = p.get("tipo","") or "(sin_tipo)"
        conteo_log[log]   = conteo_log.get(log, 0) + 1
        conteo_tipo[tipo] = conteo_tipo.get(tipo, 0) + 1
        if log not in ejemplos:
            ejemplos[log] = {"order_id": oid, "logistica": log, "tipo": tipo,
                              "tipo_calc": _calcular_tipo(log, p.get("tags",[]), p.get("shipping_id",""))}
    return jsonify({"total_pedidos": len(snap), "por_tipo": conteo_tipo,
                    "por_logistica": conteo_log, "ejemplos": ejemplos})


@app.route("/api/test_credentials")
def test_credentials():
    try:
        r = requests.post("https://api.mercadolibre.com/oauth/token",
                          headers={"Accept": "application/json",
                                   "Content-Type": "application/x-www-form-urlencoded"},
                          data={"grant_type": "client_credentials",
                                "client_id": ML_APP_ID, "client_secret": ML_SECRET_KEY},
                          timeout=10)
        return jsonify({"status": r.status_code, "app_id_usado": ML_APP_ID,
                        "credenciales_ok": r.status_code == 200,
                        "respuesta": r.json() if "json" in r.headers.get("content-type","") else r.text})
    except Exception as e:
        return jsonify({"error": str(e)})

# ═══════════════════════════════════════════════════════════════════════════════
# ETIQUETA PERSONALIZADA
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/test-libs")
def api_test_libs():
    resultado = {}
    for lib in ["reportlab","PIL","pypdf","PyPDF2"]:
        try:
            __import__(lib); resultado[lib] = "instalada"
        except ImportError:
            resultado[lib] = "NO instalada"
    return jsonify(resultado)


def _aplicar_personalizacion_etiqueta(pdf_bytes, config):
    tiene_logo  = bool(config.get("etiqueta_logo_b64"))
    tiene_texto = bool((config.get("etiqueta_texto") or "").strip())
    if not tiene_logo and not tiene_texto:
        return pdf_bytes
    try:
        import io, base64
        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError:
            try:
                import PyPDF2; PdfReader = PyPDF2.PdfReader; PdfWriter = PyPDF2.PdfWriter
            except ImportError:
                logger.error("[ETIQUETA-PERS] pypdf/PyPDF2 no disponibles")
                return pdf_bytes
        from reportlab.pdfgen import canvas as rl_canvas
        from reportlab.lib.utils import ImageReader
        from reportlab.lib import colors as rl_colors
        from PIL import Image

        reader = PdfReader(io.BytesIO(pdf_bytes))
        if not reader.pages:
            return pdf_bytes
        pw = float(reader.pages[0].mediabox.width)
        ph = float(reader.pages[0].mediabox.height)

        FRANJA_LOGO  = 50 if tiene_logo  else 0
        FRANJA_TEXTO = 16 if tiene_texto else 0
        escala = min((pw - FRANJA_TEXTO) / pw, (ph - FRANJA_LOGO) / ph)

        bg_buf = io.BytesIO()
        c = rl_canvas.Canvas(bg_buf, pagesize=(pw, ph))
        c.setFillColor(rl_colors.white); c.setStrokeColor(rl_colors.white)
        c.rect(0, 0, pw, ph, fill=1, stroke=0); c.save(); bg_buf.seek(0)

        bg_reader = PdfReader(bg_buf)
        bg_page   = bg_reader.pages[0]
        orig_page = reader.pages[0]
        try:
            from pypdf import Transformation
            bg_page.merge_transformed_page(orig_page,
                Transformation().scale(sx=escala, sy=escala).translate(tx=0, ty=0))
        except (ImportError, AttributeError):
            bg_page.merge_page(orig_page)

        ov_buf = io.BytesIO()
        c2 = rl_canvas.Canvas(ov_buf, pagesize=(pw, ph))

        if tiene_logo:
            try:
                logo_bytes = base64.b64decode(config["etiqueta_logo_b64"])
                img  = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
                pct  = max(5, min(40, int(config.get("etiqueta_logo_size", 20))))
                w_pt = (pw * pct) / 100
                h_pt = w_pt * (img.height / img.width)
                if h_pt > ph * 0.25:
                    h_pt = ph * 0.25; w_pt = h_pt * (img.width / img.height)
                img_r = img.resize((max(1,int(w_pt)), max(1,int(h_pt))), Image.Resampling.LANCZOS)
                MARGEN = 6
                pos = config.get("etiqueta_logo_pos","superior_der")
                x = MARGEN if pos == "superior_izq" else pw - w_pt - MARGEN
                y = ph - h_pt - MARGEN
                tmp = io.BytesIO(); img_r.save(tmp, format="PNG"); tmp.seek(0)
                c2.drawImage(ImageReader(tmp), x, y, width=w_pt, height=h_pt, mask="auto")
            except Exception as e:
                logger.error(f"[ETIQUETA-PERS] Error logo: {e}")

        if tiene_texto:
            try:
                texto = config["etiqueta_texto"].strip()
                c2.saveState(); c2.setFont("Helvetica-Bold", 7); c2.setFillColor(rl_colors.black)
                c2.translate(pw - FRANJA_TEXTO/2, (ph - FRANJA_LOGO)/2)
                c2.rotate(90); c2.drawCentredString(0, 0, texto[:110]); c2.restoreState()
            except Exception as e:
                logger.error(f"[ETIQUETA-PERS] Error texto: {e}")

        c2.save(); ov_buf.seek(0)
        bg_page.merge_page(PdfReader(ov_buf).pages[0])
        writer = PdfWriter(); writer.add_page(bg_page)
        out = io.BytesIO(); writer.write(out)
        return out.getvalue()
    except Exception as e:
        logger.error(f"[ETIQUETA-PERS] Error general: {e}")
        return pdf_bytes


# ── Etiqueta endpoint ──────────────────────────────────────────────────────────

@app.route("/api/diag-etiqueta/<order_id>")
def api_diag_etiqueta(order_id):
    order_id = str(order_id).strip()
    diag = {"order_id": order_id, "pasos": []}
    def paso(nombre, ok, detalle=""):
        diag["pasos"].append({"paso": nombre, "ok": ok, "detalle": str(detalle)[:400]})
    with _lock:
        snap      = dict(_pedidos_ml)
        snap_lote = dict(_estado.get("pedidos",{}))
    pedido = snap.get(order_id); origen = "pedidos_ml" if pedido else None
    if not pedido:
        for p_num, p_data in snap_lote.items():
            if str(p_data.get("_order_id","")) == order_id:
                pedido = {"shipping_id": str(p_data.get("_shipping_id","")),
                          "_cuenta": p_data.get("_cuenta","cuenta_0")}
                origen = f"lote (p={p_num})"; break
    paso("pedido_encontrado", bool(pedido), f"origen={origen}")
    if not pedido:
        diag["conclusion"] = "El pedido no esta en memoria."; return jsonify(diag), 200
    shipping_id = pedido.get("shipping_id","")
    paso("tiene_shipping_id", bool(shipping_id), f"shipping_id={shipping_id or '(vacio)'}")
    if not shipping_id:
        diag["conclusion"] = "Sin shipping_id."; return jsonify(diag), 200
    cuenta_id = pedido.get("_cuenta","cuenta_0")
    try: _token_valido_cuenta(cuenta_id)
    except Exception: pass
    at = _cuentas.get(cuenta_id,{}).get("access_token","")
    paso("token_disponible", bool(at), f"cuenta={cuenta_id}")
    if not at:
        diag["conclusion"] = "No hay token. Reconecta ML."; return jsonify(diag), 200
    estado = substatus = logistic = ""
    try:
        ri = requests.get(f"{ML_API_URL}/shipments/{shipping_id}",
                          headers={"Authorization": f"Bearer {at}"}, timeout=10)
        if ri.status_code == 200:
            sd = ri.json(); estado = sd.get("status",""); substatus = sd.get("substatus","")
            logistic = sd.get("logistic_type","") or (sd.get("logistic") or {}).get("type","")
            paso("estado_envio", True, f"status={estado}, substatus={substatus}, logistic={logistic}")
        else:
            paso("estado_envio", False, f"HTTP {ri.status_code}")
    except Exception as e:
        paso("estado_envio", False, f"excepcion: {e}")
    label_ok = False
    for rtype in ["pdf","pdf2"]:
        try:
            r = requests.get(f"{ML_API_URL}/shipment_labels",
                             headers={"Authorization": f"Bearer {at}"},
                             params={"shipment_ids": shipping_id, "response_type": rtype},
                             timeout=15)
            es_pdf = r.content[:4] == b"%PDF"
            paso(f"shipment_labels_{rtype}", r.status_code == 200 and es_pdf,
                 f"HTTP {r.status_code}, es_pdf={es_pdf}, bytes={len(r.content)}")
            if r.status_code == 200 and es_pdf:
                label_ok = True; break
        except Exception as e:
            paso(f"shipment_labels_{rtype}", False, f"excepcion: {e}")
    diag["conclusion"] = ("OK La etiqueta SI se puede descargar." if label_ok else
                          f"Error en estado '{estado}' / '{substatus}'.")
    return jsonify(diag), 200


@app.route("/api/etiqueta/<order_id>", methods=["GET","POST"])
def api_etiqueta(order_id):
    try:
        return _api_etiqueta_impl(order_id)
    except Exception as e:
        logger.error(f"[ETIQUETA] Error inesperado para {order_id}: {e}")
        return _html_error("Error interno al obtener etiqueta",
                           f"Pedido #{order_id}<br>Error: {str(e)[:200]}"), 200


def _api_etiqueta_impl(order_id):
    with _lock:
        cached    = _etiquetas_cache.get(order_id)
        snap      = dict(_pedidos_ml)
        snap_lote = {}
        for canal_name, canal_est in _estados_canal.items():
            for k, v in canal_est.get("pedidos",{}).items():
                snap_lote[k] = v

    if cached and cached.get("pdf"):
        shid = cached.get("shipping_id","label")
        return Response(cached["pdf"], status=200, mimetype="application/pdf",
                        headers={"Content-Disposition": f"inline; filename=etiqueta_{shid}.pdf",
                                 "X-Cache": "HIT"})

    pedido   = snap.get(order_id) or snap.get(str(order_id))
    real_oid = order_id
    if not pedido and len(order_id) >= 8:
        for k, v in snap.items():
            if str(k).endswith(order_id[-8:]) or str(k)[:8] == order_id[:8]:
                pedido = v; real_oid = str(k); break
    if not pedido:
        for p_num, p_data in snap_lote.items():
            if str(p_data.get("_order_id","")) == order_id:
                pedido = {"shipping_id": str(p_data.get("_shipping_id","")),
                          "comprador": p_data.get("comprador",""),
                          "_cuenta": p_data.get("_cuenta","cuenta_0"),
                          "items": p_data.get("items",[])}; real_oid = order_id; break
    if not pedido:
        return _html_error("Pedido no encontrado",
                           f"El pedido #{order_id} no esta en la lista. "
                           f"Hay {len(snap)} pedidos ML y {len(snap_lote)} en el lote."), 404

    shipping_id = pedido.get("shipping_id","")
    comprador   = pedido.get("comprador","")
    items_txt   = " | ".join(f"{it.get('sku','?')}" for it in pedido.get("items",[])[:3])
    if not shipping_id:
        return _html_error(f"Pedido #{real_oid} sin envio ML",
                           f"<b>Comprador:</b> {comprador}<br><b>Productos:</b> {items_txt}"), 200

    at = _cuentas.get(pedido.get("_cuenta","cuenta_0"),{}).get("access_token","")
    if not at:
        return _html_error("Token ML no disponible", "Reconecta la cuenta MercadoLibre."), 401

    pdf_content = None; last_status = 0
    for rtype in ["pdf","pdf2"]:
        try:
            r = requests.get(f"{ML_API_URL}/shipment_labels",
                             headers={"Authorization": f"Bearer {at}"},
                             params={"shipment_ids": shipping_id, "response_type": rtype},
                             timeout=15)
            last_status = r.status_code
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                pdf_content = r.content; break
        except Exception as e:
            logger.debug(f"[ETIQUETA] Excepcion {rtype}: {e}")

    if pdf_content:
        config = {}
        try:
            if request.method == "POST" and request.content_type == "application/json":
                config = request.get_json(silent=True) or {}
            else:
                config_str = request.args.get("config","{}")
                config = json.loads(config_str) if config_str else {}
        except Exception:
            config = {}
        pdf_final = _aplicar_personalizacion_etiqueta(pdf_content, config)
        return Response(pdf_final, status=200, mimetype="application/pdf",
                        headers={"Content-Disposition": f"inline; filename=etiqueta_{shipping_id}.pdf"})

    estado = substatus = logistic = ""
    try:
        ri = requests.get(f"{ML_API_URL}/shipments/{shipping_id}",
                          headers={"Authorization": f"Bearer {at}"}, timeout=8)
        if ri.status_code == 200:
            sd = ri.json(); estado = sd.get("status",""); substatus = sd.get("substatus","")
            logistic = sd.get("logistic_type","") or (sd.get("logistic") or {}).get("type","")
    except Exception:
        pass
    MSGS = {"delivered": ("Pedido ya entregado","La etiqueta ya no esta disponible."),
            "shipped":   ("Pedido en camino","El pedido esta en camino."),
            "cancelled": ("Cancelado","El envio fue cancelado."),
            "ready_to_ship": ("Listo para enviar","Intenta de nuevo en unos segundos."),
            "handling":  ("En preparacion","La etiqueta estara disponible pronto."),
            "pending":   ("Pendiente","El envio esta pendiente de procesamiento.")}
    tit, desc = MSGS.get(estado, ("Sin etiqueta disponible",
                                   f"Estado: {estado or 'desconocido'} / {substatus}"))
    btn_alt = ""
    if estado == "ready_to_ship" or not estado:
        btn_alt = (f'<br><br><a href="/api/etiqueta/{real_oid}/descargar" target="_blank" '
                   f'style="display:inline-block;background:#3B82F6;color:#fff;padding:12px 24px;'
                   f'border-radius:8px;text-decoration:none;font-weight:bold">'
                   f'Descargar etiqueta (metodo alternativo)</a>')
    return _html_error(tit,
        f"<b>Pedido:</b> #{real_oid} — {comprador}<br><b>Productos:</b> {items_txt}<br>"
        f"<b>Shipping:</b> {shipping_id}<br><b>Estado ML:</b> {estado}/{substatus or '-'}<br>"
        f"<b>HTTP:</b> {last_status}<br><br>{desc}{btn_alt}"), 200


@app.route("/api/etiqueta/<order_id>/descargar")
def api_etiqueta_descargar(order_id):
    try:
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
            return _html_error("Sin envio ML","Este pedido no tiene envio asignado."), 400
        at = _cuentas.get(pedido.get("_cuenta","cuenta_0"),{}).get("access_token","")
        if not at:
            return _html_error("Token no disponible","Reconecta la cuenta ML."), 401
        pdf_content = None; last_status = 0
        for rtype in ["pdf2","pdf"]:
            try:
                r = requests.get(f"{ML_API_URL}/shipment_labels",
                                 headers={"Authorization": f"Bearer {at}"},
                                 params={"shipment_ids": shipping_id, "response_type": rtype},
                                 timeout=15)
                last_status = r.status_code
                if r.status_code == 200 and len(r.content) > 200:
                    pdf_content = r.content; break
            except Exception:
                continue
        if pdf_content:
            return Response(pdf_content, status=200, mimetype="application/pdf",
                            headers={"Content-Disposition": f"attachment; filename=etiqueta_{shipping_id}.pdf"})
        return _html_error("Etiqueta no disponible",
                           f"No se pudo obtener la etiqueta #{order_id}. HTTP: {last_status}"), 200
    except Exception as e:
        return _html_error("Error al descargar", str(e)[:200]), 200

# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOK MERCADOLIBRE
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/webhook/ml", methods=["POST","GET"])
def webhook_ml():
    if request.method == "GET":
        return jsonify({"ok": True, "msg": "Webhook ML activo"}), 200
    try:
        data    = request.get_json(silent=True) or {}
        topic   = data.get("topic", data.get("type",""))
        res_id  = data.get("resource","")
        user_id = str(data.get("user_id",""))
        logger.info(f"[WEBHOOK] topic={topic} resource={res_id} user={user_id}")
        if topic in ("orders_v2","orders"):
            order_id = res_id.strip("/").split("/")[-1]
            threading.Thread(target=_procesar_notificacion_orden,
                             args=(order_id, user_id), daemon=True).start()
        elif topic == "shipments":
            shipment_id = res_id.strip("/").split("/")[-1]
            threading.Thread(target=_procesar_notificacion_shipment,
                             args=(shipment_id, user_id), daemon=True).start()
        return jsonify({"ok": True}), 200
    except Exception as e:
        logger.error(f"[WEBHOOK] Error: {e}")
        return jsonify({"ok": True}), 200


def _procesar_notificacion_orden(order_id, user_id):
    import time as _time
    _time.sleep(2)
    cuenta_id = next((cid for cid, tok in _cuentas.items()
                      if str(tok.get("user_id","")) == str(user_id)), None)
    if not cuenta_id and _cuentas:
        cuenta_id = list(_cuentas.keys())[0]
    if not cuenta_id:
        return
    at = _cuentas.get(cuenta_id,{}).get("access_token","")
    if not at:
        return
    try:
        r = requests.get(f"{ML_API_URL}/orders/{order_id}",
                         headers={"Authorization": f"Bearer {at}"}, timeout=10)
        if r.status_code != 200:
            return
        order = r.json()
        if order.get("status") not in ("paid",):
            return
        items = []
        for it in order.get("order_items",[]):
            item_data = it.get("item",{})
            var_attrs = item_data.get("variation_attributes",[])
            sku = ""
            for attr in var_attrs:
                if attr.get("name","").lower() in ("sku","seller_sku"):
                    sku = str(attr.get("value_name","")).strip(); break
            if not sku:
                sku = str(item_data.get("seller_sku") or item_data.get("seller_custom_field") or "").strip()
            items.append({"item_id": item_data.get("id",""), "titulo": item_data.get("title",""),
                           "sku": sku.upper() if sku else "",
                           "cantidad": it.get("quantity",1), "unit_price": it.get("unit_price",0)})
        ship    = order.get("shipping") or {}
        ship_id = str(ship.get("id","")) if ship.get("id") else ""
        nick    = _cuentas.get(cuenta_id,{}).get("nickname", cuenta_id)
        pedido  = {
            "order_id": str(order_id), "pack_id": str(order.get("pack_id","")) if order.get("pack_id") else "",
            "fecha": order.get("date_created","")[:10], "fecha_cierre": order.get("date_closed","")[:10],
            "comprador": (order.get("buyer") or {}).get("nickname",""),
            "total": order.get("total_amount",0), "moneda": order.get("currency_id","UYU"),
            "items": items, "shipping_id": ship_id, "logistica": "", "estado_envio": "",
            "impreso": False, "tags": order.get("tags",[]),
            "_cuenta": cuenta_id, "_nickname": nick, "_via_webhook": True,
        }
        with _lock:
            _pedidos_ml[str(order_id)] = pedido
        logger.info(f"[WEBHOOK] Orden {order_id} agregada ({nick}) — {len(items)} items")
        if ship_id:
            def _fetch_ship_now():
                try:
                    rs = _ml_get_cuenta(f"/shipments/{ship_id}", cuenta_id)
                    if rs and rs.status_code == 200:
                        sd        = rs.json()
                        logistica = (sd.get("logistic") or {}).get("type","") or sd.get("logistic_type","")
                        status    = sd.get("status","")
                        substatus = sd.get("substatus","")
                        with _lock:
                            p = _pedidos_ml.get(str(order_id))
                            if p:
                                p["logistica"] = logistica; p["estado_envio"] = status
                                p["substatus"] = substatus
                                p["tipo"] = _calcular_tipo(logistica, p.get("tags",[]), ship_id)
                except Exception as e:
                    logger.debug(f"[WEBHOOK] No se pudo consultar shipment {ship_id}: {e}")
            threading.Thread(target=_fetch_ship_now, daemon=True).start()
    except Exception as e:
        logger.error(f"[WEBHOOK] Error procesando orden {order_id}: {e}")


def _procesar_notificacion_shipment(shipment_id, user_id):
    cuenta_id = next((cid for cid, tok in _cuentas.items()
                      if str(tok.get("user_id","")) == str(user_id)), None)
    if not cuenta_id and _cuentas:
        cuenta_id = list(_cuentas.keys())[0]
    if not cuenta_id:
        return
    at = _cuentas.get(cuenta_id,{}).get("access_token","")
    if not at:
        return
    try:
        r = requests.get(f"{ML_API_URL}/shipments/{shipment_id}",
                         headers={"Authorization": f"Bearer {at}"}, timeout=10)
        if r.status_code != 200:
            return
        sd       = r.json()
        estado   = sd.get("status","")
        order_id = str(sd.get("order_id",""))
        logistic = sd.get("logistic_type","") or (sd.get("logistic") or {}).get("type","")
        logger.info(f"[WEBHOOK] Shipment {shipment_id} estado={estado} order={order_id}")
        if order_id and order_id in _pedidos_ml:
            with _lock:
                _pedidos_ml[order_id]["estado_envio"] = estado
                _pedidos_ml[order_id]["logistica"]    = logistic
                _pedidos_ml[order_id]["shipping_id"]  = str(shipment_id)
    except Exception as e:
        logger.error(f"[WEBHOOK] Error procesando shipment {shipment_id}: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# API CACHE, TOKENS, PICKING
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/etiquetas_cache")
def api_etiquetas_cache():
    with _lock:
        resultado = []; total_kb = 0.0; pdfs_activos = 0
        for oid, data in _etiquetas_cache.items():
            ped = _pedidos_ml.get(oid,{})
            kb  = round(len(data.get("pdf") or b"") / 1024, 1)
            total_kb += kb
            tiene_pdf = data.get("pdf") is not None
            if tiene_pdf: pdfs_activos += 1
            resultado.append({"order_id": oid, "comprador": ped.get("comprador",""),
                               "shipping_id": data.get("shipping_id",""),
                               "tiene_pdf": tiene_pdf, "tamanio_kb": kb,
                               "guardado_en": data.get("ts","")})
    return jsonify({"ok": True, "total_registros": len(_etiquetas_cache),
                    "pdfs_en_memoria": pdfs_activos, "en_cola_descarga": len(_cola_etiquetas),
                    "memoria_total_kb": round(total_kb,1), "etiquetas": resultado})


@app.route("/api/pedidos/impreso/<order_id>", methods=["GET"])
def chequear_impreso(order_id):
    order_id = str(order_id).strip()
    impreso = False; fuente = None; ts = None
    with _lock:
        for k in (order_id, str(order_id)):
            p = _pedidos_ml.get(k)
            if p and p.get("impreso"):
                impreso = True; fuente = "pedidos_ml"; ts = p.get("impreso_ts",""); break
        if not impreso:
            impresas = _estado.get("etiquetas_impresas",[])
            if order_id in impresas or str(order_id) in impresas:
                impreso = True; fuente = "etiquetas_impresas"
        if not impreso:
            cache = _etiquetas_cache.get(order_id) or _etiquetas_cache.get(str(order_id))
            if cache and cache.get("impreso"):
                impreso = True; fuente = "etiquetas_cache"; ts = cache.get("impreso_ts","")
    return jsonify({"ok": True, "order_id": order_id, "impreso": impreso, "fuente": fuente, "ts": ts})


@app.route("/api/pedidos/marcar_impreso/<order_id>", methods=["POST"])
@requiere_auth
def marcar_impreso(order_id):
    with _lock:
        if order_id in _pedidos_ml:
            _pedidos_ml[order_id]["impreso"] = True
        borrado_kb = 0
        if order_id in _etiquetas_cache:
            pdf_size   = len(_etiquetas_cache[order_id].get("pdf", b""))
            borrado_kb = round(pdf_size / 1024, 1)
            _etiquetas_cache[order_id]["pdf"]        = None
            _etiquetas_cache[order_id]["impreso"]    = True
            _etiquetas_cache[order_id]["impreso_ts"] = datetime.now().strftime("%d/%m %H:%M:%S")
    logger.info(f"[CACHE] PDF borrado para {order_id} ({borrado_kb} KB liberados)")
    return jsonify({"ok": True, "liberado_kb": borrado_kb})


@app.route("/api/tokens_export")
@requiere_api_key
def api_tokens_export():
    if not _cuentas:
        return jsonify({"ok": False, "msg": "No hay cuentas conectadas."}), 400
    tokens_json  = _serializar_cuentas()
    cuentas_info = []
    for cid, tok in _cuentas.items():
        exp = tok.get("expires_at")
        exp_str = f"vence en {(exp - datetime.now()).total_seconds()/3600:.1f}h"                   if isinstance(exp, datetime) else "desconocido"
        cuentas_info.append({"cuenta_id": cid, "nickname": tok.get("nickname","?"), "expira": exp_str})
    return jsonify({"ok": True, "variable_name": "ML_TOKENS_JSON",
                    "ML_TOKENS_JSON": tokens_json, "cuentas": cuentas_info})


@app.route("/api/token_status")
def api_token_status():
    result = []
    for cid, tok in _cuentas.items():
        exp = tok.get("expires_at")
        if isinstance(exp, datetime):
            horas = (exp - datetime.now()).total_seconds() / 3600
            estado = f"Vence en {horas:.1f}h" if horas > 0 else "EXPIRADO"
        else:
            estado = "Sin fecha"
        result.append({"cuenta_id": cid, "nickname": tok.get("nickname","?"),
                        "tiene_token": bool(tok.get("access_token")),
                        "tiene_refresh": bool(tok.get("refresh_token")),
                        "token_estado": estado})
    return jsonify({"ok": True, "cuentas": result, "total": len(result), "ts": _ts()})


# ═══════════════════════════════════════════════════════════════════════════════
# API PICKING — sync con app_deposito.py
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/ping")
def ping():
    return jsonify({
        "ok": True, "version": SERVER_VERSION, "ts": _ts(),
        "cargado": any(e.get("cargado") for e in _estados_canal.values()),
        "canales": {c: {"cargado": e.get("cargado",False), "skus": e.get("total_skus",0)}
                    for c, e in _estados_canal.items()},
        "skus": len(_sku_db), "autenticado": bool(_cuentas),
        "cuentas": [t.get("nickname","?") for t in _cuentas.values()],
        "pedidos_ml": len(_pedidos_ml), "data_dir": DATA_DIR,
        "persistente": DATA_DIR != "/tmp",
    })


@app.route("/api/subir_estado", methods=["POST"])
@requiere_api_key
def subir_estado():
    data = request.get_json(force=True)
    if not data or "grupos" not in data:
        return jsonify({"ok": False, "msg": "Datos invalidos"}), 400
    canal = data.get("canal","default")
    est   = _get_estado(canal)
    with _lock:
        nueva_fase = data.get("fase",1)
        est.update({
            "fase": nueva_fase, "grupos": data.get("grupos",[]),
            "total_skus": data.get("total_skus",0), "total_uds": data.get("total_uds",0),
            "colecta": data.get("colecta",{}), "colecta_completa": data.get("colecta_completa",False),
            "ultima_actualizacion": _ts(), "cargado": True, "canal": canal,
        })
        if "pedidos" in data and isinstance(data["pedidos"], dict):
            est["pedidos"] = data["pedidos"]
        if nueva_fase == 1 and not data.get("colecta"):
            est["etiquetas_impresas"] = []
        _actualizar_sync_estado(canal)
    try:
        with open(LOTE_PATH,"w") as f:
            json.dump({"canales": _estados_canal}, f, default=str)
    except Exception as e:
        logger.error(f"[LOTE] Error persistiendo: {e}")
    logger.info(f"[LOTE] Canal '{canal}' cargado: {est['total_skus']} SKUs, fase {nueva_fase}")
    return jsonify({"ok": True, "canal": canal,
                    "msg": f"Canal '{canal}': {est['total_skus']} SKUs"})


@app.route("/api/estado")
def get_estado():
    canal = request.args.get("canal","default")
    est   = _get_estado(canal)
    with _lock:
        estado = dict(est)
    estado["cargado"] = bool(estado.get("cargado",False))
    estado["canal"]   = canal
    estado["canales_disponibles"] = {
        c: {"cargado": e.get("cargado",False), "total_skus": e.get("total_skus",0), "fase": e.get("fase",1)}
        for c, e in _estados_canal.items()
    }
    return jsonify(estado)


def _pedidos_completos_segun_colecta(canal="default"):
    est      = _get_estado(canal)
    pedidos  = est.get("pedidos",{})
    colecta  = dict(est.get("colecta",{}))
    impresas = set(est.get("etiquetas_impresas",[]))
    disponible = {str(s).upper(): int(c) for s, c in colecta.items()}
    resultado  = []
    for oid in sorted(pedidos.keys()):
        ped   = pedidos[oid]
        items = ped.get("items",[])
        if not items: continue
        completo = all(disponible.get(str(it.get("sku","")).upper(),0) >= int(it.get("req", it.get("cantidad",1)))
                       for it in items)
        if completo:
            for it in items:
                s = str(it.get("sku","")).upper()
                need = int(it.get("req", it.get("cantidad",1)))
                disponible[s] = disponible.get(s,0) - need
            resultado.append({"order_id": oid, "comprador": ped.get("comprador",""),
                               "shipping_id": ped.get("shipping_id",""),
                               "_cuenta": ped.get("_cuenta","cuenta_0"),
                               "items": items, "recien_completado": oid not in impresas})
    return resultado


@app.route("/api/fase2/pedidos-completos", methods=["GET"])
def fase2_pedidos_completos():
    canal = request.args.get("canal","default")
    est   = _get_estado(canal)
    with _lock:
        completos = _pedidos_completos_segun_colecta(canal)
        fase      = est.get("fase",1)
        impresas  = list(est.get("etiquetas_impresas",[]))
    return jsonify({"ok": True, "fase": fase, "completos": completos, "ya_impresas": impresas})


@app.route("/api/fase2/marcar-impresa/<order_id>", methods=["POST"])
def fase2_marcar_impresa(order_id):
    data  = request.get_json(silent=True) or {}
    canal = data.get("canal", request.args.get("canal","default"))
    est   = _get_estado(canal)
    nuevo_estado = "idle"
    with _lock:
        if order_id not in est.get("etiquetas_impresas",[]):
            est.setdefault("etiquetas_impresas",[]).append(order_id)
        if order_id in _pedidos_ml:
            _pedidos_ml[order_id]["impreso"] = True
        _actualizar_sync_estado(canal)
        nuevo_estado = _sync_estado_canal.get(canal,"idle")
        if nuevo_estado == "casi_listo":
            logger.info(f"[SYNC] Canal '{canal}' casi_listo — reactivando consultas ML")
        try:
            with open(LOTE_PATH,"w") as f:
                json.dump({"canales": _estados_canal}, f, default=str)
        except Exception:
            pass
    return jsonify({"ok": True, "order_id": order_id, "sync_estado": nuevo_estado})


@app.route("/api/escanear", methods=["POST"])
def escanear():
    data  = request.get_json(force=True)
    sku   = str(data.get("sku","")).strip().upper()
    canal = data.get("canal","default")
    est   = _get_estado(canal)
    if not sku:
        return jsonify({"ok": False, "msg": "SKU vacio"})
    with _lock:
        if not est["cargado"]:
            return jsonify({"ok": False, "msg": f"No hay lote cargado en canal '{canal}'"})
        colecta  = est["colecta"]
        sku_info = next((it for g in est["grupos"] for it in g.get("items",[]) if it["sku"] == sku), None)
        if not sku_info:
            return jsonify({"ok": False, "tipo": "no_encontrado", "msg": f"'{sku}' no esta en ningun pedido"})
        req = sku_info["req"]
        col = colecta.get(sku,0)
        if col >= req:
            return jsonify({"ok": True, "tipo": "ya_completo",
                            "msg": f"'{sku}' ya completo ({req}/{req})", "collected": col, "req": req})
        colecta[sku] = col + 1
        est["ultima_actualizacion"] = _ts()
        todo = all(colecta.get(it["sku"],0) >= it["req"] for g in est["grupos"] for it in g["items"])
        est["colecta_completa"] = todo
        if todo and est.get("fase",1) == 1:
            est["fase"] = 2
            logger.info(f"[FASE] Canal '{canal}': Colecta completa -> Fase 2")
        nuevo = colecta[sku]
    try:
        with open(LOTE_PATH,"w") as f:
            json.dump({"canales": _estados_canal}, f, default=str)
    except Exception:
        pass
    return jsonify({"ok": True, "tipo": "completo" if nuevo >= req else "parcial",
                    "sku": sku, "nombre": sku_info.get("nombre",""),
                    "pasillo": sku_info.get("pasillo",""), "estanteria": sku_info.get("estanteria",""),
                    "collected": nuevo, "req": req, "todo_completo": todo,
                    "msg": f"ok {nuevo}/{req}" if nuevo >= req else f"{nuevo}/{req}"})


@app.route("/api/reset_sku", methods=["POST"])
def reset_sku():
    data  = request.get_json(force=True)
    sku   = str(data.get("sku","")).strip().upper()
    canal = data.get("canal","default")
    est   = _get_estado(canal)
    with _lock:
        col = est["colecta"]
        if sku in col and col[sku] > 0:
            col[sku] -= 1
            if col[sku] == 0: del col[sku]
            est["colecta_completa"] = False
            est["ultima_actualizacion"] = _ts()
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
    q = request.args.get("q","").strip().upper()
    if q:
        resultado = {k: v for k, v in _sku_db.items()
                     if q in k or q in v.get("nombre","").upper() or q in v.get("pasillo","").upper()}
    else:
        resultado = _sku_db
    return jsonify({"ok": True, "skus": resultado, "total": len(_sku_db)})


@app.route("/api/skus", methods=["POST"])
@requiere_auth
def api_sku_guardar():
    data = request.get_json(force=True)
    sku  = str(data.get("sku","")).strip().upper()
    if not sku:
        return jsonify({"ok": False, "msg": "SKU vacio"}), 400
    nombre     = str(data.get("nombre","")).strip()
    pasillo    = str(data.get("pasillo","")).strip()
    estanteria = str(data.get("estanteria","")).strip()
    if not nombre:
        return jsonify({"ok": False, "msg": "Nombre obligatorio"}), 400
    with _lock:
        _sku_db[sku] = {"nombre": nombre, "pasillo": pasillo, "estanteria": estanteria}
    return jsonify({"ok": True, "sku": sku, "msg": f"SKU '{sku}' guardado"})


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
    data   = request.get_json(force=True)
    nuevos = data.get("skus",{})
    if not isinstance(nuevos, dict):
        return jsonify({"ok": False, "msg": "Formato invalido"}), 400
    agregados = 0
    with _lock:
        for sku, info in nuevos.items():
            k = sku.strip().upper()
            if k and k not in _sku_db:
                _sku_db[k] = {"nombre": str(info.get("nombre","")).strip(),
                               "pasillo": str(info.get("pasillo","")).strip(),
                               "estanteria": str(info.get("estanteria","")).strip()}
                agregados += 1
    return jsonify({"ok": True, "agregados": agregados, "total": len(_sku_db),
                    "msg": f"{agregados} SKUs agregados ({len(_sku_db)} en total)"})


@app.route("/api/skus/exportar")
@requiere_auth
def api_skus_exportar():
    return app.response_class(
        response=json.dumps(_sku_db, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=skus_db.json"})


@app.route("/api/skus/limpiar_todo", methods=["POST"])
@requiere_auth
def api_skus_limpiar():
    with _lock:
        _sku_db.clear()
    return jsonify({"ok": True, "msg": "BD de SKUs limpiada"})


# ═══════════════════════════════════════════════════════════════════════════════
# API IMAGEN SKU
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/imagen-sku/<sku>")
def api_imagen_sku(sku):
    sku = str(sku).strip().upper()
    if not sku:
        return jsonify({"ok": False, "existe": False, "sku": sku, "imagen_url": None}), 400
    try:
        item_id = None; nombre_local = None; cuenta_id = "cuenta_0"
        with _lock:
            for canal_name, canal_est in _estados_canal.items():
                for oid, ped in canal_est.get("pedidos",{}).items():
                    for item in ped.get("items",[]):
                        if str(item.get("sku","")).strip().upper() == sku:
                            item_id = item.get("item_id","")
                            nombre_local = item.get("nombre","")
                            cuenta_id = ped.get("_cuenta","cuenta_0")
                            if item_id: break
                    if item_id: break
                if item_id: break
            if not item_id:
                for canal_name, canal_est in _estados_canal.items():
                    for grupo in canal_est.get("grupos",[]):
                        for item in grupo.get("items",[]):
                            if item.get("sku","").upper() == sku:
                                item_id = item.get("item_id","")
                                nombre_local = item.get("nombre","")
                                if item_id: break
                        if item_id: break
                    if item_id: break
            if not item_id:
                for oid, ped in _pedidos_ml.items():
                    for it in ped.get("items",[]):
                        if str(it.get("sku","")).upper() == sku and it.get("item_id"):
                            item_id = it["item_id"]; cuenta_id = ped.get("_cuenta","cuenta_0"); break
                    if item_id: break

        if not item_id:
            return jsonify({"ok": True, "existe": False, "sku": sku, "imagen_url": None}), 200

        r_item = None
        for cid in [cuenta_id] + [c for c in _cuentas.keys() if c != cuenta_id]:
            try: _token_valido_cuenta(cid)
            except Exception: pass
            at = _cuentas.get(cid,{}).get("access_token","")
            if not at: continue
            try:
                r_item = requests.get(f"{ML_API_URL}/items/{item_id}",
                                      headers={"Authorization": f"Bearer {at}"}, timeout=10)
                if r_item.status_code == 200: break
            except Exception:
                continue

        if r_item is None or r_item.status_code != 200:
            return jsonify({"ok": True, "existe": False, "sku": sku, "imagen_url": None}), 200

        item_data  = r_item.json()
        titulo     = item_data.get("title", nombre_local or sku)
        precio     = item_data.get("price",0)
        disponible = item_data.get("available_quantity",0)

        def _https(u):
            if not u: return ""
            return u.replace("http://","https://") if u.startswith("http://") else u

        imagen_url = ""
        pictures   = item_data.get("pictures",[])
        if pictures:
            pic = pictures[0]
            imagen_url = _https(pic.get("secure_url") or pic.get("url") or "")
        if not imagen_url:
            imagen_url = _https(item_data.get("secure_thumbnail") or item_data.get("thumbnail") or "")
        if not imagen_url:
            return jsonify({"ok": True, "existe": True, "sku": sku, "item_id": item_id,
                            "titulo": titulo[:100], "imagen_url": None}), 200

        return jsonify({"ok": True, "existe": True, "sku": sku, "item_id": item_id,
                        "titulo": titulo[:100], "imagen_url": imagen_url,
                        "precio": precio, "disponible": disponible,
                        "ml_url": f"https://articulo.mercadolibre.com.uy/{item_id}"}), 200
    except requests.Timeout:
        return jsonify({"ok": True, "existe": False, "sku": sku, "imagen_url": None,
                        "msg": "Timeout descargando desde MercadoLibre"}), 200
    except Exception as e:
        logger.error(f"[IMAGEN-SKU] Error para {sku}: {e}")
        return jsonify({"ok": True, "existe": False, "sku": sku, "imagen_url": None,
                        "msg": f"Error interno: {str(e)[:100]}"}), 200


@app.route("/api/diag-imagen/<sku>")
def api_diag_imagen(sku):
    sku = str(sku).strip().upper()
    diag = {"sku": sku, "pasos": []}
    def paso(nombre, ok, detalle=""):
        diag["pasos"].append({"paso": nombre, "ok": ok, "detalle": str(detalle)[:300]})
    with _lock:
        cargado = bool(_estado.get("cargado"))
        n_grupos  = len(_estado.get("grupos",[]))
        n_pedidos = len(_estado.get("pedidos",{}))
    paso("lote_cargado", cargado, f"grupos={n_grupos}, pedidos={n_pedidos}")
    item_id = None; cuenta_id = "cuenta_0"; origen = None
    with _lock:
        for oid, ped in _estado.get("pedidos",{}).items():
            for item in ped.get("items",[]):
                if str(item.get("sku","")).strip().upper() == sku:
                    item_id = item.get("item_id",""); cuenta_id = ped.get("_cuenta","cuenta_0")
                    origen = "pedidos"; break
            if item_id: break
        if not item_id:
            for grupo in _estado.get("grupos",[]):
                for item in grupo.get("items",[]):
                    if str(item.get("sku","")).strip().upper() == sku:
                        item_id = item.get("item_id",""); origen = "grupos"; break
                if item_id: break
    paso("sku_en_lote", bool(item_id), f"item_id={item_id or '(vacio)'}, origen={origen}")
    if not item_id:
        diag["conclusion"] = "El SKU no tiene item_id en el lote. Regenera el lote."
        return jsonify(diag), 200
    cuentas_con_token = [c for c, t in _cuentas.items() if t.get("access_token")]
    paso("cuentas_con_token", bool(cuentas_con_token), f"cuentas={cuentas_con_token}")
    if not cuentas_con_token:
        diag["conclusion"] = "No hay cuentas ML con token. Reconecta MercadoLibre."
        return jsonify(diag), 200
    item_data = None
    for cid in [cuenta_id] + [c for c in _cuentas.keys() if c != cuenta_id]:
        try: _token_valido_cuenta(cid)
        except Exception: pass
        at = _cuentas.get(cid,{}).get("access_token","")
        if not at: continue
        try:
            r = requests.get(f"{ML_API_URL}/items/{item_id}",
                             headers={"Authorization": f"Bearer {at}"}, timeout=10)
            paso(f"ml_items_{cid}", r.status_code == 200, f"status={r.status_code}")
            if r.status_code == 200: item_data = r.json(); break
        except Exception as e:
            paso(f"ml_items_{cid}", False, f"excepcion: {e}")
    if not item_data:
        diag["conclusion"] = "ML rechazo la llamada a /items/{id}."
        return jsonify(diag), 200
    pics  = item_data.get("pictures",[])
    thumb = item_data.get("secure_thumbnail") or item_data.get("thumbnail")
    img   = (pics[0].get("secure_url") or pics[0].get("url") or "") if pics else (thumb or "")
    paso("imagen_extraida", bool(img), f"n_pictures={len(pics)}, url={img[:120]}")
    diag["resultado"] = {"titulo": item_data.get("title",""), "imagen_url": img,
                          "precio": item_data.get("price",0),
                          "disponible": item_data.get("available_quantity",0)}
    diag["conclusion"] = "OK Todo correcto" if img else "El item existe pero no tiene imagenes en ML."
    return jsonify(diag), 200

# ═══════════════════════════════════════════════════════════════════════════════
# STARTUP + FRONTEND + ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════


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
                logger.debug(f"[ETIQUETA] Pedido {order_id} no encontrado, esperando...")
                _time.sleep(5)
                continue
            shipping_id = pedido.get("shipping_id","")
            if not shipping_id:
                logger.debug(f"[ETIQUETA] Pedido {order_id} sin shipping_id, esperando...")
                _time.sleep(8)
                continue
            cuenta_id = pedido.get("_cuenta", "cuenta_0")
            at = _cuentas.get(cuenta_id, {}).get("access_token","")
            if not at:
                logger.warning(f"[ETIQUETA] Sin token para {cuenta_id}")
                if order_id in _cola_etiquetas:
                    _cola_etiquetas.remove(order_id)
                return
            r_ship = requests.get(
                f"{ML_API_URL}/shipments/{shipping_id}",
                headers={"Authorization": f"Bearer {at}"},
                timeout=10)
            if r_ship.status_code != 200:
                logger.debug(f"[ETIQUETA] No se pudo obtener shipment {shipping_id}: {r_ship.status_code}")
                _time.sleep(10)
                continue
            ship_data  = r_ship.json()
            estado     = ship_data.get("status","")
            substatus  = ship_data.get("substatus","")
            logistic   = ship_data.get("logistic_type","") or \
                         (ship_data.get("logistic") or {}).get("type","")
            with _lock:
                if order_id in _pedidos_ml:
                    _pedidos_ml[order_id]["logistica"]    = logistic
                    _pedidos_ml[order_id]["estado_envio"] = estado
            if estado not in ("ready_to_ship", "handling", "pending", ""):
                logger.debug(f"[ETIQUETA] Pedido {order_id} en estado '{estado}' — no disponible")
                if estado in ("delivered", "shipped", "cancelled", "not_delivered"):
                    return
                _time.sleep(15)
                continue
            pdf_content = None
            for rtype in ["pdf2", "pdf"]:
                r = requests.get(
                    f"{ML_API_URL}/shipment_labels",
                    headers={"Authorization": f"Bearer {at}"},
                    params={"shipment_ids": shipping_id, "response_type": rtype},
                    timeout=15)
                if r.status_code == 200 and len(r.content) > 500:
                    pdf_content = r.content
                    logger.info(f"[ETIQUETA] PDF descargado para {order_id} ({len(pdf_content)} bytes)")
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
                if order_id in _cola_etiquetas:
                    _cola_etiquetas.remove(order_id)
                return
            logger.debug(f"[ETIQUETA] Intento {intento+1}/{reintentos} fallido para {order_id}")
            _time.sleep(20 * (intento + 1))
        except Exception as e:
            logger.debug(f"[ETIQUETA] Error en intento {intento+1} para {order_id}: {e}")
            _time.sleep(10)
    logger.warning(f"[ETIQUETA] No se pudo descargar etiqueta para {order_id} despues de {reintentos} intentos")


def _encolar_descarga_etiqueta(order_id):
    """Encola la descarga de una etiqueta en un thread separado."""
    if order_id not in _etiquetas_cache and order_id not in _cola_etiquetas:
        _cola_etiquetas.append(order_id)
        threading.Thread(
            target=_descargar_etiqueta_bg,
            args=(order_id,),
            daemon=True).start()
        logger.debug(f"[ETIQUETA] Encolada descarga para {order_id}")


def _descargar_etiquetas_lote(order_ids):
    """Descarga etiquetas de multiples pedidos en paralelo (max 5 simultaneos)."""
    from concurrent.futures import ThreadPoolExecutor
    ids_sin_cache = [oid for oid in order_ids if oid not in _etiquetas_cache]
    if not ids_sin_cache:
        return
    logger.info(f"[ETIQUETA] Descargando {len(ids_sin_cache)} etiquetas en paralelo...")
    with ThreadPoolExecutor(max_workers=5) as ex:
        ex.map(_descargar_etiqueta_bg, ids_sin_cache)


def api_diag_pedidos():
    """Diagnostico: lista cada pedido con su logistica y tipo calculado."""
    with _lock:
        pedidos = list(_pedidos_ml.values())
    detalle    = []
    contadores = {"flex":0,"colecta":0,"me1":0,"full":0,"desconocido":0,"sin_logistica":0}
    for p in pedidos:
        log       = p.get("logistica","")
        tipo_calc = _calcular_tipo(log, p.get("tags",[]), p.get("shipping_id",""))
        if not log:
            contadores["sin_logistica"] += 1
        contadores[tipo_calc] = contadores.get(tipo_calc, 0) + 1
        detalle.append({
            "order_id":       str(p.get("order_id","")),
            "nick":           p.get("nickname",""),
            "logistica":      log or "(vacio)",
            "tipo_guardado":  p.get("tipo",""),
            "tipo_calculado": tipo_calc,
            "shipping_id":    p.get("shipping_id",""),
            "estado":         p.get("estado_envio",""),
            "impreso":        p.get("impreso", False),
        })
    return jsonify({"total": len(pedidos), "contadores": contadores, "pedidos": detalle})


@app.route("/admin/usuarios")
def admin_usuarios_panel():
    """Panel web para gestionar usuarios. Requiere ?key=PICKING_API_KEY"""
    key = request.args.get("key","")
    if key != API_KEY:
        return """<html><body style='font-family:sans-serif;background:#0F172A;
        color:#F1F5F9;display:flex;align-items:center;justify-content:center;
        min-height:100vh;margin:0'>
        <div style='text-align:center'><h2>Acceso restringido</h2>
        <p>Agregá <code>?key=TU_API_KEY</code> a la URL</p>
        </div></body></html>""", 401

    usuarios_html = ""
    for u in _usuarios:
        rol_color = "#10B981" if u.get("rol") == "supervisor" else "#3B82F6"
        usuarios_html += f"""<tr>
          <td>{u.get('nombre','')}</td>
          <td><code>{u.get('usuario','')}</code></td>
          <td><code>{u.get('cuenta_id','')}</code></td>
          <td><span style='color:{rol_color};font-weight:700'>{u.get('rol','')}</span></td>
          <td><button onclick="eliminar('{u.get('usuario','')}')"
            style='background:#EF4444;color:white;border:none;padding:4px 12px;
            border-radius:6px;cursor:pointer'>Eliminar</button></td></tr>"""

    return render_template_string("""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Logibot — Usuarios</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0F172A;color:#F1F5F9;font-family:'Segoe UI',sans-serif;padding:32px 20px}
h1{color:#3B82F6;margin-bottom:4px}
.sub{color:#94A3B8;font-size:13px;margin-bottom:28px}
table{width:100%;border-collapse:collapse;background:#1E293B;border-radius:12px;
  overflow:hidden;margin-bottom:32px}
th{background:#1E3A5F;padding:12px 16px;text-align:left;font-size:13px;
  color:#93C5FD;font-weight:700}
td{padding:11px 16px;font-size:13px;border-bottom:1px solid #334155}
tr:last-child td{border-bottom:none}
tr:hover td{background:#263350}
code{background:#0F172A;padding:2px 6px;border-radius:4px;font-size:12px;color:#34D399}
.card{background:#1E293B;border-radius:12px;padding:24px;max-width:520px}
.card h2{color:#F1F5F9;font-size:16px;margin-bottom:16px}
label{display:block;color:#94A3B8;font-size:12px;margin-bottom:4px;margin-top:12px}
input,select{width:100%;padding:9px 12px;background:#0F172A;border:1px solid #334155;
  color:#F1F5F9;border-radius:8px;font-size:14px}
input:focus,select:focus{outline:none;border-color:#3B82F6}
.btn{margin-top:16px;width:100%;padding:11px;background:#3B82F6;color:white;
  border:none;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer}
.btn:hover{background:#2563EB}
#msg{margin-top:12px;font-size:13px;min-height:20px}
.ok{color:#10B981}.err{color:#EF4444}
</style></head><body>
<h1>Logibot — Gestion de Usuarios</h1>
<p class="sub">Usuarios que pueden iniciar sesion en la app de escritorio.</p>
<table><thead><tr>
  <th>Nombre</th><th>Usuario</th><th>cuenta_id ML</th>
  <th>Rol</th><th>Acciones</th>
</tr></thead><tbody>""" + usuarios_html + """</tbody></table>
<div class="card">
  <h2>Agregar usuario</h2>
  <label>Nombre visible</label>
  <input id="f-nombre" placeholder="Ej: Everest Shopping">
  <label>Usuario (para el login)</label>
  <input id="f-usuario" placeholder="Ej: everest">
  <label>Clave</label>
  <input id="f-clave" type="password" placeholder="Minimo 4 caracteres">
  <label>cuenta_id ML</label>
  <input id="f-cuenta" placeholder="Ej: cuenta_0 o cuenta_2 o todas">
  <small style="color:#64748B;font-size:11px">
    Ver /api/cuentas?key=""" + key + """ para los cuenta_id disponibles
  </small>
  <label>Rol</label>
  <select id="f-rol">
    <option value="operario">Operario - ve solo su tienda</option>
    <option value="supervisor">Supervisor - ve todo + Config</option>
  </select>
  <button class="btn" onclick="crear()">Crear usuario</button>
  <div id="msg"></div>
</div>
<script>
const KEY = '""" + key + """';
const BASE = window.location.origin;
async function crear() {
  const msg = document.getElementById('msg');
  const body = {
    nombre:    document.getElementById('f-nombre').value.trim(),
    usuario:   document.getElementById('f-usuario').value.trim().toLowerCase(),
    clave:     document.getElementById('f-clave').value.trim(),
    cuenta_id: document.getElementById('f-cuenta').value.trim(),
    rol:       document.getElementById('f-rol').value,
  };
  if (!body.usuario || !body.clave || !body.cuenta_id) {
    msg.textContent='Completa usuario, clave y cuenta_id.';
    msg.className='err'; return;
  }
  try {
    const r = await fetch(BASE+'/api/auth/usuarios', {
      method:'POST',
      headers:{'Content-Type':'application/json','X-API-Key':KEY},
      body:JSON.stringify(body)
    });
    const d = await r.json();
    if (d.ok) { msg.textContent='OK: '+d.msg; msg.className='ok';
      setTimeout(()=>location.reload(),1200);
    } else { msg.textContent='Error: '+d.msg; msg.className='err'; }
  } catch(e) { msg.textContent='Error: '+e; msg.className='err'; }
}
async function eliminar(usuario) {
  if (!confirm('Eliminar usuario '+usuario+'?')) return;
  try {
    const r = await fetch(BASE+'/api/auth/usuarios/'+usuario,
      {method:'DELETE',headers:{'X-API-Key':KEY}});
    const d = await r.json();
    if (d.ok) location.reload(); else alert('Error: '+d.msg);
  } catch(e) { alert('Error: '+e); }
}
</script></body></html>""")


def _startup():
    _cargar_sku_db()
    _cargar_usuarios()
    _cargar_tokens_persistidos()
    if not _cuentas:
        _cargar_tokens_local()
    if _cuentas:
        nicks = [t.get("nickname","?") for t in _cuentas.values()]
        logger.info(f"[STARTUP] Tokens cargados: {nicks}")
        for cid in list(_cuentas.keys()):
            tok = _cuentas[cid]
            exp = tok.get("expires_at")
            if exp and isinstance(exp, str):
                try:
                    tok["expires_at"] = datetime.fromisoformat(exp)
                except Exception:
                    tok["expires_at"] = datetime.now() + timedelta(hours=1)
                _cuentas[cid] = tok
        threading.Thread(target=_refresh_pedidos_worker,    daemon=True).start()
        threading.Thread(target=_sync_pedidos_ml_periodico, daemon=True).start()
        threading.Thread(target=_warmup_matutino,           daemon=True).start()
        logger.info("[STARTUP] Threads activos: sync ML + warmup 6:45AM")
    else:
        logger.warning("[STARTUP] Sin tokens. Conectar ML desde la app.")

    try:
        with open(LOTE_PATH) as f:
            data = json.load(f)
        if "canales" in data:
            for canal, est_data in data["canales"].items():
                if est_data.get("cargado") and est_data.get("grupos"):
                    _estados_canal[canal] = est_data
                    logger.info(f"[STARTUP] Canal '{canal}' restaurado: {est_data.get('total_skus',0)} SKUs")
        elif data.get("cargado") and data.get("grupos"):
            _estados_canal["default"] = data
            _estado.update(data)
            logger.info(f"[STARTUP] Lote legacy restaurado: {data.get('total_skus',0)} SKUs")
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error(f"[STARTUP] Error restaurando lote: {e}")

_startup()


@app.route("/")
def index():
    if not _token_valido():
        return redirect("/auth/login")
    return _leer_template("app.html")


@app.route("/manifest.json")
def manifest():
    return jsonify({"name": "Picking App", "short_name": "Picking",
                    "start_url": "/movil", "display": "standalone",
                    "background_color": "#0F172A", "theme_color": "#1E293B",
                    "icons": [{"src": "/static/icon.png", "sizes": "192x192", "type": "image/png"}]})


@app.route("/movil")
def movil():
    return _leer_template("movil.html")


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("DEBUG","false").lower() == "true"
    if debug:
        app.run(host="0.0.0.0", port=port, debug=True)
    else:
        try:
            from waitress import serve
            logger.info(f"Servidor iniciado (waitress) en http://0.0.0.0:{port} — threads=32")
            serve(app, host="0.0.0.0", port=port, threads=32,
                  channel_timeout=60,   # antes 120 — liberar conexiones colgadas más rápido
                  connection_limit=100, # antes 200 — Railway tiene límite de recursos
                  cleanup_interval=5)   # antes 10 — limpiar más seguido
        except ImportError:
            logger.info(f"Servidor iniciado (flask dev) en http://0.0.0.0:{port}")
            app.run(host="0.0.0.0", port=port, debug=False)