"""Un lote nuevo NO hereda lo 'ya buscado' del lote anterior (ni en la PC ni en el celular)."""
import os, sys, json, time, shutil, subprocess
import requests
sys.stdout.reconfigure(encoding="utf-8")
S = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(S)
PORT = "5136"; B = f"http://127.0.0.1:{PORT}"; DATA = os.path.join(S, "data_ln")
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
srv = subprocess.Popen([sys.executable, os.path.join(S, "run_srv_pedidos.py")], env=env, stdout=open(os.path.join(S, "ln.log"), "w"), stderr=subprocess.STDOUT)
K = {"X-API-Key": "everest2024"}
def lote(skus, lote_id):
    grupos = [{"pasillo": "P1", "items": [{"sku": k, "nombre": k, "req": v, "pasillo": "P1", "estanteria": ""} for k, v in skus.items()]}]
    return {"fase": 1, "grupos": grupos, "colecta": {}, "colecta_completa": False, "total_skus": len(skus), "total_uds": sum(skus.values()),
            "pedidos": {}, "canal": "flex", "operario": "C1", "usuario": "col1", "cuenta_id": "cuenta_2", **({"lote_id": lote_id} if lote_id else {})}
def vacio():   # exactamente lo que manda _resetear_lote_completo (sin lote_id)
    return {"fase": "fase1", "grupos": [], "colecta": {}, "colecta_completa": False, "total_skus": 0, "total_uds": 0, "pedidos": {},
            "canal": "flex", "usuario": "col1", "cuenta_id": "cuenta_2"}
def estado():
    return requests.get(B + "/api/estado?usuario=col1&cuenta_id=cuenta_2&canal=flex").json()
def subir(p): return requests.post(B + "/api/subir_estado", headers=K, json=p).json()
def escanear(sku):
    return requests.post(B + "/api/escanear", json={"sku": sku, "usuario": "col1", "cuenta_id": "cuenta_2", "canal": "flex"}).json()
try:
    for _ in range(80):
        try: requests.get(B + "/api/ping", timeout=1); break
        except Exception: time.sleep(.5)

    print("servidor: lote 1 -> se busca el 4378 -> termina -> lote 2")
    subir(lote({"4378": 2, "5000": 1}, "A"))
    escanear("4378"); escanear("4378")
    check("lote 1: el celular buscó 4378 x2", estado()["colecta"].get("4378") == 2, estado()["colecta"])
    subir(vacio())                                         # el escritorio termina el lote
    e = estado()
    check("al terminar el lote, el servidor NO guarda la colecta vieja", e["colecta"] == {}, e["colecta"])
    check("...y no queda lote_id viejo", e.get("lote_id", "") == "", e.get("lote_id"))
    subir(lote({"4378": 2, "7000": 1}, "B"))
    e = estado()
    check("lote 2: 4378 empieza en 0", e["colecta"].get("4378", 0) == 0 and e["lote_id"] == "B", e["colecta"])

    print("servidor: lote 2 sin pasar por 'lote vacio' (ids distintos)")
    escanear("4378")
    subir(lote({"4378": 1, "8000": 1}, "C"))
    check("id nuevo -> colecta en 0", estado()["colecta"].get("4378", 0) == 0, estado()["colecta"])

    print("servidor: lo que ya funcionaba sigue igual")
    escanear("4378")
    subir(lote({"4378": 1, "8000": 1}, "C"))
    check("mismo lote re-subido: no pierde el escaneo del celular", estado()["colecta"].get("4378") == 1, estado()["colecta"])
    subir(lote({"4378": 1, "8000": 1, "9000": 1}, "C"))
    check("'Agregar al lote' (mismo id, más SKUs): conserva lo colectado", estado()["colecta"].get("4378") == 1 and estado()["total_skus"] == 3, estado())

    print("escritorio: no hereda la colecta del lote anterior")
    sys.path.insert(0, REPO)
    import app_deposito as A
    A.RAILWAY_URL = B
    class Raiz:
        def after(self, ms, fn=None, *a):
            if fn and ms == 0: fn()
    class Fake(A.AsistenteDepositoApp):
        def __init__(self): pass
    infos = []
    def nueva(lote_local, pedidos):
        app = Fake(); app.root = Raiz(); app.pedidos = pedidos; app.fase_actual = 1; app.colecta_global = {}
        app._lote_id_local = lote_local; app._canal_lote_activo = "flex"; app._usuario_activo = {"usuario": "col1", "cuenta_id": "cuenta_2"}
        app._en_modo_descanso = lambda: False
        app._on_sync_nube = lambda info: infos.append(info)
        app._colecta_qty = lambda sku: int(app.colecta_global.get(sku, 0) or 0)
        return app
    def sondear(app):
        infos.clear(); app._sincronizar_desde_nube()
        t0 = time.time()
        while not infos and time.time() - t0 < 8: time.sleep(.1)
        return infos[0] if infos else {}
    # el servidor todavía tiene el lote C con 4378 colectado; el escritorio acaba de generar el lote D
    ped = {1: {"skus_requeridos": {"4378": 1, "8000": 1, "9000": 1}, "skus_escaneados": {}}}
    app = nueva("D", ped)
    info = sondear(app)
    check("el servidor aún tiene OTRO lote: se ignora su colecta", info.get("ok") and info.get("colecta") == {}, info)
    app._sincronizar_colecta_movil(info["colecta"])
    check("la PC no marca 4378 como buscado", app.colecta_global == {}, app.colecta_global)
    # cuando el servidor ya tiene el lote D, sí se aplica
    subir(lote({"4378": 1, "8000": 1, "9000": 1}, "D"))
    escanear("4378")
    app = nueva("D", ped); info = sondear(app)
    check("mismo lote: se aplica lo que escaneó el celular", info.get("colecta", {}).get("4378") == 1, info)
    # sesión restaurada (sin id local): sigue como antes
    app = nueva(None, ped); info = sondear(app)
    check("sesión restaurada (sin id local): sigue sincronizando", info.get("colecta", {}).get("4378") == 1, info)
    # sin lote cargado: no tilda nada
    app = nueva("D", {})
    app._sincronizar_colecta_movil({"4378": 1})
    check("sin lote cargado no se aplica ninguna colecta", app.colecta_global == {}, app.colecta_global)
finally:
    srv.kill()
print(f"\nRESULTADO lote nuevo: {ok} ok, {fa} fallas"); sys.exit(1 if fa else 0)
