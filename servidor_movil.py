"""
servidor_movil.py
=================
Servidor Flask que expone el estado de la Fase 1 al celular.
Corre en la misma PC que app_deposito.py.
Usa HTTPS con certificado autofirmado para que Chrome permita la cámara.

Uso:
    python servidor_movil.py

Luego en el celular abrir Chrome y entrar a:
    https://<IP_DE_LA_PC>:5050

IMPORTANTE: Chrome va a mostrar "Conexión no segura" la primera vez.
Tocá "Configuración avanzada" → "Continuar con <IP>" para aceptar el certificado.
Eso es normal con certificados autofirmados — la conexión sigue siendo cifrada.
"""

from flask import Flask, jsonify, request, send_from_directory
import json, os, threading

# ── Ruta al archivo de estado compartido ──────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
ESTADO_PATH = os.path.join(BASE_DIR, "estado_picking.json")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CERT_PATH   = os.path.join(BASE_DIR, "ssl_cert.pem")
KEY_PATH    = os.path.join(BASE_DIR, "ssl_key.pem")

app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "movil_static"))
_lock = threading.Lock()

# ── Generar certificado SSL autofirmado si no existe ──────────────────────────

def generar_certificado_ssl(ip_local):
    """Genera un certificado SSL autofirmado válido para la IP local."""
    if os.path.exists(CERT_PATH) and os.path.exists(KEY_PATH):
        print("  ✔  Certificado SSL existente reutilizado.")
        return True
    try:
        from OpenSSL import crypto
        k = crypto.PKey()
        k.generate_key(crypto.TYPE_RSA, 2048)

        cert = crypto.X509()
        cert.get_subject().C  = "AR"
        cert.get_subject().O  = "Picking App"
        cert.get_subject().CN = ip_local
        cert.set_serial_number(1)
        cert.gmtime_adj_notBefore(0)
        cert.gmtime_adj_notAfter(365 * 24 * 60 * 60)  # 1 año
        cert.set_issuer(cert.get_subject())
        cert.set_pubkey(k)

        # SAN (Subject Alternative Name) — necesario para Chrome moderno
        san = f"IP:{ip_local},IP:127.0.0.1,DNS:localhost"
        cert.add_extensions([
            crypto.X509Extension(b"subjectAltName", False, san.encode()),
            crypto.X509Extension(b"basicConstraints", True, b"CA:TRUE"),
        ])
        cert.sign(k, "sha256")

        with open(CERT_PATH, "wb") as f:
            f.write(crypto.dump_certificate(crypto.FILETYPE_PEM, cert))
        with open(KEY_PATH, "wb") as f:
            f.write(crypto.dump_privatekey(crypto.FILETYPE_PEM, k))

        print("  ✔  Certificado SSL generado correctamente.")
        return True
    except ImportError:
        print("  ⚠  pyopenssl no instalado. Ejecutá: pip install pyopenssl")
        return False
    except Exception as e:
        print(f"  ⚠  No se pudo generar certificado SSL: {e}")
        return False


# ── Helpers ───────────────────────────────────────────────────────────────────

def leer_estado():
    if not os.path.exists(ESTADO_PATH):
        return {"fase": 1, "grupos": [], "colecta": {}, "total_skus": 0, "total_uds": 0}
    with _lock:
        with open(ESTADO_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

def guardar_estado(estado):
    with _lock:
        with open(ESTADO_PATH, "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False, indent=2)

# ── API endpoints ─────────────────────────────────────────────────────────────

@app.route("/api/estado")
def api_estado():
    return jsonify(leer_estado())

@app.route("/api/escanear", methods=["POST"])
def api_escanear():
    data = request.get_json(force=True)
    sku  = str(data.get("sku", "")).strip().upper()
    if not sku:
        return jsonify({"ok": False, "msg": "SKU vacío"})

    estado = leer_estado()
    colecta = estado.get("colecta", {})

    sku_info = None
    for grupo in estado.get("grupos", []):
        for item in grupo.get("items", []):
            if item["sku"] == sku:
                sku_info = item
                break
        if sku_info:
            break

    if not sku_info:
        return jsonify({"ok": False, "tipo": "no_encontrado",
                        "msg": f"'{sku}' no está en ningún pedido"})

    req       = sku_info["req"]
    collected = colecta.get(sku, 0)

    if collected >= req:
        return jsonify({"ok": True, "tipo": "ya_completo",
                        "msg": f"'{sku}' ya estaba completo ({req}/{req})",
                        "collected": collected, "req": req})

    colecta[sku] = collected + 1
    estado["colecta"] = colecta

    todo = all(
        colecta.get(it["sku"], 0) >= it["req"]
        for g in estado["grupos"] for it in g["items"]
    )
    estado["colecta_completa"] = todo
    guardar_estado(estado)

    nuevo = colecta[sku]
    return jsonify({
        "ok":         True,
        "tipo":       "completo" if nuevo >= req else "parcial",
        "sku":        sku,
        "nombre":     sku_info.get("nombre", ""),
        "pasillo":    sku_info.get("pasillo", ""),
        "estanteria": sku_info.get("estanteria", ""),
        "collected":  nuevo,
        "req":        req,
        "todo_completo": todo,
        "msg":        f"✔ {nuevo}/{req}" if nuevo >= req else f"{nuevo}/{req}"
    })

@app.route("/api/reset_sku", methods=["POST"])
def api_reset_sku():
    data = request.get_json(force=True)
    sku  = str(data.get("sku", "")).strip().upper()
    estado  = leer_estado()
    colecta = estado.get("colecta", {})
    if sku in colecta and colecta[sku] > 0:
        colecta[sku] -= 1
        if colecta[sku] == 0:
            del colecta[sku]
        estado["colecta"] = colecta
        estado["colecta_completa"] = False
        guardar_estado(estado)
        return jsonify({"ok": True, "msg": f"Deshecho: {sku}"})
    return jsonify({"ok": False, "msg": "Nada que deshacer"})

# ── PWA estática ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(os.path.join(BASE_DIR, "movil_static"), "index.html")

@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(os.path.join(BASE_DIR, "movil_static"), filename)

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip_local = s.getsockname()[0]
        s.close()
    except Exception:
        ip_local = "127.0.0.1"

    ssl_ok = generar_certificado_ssl(ip_local)

    print("=" * 56)
    print("  SERVIDOR PICKING MÓVIL")
    if ssl_ok:
        print(f"  Abrí esto en el celular (misma red WiFi):")
        print(f"  ➜  https://{ip_local}:5050")
        print()
        print("  ⚠  Chrome va a mostrar 'Conexión no segura'.")
        print("     Tocá 'Configuración avanzada' → 'Continuar'")
        print("     Eso solo ocurre la primera vez.")
    else:
        print(f"  ➜  http://{ip_local}:5050  (sin cámara)")
        print("  Instalá pyopenssl para activar HTTPS + cámara:")
        print("  pip install pyopenssl")
    print("=" * 56)

    if ssl_ok:
        app.run(host="0.0.0.0", port=5050, debug=False,
                ssl_context=(CERT_PATH, KEY_PATH))
    else:
        app.run(host="0.0.0.0", port=5050, debug=False)