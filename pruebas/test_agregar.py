"""Prueba de '➕ Agregar al lote' con los pedidos REALES (foto de producción) usando la
función real de la app de escritorio contra un servidor local. REPO=<carpeta> permite
correrla también contra el código viejo (git worktree) para ver el bug antes del arreglo."""
import os, sys, json, time, shutil, subprocess, uuid, types
import requests

S_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DEFAULT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.environ.get("REPO", REPO_DEFAULT)
REPO_APP = os.environ.get("REPO_APP", REPO_DEFAULT)
PORT = os.environ.get("TPORT", "5101")
B = f"http://127.0.0.1:{PORT}"
DATA = os.path.join(S_DIR, "data_ag")
SNAP = os.path.join(S_DIR, "snap", "pedidos_live.json")
ok_n = fail_n = 0
HK = {"X-API-Key": "everest2024"}
sys.stdout.reconfigure(encoding="utf-8")


def check(n, c, extra=""):
    global ok_n, fail_n
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else f"  {extra}"))
    ok_n += bool(c); fail_n += (not c)


shutil.rmtree(DATA, ignore_errors=True); os.makedirs(DATA)
json.dump([{"usuario": "admin", "clave": "1234", "nombre": "Admin", "cuenta_id": "todas", "rol": "admin"},
           {"usuario": "colector1", "clave": "c1", "nombre": "Colector 1", "cuenta_id": "cuenta_2", "rol": "operario"}],
          open(os.path.join(DATA, "usuarios.json"), "w"))
env = dict(os.environ, PORT=PORT, DATA_DIR=DATA, PICKING_API_KEY="everest2024", REPO=REPO,
           PEDIDOS_SNAP=SNAP, ML_API_URL="http://127.0.0.1:1", ML_APP_ID="x", ML_SECRET_KEY="x")
srv = subprocess.Popen([sys.executable, os.path.join(S_DIR, "run_srv_pedidos.py")], env=env,
                       stdout=open(os.path.join(S_DIR, "ag.log"), "w"), stderr=subprocess.STDOUT)
try:
    for _ in range(80):
        try:
            requests.get(B + "/api/ping", timeout=1); break
        except Exception:
            time.sleep(0.5)

    sys.path.insert(0, REPO_APP)
    import app_deposito as A
    from tkinter import messagebox
    A.RAILWAY_URL = B
    dialogos = []
    messagebox.askyesno = lambda *a, **k: (dialogos.append(("askyesno", a[1])), True)[1]
    messagebox.showinfo = lambda *a, **k: dialogos.append(("info", a[1]))
    messagebox.showwarning = lambda *a, **k: dialogos.append(("warn", a[1]))
    messagebox.showerror = lambda *a, **k: dialogos.append(("error", a[1]))
    A.messagebox = messagebox

    pedidos_ml = {p["order_id"]: p for p in requests.get(B + "/api/pedidos").json()["pedidos"]}
    print(f"pedidos reales cargados: {len(pedidos_ml)}")

    # ── doble de la app: usa los MÉTODOS REALES de la clase ─────────────────────
    class Var:
        def __init__(self, v): self.v = v
        def get(self): return self.v
        def set(self, v): self.v = v

    class Boton:
        def __init__(self): self.cfg = {"state": "disabled", "text": ""}
        def config(self, **k): self.cfg.update(k)

    class Etiqueta:
        def config(self, **k): pass

    class Raiz:
        def after(self, ms, fn=None, *a): return None

    class Fake(A.AsistenteDepositoApp):
        def __init__(self):        # sin Tk: solo se usan los métodos reales
            pass
    app = Fake()
    app.pedidos = {}; app.fase_actual = 1; app.colecta_global = {}
    app._ml_pedidos = pedidos_ml
    app._ml_filtro_tipo = Var("colecta"); app._canal_lote_activo = "colecta"
    app.var_proximos_dias = Var(False)
    app.btn_ml_agregar_lote = Boton(); app.lbl_estado_pdf = Etiqueta(); app.root = Raiz()
    app.db_nombres = {}; app.sku_item_id = {}; app.sku_nombre_ml = {}
    app._usuario_activo = {"usuario": "colector1", "nombre": "Colector 1", "cuenta_id": "cuenta_2", "rol": "operario"}
    app._lote_id_local = uuid.uuid4().hex
    subidas = []
    app._dibujar_fase1 = lambda: None
    app.actualizar_contador_global = lambda: None
    app._exportar_estado_movil = lambda: None
    app._sincronizar_desde_nube = lambda: None
    app._subir_a_nube_async = lambda: subidas.append(app._construir_payload_nube())
    for n, d in [("_colecta_qty", lambda sku: int(app.colecta_global.get(sku, 0) or 0)),
                 ("_ubicacion_sku", lambda sku: ("P1 PRUEBA", "V1")),
                 ("_nombre_sku", lambda sku: sku)]:
        setattr(app, n, d)

    def lote_inicial(pedidos, n):
        """Misma regla que _ml_generar_lote: un pedido interno por pedido ML."""
        app.pedidos.clear()
        for i, p in enumerate(pedidos[:n], start=1):
            req = {}
            for it in p.get("items", []):
                sku = (it.get("sku") or it.get("item_id") or "").upper().strip()
                if sku:
                    req[sku] = req.get(sku, 0) + it.get("cantidad", 1)
            app.pedidos[i] = {"pagina": i, "skus_requeridos": req, "skus_escaneados": {}, "impreso": False,
                              "descripcion": p.get("comprador", ""), "_order_id": p["order_id"],
                              "_cuenta": p.get("_cuenta", "cuenta_0"), "_shipping_id": p.get("shipping_id", ""),
                              "_logistica": p.get("logistica", ""), "items": p.get("items", []),
                              "comprador": p.get("comprador", "")}

    colecta_ped = [p for p in pedidos_ml.values()
                   if app._ml_en_scope(p, "colecta", True)]
    print(f"imprimibles de Colecta (reales): {len(colecta_ped)}")
    check("hay pedidos reales imprimibles para probar", len(colecta_ped) >= 6)
    N0 = max(3, len(colecta_ped) // 2)
    lote_inicial(colecta_ped, N0)

    print("1) botón: se habilita al tener lote (sin esperar el refresco de 60 s)")
    app._actualizar_btn_agregar()
    cand = app._ml_candidatos_agregar()
    check("candidatos = pendientes que NO están en el lote", len(cand) == len(colecta_ped) - N0, f"{len(cand)} vs {len(colecta_ped) - N0}")
    check("botón habilitado y con contador", app.btn_ml_agregar_lote.cfg["state"] == "normal"
          and f"({len(cand)})" in app.btn_ml_agregar_lote.cfg["text"], str(app.btn_ml_agregar_lote.cfg))
    app.fase_actual = 2; app._actualizar_btn_agregar()
    check("en Fase 2 el botón se deshabilita", app.btn_ml_agregar_lote.cfg["state"] == "disabled")
    app.fase_actual = 1

    print("2) lote inicial al servidor + escaneos del celular")
    pl = app._construir_payload_nube()
    r = requests.post(B + "/api/subir_estado", json=pl, headers=HK).json()
    check("servidor acepta el lote inicial", r.get("ok"), str(r))
    skus0 = [it["sku"] for g in pl["grupos"] for it in g["items"]]
    q = "usuario=colector1&cuenta_id=cuenta_2&canal=colecta"
    escaneados = {}
    for sku in skus0[:4]:
        rr = requests.post(B + "/api/escanear", json={"sku": sku, "usuario": "colector1", "cuenta_id": "cuenta_2", "canal": "colecta"}).json()
        if rr.get("ok") and rr.get("tipo") in ("parcial", "completo"):
            escaneados[sku] = escaneados.get(sku, 0) + 1
    est0 = requests.get(B + f"/api/estado?{q}").json()
    check("el celular escaneó SKUs del lote real", len(escaneados) >= 1 and est0["colecta"], str(est0.get("colecta")))
    col_antes = dict(est0["colecta"])

    print("3) ➕ Agregar al lote (función real de la app)")
    dialogos.clear(); subidas.clear()
    app._ml_agregar_al_lote()
    check("la app agregó pedidos (sin errores ni avisos)", not any(t in ("error", "warn") for t, _ in dialogos), str(dialogos))
    check("el lote creció", len(app.pedidos) > N0, f"{len(app.pedidos)} vs {N0}")
    check("lote = todos los pendientes del canal, sin duplicados",
          len({d["_order_id"] for d in app.pedidos.values()}) == len(app.pedidos) == len(colecta_ped),
          f"{len(app.pedidos)} / {len(colecta_ped)}")
    check("los pedidos agregados llevan _cuenta", all(d.get("_cuenta") == "cuenta_2" for d in app.pedidos.values()))
    check("la app subió el lote ampliado", len(subidas) == 1)
    check("botón sin pendientes tras agregar", "(" not in app.btn_ml_agregar_lote.cfg["text"], str(app.btn_ml_agregar_lote.cfg))
    r = requests.post(B + "/api/subir_estado", json=subidas[0], headers=HK).json()
    check("servidor acepta el lote ampliado", r.get("ok"), str(r))
    est1 = requests.get(B + f"/api/estado?{q}").json()
    check("MISMO lote_id (no se cambió de lote)", est1.get("lote_id") == est0.get("lote_id"), f"{est1.get('lote_id')} vs {est0.get('lote_id')}")
    check("lo escaneado desde el celular se CONSERVA", all(est1["colecta"].get(k, 0) >= v for k, v in col_antes.items()),
          f"antes={col_antes} despues={est1['colecta']}")
    skus1 = {it["sku"] for g in est1["grupos"] for it in g["items"]}
    skus_nuevos = {sku for d in app.pedidos.values() for sku in d["skus_requeridos"]}
    check("el celular ve los SKUs nuevos", skus_nuevos <= skus1, f"faltan {len(skus_nuevos - skus1)}")
    check("total_skus del servidor = el de la app", est1["total_skus"] == len(skus_nuevos))
    # agregar de nuevo no duplica
    dialogos.clear(); app._ml_agregar_al_lote()
    check("segundo clic: avisa que no hay nada nuevo", any("Sin pedidos nuevos" in str(t) for t in dialogos) or any(k == "info" for k, _ in dialogos), str(dialogos))
    check("segundo clic no duplicó pedidos", len(app.pedidos) == len(colecta_ped))

    print("4) canal: con un lote de Colecta no se cuelan pedidos Flex aunque la pestaña sea Flex")
    app._ml_filtro_tipo.set("flex")
    lote_inicial(colecta_ped, N0)
    cand = app._ml_candidatos_agregar()
    check("candidatos solo del canal del lote", all(app._tipo_logistica(p) == "colecta" for p in cand), str({app._tipo_logistica(p) for p in cand}))
finally:
    srv.kill()
print(f"\nRESULTADO agregar-al-lote: {ok_n} ok, {fail_n} fallas")
sys.exit(1 if fail_n else 0)
