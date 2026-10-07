"""'Empacar siguiente': los pedidos completos (por la PC o por el CELULAR) llegan a la cola y
el boton los empaca. Usa los metodos REALES de la app con un doble sin Tk y pedidos reales."""
import os, sys, json
sys.stdout.reconfigure(encoding="utf-8")
S = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(S)
sys.path.insert(0, REPO)
import app_deposito as A
ok = fa = 0
def check(n, c, x=""):
    global ok, fa
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else "  -> " + str(x)))
    if c: ok += 1
    else: fa += 1

class Boton:
    def __init__(self): self.cfg = {"state": "disabled", "text": ""}
    def config(self, **k): self.cfg.update(k)
    def winfo_ismapped(self): return True
    def pack(self, **k): pass
    def pack_forget(self): pass
class Etiqueta:
    def config(self, **k): pass
class Raiz:
    def after(self, ms, fn=None, *a): return None
    def update_idletasks(self): pass
class Fake(A.AsistenteDepositoApp):
    def __init__(self): pass

def nueva_app():
    app = Fake()
    app.pedidos = {}; app.fase_actual = 1; app.colecta_global = {}; app._cola_listos_pack = []
    app.btn_empacar_siguiente = Boton(); app.lbl_estado_pdf = Etiqueta(); app.root = Raiz()
    app.col_control = Etiqueta(); app.fase1_items = {}; app.pedido_en_proceso = None
    app.impresos = []
    app.actualizar_contador_global = lambda: 0
    app._flash_sku_escaneado = lambda s: None
    app._actualizar_checkmarks_fase1 = lambda *a, **k: None
    app._ejecutar_transicion_fase2 = lambda: app.__dict__.setdefault("fase2", True)
    def imprimir(pn):
        app.impresos.append(pn); app.pedidos[pn]["impreso"] = True
    app._imprimir_automatico = imprimir
    return app

def pedido(i, req, oid=None):
    return {"pagina": i, "skus_requeridos": dict(req), "skus_escaneados": {}, "impreso": False,
            "descripcion": "x", "_order_id": oid or str(1000 + i), "items": []}

snap = json.load(open(os.path.join(S, "snap", "pedidos_live.json"), encoding="utf-8"))["pedidos"]
reales = []
for p in snap:
    req = {}
    for it in p.get("items", []):
        sku = (it.get("sku") or it.get("item_id") or "").upper().strip()
        if sku: req[sku] = req.get(sku, 0) + it.get("cantidad", 1)
    if req: reales.append((p["order_id"], req))
reales = reales[:6]
print(f"pedidos reales usados: {len(reales)}")

print("a) escaneo en la PC completa un pedido")
app = nueva_app()
for i, (oid, req) in enumerate(reales, 1): app.pedidos[i] = pedido(i, req, oid)
check("sin colecta: nada listo y boton apagado", app._listos_por_colecta() == [] and app.btn_empacar_siguiente.cfg["state"] == "disabled")
for sku, q in reales[0][1].items(): app.colecta_global[sku] = q
app._revisar_pedidos_listos()
check("pedido 1 completo -> en la cola", app._cola_listos_pack[:1] == [1], app._cola_listos_pack)
check("el boton se habilita con el contador", app.btn_empacar_siguiente.cfg["state"] == "normal" and "EMPACAR" in app.btn_empacar_siguiente.cfg["text"], app.btn_empacar_siguiente.cfg)
check("los incompletos NO entran (solo el 1)", app._cola_listos_pack == [1], app._cola_listos_pack)

print("b) la colecta llega del CELULAR (sync)")
app = nueva_app()
for i, (oid, req) in enumerate(reales, 1): app.pedidos[i] = pedido(i, req, oid)
movil = {}
for sku, q in reales[1][1].items(): movil[sku] = q
app._sincronizar_colecta_movil(movil)
check("el pedido completo por celular entra a la cola", 2 in app._cola_listos_pack, app._cola_listos_pack)
check("el boton lo muestra", app.btn_empacar_siguiente.cfg["state"] == "normal", app.btn_empacar_siguiente.cfg)
n0 = len(app._cola_listos_pack)
app._sincronizar_colecta_movil(movil)
check("repetir el sync no duplica", len(app._cola_listos_pack) == n0)

print("d) el boton empaca y no reaparece")
app._empacar_siguiente()
check("tomo un pedido listo y lo mando a imprimir", len(app.impresos) == 1 and app.impresos[0] in (2, 1, 3, 4, 5, 6), app.impresos)
cola_antes = list(app._cola_listos_pack)
app._sincronizar_colecta_movil(movil)
check("el pedido ya impreso no vuelve a la cola", app.impresos[0] not in app._cola_listos_pack, app._cola_listos_pack)
while app._cola_listos_pack: app._empacar_siguiente()
check("vaciar la cola: cada pedido se imprimio una sola vez", len(app.impresos) == len(set(app.impresos)), app.impresos)
check("boton apagado cuando no quedan", app.btn_empacar_siguiente.cfg["state"] == "disabled")

print("c) una unidad, dos pedidos que piden 1")
app = nueva_app()
app.pedidos[1] = pedido(1, {"SKU-X": 1}); app.pedidos[2] = pedido(2, {"SKU-X": 1}); app.pedidos[3] = pedido(3, {"SKU-X": 1, "SKU-Y": 2})
app._sincronizar_colecta_movil({"SKU-X": 1})
check("solo el primero esta listo", app._cola_listos_pack == [1], app._cola_listos_pack)
app._sincronizar_colecta_movil({"SKU-X": 2})
check("con 2 unidades, listos 1 y 2 (3 falta SKU-Y)", app._cola_listos_pack == [1, 2], app._cola_listos_pack)

print("e) incompletos y casos limite")
app = nueva_app()
app.pedidos[1] = pedido(1, {"A": 2, "B": 1})
app._sincronizar_colecta_movil({"A": 2})
check("falta un SKU: no se encola", app._cola_listos_pack == [])
app._empacar_siguiente()
check("el boton con cola vacia no imprime ni falla", app.impresos == [])
app._cola_listos_pack = [1]               # cola vieja (sesion restaurada) con pedido que ya no alcanza
app._empacar_siguiente()
check("un pedido de la cola que ya no esta completo se descarta sin imprimir", app.impresos == [] and app._cola_listos_pack == [])
app.pedidos[1]["_impreso_local"] = True; app.colecta_global.update({"A": 2, "B": 1}); app._cola_listos_pack = [1]
app._empacar_siguiente()
check("un pedido ya impreso no se vuelve a empacar", app.impresos == [])

print("f) Fase 2: sin cambios")
app = nueva_app(); app.fase_actual = 2
app.pedidos[1] = pedido(1, {"A": 1}); app.colecta_global["A"] = 1
app._revisar_pedidos_listos()
check("en Fase 2 no se encola nada", app._cola_listos_pack == [])
print(f"\nRESULTADO empacar: {ok} ok, {fa} fallas"); sys.exit(1 if fa else 0)
