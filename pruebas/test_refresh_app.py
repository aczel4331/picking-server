"""Escritorio: con un lote activo (Fase 1/2) el auto-refresh sigue trayendo pedidos nuevos, sin tocar el
lote ni redibujar la lista si la pestaña ML no se ve."""
import os, sys, json, time, shutil, subprocess, threading
import requests
sys.stdout.reconfigure(encoding="utf-8")
S = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(S)
PORT = "5135"; B = f"http://127.0.0.1:{PORT}"; DATA = os.path.join(S, "data_rfa")
ok = fa = 0
def check(n, c, x=""):
    global ok, fa
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else "  -> " + str(x)))
    if c: ok += 1
    else: fa += 1
shutil.rmtree(DATA, ignore_errors=True); os.makedirs(DATA)
json.dump([{"usuario": "colector1", "clave": "c1", "nombre": "C1", "cuenta_id": "cuenta_2", "rol": "operario"}], open(os.path.join(DATA, "usuarios.json"), "w"))
env = dict(os.environ, PORT=PORT, DATA_DIR=DATA, PICKING_API_KEY="everest2024", REPO=REPO, PEDIDOS_SNAP=os.path.join(S, "snap", "pedidos_live.json"),
           APP_SECRET_KEY="s" * 40, ML_API_URL="http://127.0.0.1:1", ML_APP_ID="x", ML_SECRET_KEY="x", PYTHONIOENCODING="utf-8")
srv = subprocess.Popen([sys.executable, os.path.join(S, "run_srv_pedidos.py")], env=env, stdout=open(os.path.join(S, "rfa.log"), "w"), stderr=subprocess.STDOUT)
try:
    for _ in range(80):
        try: requests.get(B + "/api/ping", timeout=1); break
        except Exception: time.sleep(.5)
    sys.path.insert(0, REPO)
    import app_deposito as A
    A.RAILWAY_URL = B; A.API_KEY_NUBE = "everest2024"
    class Panel:
        def __init__(self, v): self.v = v
        def winfo_ismapped(self): return self.v
    class Lbl:
        def __init__(self): self.t = ""
        def config(self, **k): self.t = k.get("text", self.t)
    class Raiz:
        def __init__(self): self.agendado = []
        def after(self, ms, fn=None, *a):
            if fn and ms == 0: fn()
            else: self.agendado.append(ms)
        def winfo_exists(self): return True
    class Fake(A.AsistenteDepositoApp):
        def __init__(self): pass
    def nueva(panel_visible):
        app = Fake(); app.root = Raiz(); app.panel_ml = Panel(panel_visible)
        app.fase_actual = 1; app.pedidos = {1: {"skus_requeridos": {"A": 1}}}      # LOTE ACTIVO
        app._ml_pedidos = {}; app._ml_cuenta_filtro = "todas"; app._refresh_countdown_seg = 0
        app.lbl_flex_badge = Lbl(); app.lbl_me2_badge = Lbl()
        app.llamadas = {"filtrar": 0, "btn": 0, "notif": 0}
        app._ml_filtrar = lambda: app.llamadas.__setitem__("filtrar", app.llamadas["filtrar"] + 1)
        app._actualizar_btn_agregar = lambda: app.llamadas.__setitem__("btn", app.llamadas["btn"] + 1)
        app._notificar_pedidos_nuevos = lambda n: app.llamadas.__setitem__("notif", n)
        app._en_modo_descanso = lambda: False
        return app
    def esperar(f, t=8):
        t0 = time.time()
        while time.time() - t0 < t:
            if f(): return True
            time.sleep(.1)
        return False

    print("con lote activo y la pestaña ML oculta")
    app = nueva(False)
    app._ml_pedidos = {"x": {"order_id": "x", "logistica": "self_service", "substatus": "ready_to_print"}}
    app._auto_refresh_ml()
    check("el auto-refresh consulta aunque haya lote activo", esperar(lambda: len(app._ml_pedidos) > 5), len(app._ml_pedidos))
    time.sleep(0.5)
    check("NO redibuja la lista (pestaña oculta)", app.llamadas["filtrar"] == 0, app.llamadas)
    check("deja la lista pendiente de redibujar", getattr(app, "_ml_render_pendiente", False) is True)
    check("avisa de pedidos nuevos y actualiza el boton Agregar", app.llamadas["notif"] > 0 and app.llamadas["btn"] > 0, app.llamadas)
    check("el lote activo no se toca", app.pedidos == {1: {"skus_requeridos": {"A": 1}}})
    check("anota la hora de la ultima actualizacion", getattr(app, "_ml_ultima_act", 0) > time.time() - 30)
    check("reprograma el siguiente (45 s)", 45_000 in app.root.agendado, app.root.agendado)

    print("pedido nuevo que entra al servidor con el lote activo")
    antes = len(app._ml_pedidos)
    requests.post(B + "/__test/inject", json={"pedidos": [{"order_id": "99000000001", "shipping_id": "9", "logistica": "self_service", "substatus": "ready_to_print",
        "estado_envio": "ready_to_ship", "_cuenta": "cuenta_2", "comprador": "NUEVO", "items": [{"sku": "NN", "cantidad": 1}], "fecha_cierre_ts": "2026-10-08T10:00:00.000-04:00"}]})
    app.llamadas["notif"] = 0
    app._auto_refresh_ml()
    check("aparece en la lista de la PC sin tocar nada", esperar(lambda: "99000000001" in app._ml_pedidos))
    check("avisa 1 pedido nuevo", esperar(lambda: app.llamadas["notif"] == 1), app.llamadas)

    print("con la pestaña ML visible se redibuja")
    app2 = nueva(True)
    app2._ml_pedidos = {"x": {"order_id": "x"}}
    app2._auto_refresh_ml()
    check("redibuja la lista", esperar(lambda: app2.llamadas["filtrar"] >= 1), app2.llamadas)

    print("al volver a la pestaña ML se redibuja lo pendiente")
    class Frame:
        def pack_forget(self): pass
        def pack(self, **k): pass
    app.panel_picking = Frame()
    app.panel_ml = Frame(); app._set_nav_active = lambda n: None
    app.llamadas["filtrar"] = 0
    app._switch_tab("ml")
    check("_switch_tab('ml') redibuja y limpia la marca", app.llamadas["filtrar"] == 1 and app._ml_render_pendiente is False, app.llamadas)

    print("contador de antiguedad")
    app3 = nueva(True); app3._ml_ultima_act = time.time() - 400
    lbl = Lbl(); lbl.winfo_exists = lambda: True; app3.lbl_refresh_countdown = lbl; app3._refresh_countdown_seg = 30
    app3.root.winfo_exists = lambda: True
    app3._tick_refresh_countdown()
    check("a los 6 min dice 'act. hace 6 min'", "act. hace 6 min" in lbl.t, lbl.t)
finally:
    srv.kill()
print(f"\nRESULTADO refresco escritorio: {ok} ok, {fa} fallas"); sys.exit(1 if fa else 0)
