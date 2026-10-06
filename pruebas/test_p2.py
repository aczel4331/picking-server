"""Fase 2: el escritorio manda el token de la tienda en TODOS sus pedidos (sin tocar cada
llamada) y Everest sigue con la clave de siempre. Con --demo deja el servidor local
levantado (puerto 5131) con una tienda de prueba para probar el celular en el navegador."""
import os, sys, json, time, shutil, subprocess, base64, io, ssl
import urllib.request, urllib.error
import requests
from PIL import Image

sys.stdout.reconfigure(encoding="utf-8")
S_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DEFAULT = os.path.dirname(S_DIR)
REPO = os.environ.get("REPO", REPO_DEFAULT)
PORT = "5131"
B = f"http://127.0.0.1:{PORT}"
DATA = os.path.join(S_DIR, "data_p2")
SNAP = os.path.join(S_DIR, "snap", "pedidos_live.json")
DEMO = "--demo" in sys.argv
ok_n = fail_n = 0


def check(n, c, extra=""):
    global ok_n, fail_n
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else f"   → {extra}"))
    if c: ok_n += 1
    else: fail_n += 1


shutil.rmtree(DATA, ignore_errors=True); os.makedirs(DATA)
json.dump([{"usuario": "admin", "clave": "1234", "nombre": "Admin", "cuenta_id": "todas", "rol": "admin"},
           {"usuario": "colector1", "clave": "c1", "nombre": "Colector 1", "cuenta_id": "cuenta_2", "rol": "operario"}],
          open(os.path.join(DATA, "usuarios.json"), "w"))
env = dict(os.environ, PORT=PORT, DATA_DIR=DATA, PICKING_API_KEY="everest2024", REPO=REPO, PEDIDOS_SNAP=SNAP,
           APP_SECRET_KEY="s" * 40, ML_API_URL="http://127.0.0.1:1", ML_APP_ID="x", ML_SECRET_KEY="x", PYTHONIOENCODING="utf-8")
srv = subprocess.Popen([sys.executable, os.path.join(REPO_DEFAULT, "pruebas", "run_srv_pedidos.py")], env=env,
                       stdout=open(os.path.join(S_DIR, "p2.log"), "w"), stderr=subprocess.STDOUT)
try:
    for _ in range(80):
        try: requests.get(B + "/api/ping", timeout=1); break
        except Exception: time.sleep(0.5)
    b = io.BytesIO(); Image.new("RGBA", (160, 50), (200, 40, 90, 255)).save(b, "PNG")
    ADM = {"X-Admin-Usuario": "admin", "X-Admin-Clave": "1234"}
    TID = requests.post(B + "/api/admin/tiendas", headers=ADM, json={"nombre": "Tienda Beta", "logo_b64": base64.b64encode(b.getvalue()).decode()}).json()["tienda"]["id"]
    ses = requests.Session(); ses.post(B + "/admin/usuarios", data={"usuario": "admin", "clave": "1234"})
    ses.post(B + "/api/auth/usuarios", headers={"X-API-Key": "everest2024"},
             json={"usuario": "bop", "clave": "bpw2", "nombre": "Op Beta", "cuenta_id": TID, "rol": "operario"})
    pedidos_b = [{"order_id": f"9000000000{i}", "pack_id": "", "shipping_id": f"7000{i}", "comprador": "COMPRADOR-BETA", "_cuenta": TID,
                  "_nickname": "BETA_SHOP", "logistica": "cross_docking", "substatus": "ready_to_print", "estado_envio": "ready_to_ship",
                  "impreso": False, "tipo": "colecta", "fecha": "2026-10-05", "fecha_cierre": "2026-10-05",
                  "items": [{"sku": f"SKU-B-{i}", "item_id": f"MLUB{i}", "titulo": f"Producto Beta {i}", "cantidad": 2}]} for i in range(1, 4)]
    requests.post(B + "/__test/inject", json={"pedidos": pedidos_b, "cuentas": {TID: {"access_token": "AT", "refresh_token": "RT", "user_id": "222", "nickname": "BETA_SHOP"}}})
    tok = requests.post(B + "/api/auth/login", json={"usuario": "bop", "clave": "bpw2"}).json()["token"]
    requests.post(B + "/api/subir_estado", headers={"X-API-Key": tok}, json={
        "fase": 1, "grupos": [{"pasillo": "B1 PASILLO", "items": [{"sku": f"SKU-B-{i}", "nombre": f"Producto Beta {i}", "req": 2, "pasillo": "B1", "estanteria": "", "item_id": f"MLUB{i}"} for i in (1, 2, 3)]}],
        "colecta": {}, "colecta_completa": False, "total_skus": 3, "total_uds": 6, "pedidos": {}, "canal": "colecta",
        "operario": "Op Beta", "usuario": "bop", "cuenta_id": TID, "lote_id": "LB1"})

    if DEMO:
        print(f"DEMO lista: {B}/movil  (tienda {TID}: usuario bop / bpw2 · Everest: colector1 / c1)", flush=True)
        while True: time.sleep(60)

    sys.path.insert(0, REPO)
    import app_deposito as A
    A.RAILWAY_URL = B
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

    def get(path, headers=None, con_ctx=False):
        r = urllib.request.Request(B + path, headers=headers or {})
        with urllib.request.urlopen(r, timeout=10, **({"context": ctx} if con_ctx else {})) as x:
            return json.loads(x.read())

    print("escritorio: usuario de Everest (sin token)")
    r = requests.post(B + "/api/auth/login", json={"usuario": "colector1", "clave": "c1"}).json()
    A.activar_marca_de_usuario(r["tienda"], r["token"])
    check("Everest no usa token (sigue con la clave de siempre)", A._TOKEN_SESION["v"] == "")
    pe = get("/api/pedidos")
    check("Everest: ve sus pedidos y ninguno de la tienda nueva", pe["total"] >= 20 and not any(p["_cuenta"] == TID for p in pe["pedidos"]))

    print("escritorio: usuario de la tienda nueva")
    r = requests.post(B + "/api/auth/login", json={"usuario": "bop", "clave": "bpw2"}).json()
    A.activar_marca_de_usuario(r["tienda"], r["token"])
    check("el token de la tienda queda guardado", A._TOKEN_SESION["v"].startswith("tk1."))
    d = get("/api/pedidos")
    check("pedido SIN encabezados → llega con el token: solo los de la tienda", d["total"] == 3 and all(p["_cuenta"] == TID for p in d["pedidos"]), str(d.get("total")))
    d = get("/api/pedidos", con_ctx=True)
    check("…también cuando la app usa su propio contexto SSL", d["total"] == 3)
    d = get("/api/pedidos", headers={"X-API-Key": "everest2024"})
    check("…aunque el código mande la clave de Everest, gana el token (no ve Everest)", d["total"] == 3)
    e = get("/api/estado?usuario=bop&cuenta_id=cuenta_2&canal=colecta")
    check("lote de la tienda: lo ve; no cruza a Everest", e["cargado"] and e["total_skus"] == 3)
    req = urllib.request.Request(B + "/api/subir_estado", method="POST", headers={"Content-Type": "application/json", "X-API-Key": "everest2024"},
                                 data=json.dumps({"fase": 1, "grupos": [{"pasillo": "P", "items": [{"sku": "SKU-B-9", "nombre": "x", "req": 1, "pasillo": "P", "estanteria": ""}]}], "colecta": {}, "total_skus": 1, "total_uds": 1,
                                                  "pedidos": {}, "canal": "colecta", "usuario": "bop", "cuenta_id": TID, "lote_id": "LB2"}).encode())
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    check("subir lote desde la app de la tienda: la clave la arma el servidor", resp.get("clave", "").startswith(TID + ":"), str(resp))
    r2 = urllib.request.Request(B + "/api/ping")
    check("cada pedido informa la versión del cliente", r2.get_header("X-client-version") == A.APP_VERSION, str(r2.headers))
    r3 = urllib.request.Request("https://api.mercadolibre.com/users/me")
    check("pedidos a OTROS servidores no llevan el token ni la versión", r3.get_header("X-api-key") is None and r3.get_header("X-client-version") is None)
    A.set_token_sesion("")
    check("al cerrar sesión se borra el token", urllib.request.Request(B + "/api/pedidos").get_header("X-api-key") is None)
    r = requests.post(B + "/api/auth/login", json={"usuario": "colector1", "clave": "c1"}).json()
    A.activar_marca_de_usuario(r["tienda"], r["token"])
    check("al volver Everest, sin token otra vez", A._TOKEN_SESION["v"] == "" and get("/api/pedidos")["total"] >= 20)
finally:
    srv.kill()
print(f"\nRESULTADO P2 escritorio: {ok_n} ok, {fail_n} fallas")
sys.exit(1 if fail_n else 0)
