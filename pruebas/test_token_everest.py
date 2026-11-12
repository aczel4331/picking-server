"""4.1.37: los usuarios de Everest (app y celular) usan el token firmado que entrega el login en vez de la clave.
Para cada ruta que usan la app y el celular, con el MISMO pedido: token == clave de siempre (mismo código y mismo
cuerpo). Además la app manda siempre alguna credencial, y el token de Everest por vencer vuelve a la clave."""
import os, sys, json, tempfile, time
sys.stdout.reconfigure(encoding="utf-8")
S_DIR = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(S_DIR)
DATA = tempfile.mkdtemp()
os.environ.update(DATA_DIR=DATA, PICKING_API_KEY="everest2024", APP_SECRET_KEY="s" * 40, ML_APP_ID="x",
                  ML_SECRET_KEY="x", ML_API_URL="http://127.0.0.1:1")
sys.path.insert(0, REPO); os.chdir(REPO)
import server as S
from datetime import datetime, timedelta

ok = fa = 0
def check(n, c, x=""):
    global ok, fa
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else "  -> " + str(x)[:260]))
    if c: ok += 1
    else: fa += 1

C = S.app.test_client()
S._cuentas["cuenta_2"] = {"access_token": "AT", "refresh_token": "RT", "expires_at": datetime.now() + timedelta(hours=5),
                          "user_id": "1", "nickname": "EVEREST"}
for i, sku in enumerate(("SKU-A", "SKU-B"), 1):
    S._pedidos_ml[f"20000000000000{i:02d}"] = {"order_id": f"20000000000000{i:02d}", "shipping_id": f"4400000000{i}",
        "_cuenta": "cuenta_2", "comprador": "B", "impreso": False, "logistica": "self_service", "substatus": "ready_to_print",
        "estado_envio": "ready_to_ship", "fecha": "2026-11-12", "items": [{"sku": sku, "item_id": "MLU1", "titulo": "P " + sku, "cantidad": 1}]}
S._usuarios.extend([
    {"usuario": "oper_e", "clave": "c-oper", "nombre": "Operario E", "cuenta_id": "cuenta_2", "rol": "operario"},
    {"usuario": "sup_e", "clave": "c-sup", "nombre": "Supervisor E", "cuenta_id": "cuenta_2", "rol": "supervisor"},
    {"usuario": "adm_e", "clave": "c-adm", "nombre": "Admin general", "cuenta_id": "todas", "rol": "admin"},
])

def login(u, c):
    r = C.post("/api/auth/login", json={"usuario": u, "clave": c}); return r.get_json()

print("1) el login entrega token a los usuarios de Everest")
toks = {}
for u, c in (("oper_e", "c-oper"), ("sup_e", "c-sup"), ("adm_e", "c-adm")):
    d = login(u, c); toks[u] = d.get("token", "")
    check(f"{u}: login ok con token y tienda primaria (tienda_id vacío)", d.get("ok") and toks[u].startswith("tk1.")
          and (d.get("tienda") or {}).get("tienda_id") == "", {k: v for k, v in d.items() if k != "token"})

LEG = {"X-API-Key": "everest2024"}
def tk(u): return {"X-API-Key": toks[u]}
ID = "usuario=oper_e&cuenta_id=cuenta_2&canal=flex"

def lote():
    return {"fase": 1, "grupos": [{"pasillo": "P1", "items": [{"sku": "SKU-A", "nombre": "A", "req": 2, "pasillo": "P1", "estanteria": ""}]}],
            "colecta": {}, "colecta_completa": False, "total_skus": 1, "total_uds": 2, "pedidos": {}, "operario": "OE",
            "lote_id": "LT1", "usuario": "oper_e", "cuenta_id": "cuenta_2", "canal": "flex"}

LECTURAS = [("/api/pedidos", None), ("/api/pedidos/estados", None), ("/auth/status", None), ("/api/cuentas", None),
            ("/api/cortes", None), ("/api/config-app", None), (f"/api/estado?{ID}", None), (f"/api/lote-en-vivo?{ID}", None),
            ("/api/auth/pendientes", None), ("/api/alertas", None), ("/api/alertas/sin-stock", None),
            ("/api/imagen-sku/SKU-A", None), ("/api/sesion/recuperar/oper_e", None), ("/api/pedidos/impreso/2000000000000001", None),
            ("/api/auth/usuarios", None), ("/api/metricas", None), ("/api/ping", None), ("/api/etiquetas_cache", None)]
ESCRITURAS = [("POST", "/api/subir_estado", lote()), ("POST", "/api/escanear", {"sku": "SKU-A", "usuario": "oper_e", "cuenta_id": "cuenta_2", "canal": "flex"}),
              ("POST", "/api/reset_sku", {"sku": "SKU-A", "usuario": "oper_e", "cuenta_id": "cuenta_2", "canal": "flex"}),
              ("POST", "/api/sesion/guardar", {"usuario": "oper_e", "estado": {"x": 1}}),
              ("POST", "/api/metricas/subir", {"metricas": [{"ts": "2026-11-12 10:00:00", "canal": "flex", "operario": "OE", "n_pedidos": 1, "total_uds": 1}]}),
              ("POST", "/api/auth/solicitar", {"usuario": "oper_e", "motivo": "x", "tipo": "saltar_fase1"})]

def norm(r):
    try: d = r.get_json()
    except Exception: d = None
    if isinstance(d, dict):
        for k in ("ts", "timestamp", "rev", "hora", "fecha", "token", "expira", "ahora", "uptime", "generado", "id", "request_id"):
            d.pop(k, None)
    return d if d is not None else r.data[:200]

print("2) con el token de cada usuario de Everest: mismas respuestas que con la clave de siempre")
for u in ("oper_e", "sup_e", "adm_e"):
    difs = []
    for ruta, _ in LECTURAS:
        a = C.get(ruta, headers=LEG); b = C.get(ruta, headers=tk(u))
        if a.status_code != b.status_code or (norm(a) != norm(b) and ruta not in ("/api/auth/usuarios",)):
            difs.append((ruta, a.status_code, b.status_code))
    check(f"{u}: {len(LECTURAS)} lecturas iguales con token y con clave", not difs, difs)
difs = []
for m, ruta, body in ESCRITURAS:
    a = C.post(ruta, headers=LEG, json=body); b = C.post(ruta, headers=tk("oper_e"), json=body)
    if a.status_code != b.status_code or a.status_code >= 500 or b.status_code in (401, 403):
        difs.append((ruta, a.status_code, b.status_code, b.data[:80]))
check(f"las {len(ESCRITURAS)} escrituras (lote, escanear, deshacer, sesión, métricas, aprobación) responden igual", not difs, difs)

print("3) el token no cambia a quién ve cada uno")
d = C.get("/api/pedidos", headers=tk("oper_e")).get_json()
check("operario con token: los pedidos de Everest", d.get("ok") and len(d.get("pedidos", [])) == 2, d)
lu = C.get("/api/auth/usuarios", headers=tk("oper_e")).get_json().get("usuarios", [])
check("operario con token: no ve usuarios de otras tiendas", all(S._tienda_de_cuenta(u.get("cuenta_id")) == "" for u in lu), lu)
r = C.post("/api/escanear", headers=tk("oper_e"), json={"sku": "SKU-A", "usuario": "oper_e", "cuenta_id": "cuenta_2", "canal": "flex"})
check("escanear con token cuenta en el lote de ese colector", (r.get_json() or {}).get("ok") is not False or "no" in str(r.get_json()), r.get_json())
r = C.get("/api/pedidos", headers={"X-API-Key": "tk1.token-falso"})
check("token inventado -> 401 con 'relogin' (el celular vuelve al login)", r.status_code == 401 and r.get_json().get("relogin"), r.status_code)

print("4) la app manda siempre alguna credencial")
import urllib.request as ur
import app_deposito as A
base = A.RAILWAY_URL
def cab(url="/api/x", **k):
    return {h.lower(): v for h, v in ur.Request(base + url, **k).header_items()}
A.set_token_sesion("")
h = cab()
check("sin sesión (pantalla de login): X-API-Key = la clave de la instalación", h.get("x-api-key") == A.API_KEY_NUBE and h.get("x-client-version") == A.APP_VERSION, h)
h = cab(headers={"X-API-Key": "la-que-ya-traia"})
check("si la llamada ya trae su clave, no se pisa", h.get("x-api-key") == "la-que-ya-traia", h)
A.set_token_sesion("tk1.abc", primaria=True)
check("con sesión de Everest: va el token", cab().get("x-api-key") == "tk1.abc")
A._TOKEN_SESION["t"] = time.time() - 26 * 86400
check("token de Everest con más de 25 días: vuelve a la clave de siempre", cab().get("x-api-key") == A.API_KEY_NUBE, cab())
A.set_token_sesion("tk1.tienda", primaria=False); A._TOKEN_SESION["t"] = time.time() - 26 * 86400
check("token de una tienda nueva no se reemplaza nunca (no tiene clave)", cab().get("x-api-key") == "tk1.tienda")
check("pedidos a otro servidor no reciben credenciales", "x-api-key" not in {h.lower() for h, _ in ur.Request("https://ejemplo.com/x").header_items()})
A.set_token_sesion("")
src = open(os.path.join(REPO, "app_deposito.py"), encoding="utf-8").read()
check("el login guarda el token de Everest (set_token_sesion(token, primaria=not tid))", "set_token_sesion(token, primaria=not tid)" in src)

print("5) celular")
m = open(os.path.join(REPO, "templates", "movil.html"), encoding="utf-8").read()
check("guarda el token de todo colector", "if (d.token) _colector.token = d.token;" in m and "d.tienda.tienda_id) _colector.token" not in m)
check("un 401 (con o sin token guardado) lo manda al login: sondeos y escaneo",
      m.count("if (r.status === 401 && _colector) { _sesionVencida(); return; }") == 3)

print(f"\nRESULTADO token Everest: {ok} ok, {fa} fallas"); sys.exit(1 if fa else 0)
