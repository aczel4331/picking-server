"""Corre TODAS las pruebas de Logibot de una vez, con los pedidos reales cargados.

  python pruebas/correr_todo.py            → baja una foto de SOLO LECTURA de los pedidos de
                                              producción (un GET) y prueba en un servidor LOCAL.
  python pruebas/correr_todo.py <ref-git>  → además compara contra el servidor de esa versión
                                              (por defecto, HEAD) para ver que Everest no cambia.

Nada se escribe en producción ni en Mercado Libre: la tienda no nota nada.
La foto (pedidos reales) queda en pruebas/snap/ y NO se sube a git."""
import os, subprocess, sys, shutil, urllib.request

AQUI = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(AQUI)
URL = os.environ.get("SERVIDOR", "https://picking-server-production.up.railway.app")
ref = sys.argv[1] if len(sys.argv) > 1 else "HEAD"

os.makedirs(os.path.join(AQUI, "snap"), exist_ok=True)
SNAP = os.path.join(AQUI, "snap", "pedidos_live.json")
MIN_PEDIDOS = 100     # varias pruebas suponen bastantes pedidos reales: una foto casi vacia no las deja correr
with urllib.request.urlopen(URL + "/api/pedidos", timeout=30) as r:
    _foto = r.read()
try:
    _n = len(__import__("json").loads(_foto).get("pedidos", []))
except Exception:
    _n = 0
if _n >= MIN_PEDIDOS or not os.path.exists(SNAP):
    with open(SNAP, "wb") as f:
        f.write(_foto)
    print(f"Foto de pedidos reales actualizada: {_n} pedidos")
else:
    print(f"La foto de hoy trae solo {_n} pedidos (< {MIN_PEDIDOS}): se conserva la anterior para que las pruebas tengan datos")
viejo = os.path.join(AQUI, "old_head")
shutil.rmtree(viejo, ignore_errors=True); os.makedirs(viejo)
open(os.path.join(viejo, "server.py"), "wb").write(subprocess.check_output(["git", "-C", REPO, "show", f"{ref}:server.py"]))
shutil.copytree(os.path.join(REPO, "templates"), os.path.join(viejo, "templates"))

env = dict(os.environ, PYTHONIOENCODING="utf-8")
fallas = 0
for t in ("test_agregar.py", "test_integral.py", "test_p2.py", "test_stats_sel.py", "test_etiqueta_vacia.py", "test_imp_blanco.py", "test_empacar.py", "test_refresh.py", "test_refresh_app.py", "test_lote_nuevo.py", "test_pack_envio.py", "test_pack_app.py", "test_lote_replay.py", "test_rev_sync.py", "test_rev_b.py", "test_desktop_sync.py", "test_movil_sync.py", "test_diff2.py"):
    print(f"\n════ {t} ════", flush=True)
    fallas += subprocess.call([sys.executable, os.path.join(AQUI, t)], env=env) != 0
print("\nTODO EN VERDE" if not fallas else f"\n{fallas} grupo(s) con fallas")
sys.exit(1 if fallas else 0)
