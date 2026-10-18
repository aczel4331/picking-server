"""rev/base_rev: escaneo sube rev, reset_sku no lo revierte un subir_estado viejo, fase no retrocede."""
import os, sys, json, time, shutil, subprocess
import requests
sys.stdout.reconfigure(encoding="utf-8")
S = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(S)
PORT = "5139"; B = f"http://127.0.0.1:{PORT}"; DATA = os.path.join(S, "data_rev")
ok = fa = 0
def check(n, c, x=""):
    global ok, fa
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else "  -> " + str(x)))
    if c: ok += 1
    else: fa += 1
shutil.rmtree(DATA, ignore_errors=True); os.makedirs(DATA)
json.dump([{"usuario": "col1", "clave": "c1", "nombre": "C1", "cuenta_id": "cuenta_2", "rol": "operario"}], open(os.path.join(DATA, "usuarios.json"), "w"))
env = dict(os.environ, PORT=PORT, DATA_DIR=DATA, PICKING_API_KEY="everest2024", REPO=REPO, PEDIDOS_SNAP=os.path.join(S, "snap", "pedidos_live.json"),
           APP_SECRET_KEY="s" * 40, ML_API_URL="http://127.0.0.1:1", ML_APP_ID="x", ML_SECRET_KEY="x", PYTHONIOENCODING="utf-8")
srv = subprocess.Popen([sys.executable, os.path.join(S, "run_srv_pedidos.py")], env=env, stdout=open(os.path.join(S, "rev.log"), "w"), stderr=subprocess.STDOUT)
K = {"X-API-Key": "everest2024"}
ID = {"usuario": "col1", "cuenta_id": "cuenta_2", "canal": "flex"}
def lote(skus, colecta=None, fase=1, completa=False, **extra):
    grupos = [{"pasillo": "P1", "items": [{"sku": k, "nombre": k, "req": v, "pasillo": "P1", "estanteria": ""} for k, v in skus.items()]}]
    return {"fase": fase, "grupos": grupos, "colecta": colecta or {}, "colecta_completa": completa, "total_skus": len(skus),
            "total_uds": sum(skus.values()), "pedidos": {}, "operario": "C1", "lote_id": "L1", **ID, **extra}
def estado(): return requests.get(B + "/api/estado", params=ID).json()
def subir(p): return requests.post(B + "/api/subir_estado", headers=K, json=p).json()
def esc(sku): return requests.post(B + "/api/escanear", json={"sku": sku, **ID}).json()
def undo(sku): return requests.post(B + "/api/reset_sku", json={"sku": sku, **ID}).json()
try:
    for _ in range(80):
        try: requests.get(B + "/api/ping", timeout=1); break
        except Exception: time.sleep(.5)
    r = subir(lote({"A": 3, "B": 1}))
    check("subir_estado devuelve lote_id y rev", r.get("lote_id") == "L1" and "rev" in r, r)
    r0 = estado()["rev"]
    a = esc("A"); b = esc("A")
    check("escanear devuelve lote_id y rev creciente", a["lote_id"] == "L1" and b["rev"] > a["rev"] > r0, (r0, a, b))
    check("/api/estado trae rev", estado()["rev"] == b["rev"])
    check("escanear con JSON invalido -> 400", requests.post(B + "/api/escanear", json=[1]).status_code == 400)
    check("reset_sku con sku invalido -> 400", requests.post(B + "/api/reset_sku", json={"sku": {"x": 1}}).status_code == 400)
    # PC con base_rev viejo (2 scans antes) intenta subir; el celular ya hizo undo
    stale_rev = a["rev"] - 1
    u = undo("A")
    check("reset_sku sube rev", u["ok"] and u["rev"] > b["rev"], u)
    r = subir(lote({"A": 3, "B": 1}, colecta={"A": 2}, base_rev=stale_rev))
    e = estado()
    check("subir_estado con base_rev viejo NO revierte el undo", e["colecta"].get("A") == 1, e["colecta"])
    check("...y devuelve la colecta del servidor + rev", r.get("colecta", {}).get("A") == 1 and r["rev"] == e["rev"], r)
    r = subir(lote({"A": 3, "B": 1}, colecta={"A": 1, "B": 1}, base_rev=e["rev"]))
    check("base_rev actual -> se aplica la colecta de la PC", estado()["colecta"].get("B") == 1, estado()["colecta"])
    r = subir(lote({"A": 3, "B": 1}, colecta={"A": 3}))
    check("sin base_rev -> MAX-merge legado", estado()["colecta"].get("A") == 3, estado()["colecta"])
    # fase / colecta_completa no retroceden
    subir(lote({"A": 3, "B": 1}, colecta={"A": 3, "B": 1}, fase=2, completa=True))
    subir(lote({"A": 3, "B": 1}, colecta={"A": 3, "B": 1}, fase=1, completa=False))
    e = estado()
    check("fase no retrocede", e["fase"] == 2, e["fase"])
    check("colecta_completa no retrocede", e["colecta_completa"] is True, e["colecta_completa"])
finally:
    srv.terminate()
print(f"\n{ok} OK, {fa} FALLAS")
sys.exit(1 if fa else 0)
