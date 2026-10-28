"""Consumo de Railway: la planilla se baja solo si cambió y los sondeos responden 304 cuando nada cambió.
Servidor LOCAL; nada toca produccion."""
import os, sys, io, json, time, base64, shutil, subprocess, threading, types
import requests
from openpyxl import Workbook
sys.stdout.reconfigure(encoding="utf-8")
S = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(S)
PORT = "5147"; B = f"http://127.0.0.1:{PORT}"; DATA = os.path.join(S, "data_consumo")
ok = fa = 0
def check(n, c, x=""):
    global ok, fa
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else "  -> " + str(x)))
    if c: ok += 1
    else: fa += 1

def planilla(filas):
    wb = Workbook(); ws = wb.active; ws.title = "productos"
    ws.append(["ID", "SKU", "NOMBRE", "VERTICAL", "PASILLOS"])
    for f in filas: ws.append(f)
    b = io.BytesIO(); wb.save(b); return b.getvalue()

shutil.rmtree(DATA, ignore_errors=True); os.makedirs(DATA)
env = dict(os.environ, PORT=PORT, DATA_DIR=DATA, PICKING_API_KEY="everest2024", REPO=REPO,
           PEDIDOS_SNAP=os.path.join(S, "snap", "pedidos_live.json"), APP_SECRET_KEY="s" * 40,
           ML_API_URL="http://127.0.0.1:1", ML_APP_ID="x", ML_SECRET_KEY="x", PYTHONIOENCODING="utf-8")
srv = subprocess.Popen([sys.executable, os.path.join(S, "run_srv_pedidos.py")], env=env,
                       stdout=open(os.path.join(S, "consumo.log"), "w", encoding="utf-8"), stderr=subprocess.STDOUT)
K = {"X-API-Key": "everest2024"}
ID = {"usuario": "col1", "cuenta_id": "cuenta_2", "canal": "flex"}
try:
    for _ in range(80):
        try: requests.get(B + "/api/ping", timeout=1); break
        except Exception: time.sleep(.5)

    print("1) PLANILLA: se baja solo si cambió")
    x1 = planilla([[1, "SKU-A", "Producto A", "V", "P1 COCINA"], [2, "SKU-B", "Producto B", "V", "P2 BAÑO"]])
    r = requests.post(B + "/api/config-app/excel", headers=K, json={"excel_b64": base64.b64encode(x1).decode(), "filename": "PLANILLA DE PRUEBA.xlsx"})
    ts1 = r.json().get("ts", "")
    check("subir planilla -> ok con ts con segundos", r.ok and len(ts1) == 19, r.text[:200])
    check("/archivo sin clave -> 401", requests.get(B + "/api/config-app/excel/archivo").status_code == 401)
    r = requests.get(B + "/api/config-app/excel/archivo", headers=K)
    check("/archivo sin ts -> el xlsx byte a byte", r.status_code == 200 and r.content == x1)
    check("/archivo informa ts y nombre", requests.utils.unquote(r.headers.get("X-Excel-Ts", "")) == ts1
          and requests.utils.unquote(r.headers.get("X-Excel-Filename", "")) == "PLANILLA DE PRUEBA.xlsx", dict(r.headers))
    r = requests.get(B + "/api/config-app/excel/archivo", headers=K, params={"ts": ts1})
    check("/archivo con el mismo ts -> 304 sin cuerpo", r.status_code == 304 and not r.content, (r.status_code, len(r.content)))
    r = requests.get(B + "/api/config-app/excel/archivo", headers=K, params={"ts": "2020-01-01 00:00:00"})
    check("/archivo con otro ts -> 200", r.status_code == 200 and r.content == x1)
    r = requests.get(B + "/api/config-app/excel", headers=K).json()
    check("endpoint viejo (exe <=4.1.32) sin cambios", r.get("ok") and base64.b64decode(r["excel_b64"]) == x1 and r.get("ts") == ts1)
    time.sleep(1.1)
    x2 = planilla([[1, "SKU-A", "Producto A", "V", "P9 NUEVO"]])
    ts2 = requests.post(B + "/api/config-app/excel", headers=K, json={"excel_b64": base64.b64encode(x2).decode(), "filename": "P2.xlsx"}).json()["ts"]
    r = requests.get(B + "/api/config-app/excel/archivo", headers=K, params={"ts": ts1})
    check("planilla nueva -> con el ts viejo se baja la nueva", ts2 != ts1 and r.status_code == 200 and r.content == x2)

    print("2) SONDEOS: 304 cuando nada cambió, igual que siempre sin If-None-Match")
    grupos = [{"pasillo": "P1", "items": [{"sku": "SKU-A", "nombre": "A", "req": 2, "pasillo": "P1", "estanteria": ""}]}]
    requests.post(B + "/api/subir_estado", headers=K, json={"fase": 1, "grupos": grupos, "colecta": {}, "total_skus": 1,
                  "total_uds": 2, "pedidos": {}, "lote_id": "LC", **ID})
    for ruta, params in (("/api/estado", ID), ("/api/pedidos", None), ("/api/pedidos/estados", None)):
        r1 = requests.get(B + ruta, params=params, headers=K)
        et = r1.headers.get("ETag")
        check(f"{ruta}: 200 con ETag", r1.status_code == 200 and et, (r1.status_code, dict(r1.headers)))
        r2 = requests.get(B + ruta, params=params, headers={**K, "If-None-Match": et or "x"})
        check(f"{ruta}: mismo ETag -> 304 sin cuerpo", r2.status_code == 304 and not r2.content, (r2.status_code, r2.content[:80]))
        r3 = requests.get(B + ruta, params=params, headers={**K, "If-None-Match": '"otro"'})
        check(f"{ruta}: ETag distinto -> 200 con el mismo cuerpo", r3.status_code == 200 and r3.json() == r1.json())
    et = requests.get(B + "/api/pedidos/estados", headers=K).headers.get("ETag"); time.sleep(1.2)
    check("/api/pedidos/estados: su hora no cambia el ETag (304 un segundo después)",
          requests.get(B + "/api/pedidos/estados", headers={**K, "If-None-Match": et}).status_code == 304)
    et = requests.get(B + "/api/estado", params=ID).headers.get("ETag")
    requests.post(B + "/api/escanear", json={"sku": "SKU-A", **ID})
    r = requests.get(B + "/api/estado", params=ID, headers={"If-None-Match": et})
    check("tras un escaneo el ETag cambia -> 200 con la colecta nueva", r.status_code == 200 and r.json()["colecta"].get("SKU-A") == 1, (r.status_code, r.text[:200]))

    print("3) /api/estado: leer no crea lotes")
    antes = set(requests.get(B + "/api/estado", params=ID).json()["canales_disponibles"])
    for i in range(5):
        r = requests.get(B + "/api/estado", params={"usuario": f"nadie{i}", "cuenta_id": "cuenta_2", "canal": "flex"})
        assert r.ok and r.json()["cargado"] is False
    despues = set(requests.get(B + "/api/estado", params=ID).json()["canales_disponibles"])
    check("consultar 5 usuarios sin lote no agrega entradas", antes == despues, (antes, despues))

    print("4) APP DE ESCRITORIO 4.1.33")
    sys.path.insert(0, REPO)
    import app_deposito as A
    url = B + "/api/estado?canal=flex&usuario=col1&cuenta_id=cuenta_2"
    d1 = A._get_json_sondeo(url)
    check("_get_json_sondeo: primera vez trae el estado y guarda el ETag", d1.get("lote_id") == "LC" and url in A._sondeo_etags)
    et, _ = A._sondeo_etags[url]
    A._sondeo_etags[url] = (et, json.dumps({"marca": "cache"}).encode())
    check("_get_json_sondeo: sin cambios usa lo guardado (llegó 304)", A._get_json_sondeo(url) == {"marca": "cache"})
    requests.post(B + "/api/escanear", json={"sku": "SKU-A", **ID})
    d3 = A._get_json_sondeo(url)
    check("_get_json_sondeo: con cambios trae lo nuevo", d3.get("colecta", {}).get("SKU-A") == 2, d3.get("colecta"))
    check("_get_json_sondeo: /api/pedidos con clave", A._get_json_sondeo(B + "/api/pedidos", headers=K).get("ok") is True)

    # Planilla desde la app: doble mínimo de la ventana (solo lo que usa _cargar_excel_desde_railway)
    tmpd = os.path.join(DATA, "pc"); os.makedirs(tmpd)
    A.EXCEL_CACHE_PATH = os.path.join(tmpd, "excel_cache.json"); A.EXCEL_PLANILLA_PATH = os.path.join(tmpd, "excel_planilla.json")
    log = []
    class Root:
        def after(self, ms, fn): fn()
    fake = types.SimpleNamespace(config={"servidor_nube": B, "clave_nube": "everest2024"}, db_nombres={},
                                 root=Root(), pedidos={}, fase_actual=1, _log_impresion=log.append)
    class HiloInline:
        def __init__(self, target, **kw): self.t = target
        def start(self): self.t()
    _orig = threading.Thread
    cls = next(c for c in vars(A).values() if isinstance(c, type) and hasattr(c, "_cargar_excel_desde_railway"))
    def correr():
        log.clear(); threading.Thread = HiloInline
        try: cls._cargar_excel_desde_railway(fake)
        finally: threading.Thread = _orig
    fake.db_nombres = {}
    if os.path.exists(A.EXCEL_CACHE_PATH): os.remove(A.EXCEL_CACHE_PATH)
    if os.path.exists(A.EXCEL_PLANILLA_PATH): os.remove(A.EXCEL_PLANILLA_PATH)
    correr()
    check("app: primera vez baja la planilla y la aplica", fake.db_nombres.get("SKU-A", {}).get("pasillo") == "P9 NUEVO", (log, fake.db_nombres))
    check("app: guarda la planilla con su versión", A.cargar_planilla_local()[0] == ts2)
    fake.db_nombres = {"SKU-A": {"nombre": "x", "pasillo": "BD INTERNA VIEJA"}}   # como si la BD interna lo pisara al abrir
    correr()
    check("app: segunda apertura -> 'sin cambios', no baja nada", any("sin cambios (" in l.lower() for l in log)
          and not any("Parseando" in l for l in log), log)
    check("app: sin cambios igual aplica la planilla con prioridad (le gana a la BD interna)",
          fake.db_nombres.get("SKU-A", {}).get("pasillo") == "P9 NUEVO", fake.db_nombres)
    open(A.EXCEL_PLANILLA_PATH, "w").write('{"ts": "x", "skus": {'); fake.db_nombres = {}; correr()
    check("app: planilla guardada dañada -> la vuelve a bajar", any("Parseando" in l for l in log)
          and fake.db_nombres.get("SKU-A", {}).get("pasillo") == "P9 NUEVO", log)
    time.sleep(1.1)
    x3 = planilla([[1, "SKU-A", "Producto A", "V", "P5 OTRO"], [3, "SKU-C", "Producto C", "V", "P6"]])
    ts3 = requests.post(B + "/api/config-app/excel", headers=K, json={"excel_b64": base64.b64encode(x3).decode(), "filename": "P3.xlsx"}).json()["ts"]
    correr()
    check("app: planilla nueva en el servidor -> la baja y aplica", fake.db_nombres.get("SKU-A", {}).get("pasillo") == "P5 OTRO"
          and "SKU-C" in fake.db_nombres and A.cargar_planilla_local()[0] == ts3, log)
finally:
    srv.terminate()
    try: srv.wait(10)
    except Exception: srv.kill()

print(f"\n{ok} OK, {fa} FALLAS")
sys.exit(1 if fa else 0)
