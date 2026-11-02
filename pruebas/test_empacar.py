"""'Empacar siguiente': los pedidos completos (por la PC o por el CELULAR) llegan a la cola; el boton
muestra todas las ventas listas y PREPARAR las empaca juntas como una Fase 2 chica (escaneo libre: la app
abre la venta del producto, al completarla sale la etiqueta y sigue) sin sacar al lote de la Fase 1.
Usa los metodos REALES de la app con un doble y pedidos reales."""
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
    app._modo_empaque_pn = None; app._tanda_empaque = []; app._frame_pac_detalle = None
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
        if app._modo_empaque_pn == pn: app._cerrar_paquete_tanda(pn)
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

def escanear(app, sku):
    app.entrada_sku.get = lambda: sku; app.entrada_sku.delete = lambda *a: None
    app.procesar_escaneo(None)

print("d) PREPARAR toma TODAS las listas y se escanea libre (Fase 2 chica)")
app = nueva_app()
app.pedidos[1] = pedido(1, {"SEC": 1}); app.pedidos[2] = pedido(2, {"MOC": 1, "LIN": 2})
app.pedidos[3] = pedido(3, {"AUT": 1}); app.pedidos[4] = pedido(4, {"FALTA": 1})
app._sincronizar_colecta_movil({"SEC": 1, "MOC": 1, "LIN": 2, "AUT": 1})
check("3 listas en la cola (la 4 no)", app._cola_listos_pack == [1, 2, 3], app._cola_listos_pack)
app._empacar_siguiente()
check("el boton abre la lista con TODAS las listas y NO imprime", app.listas == [[1, 2, 3]] and app.impresos == [], app.listas)
colecta_antes = dict(app.colecta_global)
app._entrar_tanda(app.listas[-1])
check("PREPARAR arma la tanda con las 3 y las saca de la cola", app._tanda_empaque == [1, 2, 3]
      and app._cola_listos_pack == [] and app._modo_empaque_pn is None, (app._tanda_empaque, app._cola_listos_pack))
check("rotulo EMPAQUE y boton VOLVER A LA COLECTA (quedan 3)", "EMPAQUE" in app.lbl_fase_actual.cfg["text"]
      and "VOLVER" in app.btn_empacar_siguiente.cfg["text"] and "quedan 3" in app.btn_empacar_siguiente.cfg["text"]
      and app.btn_empacar_siguiente.cfg.get("command") == app._salir_modo_empaque, app.btn_empacar_siguiente.cfg)
check("espera escaneo mostrando la tanda", "TANDA" in app.lbl_num_caja.cfg["text"] and app._frame_pac_detalle is not None,
      app.lbl_num_caja.cfg)
escanear(app, "ZZ-NO-ES")
check("SKU ajeno a la tanda: avisa, no abre nada, no colecta", app._modo_empaque_pn is None and app.impresos == []
      and app.colecta_global == colecta_antes and "VOLVER A LA COLECTA" in app.lbl_resultado.cget("text"),
      app.lbl_resultado.cget("text"))
escanear(app, "LIN")
check("escanear un producto de la 2.a abre la 2.a (no hace falta elegir)", app._modo_empaque_pn == 2
      and app.pedido_en_proceso == 2 and app.pedidos[2]["skus_escaneados"] == {"LIN": 1}, app.pedidos[2]["skus_escaneados"])
escanear(app, "SEC")
check("con paquete abierto, un SKU de otra venta avisa 'termina esta primero'", app._modo_empaque_pn == 2
      and app.pedidos[1]["skus_escaneados"] == {} and "otra venta" in app.lbl_resultado.cget("text"), app.lbl_resultado.cget("text"))
escanear(app, "LIN"); check("todavia no imprime", app.impresos == [])
escanear(app, "MOC")
check("venta 2 completa -> imprime una vez", app.impresos == [2], app.impresos)
check("sigue la tanda (quedan 2) sin paquete abierto", app._tanda_empaque == [1, 3] and app._modo_empaque_pn is None
      and app.pedido_en_proceso is None and "quedan 2" in app.lbl_num_caja.cfg["text"], (app._tanda_empaque, app.lbl_num_caja.cfg))
escanear(app, "AUT")
check("la 3 (un producto) imprime al primer escaneo", app.impresos == [2, 3] and app._tanda_empaque == [1], app.impresos)
escanear(app, "SEC")
check("la ultima imprime y vuelve SOLA a la colecta", app.impresos == [2, 3, 1] and app._tanda_empaque == []
      and app._modo_empaque_pn is None and "FASE 1" in app.lbl_fase_actual.cfg["text"]
      and "Tanda terminada: 3" in app.lbl_resultado.cget("text"), app.lbl_resultado.cget("text"))
check("empacar la tanda NO sumo nada a la colecta", app.colecta_global == colecta_antes, (app.colecta_global, colecta_antes))
colectas = []
app._procesar_escaneo_fase1 = lambda s: colectas.append(s)
escanear(app, "FALTA")
check("despues de la tanda la pistola cuenta como colecta", colectas == ["FALTA"], colectas)
del app._procesar_escaneo_fase1
app._sincronizar_colecta_movil({"SEC": 1, "MOC": 1, "LIN": 2, "AUT": 1})
check("las ya impresas no vuelven a la cola", not ({1, 2, 3} & set(app._cola_listos_pack)), app._cola_listos_pack)

print("d2) mismo producto en dos ventas de la tanda")
app = nueva_app()
app.pedidos[1] = pedido(1, {"X": 1, "Y": 1}); app.pedidos[2] = pedido(2, {"X": 1})
app._sincronizar_colecta_movil({"X": 2, "Y": 1}); app._entrar_tanda([1, 2])
escanear(app, "X")
check("va a la primera que lo necesita", app._modo_empaque_pn == 1, app._modo_empaque_pn)
escanear(app, "Y"); escanear(app, "X")
check("despues la segunda", app.impresos == [1, 2] and app._tanda_empaque == [], app.impresos)

print("e) cancelar / volver a la colecta")
app = nueva_app()
app.pedidos[1] = pedido(1, {"A": 2}); app.pedidos[2] = pedido(2, {"B": 1})
app._sincronizar_colecta_movil({"A": 2, "B": 1}); app._entrar_tanda([1, 2])
escanear(app, "A")
check("una venta de 2 unidades queda a medias", app.pedidos[1]["skus_escaneados"] == {"A": 1} and app.impresos == [])
app.cancelar_paquete()
check("CANCELAR descarta solo ese paquete y sigue la tanda", app._tanda_empaque == [1, 2] and app._modo_empaque_pn is None
      and app.pedido_en_proceso is None and app.pedidos[1]["skus_escaneados"] == {}
      and "cancelado" in app.lbl_resultado.cget("text"), app.lbl_resultado.cget("text"))
escanear(app, "B")
check("despues de cancelar se puede seguir escaneando la tanda", app.impresos == [2], app.impresos)
escanear(app, "A")
app._salir_modo_empaque()
check("VOLVER: sale de todo, descarta lo escaneado y la no impresa vuelve a la cola",
      app._tanda_empaque == [] and app._modo_empaque_pn is None and app.pedidos[1]["skus_escaneados"] == {}
      and app._cola_listos_pack == [1] and "FASE 1" in app.lbl_fase_actual.cfg["text"], app._cola_listos_pack)
app._entrar_tanda([1]); app.cancelar_paquete()
check("CANCELAR sin paquete abierto = volver a la colecta", app._tanda_empaque == [] and app._cola_listos_pack == [1])

print("e2) impresion fallida y 'ya impresa, no reimprimir'")
app = nueva_app()
app.pedidos[1] = pedido(1, {"A": 1}); app.pedidos[2] = pedido(2, {"B": 1})
app._sincronizar_colecta_movil({"A": 1, "B": 1}); app._entrar_tanda([1, 2])
_imp = app._imprimir_automatico
app._imprimir_automatico = lambda pn: None          # la impresion fallo (no marca impreso)
escanear(app, "A")
check("si la impresion falla el paquete queda abierto (Reintentar)", app._modo_empaque_pn == 1
      and app._tanda_empaque == [1, 2], (app._modo_empaque_pn, app._tanda_empaque))
real = A.AsistenteDepositoApp._imprimir_automatico.__get__(app)
app.config = {"impresora": "PRUEBA"}
app.pedidos[1]["impreso"] = True                    # otra PC ya la imprimio
_ask = A.messagebox.askyesno; A.messagebox.askyesno = lambda *a, **k: False
try:
    real(1)                                         # "ya impresa" -> no reimprimir
finally:
    A.messagebox.askyesno = _ask
check("'ya impresa, no reimprimir' cierra esa venta y la tanda sigue", app._tanda_empaque == [2]
      and app._modo_empaque_pn is None and app.pedido_en_proceso is None and "quedan 1" in app.lbl_num_caja.cfg["text"],
      (app._tanda_empaque, app._modo_empaque_pn, app.lbl_num_caja.cfg))
app._imprimir_automatico = _imp
escanear(app, "B")
check("y la ultima termina la tanda (cuenta 1 etiqueta)", app._tanda_empaque == [] and app.impresos == [2]
      and app._modo_empaque_pn is None and "FASE 1" in app.lbl_fase_actual.cfg["text"]
      and "Tanda terminada: 1" in app.lbl_resultado.cget("text"), app.lbl_resultado.cget("text"))

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
app._entrar_tanda([1])
check("y tampoco entra a una tanda", app._tanda_empaque == [] and app._modo_empaque_pn is None)
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
app._entrar_tanda([1])
app._sincronizar_colecta_movil({"SKU-X": 2})
check("una venta de la tanda no vuelve a la cola con el sync", app._cola_listos_pack == [2], app._cola_listos_pack)
check("con tanda el boton sigue siendo VOLVER y avisa los nuevos listos", "VOLVER" in app.btn_empacar_siguiente.cfg["text"]
      and "1 nuevo" in app.btn_empacar_siguiente.cfg["text"], app.btn_empacar_siguiente.cfg)

print("h) Fase 2: sin cambios")
app = nueva_app(); app.fase_actual = 2
app.pedidos[1] = pedido(1, {"A": 1}); app.colecta_global["A"] = 1
app._revisar_pedidos_listos()
check("en Fase 2 no se encola nada", app._cola_listos_pack == [])

print("i) ventana real de la lista")
app = nueva_app(); cargar(app); app.root = TK
for n in (0, 1):
    for sku, q in reales[n][1].items(): app.colecta_global[sku] = app.colecta_global.get(sku, 0) + q
app._cargar_miniatura_async = lambda *a, **k: None
del app._mostrar_listos_empaque
app._revisar_pedidos_listos(); esperadas = list(app._cola_listos_pack); app._empacar_siguiente()
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
check("tiene UN boton PREPARAR TODAS y ninguno por venta", "PREPARAR TODAS" in t and "EMPACAR ESTE" not in t, t[-200:])
def botones(w):
    out = [c for c in w.winfo_children() if isinstance(c, tk.Button)]
    for c in w.winfo_children(): out += botones(c)
    return out
emp = [b for b in (botones(wins[-1]) if wins else []) if "PREPARAR TODAS" in b.cget("text")]
if emp: emp[0].invoke(); TK.update()
check("PREPARAR TODAS cierra la lista y arma la tanda con todas", len(esperadas) >= 2 and app._tanda_empaque == esperadas
      and not any(isinstance(w, tk.Toplevel) and w.winfo_exists() for w in TK.winfo_children()), (app._tanda_empaque, esperadas))

TK.destroy()
print(f"\nRESULTADO empacar: {ok} ok, {fa} fallas"); sys.exit(1 if fa else 0)
