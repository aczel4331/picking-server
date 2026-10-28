"""App de escritorio: un envío (una etiqueta) = UN pedido con TODOS sus productos; el sondeo es UNA cadena;
una respuesta vieja no pisa el lote nuevo. Usa los métodos reales de la app con pedidos reales."""
import os, sys, json, copy, types, urllib.request
sys.stdout.reconfigure(encoding="utf-8")
S_DIR = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(S_DIR)
sys.path.insert(0, REPO); os.chdir(REPO)
import app_deposito as A
import tkinter as tk
from tkinter import messagebox
_raiz_tk = tk.Tk(); _raiz_tk.withdraw()                # _ml_generar_lote crea un BooleanVar por defecto

ok = fa = 0
def check(n, c, x=""):
    global ok, fa
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else "  -> " + str(x)))
    if c: ok += 1
    else: fa += 1

NUNCA = "http://127.0.0.1:1"                       # ninguna llamada de red sale de esta prueba
def sin_red(*a, **k): raise OSError("sin red en la prueba")
urllib.request.urlopen = sin_red
dialogos = []
respuesta = {"askyesno": True, "askokcancel": True}
messagebox.askyesno = lambda *a, **k: (dialogos.append(("askyesno", a[0], a[1])), respuesta["askyesno"])[1]
messagebox.askokcancel = lambda *a, **k: (dialogos.append(("askokcancel", a[0], a[1])), respuesta["askokcancel"])[1]
messagebox.showinfo = lambda *a, **k: dialogos.append(("info", a[0], a[1]))
messagebox.showwarning = lambda *a, **k: dialogos.append(("warn", a[0], a[1]))
messagebox.showerror = lambda *a, **k: dialogos.append(("error", a[0], a[1]))
A.messagebox = messagebox
inicios = []
A.registrar_inicio_lote = lambda *a, **k: inicios.append(k)

class Var:
    def __init__(self, v): self.v = v
    def get(self): return self.v
    def set(self, v): self.v = v
class Cualquiera:
    def __getattr__(self, n): return lambda *a, **k: None
class Raiz:
    def __init__(self): self.pend = {}; self.n = 0
    def after(self, ms, fn=None, *a):
        if fn is None: return None
        if ms == 0: fn(*a); return None
        self.n += 1; self.pend[self.n] = (ms, fn); return self.n
    def after_cancel(self, i): self.pend.pop(i, None)
    def update_idletasks(self): pass
    def update(self): pass

class Fake(A.AsistenteDepositoApp):
    def __init__(self): pass

def nueva_app(pedidos_ml, tipo="colecta"):
    app = Fake()
    app.root = Raiz(); app.config = {"servidor_nube": NUNCA, "clave_nube": "everest2024", "impresora": "PRUEBA"}
    app._ml_pedidos = {str(p["order_id"]): copy.deepcopy(p) for p in pedidos_ml}
    app._ml_filtro_tipo = Var(tipo); app.var_proximos_dias = Var(True)
    app._pedidos_seleccionados = set(); app._canal_lote_activo = tipo
    app.pedidos = {}; app.fase_actual = 1; app.colecta_global = {}; app.pedido_en_proceso = None
    app._cola_listos_pack = []; app._pedidos_pausados = set(); app._lote_id_local = None
    app.db_nombres = {}; app.sku_item_id = {}; app.sku_nombre_ml = {}
    app._usuario_activo = {"usuario": "col1", "nombre": "C1", "cuenta_id": "cuenta_2", "rol": "operario"}
    for w in ("lbl_ml_estado", "lbl_resultado", "lbl_num_caja", "lbl_imprimiendo", "entrada_sku", "btn_fase2", "lbl_fase_actual",
              "lbl_col1_header", "lbl_estado_pdf", "lbl_seleccion", "btn_ml_agregar_lote", "btn_cancelar", "btn_saltar"):
        setattr(app, w, Cualquiera())
    for m in ("_mostrar_placeholder_visor", "_dibujar_fase1", "actualizar_contador_global", "_guardar_sesion", "_exportar_estado_movil",
              "_subir_a_nube_async", "_switch_tab", "_predescargar_etiquetas_fase2", "_refrescar_barra_operacion"):
        setattr(app, m, lambda *a, **k: None)
    app._actualizar_btn_agregar = lambda *a, **k: None
    return app

def it(sku, cant=1, item_id=None):
    return {"item_id": item_id or "MLU" + sku, "titulo": "P " + sku, "sku": sku, "cantidad": cant, "color": "", "talle": "", "unit_price": 10}
def orden(oid, ship, items, logi="cross_docking", pack="PK", **kw):
    p = {"order_id": str(oid), "pack_id": pack, "shipping_id": ship, "items": items, "total": 10, "_cuenta": "cuenta_2",
         "comprador": "B", "logistica": logi, "tipo": "colecta" if logi == "cross_docking" else "flex", "substatus": "ready_to_print",
         "estado_envio": "ready_to_ship", "impreso": False, "fecha": "2026-10-14", "fecha_cierre": "2026-10-14"}
    p.update(kw); return p
def uds(p): return sum(i["cantidad"] for i in p["items"])
def req_total(d): return sum(d["skus_requeridos"].values())

# ── 1. agrupar_por_envio ────────────────────────────────────────────────────────
print("1) agrupar_por_envio")
base = [orden(1, "S1", [it("X", 1)]), orden(2, "S1", [it("X", 3)]), orden(3, "S1", [it("Y", 2)]), orden(4, "S2", [it("X", 1)])]
orig = copy.deepcopy(base)
g = A.agrupar_por_envio(base)
check("3 órdenes del mismo envío → 1 pedido", len(g) == 2 and g[0]["_orders_agrupadas"] == ["1", "2", "3"], [x["_orders_agrupadas"] for x in g])
check("el mismo producto en dos órdenes SUMA (1+3), no se pierde", sorted((i["sku"], i["cantidad"]) for i in g[0]["items"]) == [("X", 4), ("Y", 2)], g[0]["items"])
check("no modifica los pedidos de ML (trabaja sobre copias)", base == orig)
g2 = A.agrupar_por_envio([copy.deepcopy(g[0]), orden(2, "S1", [it("X", 3)]), orden(9, "S1", [it("Z", 1)])])
check("agrupado parcial + hermana repetida + hermana nueva: no duplica cantidades", sorted((i["sku"], i["cantidad"]) for i in g2[0]["items"]) == [("X", 4), ("Y", 2), ("Z", 1)], g2[0]["items"])
sinsh = A.agrupar_por_envio([orden(5, "", [it("A")], pack="P9"), orden(6, "", [it("B")], pack="P9"), orden(7, "", [it("C")], pack="")])
check("sin shipping_id agrupa por pack (como antes) y sin pack queda suelto", len(sinsh) == 2 and uds(sinsh[0]) == 2)
fuera = orden(8, "S1", [it("W", 2)], substatus="pending")
g3 = A.agrupar_por_envio(base[:2], universo=base + [fuera], valido=lambda q: True)
check("hermanas que quedaron fuera del filtro igual se suman (el envío va completo): X4+Y2+W2", uds(g3[0]) == 8, g3[0]["items"])

# ── 2. Generar lote (método real) con órdenes reales sueltas ─────────────────────
print("2) _ml_generar_lote con la foto real (envíos sueltos como los dejaba el webhook)")
snaps = [os.path.join(S_DIR, "snap", n) for n in ("pedidos_live_0814.json", "pedidos_live.json")]
for ruta in [r for r in snaps if os.path.exists(r)]:
    nombre = os.path.basename(ruta)
    reales = json.load(open(ruta, encoding="utf-8"))["pedidos"]
    for p in reales: p.pop("_orders_agrupadas", None)           # todas sueltas
    app = nueva_app(reales, "colecta")
    antes = copy.deepcopy(app._ml_pedidos)
    en_scope = [p for p in app._ml_pedidos.values() if app._ml_en_scope(p, "colecta", True)]
    del dialogos[:]; del inicios[:]
    app._ml_generar_lote()
    envios = [d["_shipping_id"] for d in app.pedidos.values() if d["_shipping_id"]]
    check(f"[{nombre}] {len(en_scope)} órdenes de Colecta → {len(app.pedidos)} pedidos, ningún envío repetido", len(envios) == len(set(envios)) and len(app.pedidos) > 0,
          (len(envios), len(set(envios))))
    # unidades esperadas: todas las órdenes no finalizadas de cada envío en scope + las sueltas sin envío
    ships = {p["shipping_id"] for p in en_scope if p.get("shipping_id")}
    esperado = sum(uds(p) for p in app._ml_pedidos.values() if p.get("shipping_id") in ships and not app._ml_pedido_finalizado(p)) \
        + sum(uds(p) for p in en_scope if not p.get("shipping_id"))
    check(f"[{nombre}] el lote pide TODAS las unidades de cada envío ({esperado})", sum(req_total(d) for d in app.pedidos.values()) == esperado,
          (sum(req_total(d) for d in app.pedidos.values()), esperado))
    check(f"[{nombre}] la lista de Pedidos ML no se modificó", app._ml_pedidos == antes)
    ids_lote = {o for d in app.pedidos.values() for o in d["_orders"]}
    check(f"[{nombre}] cada orden del canal figura en algún pedido del lote", {str(p['order_id']) for p in en_scope} <= ids_lote)
    hist = inicios[-1].get("order_ids", []) if inicios else []
    check(f"[{nombre}] el historial del lote guarda TODAS las órdenes (no solo la principal)", {h["order_id"] for h in hist} >= ids_lote, (len(hist), len(ids_lote)))
    multi = [d for d in app.pedidos.values() if len(d["_orders"]) > 1]
    if multi or nombre != "pedidos_live.json":
        check(f"[{nombre}] hay envíos con varias órdenes en el lote ({len(multi)}) y el nombre del pedido lo marca", multi and all("🛒×" in app._nombre_pedido(pn) for pn, d in app.pedidos.items() if len(d["_orders"]) > 1))
    else:
        print(f"  SKIP  [{nombre}] la foto de hoy no trae envíos con varias órdenes (depende de los datos del día)")

# el caso del dueño: 4 productos escaneados, etiqueta de 16
print("3) el caso reportado: envío de 16 unidades en 4 órdenes")
ords = [orden(100 + i, "S16", [it(f"K{i}a", 2), it(f"K{i}b", 2)]) for i in range(4)]            # 4 órdenes × 4 uds = 16
app = nueva_app(ords, "colecta"); app._ml_generar_lote()
d = list(app.pedidos.values())[0]
check("un solo pedido de 16 unidades (antes: pedidos de 4)", len(app.pedidos) == 1 and req_total(d) == 16 and len(d["skus_requeridos"]) == 8, (len(app.pedidos), req_total(d)))
app2 = nueva_app([ords[0]], "colecta"); app2._ml_generar_lote()
check("lote armado con SOLO una orden (como dejaba el webhook): pide 4", req_total(list(app2.pedidos.values())[0]) == 4)
# ...después llegan las otras tres: «Agregar al lote» completa el envío (no crea pedidos nuevos)
for o in ords[1:]: app2._ml_pedidos[o["order_id"]] = copy.deepcopy(o)
cand = app2._ml_candidatos_agregar()
check("las órdenes que llegan después aparecen como UN candidato que completa el envío", len(cand) == 1 and cand[0].get("_completa_envio"), cand)
del dialogos[:]
app2._ml_agregar_al_lote()
d2 = list(app2.pedidos.values())[0]
check("el pedido pasa a pedir las 16 unidades, sigue siendo 1 solo", len(app2.pedidos) == 1 and req_total(d2) == 16 and len(d2["_orders"]) == 4, (len(app2.pedidos), req_total(d2)))
check("avisa que se completó un envío", any("completan 1 env" in str(x[2]) or "completaron 1 env" in str(x[2]) for x in dialogos), dialogos)
check("segunda vez: ya no hay nada que agregar", app2._ml_candidatos_agregar() == [])
app3 = nueva_app([ords[0]], "colecta"); app3._ml_generar_lote()
list(app3.pedidos.values())[0]["impreso"] = True
for o in ords[1:]: app3._ml_pedidos[o["order_id"]] = copy.deepcopy(o)
check("si la etiqueta de ese envío ya se imprimió no se intenta completar", app3._ml_candidatos_agregar() == [])

# ── 4. control antes de imprimir ────────────────────────────────────────────────
print("4) control antes de imprimir (la etiqueta es del envío completo)")
app = nueva_app([ords[0]], "colecta"); app._ml_generar_lote()
for o in ords[1:]: app._ml_pedidos[o["order_id"]] = copy.deepcopy(o)       # ML ya tiene las 16
del dialogos[:]; respuesta["askyesno"] = False
check("ML tiene 16 y el pedido 4: avisa y, si el operario dice NO, no imprime", app._confirmar_envio_completo(1) is False and dialogos and "16" in dialogos[0][2] and "4" in dialogos[0][2], dialogos)
respuesta["askyesno"] = True
check("...y si dice SÍ, imprime igual", app._confirmar_envio_completo(1) is True)
app = nueva_app(ords, "colecta"); app._ml_generar_lote(); del dialogos[:]
check("pedido completo: no pregunta nada", app._confirmar_envio_completo(1) is True and not dialogos)
impreso = []
app._enviar_a_impresora = lambda *a, **k: impreso.append(a) or True
app._confirmar_envio_completo = lambda pn: False
app.ruta_pdf = ""; app._imprimir_automatico(1)
check("_imprimir_automatico se detiene antes de bajar/enviar la etiqueta", impreso == [])

# ── 5. sesión guardada y restaurada conserva las órdenes del envío ──────────────
print("5) sesión")
app = nueva_app(ords, "colecta"); app._ml_generar_lote()
cap = []
class Resp:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return b"{}"
urllib.request.urlopen = lambda req, *a, **k: (cap.append(json.loads(req.data.decode())) if getattr(req, "data", None) else None, Resp())[1]
app.root.after = lambda ms, fn=None, *a: (fn(*a) if ms == 0 or fn is None else None)
app._guardar_sesion = A.AsistenteDepositoApp._guardar_sesion.__get__(app)
app._guardar_sesion_flush()
import time as _t
for _ in range(30):
    if cap: break
    _t.sleep(0.1)
urllib.request.urlopen = sin_red
ses = cap[0] if cap else {}
check("la sesión guardada lleva las 4 órdenes del envío", cap and len(list(ses["pedidos"].values())[0]["_orders"]) == 4, ses.get("pedidos"))
app = nueva_app([], "colecta")
for m in ("_revisar_pedidos_listos", "_actualizar_badge_cola", "_dibujar_fase2"): setattr(app, m, lambda *a, **k: None)
app._restaurar_sesion(dict(ses, canal="colecta", fase=1))
check("al restaurar, el pedido conserva sus 4 órdenes (para completar/controlar el envío)", len(list(app.pedidos.values())[0]["_orders"]) == 4)

# ── 6. una sola cadena de sondeo ────────────────────────────────────────────────
print("6) sondeo")
app = nueva_app([], "colecta"); llamadas = []
app._sincronizar_desde_nube = lambda: llamadas.append(1)
for ms in [5000, 4000, 5000, 5000, 3000, 8000, 600000, 5000] * 10:
    app._programar_sondeo(ms)
check("80 pedidos de sondeo seguidos dejan UNA sola cadena programada", len(app.root.pend) == 1, len(app.root.pend))
check("y queda la más cercana (3 s)", list(app.root.pend.values())[0][0] == 3000, app.root.pend)
ms, fn = list(app.root.pend.values())[0]; app.root.pend.clear()
fn()
check("al dispararse corre UN sondeo y re-programa UNA vez (la cadena no se corta ni se multiplica)", len(llamadas) == 1 and len(app.root.pend) == 1, (len(llamadas), len(app.root.pend)))
app = nueva_app([], "colecta"); app._sincronizar_desde_nube = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
app._programar_sondeo(3000); ms, fn = list(app.root.pend.values())[0]; app.root.pend.clear(); fn()
check("si un sondeo falla, la cadena sigue", len(app.root.pend) == 1)

# ── 7. respuestas viejas no pisan el lote nuevo ─────────────────────────────────
print("7) lote_id")
app = nueva_app([], "colecta"); app.lbl_estado_pdf = Cualquiera(); app.pedidos = {1: {}}
app._lote_id_local = "L2"
app._on_subida_ok({"ok": True, "lote_id": "L1"}, "L1")
check("respuesta tardía de la subida del lote 1: NO pisa el id del lote 2", app._lote_id_local == "L2", app._lote_id_local)
app._on_subida_ok({"ok": True, "lote_id": "L2"}, "L2")
check("respuesta de la subida del lote actual: se acepta", app._lote_id_local == "L2")
app._lote_id_local = None
app._on_subida_ok({"ok": True, "lote_id": "srv_123"}, "")
check("sesión restaurada (sin id): toma el que asigna el servidor", app._lote_id_local == "srv_123")
app._lote_id_local = "L3"
app._on_subida_ok({"ok": True, "lote_id": "viejo"}, "")
check("respuesta de una subida SIN id llegando con un lote nuevo en pantalla: se ignora", app._lote_id_local == "L3")

aplicadas = []
app = nueva_app([], "colecta"); app.pedidos = {1: {"skus_requeridos": {"3684": 1}}}
app._sincronizar_colecta_movil = lambda c, autoritativo=False: aplicadas.append(dict(c))
app._barra_op_conexion = lambda *a, **k: None; app.root.title = lambda *a, **k: None
app._lote_id_local = "L2"
app._on_sync_nube({"ok": True, "n": 1, "colecta": {"3684": 1}, "lote_id": "L1", "lote_id_local": "L1", "fase": 1})
check("la colecta validada contra el lote 1 NO se aplica si ahora el lote es el 2 (carrera hilo de red → hilo principal)", all(not c for c in aplicadas), aplicadas)
del aplicadas[:]
app._on_sync_nube({"ok": True, "n": 1, "colecta": {"3684": 1}, "lote_id": "L2", "lote_id_local": "L2", "fase": 1})
check("la del lote vigente sí se aplica", aplicadas == [{"3684": 1}], aplicadas)

# ── 8. fin de lote ──────────────────────────────────────────────────────────────
print("8) fin de lote")
app = nueva_app(ords, "colecta"); app._ml_generar_lote()
app.config["servidor_nube"] = NUNCA
app._cola_listos_pack = [1, 2]; app._pedidos_pausados = {1}; app._lote_id_local = "L1"; app.colecta_global = {"3684": 1}
app._img_fallos = {}; app._img_cargando = set(); app._img_labels = {}
app._borrar_sesion = lambda: None; app._actualizar_badge_cola = lambda: None
app._resetear_lote_completo()
check("queda sin lote, sin colecta, sin colas, Fase 1 (int) y sin id de lote", app.pedidos == {} and app.colecta_global == {} and app._cola_listos_pack == []
      and app._pedidos_pausados == set() and app.fase_actual == 1 and app._lote_id_local is None,
      (app.fase_actual, app._lote_id_local, app._cola_listos_pack))
app = nueva_app(ords, "colecta"); app._cola_listos_pack = [7, 8]; app._pedidos_pausados = {3}
app._ml_generar_lote()
check("generar un lote nuevo vacía las colas del anterior", app._cola_listos_pack == [] and app._pedidos_pausados == set())

# ── 9. el refresco silencioso REEMPLAZA la lista (no deja sueltas las órdenes que el servidor fundió) ──
print("9) refresco silencioso de Pedidos ML")
app = nueva_app(ords, "colecta")                                   # la PC tiene las 4 órdenes sueltas (como las dejaba el webhook)
principal = A.agrupar_por_envio([copy.deepcopy(o) for o in ords])[0]  # y el servidor ya las manda fundidas en 1 envío
class RespJ:
    headers = {}                                   # como una respuesta real (sin ETag ni gzip)
    def __init__(self, d): self.d = json.dumps(d).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self.d
urllib.request.urlopen = lambda req, *a, **k: RespJ({"ok": True, "pedidos": [principal]})
Raiz.winfo_exists = lambda self: True
app._ml_cuenta_filtro = "todas"; app.panel_ml = Cualquiera(); app._ml_actualizar_contadores = lambda: None
app._notificar_pedidos_nuevos = lambda n: None
app._ml_refresh_pedidos_silencioso()
import time as _t2
for _ in range(50):
    if len(app._ml_pedidos) == 1: break
    _t2.sleep(0.1)
urllib.request.urlopen = sin_red
check("las 4 órdenes sueltas quedan reemplazadas por el envío agrupado (sin duplicados en la lista)", len(app._ml_pedidos) == 1 and uds(list(app._ml_pedidos.values())[0]) == 16,
      [(k, uds(v)) for k, v in app._ml_pedidos.items()])

print(f"\nRESULTADO pack-por-envío (app): {ok} ok, {fa} fallas")
sys.exit(1 if fa else 0)
