"""Lectora: el SKU escaneado se compara EXACTO con el de la venta (solo se ignoran mayusculas y espacios
invisibles/repetidos). Nunca se 'adivina' quitando simbolos. Servidor LOCAL; nada toca produccion."""
import os, sys, json, time, shutil, subprocess, types
import requests
sys.stdout.reconfigure(encoding="utf-8")
S = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(S)
PORT = "5143"; B = f"http://127.0.0.1:{PORT}"; DATA = os.path.join(S, "data_sku_lectora")
LOG = os.path.join(S, "sku_lectora.log")
ok = fa = 0
def check(n, c, x=""):
    global ok, fa
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else "  -> " + str(x)))
    if c: ok += 1
    else: fa += 1

SKUS = {"7472+PORTALAMPARA": 3, "82555 + 82556": 3, "7548 X10": 3, "CARRO +AA": 5, "BM1001BLACK  NEW": 3, "3684": 2}

shutil.rmtree(DATA, ignore_errors=True); os.makedirs(DATA)
json.dump([{"usuario": "col1", "clave": "c1", "nombre": "C1", "cuenta_id": "cuenta_2", "rol": "operario"}], open(os.path.join(DATA, "usuarios.json"), "w"))
env = dict(os.environ, PORT=PORT, DATA_DIR=DATA, PICKING_API_KEY="everest2024", REPO=REPO, PEDIDOS_SNAP=os.path.join(S, "snap", "pedidos_live.json"),
           APP_SECRET_KEY="s" * 40, ML_API_URL="http://127.0.0.1:1", ML_APP_ID="x", ML_SECRET_KEY="x", PYTHONIOENCODING="utf-8")
srv = subprocess.Popen([sys.executable, os.path.join(S, "run_srv_pedidos.py")], env=env, stdout=open(LOG, "w", encoding="utf-8"), stderr=subprocess.STDOUT)
K = {"X-API-Key": "everest2024"}
ID = {"usuario": "col1", "cuenta_id": "cuenta_2", "canal": "flex"}
def esc(sku): return requests.post(B + "/api/escanear", json={"sku": sku, **ID}).json()
def undo(sku): return requests.post(B + "/api/reset_sku", json={"sku": sku, **ID}).json()
def colecta(): return requests.get(B + "/api/estado", params=ID).json()["colecta"]
try:
    for _ in range(80):
        try: requests.get(B + "/api/ping", timeout=1); break
        except Exception: time.sleep(.5)
    grupos = [{"pasillo": "P1", "items": [{"sku": k, "nombre": k, "req": v, "pasillo": "P1", "estanteria": ""} for k, v in SKUS.items()]}]
    requests.post(B + "/api/subir_estado", headers=K, json={"fase": 1, "grupos": grupos, "colecta": {}, "total_skus": len(SKUS),
                  "total_uds": sum(SKUS.values()), "pedidos": {}, "lote_id": "LS", **ID})

    print("1) SERVIDOR (celular): el mismo SKU de la venta se marca")
    for escaneado, venta in (("7472+PORTALAMPARA", "7472+PORTALAMPARA"), ("82555 + 82556", "82555 + 82556"),
                             ("7548 X10", "7548 X10"), ("CARRO +AA", "CARRO +AA"), ("carro +aa", "CARRO +AA"),
                             ("  CARRO +AA  ", "CARRO +AA"), ("CARRO  +AA", "CARRO +AA"), ("CARRO +AA", "CARRO +AA"),
                             ("BM1001BLACK NEW", "BM1001BLACK  NEW"), ("3684", "3684")):
        r = esc(escaneado)
        check(f"escanear {escaneado!r} -> cuenta para {venta!r}", r.get("ok") and r.get("tipo") in ("parcial", "completo") and r.get("sku") == venta, r)
    c = colecta()
    check("la colecta usa el SKU EXACTO de la venta", c == {"7472+PORTALAMPARA": 1, "82555 + 82556": 1, "7548 X10": 1, "CARRO +AA": 5,
                                                        "BM1001BLACK  NEW": 1, "3684": 1}, c)

    print("2) SERVIDOR: NO se adivina (texto distinto = no está)")
    for escaneado in ("7472PORTALAMPARA", "7472]PORTALAMPARA", "7472¿PORTALAMPARA", "7548X10", "82555+82556", "CARRO+AA", "CARRO -AA"):
        r = esc(escaneado)
        check(f"escanear {escaneado!r} -> no encontrado", r.get("ok") is False and r.get("tipo") == "no_encontrado", r)
    r = esc("7472]PORTALAMPARA")
    check("el mensaje muestra lo que mandó la lectora", "la lectora envió '7472]PORTALAMPARA'" in r.get("msg", ""), r.get("msg"))
    check("la colecta no cambió por los escaneos distintos", colecta() == c, colecta())

    print("3) SERVIDOR: deshacer con la misma regla")
    u = undo("carro +aa")
    check("deshacer 'carro +aa' baja 'CARRO +AA'", u.get("ok") and colecta().get("CARRO +AA") == 4, (u, colecta()))
    u = undo("BM1001BLACK NEW")
    check("deshacer 'BM1001BLACK NEW' baja 'BM1001BLACK  NEW'", u.get("ok") and "BM1001BLACK  NEW" not in colecta(), (u, colecta()))
    u = undo("7472PORTALAMPARA")
    check("deshacer de un texto distinto no toca nada", u.get("ok") is False and colecta().get("7472+PORTALAMPARA") == 1, (u, colecta()))
finally:
    srv.terminate()
    try: srv.wait(10)
    except Exception: srv.kill()
log = open(LOG, encoding="utf-8", errors="replace").read()
check("el log del servidor registra [SCAN?] con el texto crudo y la pista de la venta",
      "[SCAN?]" in log and "'7472]PORTALAMPARA'" in log and "7472+PORTALAMPARA" in log.split("'7472]PORTALAMPARA'", 1)[1][:200], log[-600:])

print("4) PC (escritorio): misma regla en procesar_escaneo / mensaje de 'no está'")
sys.path.insert(0, REPO)
import app_deposito as A
C = A.AsistenteDepositoApp
check("sku_canon no quita simbolos ni espacios reales", A.sku_canon(" carro  +aa ") == "CARRO +AA" and A.sku_canon("7472+PORTALAMPARA") != A.sku_canon("7472 PORTALAMPARA"))
class Entrada:
    def __init__(self, v): self.v = v
    def get(self): return self.v
    def delete(self, *a): self.v = ""
def nueva(fase=1):
    o = types.SimpleNamespace()
    o.pedidos = {1: {"skus_requeridos": {"CARRO +AA": 2, "BM1001BLACK  NEW": 1, "7472+PORTALAMPARA": 1}}}
    o.fase_actual = fase
    o.registro = []; o.log = []
    o._procesar_escaneo_fase1 = lambda s: o.registro.append(s)
    o._sku_del_lote = lambda s: C._sku_del_lote(o, s)
    o._log_impresion = lambda m: o.log.append(m)
    return o
for escaneado, esperado in (("carro +aa", "CARRO +AA"), ("CARRO  +AA", "CARRO +AA"), ("BM1001BLACK NEW", "BM1001BLACK  NEW"),
                            ("7472+PORTALAMPARA", "7472+PORTALAMPARA"), ("7472]PORTALAMPARA", "7472]PORTALAMPARA"),
                            ("CARRO+AA", "CARRO+AA")):
    o = nueva(); o.entrada_sku = Entrada(escaneado)
    C.procesar_escaneo(o, None)
    check(f"PC: {escaneado!r} llega a la colecta como {esperado!r}", o.registro == [esperado], o.registro)
o = nueva(); o._ultimo_escaneo_crudo = "7472]PORTALAMPARA"
t = C._detalle_no_encontrado(o, "7472]PORTALAMPARA")
check("PC: el aviso muestra lo que mandó la lectora", "la lectora envió '7472]PORTALAMPARA'" in t, t)
check("PC: picking_print.log anota [SCAN?] con la pista del SKU de la venta", o.log and "[SCAN?]" in o.log[0] and "7472+PORTALAMPARA" in o.log[0], o.log)
o = nueva(); o._ultimo_escaneo_crudo = "ABC123"
check("PC: un SKU común sin símbolos no agrega texto extra", C._detalle_no_encontrado(o, "ABC123") == "")
print(f"\n{ok} OK, {fa} FALLAS"); sys.exit(1 if fa else 0)
