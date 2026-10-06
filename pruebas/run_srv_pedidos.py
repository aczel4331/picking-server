"""Servidor LOCAL de pruebas: carga el servidor real, le inyecta los pedidos reales
(foto de solo lectura de producción) y tokens falsos de ML. No toca producción ni ML.
Extra solo para tests: /__test/inject para sembrar pedidos de otra tienda."""
import os, sys, json
from datetime import datetime, timedelta
REPO_DEFAULT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.environ.get("REPO", REPO_DEFAULT)
sys.path.insert(0, REPO); os.chdir(REPO)
import server as S
from flask import request, jsonify

# Las pruebas deben dar lo mismo de día y de noche: se simula horario laboral.
S._en_modo_descanso = lambda: False

fut = datetime.now() + timedelta(hours=5)
snap = os.environ.get("PEDIDOS_SNAP")
if snap and os.path.exists(snap):
    with open(snap, encoding="utf-8") as f:
        for p in json.load(f)["pedidos"]:
            S._pedidos_ml[str(p["order_id"])] = p
S._cuentas["cuenta_2"] = {"access_token": "AT-FAKE", "refresh_token": "RT", "expires_at": fut,
                          "user_id": "502960739", "nickname": "EVEREST_SHOPPING.UY"}
try:
    S._asegurar_primaria()
except Exception:
    pass


@S.app.route("/__test/inject", methods=["POST"])
def _inject():
    d = request.get_json(force=True)
    for p in d.get("pedidos", []):
        S._pedidos_ml[str(p["order_id"])] = p
    for cid, tok in (d.get("cuentas") or {}).items():
        tok["expires_at"] = fut
        S._cuentas[cid] = tok
    return jsonify(ok=True, n=len(S._pedidos_ml))


@S.app.route("/__test/estado")
def _estado_dbg():
    return jsonify(claves=list(S._estados_canal.keys()), pedidos=len(S._pedidos_ml))


S.app.run(host="127.0.0.1", port=int(os.environ["PORT"]), debug=False, use_reloader=False, threaded=True)
