"""Un lote nuevo NO hereda lo 'ya buscado' del anterior, aunque los eventos lleguen en orden adverso.
Reproduce lote 1 -> Fase 2 -> fin -> lote 2 (mismo SKU 3684) con los métodos REALES de la app (generar lote, subir,
sondear, aplicar colecta, terminar lote) contra un servidor local real y un 'celular' que escanea por HTTP."""
import os, sys, json, time, shutil, subprocess, threading, builtins, urllib.request
import requests
sys.stdout.reconfigure(encoding="utf-8")
S = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(S)
PORT = "5137"; B = f"http://127.0.0.1:{PORT}"; DATA = os.path.join(S, "data_replay")
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
srv = subprocess.Popen([sys.executable, os.path.join(S, "run_srv_pedidos.py")], env=env, stdout=open(os.path.join(S, "replay.log"), "w"), stderr=subprocess.STDOUT)

# la app escribe picking_sync.log al lado de app_deposito.py: no ensuciar el del desarrollo
_open = builtins.open
builtins.open = lambda f, *a, **k: _open(os.devnull, *a, **k) if isinstance(f, str) and f.endswith("picking_sync.log") else _open(f, *a, **k)

def esperar(cond, t=10):
    t0 = time.time()
    while time.time() - t0 < t:
        try:
            if cond(): return True
        except Exception: pass
        time.sleep(0.05)
    return False
def est(): return requests.get(B + "/api/estado?usuario=col1&cuenta_id=cuenta_2").json()
def celular_escanea(sku): return requests.post(B + "/api/escanear", json={"sku": sku, "usuario": "col1", "cuenta_id": "cuenta_2"}).json()

try:
    for _ in range(80):
        try: requests.get(B + "/api/ping", timeout=1); break
        except Exception: time.sleep(.5)

    sys.path.insert(0, os.environ.get("REPO_APP", REPO)); os.chdir(REPO)
    import app_deposito as A
    import tkinter as tk
    from tkinter import messagebox
    _raiz_tk = tk.Tk(); _raiz_tk.withdraw()
    A.RAILWAY_URL = B
    messagebox.askokcancel = lambda *a, **k: True
    messagebox.askyesno = lambda *a, **k: True
    for n in ("showinfo", "showwarning", "showerror"): setattr(messagebox, n, lambda *a, **k: None)
    A.messagebox = messagebox
    A.registrar_inicio_lote = lambda *a, **k: None

    # urlopen con control: se puede RETENER la subida del 'lote vacío' para entregarla tarde
    _urlopen = urllib.request.urlopen
    retener = {"vaciado": False}; parked = []
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"
    def urlopen_ctrl(req, *a, **k):
        url = getattr(req, "full_url", str(req))
        if "verificar-ahora" in url or "/api/sesion" in url or "/api/imagen-sku" in url:
            raise OSError("fuera de la prueba")
        if retener["vaciado"] and getattr(req, "data", None) and url.endswith("/api/subir_estado") and b'"grupos": []' in req.data:
            parked.append((req, a, k)); return Resp()
        return _urlopen(req, *a, **k)
    urllib.request.urlopen = urlopen_ctrl

    class Var:
        def __init__(self, v): self.v = v
        def get(self): return self.v
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
        def title(self, *a): pass
    class Fake(A.AsistenteDepositoApp):
        def __init__(self): pass

    def orden(oid, ship, skus):
        return {"order_id": str(oid), "pack_id": "", "shipping_id": ship, "_cuenta": "cuenta_2", "comprador": "B", "logistica": "self_service",
                "tipo": "flex", "substatus": "ready_to_print", "estado_envio": "ready_to_ship", "impreso": False, "fecha": "2026-10-14",
                "items": [{"item_id": "MLU" + s, "titulo": "P" + s, "sku": s, "cantidad": q, "color": "", "talle": ""} for s, q in skus.items()]}

    app = Fake()
    app.root = Raiz(); app.config = {"servidor_nube": B, "clave_nube": "everest2024", "impresora": "X"}
    app._ml_pedidos = {}; app._ml_filtro_tipo = Var("flex"); app.var_proximos_dias = Var(True); app._pedidos_seleccionados = set()
    app._canal_lote_activo = "flex"; app.pedidos = {}; app.fase_actual = 1; app.colecta_global = {}; app.pedido_en_proceso = None
    app._cola_listos_pack = []; app._pedidos_pausados = set(); app._lote_id_local = None; app.db_nombres = {}; app.sku_item_id = {}; app.sku_nombre_ml = {}
    app._usuario_activo = {"usuario": "col1", "nombre": "C1", "cuenta_id": "cuenta_2", "rol": "operario"}
    app._img_fallos = {}; app._img_cargando = set(); app._img_labels = {}
    for w in ("lbl_ml_estado", "lbl_resultado", "lbl_num_caja", "lbl_imprimiendo", "entrada_sku", "btn_fase2", "lbl_fase_actual", "lbl_col1_header",
              "lbl_estado_pdf", "lbl_seleccion", "btn_empacar_siguiente", "col_control"):
        setattr(app, w, Cualquiera())
    for m in ("_mostrar_placeholder_visor", "_dibujar_fase1", "actualizar_contador_global", "_guardar_sesion", "_exportar_estado_movil", "_switch_tab",
              "_predescargar_etiquetas_fase2", "_refrescar_barra_operacion", "_actualizar_btn_agregar", "_barra_op_conexion", "_log_impresion",
              "_borrar_sesion", "_recargar_miniaturas_pendientes", "_flash_sku_escaneado", "_on_subida_error"):
        setattr(app, m, lambda *a, **k: None)
    app._ubicacion_sku = lambda sku: ("P1", "")
    app._nombre_sku = lambda sku: sku
    app._en_modo_descanso = lambda: False
    app.sondeos = 0

    def sondear():
        """Un sondeo real: hilo de red → _on_sync_nube real (en el hilo principal simulado)."""
        listo = threading.Event()
        real = A.AsistenteDepositoApp._on_sync_nube.__get__(app)
        def on(info):
            real(info); listo.set()
        app._on_sync_nube = on
        app._sincronizar_desde_nube()
        listo.wait(8)
        app.root.pend.clear(); app._sondeo_after_id = None      # (como si ya hubiera corrido el sondeo programado)
    def generar(ml_orders):
        app._ml_pedidos = {o["order_id"]: o for o in ml_orders}
        app._ml_generar_lote()
        id_lote = app._lote_id_local
        assert esperar(lambda: est().get("lote_id") == id_lote), (est().get("lote_id"), id_lote)
        return id_lote
    def terminar_lote():
        for d in app.pedidos.values(): d["impreso"] = True
        app._resetear_lote_completo()

    # ── A. flujo normal: el mismo SKU 3684 en el lote 1 y en el lote 2 ─────────────
    print("A) lote 1 → el celular busca 3684 → Fase 2 → fin → lote 2 con 3684")
    L1 = generar([orden(1, "S1", {"3684": 1, "4378": 1})])
    celular_escanea("3684")
    sondear()
    check("lote 1: la PC recibe lo que buscó el celular", app.colecta_global.get("3684") == 1, app.colecta_global)
    terminar_lote()
    check("fin de lote: la PC queda sin lote ni id", app.pedidos == {} and app._lote_id_local is None and app.colecta_global == {})
    check("...y el servidor queda limpio", esperar(lambda: est()["colecta"] == {} and est().get("lote_id", "") == ""), est())
    L2 = generar([orden(2, "S2", {"3684": 1, "5000": 1})])
    for _ in range(3): sondear()
    e = est()
    check("lote 2: la PC NO marca 3684 como buscado", app.colecta_global == {}, app.colecta_global)
    check("lote 2: el servidor/celular NO lo marcan", e["colecta"] == {} and e["lote_id"] == L2, e["colecta"])
    r = celular_escanea("3684")
    check("lote 2: la primera lectura de 3684 cuenta 1/1 (no 'ya completo')", r.get("tipo") == "completo" and r.get("collected") == 1, r)

    # ── B. el 'lote vacío' del lote 1 llega DESPUÉS de que ya se subió el lote 2 ───
    print("B) el aviso de fin del lote 1 llega tarde, con el lote 2 ya cargado")
    terminar_lote(); esperar(lambda: est()["colecta"] == {})
    L3 = generar([orden(3, "S3", {"3684": 1, "6000": 1})])
    celular_escanea("6000")
    sondear()
    retener["vaciado"] = True
    terminar_lote()                                       # el vaciado queda retenido en la red…
    L4 = generar([orden(4, "S4", {"3684": 1, "7000": 1})])     # …y se sube el lote 4
    retener["vaciado"] = False
    for req, a, k in parked: _urlopen(req, *a, **k)       # …y recién ahora llega el vaciado del lote 3
    parked.clear()
    app._subir_a_nube_async()                             # la PC vuelve a subir su lote (como en el próximo escaneo)
    check("el servidor recupera el lote 4", esperar(lambda: est().get("lote_id") == L4), est().get("lote_id"))
    for _ in range(3): sondear()
    e = est()
    check("sin arrastre del lote 3 (ni 3684 ni 6000) en servidor", e["colecta"] == {}, e["colecta"])
    check("sin arrastre en la PC", app.colecta_global == {}, app.colecta_global)
    check("el lote 4 sigue cargado completo (3684 y 7000)", sorted(i["sku"] for g in e["grupos"] for i in g["items"]) == ["3684", "7000"])

    # ── C. la respuesta de la subida del lote 1 llega cuando ya está el lote 2 ─────
    print("C) respuesta TARDÍA de una subida vieja")
    terminar_lote(); esperar(lambda: est()["colecta"] == {})
    L5 = generar([orden(5, "S5", {"3684": 1, "8000": 1})])
    celular_escanea("3684")
    sondear()
    viejo = {"ok": True, "lote_id": L5, "canal": "flex", "clave": "cuenta_2:col1"}
    terminar_lote(); esperar(lambda: est()["colecta"] == {})
    L6 = generar([orden(6, "S6", {"3684": 1, "9000": 1})])
    app._on_subida_ok(viejo, L5)                          # llega tarde la confirmación de la subida del lote 5
    check("el id del lote 6 no se pisa", app._lote_id_local == L6, (app._lote_id_local, L6))
    celular_escanea("9000")
    sondear()
    check("el sondeo sigue reconociendo el lote 6: aplica lo del celular", app.colecta_global.get("9000") == 1 and "3684" not in app.colecta_global, app.colecta_global)

    # ── D. un sondeo en vuelo del lote anterior se aplica con el lote nuevo en pantalla ──
    print("D) sondeo en vuelo del lote anterior")
    real = A.AsistenteDepositoApp._on_sync_nube.__get__(app)
    info_viejo = {"ok": True, "n": 1, "colecta": {"3684": 1}, "lote_id": L5, "lote_id_local": L5, "fase": 1}
    app._on_sync_nube = real
    app.colecta_global = {}
    app._on_sync_nube(info_viejo)
    check("la colecta del lote 5 no entra al lote 6", app.colecta_global == {}, app.colecta_global)

    # ── E. cuántos sondeos quedan programados ──────────────────────────────────────
    print("E) cadena de sondeo")
    for _ in range(60): app._on_subida_ok({"ok": True, "lote_id": L6}, L6)
    cadenas = [1 for ms, fn in app.root.pend.values() if getattr(fn, "__name__", "") == "_sondeo_tick"]
    check("tras 60 confirmaciones de subida hay UNA sola cadena de sondeo programada", len(cadenas) == 1, len(cadenas))
finally:
    srv.kill()
    urllib.request.urlopen = urllib.request.urlopen
print(f"\nRESULTADO replay de lotes: {ok} ok, {fa} fallas"); sys.exit(1 if fa else 0)
