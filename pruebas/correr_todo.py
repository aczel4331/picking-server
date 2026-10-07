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
with urllib.request.urlopen(URL + "/api/pedidos", timeout=30) as r, open(os.path.join(AQUI, "snap", "pedidos_live.json"), "wb") as f:
    f.write(r.read())
viejo = os.path.join(AQUI, "old_head")
shutil.rmtree(viejo, ignore_errors=True); os.makedirs(viejo)
open(os.path.join(viejo, "server.py"), "wb").write(subprocess.check_output(["git", "-C", REPO, "show", f"{ref}:server.py"]))
shutil.copytree(os.path.join(REPO, "templates"), os.path.join(viejo, "templates"))

env = dict(os.environ, PYTHONIOENCODING="utf-8")
fallas = 0
for t in ("test_agregar.py", "test_integral.py", "test_p2.py", "test_stats_sel.py", "test_etiqueta_vacia.py", "test_imp_blanco.py", "test_empacar.py", "test_diff2.py"):
    print(f"\n════ {t} ════", flush=True)
    fallas += subprocess.call([sys.executable, os.path.join(AQUI, t)], env=env) != 0
print("\nTODO EN VERDE" if not fallas else f"\n{fallas} grupo(s) con fallas")
sys.exit(1 if fallas else 0)
