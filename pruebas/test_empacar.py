"""'Empacar siguiente': los pedidos completos (por la PC o por el CELULAR) llegan a la cola; el boton
muestra la lista de listos y el empacador empaca uno como una Fase 2 chica (escanea y sale la etiqueta)
sin sacar al lote de la Fase 1. Usa los metodos REALES de la app con un doble y pedidos reales."""
import os, sys, json
import tkinter as tk
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

TK = tk.Tk(); TK.withdraw()

class Boton:
    def __init__(self): self.cfg = {"state": "disabled", "text": ""}
    def config(self, **k): self.cfg.update(k)
    def winfo_ismapped(self): return True
    def pack(self, **k): pass
    def pack_forget(self): pass
class Etiqueta:
    def __init__(self): self.cfg = {"text": ""}
    def config(self, **k): self.cfg.update(k)
    def focus(self): pass
class Raiz:
    def after(self, ms, fn=None, *a):
        if fn and ms <= 100: fn(*a)      # lo inmediato (imprimir tras completar) corre ya
        return None
    def update_idletasks(self): pass
    def winfo_screenheight(self): return 900
class Fake(A.AsistenteDepositoApp):
    def __init__(self): pass

def nueva_app():
    app = Fake()
    app.pedidos = {}; app.fase_actual = 1; app.colecta_global = {}; app._cola_listos_pack = []
    app.btn_empacar_siguiente = Boton(); app.lbl_estado_pdf = Etiqueta(); app.root = Raiz()
    app.col_control = Etiqueta(); app.fase1_items = {}; app.pedido_en_proceso = None
    app._modo_empaque_pn = None; app._frame_pac_detalle = None
    app.lbl_fase_actual = Etiqueta(); app.lbl_num_caja = Etiqueta(); app.lbl_imprimiendo = Etiqueta()
    app.btn_cancelar = Boton(); app.btn_saltar = Boton(); app.btn_pausar = Boton(); app.entrada_sku = Etiqueta()
    marco = tk.Frame(TK); app.lbl_resultado = tk.Label(marco); app.lbl_resultado.pack()
    app.lbl_visor_info = Etiqueta()
    app.sku_nombre_ml = {}; app.db_nombres = {}; app.sku_descripciones = {}
    app.impresos = []; app.listas = []; app.log = []
    app.actualizar_contador_global = lambda: 0
    app._flash_sku_escaneado = lambda s: None
    app._actualizar_checkmarks_fase1 = lambda *a, **k: None
    app._ejecutar_transicion_fase2 = lambda: app.__dict__.setdefault("fase2", True)
    app._mostrar_caja_completa = lambda pn: None
    app.mostrar_vista_previa = lambda pn: None
    app._log_impresion = app.log.append
    app._mostrar_listos_empaque = lambda listos: app.listas.append(list(listos))
    def imprimir(pn):
        app.impresos.append(pn); app.pedidos[pn]["impreso"] = True
        if app._modo_empaque_pn == pn: app._salir_modo_empaque(volver_a_cola=False)
    app._imprimir_automatico = imprimir
    return app

def pedido(i, req, oid=None):
    return {"pagina": i, "skus_requeridos": dict(req), "skus_escaneados": {}, "impreso": False,
            "descripcion": "x", "_order_id": oid or str(1000 + i), "items": []}

snap = json.load(open(os.path.join(S, "snap", "pedidos_live.json"), encoding="utf-8"))["pedidos"]
reales = []
for p in snap:
    req, items = {}, []
    for it in p.get("items", []):
        sku = (it.get("sku") or it.get("item_id") or "").upper().strip()
        if sku:
            req[sku] = req.get(sku, 0) + it.get("cantidad", 1)
            items.append({"sku": sku, "item_id": it.get("item_id", ""), "titulo": it.get("titulo") or it.get("title", "")})
    if req: reales.append((p["order_id"], req, items))
multi = next((r for r in reales if sum(r[1].values()) >= 2), None)
reales = reales[:6]
print(f"pedidos reales usados: {len(reales)}")

def cargar(app):
    for i, (oid, req, items) in enumerate(reales, 1):
        app.pedidos[i] = pedido(i, req, oid); app.pedidos[i]["items"] = items

print("a) escaneo en la PC completa un pedido")
app = nueva_app(); cargar(app)
check("sin colecta: nada listo y boton apagado", app._listos_por_colecta() == [] and app.btn_empacar_siguiente.cfg["state"] == "disabled")
for sku, q in reales[0][1].items(): app.colecta_global[sku] = q
app._revisar_pedidos_listos()
check("pedido 1 completo -> en la cola", app._cola_listos_pack[:1] == [1], app._cola_listos_pack)
check("el boton se habilita con el contador", app.btn_empacar_siguiente.cfg["state"] == "normal" and "EMPACAR" in app.btn_empacar_siguiente.cfg["text"], app.btn_empacar_siguiente.cfg)
check("los incompletos NO entran (solo el 1)", app._cola_listos_pack == [1], app._cola_listos_pack)

print("b) la colecta llega del CELULAR (sync)")
app = nueva_app(); cargar(app)
movil = {}
for sku, q in reales[1][1].items(): movil[sku] = q
app._sincronizar_colecta_movil(movil)
check("el pedido completo por celular entra a la cola", 2 in app._cola_listos_pack, app._cola_listos_pack)
check("el boton lo muestra", app.btn_empacar_siguiente.cfg["state"] == "normal", app.btn_empacar_siguiente.cfg)
n0 = len(app._cola_listos_pack)
app._sincronizar_colecta_movil(movil)
check("repetir el sync no duplica", len(app._cola_listos_pack) == n0)

print("d) el boton muestra la lista y se empaca escaneando (Fase 2 chica)")
app._empacar_siguiente()
check("el boton abre la lista de listos y NO imprime", app.listas and 2 in app.listas[-1] and app.impresos == [], (app.listas, app.impresos))
filas = app._productos_de_pedido(2)
check("la lista trae SKU, nombre y cantidad de cada producto",
      {f[0]: f[2] for f in filas} == reales[1][1] and all(f[1] for f in filas), filas)
colecta_antes = dict(app.colecta_global)
app._entrar_modo_empaque(2)
check("elegir el pedido entra en modo empaque", app._modo_empaque_pn == 2 and app.pedido_en_proceso == 2
      and 2 not in app._cola_listos_pack and "EMPAQUE" in app.lbl_fase_actual.cfg["text"], app.lbl_fase_actual.cfg)
check("el boton pasa a VOLVER A LA COLECTA", "VOLVER" in app.btn_empacar_siguiente.cfg["text"]
      and app.btn_empacar_siguiente.cfg.get("command") == app._salir_modo_empaque, app.btn_empacar_siguiente.cfg)
app.entrada_sku.get = lambda: "ZZ-NO-ES-DEL-PAQUETE"; app.entrada_sku.delete = lambda *a: None
app.procesar_escaneo(None)
check("SKU ajeno en modo empaque: avisa y no cambia nada", app.impresos == [] and app.colecta_global == colecta_antes
      and "VOLVER A LA COLECTA" in app.lbl_resultado.cget("text"), app.lbl_resultado.cget("text"))
pasos = [s for s, q in reales[1][1].items() for _ in range(q)]
for i, s in enumerate(pasos):
    app.entrada_sku.get = lambda s=s: s
    app.procesar_escaneo(None)
    if i < len(pasos) - 1:
        check(f"escaneo {i+1}/{len(pasos)}: todavia no imprime", app.impresos == [])
check("escanear el paquete NO suma a la colecta", app.colecta_global == colecta_antes, (app.colecta_global, colecta_antes))
check("paquete completo -> imprime una vez", app.impresos == [2], app.impresos)
check("tras imprimir vuelve a la colecta", app._modo_empaque_pn is None and app.pedido_en_proceso is None
      and "FASE 1" in app.lbl_fase_actual.cfg["text"], app.lbl_fase_actual.cfg)
app._sincronizar_colecta_movil(movil)
check("el pedido ya impreso no vuelve a la cola", 2 not in app._cola_listos_pack, app._cola_listos_pack)

print("e) volver a la colecta / cancelar")
app = nueva_app(); cargar(app)
if multi:
    app.pedidos[1] = pedido(1, multi[1], multi[0]); app.pedidos[1]["items"] = multi[2]
for sku, q in app.pedidos[1]["skus_requeridos"].items(): app.colecta_global[sku] = q
app._revisar_pedidos_listos(); app._empacar_siguiente(); app._entrar_modo_empaque(1)
s1 = next(iter(app.pedidos[1]["skus_requeridos"]))
app.entrada_sku.get = lambda: s1; app.entrada_sku.delete = lambda *a: None
app.procesar_escaneo(None)
check("pedido de varias unidades: un escaneo queda a medias", app.pedidos[1]["skus_escaneados"].get(s1) == 1 and app.impresos == [])
app._salir_modo_empaque()
check("VOLVER: sale del modo, descarta lo escaneado y el pedido vuelve a la cola",
      app._modo_empaque_pn is None and app.pedidos[1]["skus_escaneados"] == {} and 1 in app._cola_listos_pack, app._cola_listos_pack)
colectas = []; _f1 = app._procesar_escaneo_fase1
app._procesar_escaneo_fase1 = lambda s: (colectas.append(s), _f1(s))
app.procesar_escaneo(None)
check("despues de volver, la pistola cuenta como colecta otra vez", colectas == [s1], colectas)
del app._procesar_escaneo_fase1
app._entrar_modo_empaque(1)
app.cancelar_paquete()
check("CANCELAR en modo empaque = volver a la colecta", app._modo_empaque_pn is None and 1 in app._cola_listos_pack)

print("f) pedidos que no se pueden empacar")
app = nueva_app()
app.pedidos[1] = pedido(1, {"A": 2, "B": 1})
app._sincronizar_colecta_movil({"A": 2})
check("falta un SKU: no se encola", app._cola_listos_pack == [])
app._empacar_siguiente()
check("con cola vacia no abre lista ni imprime", app.listas == [] and app.impresos == [])
app._cola_listos_pack = [1]               # cola vieja (sesion restaurada) con pedido que ya no alcanza
app._empacar_siguiente()
check("un pedido de la cola que ya no esta completo se descarta", app._cola_listos_pack == [] and app.listas == [])
app._entrar_modo_empaque(1)
check("y tampoco se puede entrar a empacarlo", app._modo_empaque_pn is None)
app.pedidos[1]["_impreso_local"] = True; app.colecta_global.update({"A": 2, "B": 1}); app._cola_listos_pack = [1]
app._empacar_siguiente()
check("un pedido ya impreso no se vuelve a empacar", app.listas == [] and app.impresos == [])

print("g) una unidad, dos pedidos que piden 1")
app = nueva_app()
app.pedidos[1] = pedido(1, {"SKU-X": 1}); app.pedidos[2] = pedido(2, {"SKU-X": 1}); app.pedidos[3] = pedido(3, {"SKU-X": 1, "SKU-Y": 2})
app._sincronizar_colecta_movil({"SKU-X": 1})
check("solo el primero esta listo", app._cola_listos_pack == [1], app._cola_listos_pack)
app._sincronizar_colecta_movil({"SKU-X": 2})
check("con 2 unidades, listos 1 y 2 (3 falta SKU-Y)", app._cola_listos_pack == [1, 2], app._cola_listos_pack)
app._entrar_modo_empaque(1)
app._sincronizar_colecta_movil({"SKU-X": 2})
check("el pedido que se esta empacando no vuelve a la cola con el sync", app._cola_listos_pack == [2], app._cola_listos_pack)
check("con modo empaque el boton sigue siendo VOLVER", "VOLVER" in app.btn_empacar_siguiente.cfg["text"], app.btn_empacar_siguiente.cfg)

print("h) Fase 2: sin cambios")
app = nueva_app(); app.fase_actual = 2
app.pedidos[1] = pedido(1, {"A": 1}); app.colecta_global["A"] = 1
app._revisar_pedidos_listos()
check("en Fase 2 no se encola nada", app._cola_listos_pack == [])

print("i) ventana real de la lista")
app = nueva_app(); cargar(app); app.root = TK
for sku, q in reales[0][1].items(): app.colecta_global[sku] = q
app._cargar_miniatura_async = lambda *a, **k: None
del app._mostrar_listos_empaque
app._revisar_pedidos_listos(); app._empacar_siguiente()
wins = [w for w in TK.winfo_children() if isinstance(w, tk.Toplevel)]
def textos(w):
    out = []
    for c in w.winfo_children():
        try: out.append(str(c.cget("text")))
        except Exception: pass
        out += textos(c)
    return out
t = " | ".join(textos(wins[-1])) if wins else ""
check("se abre la ventana 'Listos para empacar'", wins and wins[-1].title() == "Listos para empacar")
check("muestra la venta, cada SKU y su cantidad", str(reales[0][0]) in t and all(s in t and f"×{q}" in t for s, q in reales[0][1].items()), t[:300])
check("tiene el boton EMPACAR ESTE", "EMPACAR ESTE" in t)
def botones(w):
    out = [c for c in w.winfo_children() if isinstance(c, tk.Button)]
    for c in w.winfo_children(): out += botones(c)
    return out
emp = [b for b in (botones(wins[-1]) if wins else []) if "EMPACAR ESTE" in b.cget("text")]
if emp: emp[0].invoke(); TK.update()
check("EMPACAR ESTE cierra la lista y entra en modo empaque", app._modo_empaque_pn == 1
      and not any(isinstance(w, tk.Toplevel) and w.winfo_exists() for w in TK.winfo_children()), app._modo_empaque_pn)

TK.destroy()
print(f"\nRESULTADO empacar: {ok} ok, {fa} fallas"); sys.exit(1 if fa else 0)
