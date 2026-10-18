"""Sincronizacion rev/base_rev (opcion B): fusion por SKU, rev que no retrocede, entradas raras sin 500,
lote que crece, reanudar sesion, persistencia atomica. Servidor LOCAL con pedidos reales; nada toca produccion."""
import os, sys, json, time, shutil, subprocess, threading
import requests
sys.stdout.reconfigure(encoding="utf-8")
S = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(S)
PORT = "5141"; B = f"http://127.0.0.1:{PORT}"; DATA = os.path.join(S, "data_revb")
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
srv = None
def arrancar():
    global srv
    srv = subprocess.Popen([sys.executable, os.path.join(S, "run_srv_pedidos.py")], env=env,
                           stdout=open(os.path.join(S, "revb.log"), "a"), stderr=subprocess.STDOUT)
    for _ in range(100):
        try: requests.get(B + "/api/ping", timeout=1); return
        except Exception: time.sleep(.3)
def parar():
    srv.terminate()
    try: srv.wait(10)
    except Exception: srv.kill()

K = {"X-API-Key": "everest2024"}
ID = {"usuario": "col1", "cuenta_id": "cuenta_2", "canal": "flex"}
def lote(skus, colecta=None, fase=1, lote_id="L1", **extra):
    grupos = [{"pasillo": "P1", "items": [{"sku": k, "nombre": k, "req": v, "pasillo": "P1", "estanteria": ""} for k, v in skus.items()]}]
    return {"fase": fase, "grupos": grupos, "colecta": colecta or {}, "colecta_completa": False, "total_skus": len(skus),
            "total_uds": sum(skus.values()), "pedidos": {}, "operario": "C1", "lote_id": lote_id, **ID, **extra}
def estado(): return requests.get(B + "/api/estado", params=ID).json()
def subir(p, **kw): return requests.post(B + "/api/subir_estado", headers=K, json=p, **kw)
def esc(sku): return requests.post(B + "/api/escanear", json={"sku": sku, **ID}).json()
def undo(sku): return requests.post(B + "/api/reset_sku", json={"sku": sku, **ID}).json()
def limpiar(): requests.post(B + "/api/limpiar", headers=K, json=ID)

try:
    arrancar()

    print("1) fase como texto / valores raros: nunca 500")
    r = subir(lote({"A": 3}, fase="fase1", lote_id="LA")); check("lote con fase 'fase1' -> 200", r.status_code == 200, r.text[:200])
    check("la fase se guarda como numero (1)", estado()["fase"] == 1, estado()["fase"])
    for f in ("fase1", 1, "fase2", "2", [1], {"a": 1}, None, 0, 99, 2.5, True):
        r = subir(lote({"A": 3}, fase=f, lote_id="LA"))
        check(f"mismo lote re-subido con fase={f!r} -> 200", r.status_code == 200, (r.status_code, r.text[:120]))
    check("tras fases raras la fase sigue siendo 1 o 2", estado()["fase"] in (1, 2), estado()["fase"])
    r = subir({"fase": "fase1", "grupos": [], **ID}); check("vaciar con 'fase1' -> 200", r.status_code == 200)
    check("y queda fase numerica", estado()["fase"] == 1, estado()["fase"])

    print("2) base_rev raro y cuerpos invalidos")
    subir(lote({"A": 3}, lote_id="LB"))
    for br in ("abc", -1, None, True, [1], {"x": 1}):
        r = subir(lote({"A": 3}, lote_id="LB", base_rev=br)); check(f"base_rev={br!r} -> 200", r.status_code == 200, (r.status_code, r.text[:100]))
    for crudo in ('Infinity', '-Infinity', '1e999', 'NaN'):
        cuerpo = json.dumps(lote({"A": 3}, lote_id="LB")).replace('"operario"', f'"base_rev": {crudo}, "operario"')
        r = requests.post(B + "/api/subir_estado", headers={**K, "Content-Type": "application/json"}, data=cuerpo)
        check(f"base_rev={crudo} (JSON crudo) -> 200", r.status_code == 200, (r.status_code, r.text[:100]))
    for nombre, cuerpo in (("lista", [1, 2]), ("numero", 5), ("texto", "x"), ("grupos texto", {"grupos": "x"}), ("grupos nulo", {"grupos": None})):
        r = requests.post(B + "/api/subir_estado", headers=K, json=cuerpo); check(f"cuerpo {nombre} -> 400", r.status_code == 400, r.status_code)
    r = subir(lote({"A": 3}, colecta={"A": "xx", "B": [1], "C": None, "D": 2}, lote_id="LB"))
    check("colecta con valores no numericos -> 200 (se ignoran)", r.status_code == 200, r.status_code)

    print("3) subidas solapadas de la PC con el MISMO base_rev: no se pierde ningun escaneo")
    limpiar(); subir(lote({"A": 3, "B": 3, "C": 3}, lote_id="LC"))
    r0 = estado()["rev"]
    subir(lote({"A": 3, "B": 3, "C": 3}, colecta={"A": 1}, lote_id="LC", base_rev=r0))
    r = subir(lote({"A": 3, "B": 3, "C": 3}, colecta={"A": 1, "B": 1}, lote_id="LC", base_rev=r0)).json()
    check("la segunda subida (base viejo) conserva A y B", estado()["colecta"] == {"A": 1, "B": 1}, estado()["colecta"])
    check("...y la respuesta trae la colecta fusionada", r.get("stale") is True and r.get("colecta") == {"A": 1, "B": 1}, r)
    r1 = estado()["rev"]
    subir(lote({"A": 3, "B": 3, "C": 3}, colecta={"A": 2, "B": 1}, lote_id="LC", base_rev=r0))
    check("dos escaneos seguidos del mismo SKU en la PC (A=2) no los revierte el servidor", estado()["colecta"].get("A") == 2, estado()["colecta"])

    print("4) un escaneo/deshacer del celular DESPUES de lo que vio la PC gana en ese SKU, y lo demas de la PC se conserva")
    limpiar(); subir(lote({"A": 3, "B": 3}, lote_id="LD"))
    base = estado()["rev"]
    esc("A"); esc("A"); u = undo("A")                 # el celular: A=2 y luego deshace -> A=1
    r = subir(lote({"A": 3, "B": 3}, colecta={"A": 2, "B": 1}, lote_id="LD", base_rev=base)).json()
    c = estado()["colecta"]
    check("A queda como lo dejo el celular (1), no como lo traia la PC vieja (2)", c.get("A") == 1, c)
    check("B (solo de la PC) se conserva", c.get("B") == 1, c)
    check("con base_rev al dia se aplica la PC normalmente (MAX)", subir(lote({"A": 3, "B": 3}, colecta={"A": 1, "B": 2}, lote_id="LD", base_rev=estado()["rev"])).status_code == 200 and estado()["colecta"].get("B") == 2)
    check("/api/estado no expone sku_rev", "sku_rev" not in estado())

    print("5) rev no retrocede aunque el servidor reinicie sin restaurar el lote")
    limpiar(); subir(lote({"A": 1}, lote_id="LE"))
    e1 = esc("A"); rev_viejo = e1["rev"]
    check("colecta completa -> fase 2", estado()["fase"] == 2 and estado()["colecta_completa"])
    parar(); arrancar()
    check("tras el reinicio el lote completo no se restaura", estado().get("cargado") is False, estado().get("cargado"))
    r = subir(lote({"A": 1}, lote_id="LE")).json()
    check("al volver a subir el mismo lote el rev es MAYOR que el de antes", r["rev"] > rev_viejo, (r["rev"], rev_viejo))
    check("...y la colecta NO se hereda (queda en 0)", estado()["colecta"] == {}, estado()["colecta"])
    esc("A") if False else None

    print("6) 'Agregar al lote': el lote crece y la completitud/fase se recalculan")
    limpiar(); subir(lote({"A": 1}, lote_id="LF"))
    esc("A")
    check("completo y en fase 2", estado()["colecta_completa"] and estado()["fase"] == 2)
    r = subir(lote({"A": 1, "B": 1}, colecta={"A": 1}, lote_id="LF", fase=1))
    e = estado()
    check("el lote crecio: ya no esta completo y vuelve a fase 1", r.status_code == 200 and e["colecta_completa"] is False and e["fase"] == 1, (e["colecta_completa"], e["fase"]))
    check("la colecta de A se conserva", e["colecta"].get("A") == 1, e["colecta"])
    esc("B")
    e = estado()
    check("al colectar lo agregado vuelve a completarse (fase 2)", e["colecta_completa"] and e["fase"] == 2, (e["colecta_completa"], e["fase"]))
    r = subir(lote({"A": 1, "B": 1}, colecta={"A": 1, "B": 1}, lote_id="LF", fase=1))
    check("sin crecer, la fase 2 no retrocede", estado()["fase"] == 2 and estado()["colecta_completa"])

    print("7) reanudar sesion: solo con el aviso explicito se acepta la colecta de un lote que el servidor no tiene")
    limpiar()
    subir(lote({"A": 3, "B": 3}, colecta={"A": 2, "Z": 5}, lote_id="LG", reanudar=True))
    c = estado()["colecta"]
    check("con reanudar: se acepta la colecta de los SKU del lote (Z, ajeno, se descarta)", c == {"A": 2}, c)
    limpiar()
    subir(lote({"A": 3, "B": 3}, colecta={"A": 2}, lote_id="LH"))
    check("SIN reanudar: un lote nuevo no hereda colecta", estado()["colecta"] == {}, estado()["colecta"])
    subir(lote({"A": 3, "B": 3}, colecta={"A": 2}, lote_id="LH2", reanudar=False))
    check("reanudar=false tampoco", estado()["colecta"] == {}, estado()["colecta"])
    subir(lote({"A": 3, "B": 3}, colecta={"A": 2}, lote_id="LH3", reanudar="true"))
    check("reanudar='true' (texto) tampoco: solo el booleano true", estado()["colecta"] == {}, estado()["colecta"])

    print("8) persistencia: lote_estado.json valido tras muchos escaneos simultaneos, sin temporales")
    limpiar(); subir(lote({"A": 500, "B": 500}, lote_id="LI"))
    def hilo(sku):
        for _ in range(40): esc(sku)
    ts = [threading.Thread(target=hilo, args=(s,)) for s in ("A", "B", "A", "B", "A", "B")]
    [t.start() for t in ts]
    for _ in range(5): subir(lote({"A": 500, "B": 500}, lote_id="LI", base_rev=estado()["rev"]))
    [t.join() for t in ts]
    c = estado()["colecta"]
    check("ningun escaneo perdido (A=120, B=120)", c == {"A": 120, "B": 120}, c)
    ruta = os.path.join(DATA, "lote_estado.json")
    try:
        d = json.load(open(ruta, encoding="utf-8")); valido = True
    except Exception as e:
        valido = False; d = {"canales": {}}
    check("lote_estado.json es JSON valido", valido)
    check("el archivo en disco tiene el mismo rev que la memoria", d.get("canales", {}).get("cuenta_2:col1", {}).get("rev") == estado()["rev"],
          (d.get("canales", {}).get("cuenta_2:col1", {}).get("rev"), estado()["rev"]))
    check("no quedan archivos temporales", not [f for f in os.listdir(DATA) if f.endswith(".tmp")], os.listdir(DATA))
finally:
    try: parar()
    except Exception: pass
print(f"\n{ok} OK, {fa} FALLAS"); sys.exit(1 if fa else 0)
