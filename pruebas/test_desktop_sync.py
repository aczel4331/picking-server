"""Logica de sincronizacion del escritorio (sin GUI): decrementos autoritativos, rev/base_rev, descarte de polls viejos."""
import os, sys, types
sys.stdout.reconfigure(encoding="utf-8")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, REPO)
import app_deposito as A
ok = fa = 0
def check(n, c, x=""):
    global ok, fa
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else "  -> " + str(x)))
    if c: ok += 1
    else: fa += 1

C = A.PickingApp if hasattr(A, "PickingApp") else None
if C is None:
    C = next(v for v in vars(A).values() if isinstance(v, type) and hasattr(v, "_sincronizar_colecta_movil"))

class Root:
    def update_idletasks(self): pass
    def after(self, *a, **k): pass
    def title(self, *a): pass

def mk(fase=1, lote="L1"):
    o = types.SimpleNamespace()
    o.pedidos = {1: {"skus_requeridos": {"A": 2, "B": 1}}}
    o.colecta_global = {}; o.fase_actual = fase; o._lote_id_local = lote
    o.root = Root(); o.fase1_items = {}
    o._colecta_qty = lambda s: C._colecta_qty(o, s)
    o.actualizar_contador_global = lambda: None
    o._flash_sku_escaneado = lambda s: None
    o._revisar_pedidos_listos = lambda: None
    o._actualizar_checkmarks_fase1 = lambda s: None
    o._ejecutar_transicion_fase2 = lambda: setattr(o, "fase_actual", 2)
    o.lbl_resultado = types.SimpleNamespace(config=lambda **k: None)
    o.col_control = types.SimpleNamespace(config=lambda **k: None)
    o._registrar_rev_nube = lambda l, r: C._registrar_rev_nube(o, l, r)
    o._rev_vieja_sigue = lambda: C._rev_vieja_sigue(o)
    o._colecta_pendiente = set()
    o._marcar_colecta_pendiente = lambda s: C._marcar_colecta_pendiente(o, s)
    o._sincronizar_colecta_movil = lambda c, autoritativo=False: C._sincronizar_colecta_movil(o, c, autoritativo)
    return o

print("legado: solo incrementa")
o = mk(); o.colecta_global = {"A": 2}
C._sincronizar_colecta_movil(o, {"A": 1})
check("no decrementa sin autoritativo", o.colecta_global["A"] == 2, o.colecta_global)

print("autoritativo: decrementos")
o = mk(); o.colecta_global = {"A": 2}
C._sincronizar_colecta_movil(o, {"A": 1}, autoritativo=True)
check("A baja a 1", o.colecta_global.get("A") == 1, o.colecta_global)
C._sincronizar_colecta_movil(o, {}, autoritativo=True)
check("deshecho hasta 0 -> se borra", "A" not in o.colecta_global, o.colecta_global)
o = mk(fase=2); o.colecta_global = {"A": 2}
C._sincronizar_colecta_movil(o, {}, autoritativo=True)
check("no toca colecta fuera de Fase 1", o.colecta_global == {"A": 2}, o.colecta_global)

print("payload base_rev")
o = mk(); o.canal = None
C._registrar_rev_nube(o, "L1", 5); C._registrar_rev_nube(o, "L1", 3)
check("rev monotonica", o._rev_nube == 5, o._rev_nube)
C._registrar_rev_nube(o, "L2", 1)
check("lote nuevo reinicia rev", (o._rev_nube_lote, o._rev_nube) == ("L2", 1))

print("_on_sync_nube descarta lote ajeno / rev vieja / poll anterior a subida")
class Fake(types.SimpleNamespace): pass
def sync(o, info):
    got = []
    o._sincronizar_colecta_movil = lambda c, autoritativo=False: got.append((dict(c), autoritativo))
    o._barra_op_conexion = lambda *a, **k: None
    o.n = 0
    base = {"ok": True, "n": 0, "err": None, "fase": 1}
    base.update(info)
    try:
        C._on_sync_nube(o, base)
    except Exception as e:
        got.append(("EXC", repr(e)))
    return got
o = mk(); o._t_subida = 0
r = sync(o, {"colecta": {"A": 1}, "lote_id": "L1", "lote_id_local": "L1", "rev": 4, "autoritativo": True, "t0": 10})
check("aplica lote propio", r == [({"A": 1}, True)], r)
r = sync(o, {"colecta": {"A": 2}, "lote_id": "L0", "lote_id_local": "L0", "rev": 9, "autoritativo": True, "t0": 10})
check("descarta lote anterior", not r or all(not c for c, _ in r), r)
r = sync(o, {"colecta": {"A": 2}, "lote_id": "L1", "lote_id_local": "L1", "rev": 2, "autoritativo": True, "t0": 10})
check("descarta rev vieja", not r or all(not c for c, _ in r), r)
o._t_subida = 50
r = sync(o, {"colecta": {"A": 2}, "lote_id": "L1", "lote_id_local": "L1", "rev": 7, "autoritativo": True, "t0": 10})
check("descarta poll anterior a la subida", not r or all(not c for c, _ in r), r)

print("escaneos locales pendientes: ni un sondeo ni una respuesta atrasada los bajan o borran")
o = mk(); o.colecta_global = {"A": 2, "B": 1}; o._colecta_pendiente = {"A"}
C._sincronizar_colecta_movil(o, {}, autoritativo=True)
check("A (pendiente) se conserva y B (confirmado) se borra", o.colecta_global == {"A": 2}, o.colecta_global)
o = mk(); o.colecta_global = {"A": 2, "B": 2}; o._colecta_pendiente = {"A"}
C._sincronizar_colecta_movil(o, {"A": 1, "B": 1}, autoritativo=True)
check("el servidor trae menos: A (pendiente) no baja, B (confirmado) baja", o.colecta_global == {"A": 2, "B": 1}, o.colecta_global)
o = mk(); o.colecta_global = {"A": 1}; o._colecta_pendiente = {"A"}
C._sincronizar_colecta_movil(o, {"A": 3}, autoritativo=True)
check("si el servidor trae MAS, sube igual aunque este pendiente", o.colecta_global == {"A": 3}, o.colecta_global)
o = mk(); o._marcar_colecta_pendiente("Z")
check("marcar pendiente", o._colecta_pendiente == {"Z"})
print("rev que retrocede: a la 3a respuesta vieja se acepta")
o = mk(); o._rev_viejos = 0; o._rev_nube_lote = "L"; o._rev_nube = 100
r = [C._rev_vieja_sigue(o), C._rev_vieja_sigue(o), C._rev_vieja_sigue(o)]
check("descarta, descarta, acepta (y reinicia la referencia)", r == [True, True, False] and o._rev_nube_lote is None, (r, o._rev_nube_lote))
print(f"\n{ok} OK, {fa} FALLAS"); sys.exit(1 if fa else 0)
