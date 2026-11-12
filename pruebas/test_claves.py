"""Contraseñas cifradas (auditoría 11/11/2026, paso 4): al arrancar se cifran las claves en texto plano (con
respaldo para volver atrás), los sha256 viejos se cifran en el primer login, el hash guardado ya no sirve como
clave, toda alta o cambio queda cifrado y el login del panel web tiene límite de intentos. Todo local."""
import os, sys, json, tempfile, hashlib
sys.stdout.reconfigure(encoding="utf-8")
S_DIR = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(S_DIR)
DATA = tempfile.mkdtemp()
SHA = hashlib.sha256(b"clave-sha").hexdigest()
ORIG = [{"usuario": "admin", "clave": "adm-plano", "nombre": "Admin", "cuenta_id": "todas", "rol": "admin"},
        {"usuario": "op1", "clave": "op-plano", "nombre": "Op", "cuenta_id": "cuenta_2", "rol": "operario"},
        {"usuario": "sup1", "clave": SHA, "nombre": "Sup", "cuenta_id": "cuenta_2", "rol": "supervisor"}]
json.dump(ORIG, open(os.path.join(DATA, "usuarios.json"), "w", encoding="utf-8"))
os.environ.update(DATA_DIR=DATA, PICKING_API_KEY="everest2024", APP_SECRET_KEY="s" * 40, ML_APP_ID="x",
                  ML_SECRET_KEY="x", ML_API_URL="http://127.0.0.1:1")
sys.path.insert(0, REPO); os.chdir(REPO)
import server as S

ok = fa = 0
def check(n, c, x=""):
    global ok, fa
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else "  -> " + str(x)[:200]))
    if c: ok += 1
    else: fa += 1

def en_disco():
    return {u["usuario"]: u["clave"] for u in json.load(open(os.path.join(DATA, "usuarios.json"), encoding="utf-8"))}
C = S.app.test_client()
def login(u, c, ip="10.0.0.1"):
    return C.post("/api/auth/login", json={"usuario": u, "clave": c}, headers={"X-Forwarded-For": ip})

print("1) al arrancar")
d = en_disco()
check("las claves en texto plano quedaron cifradas en disco", d["admin"].startswith("pbkdf2:") and d["op1"].startswith("pbkdf2:")
      and "adm-plano" not in json.dumps(d) and "op-plano" not in json.dumps(d), d)
check("la sha256 vieja queda igual hasta su próximo login", d["sup1"] == SHA)
bak = os.path.join(DATA, "usuarios.json.antes_de_cifrar.bak")
check("quedó el respaldo para volver atrás (idéntico al original)", os.path.exists(bak)
      and json.load(open(bak, encoding="utf-8")) == ORIG)

print("2) login")
r = login("op1", "op-plano")
check("clave que estaba en texto plano: entra", r.status_code == 200 and r.get_json().get("ok"), r.status_code)
check("clave equivocada: no entra", login("op1", "otra").status_code == 401)
r = login("sup1", "clave-sha")
check("clave sha256 vieja: entra", r.status_code == 200 and r.get_json().get("ok"), r.status_code)
check("…y en ese login se cifró", en_disco()["sup1"].startswith("pbkdf2:"))
check("…y sigue entrando con la cifrada", login("sup1", "clave-sha").status_code == 200)
check("escribir el hash guardado como clave NO entra (antes sí con sha256)", login("op1", en_disco()["op1"]).status_code == 401)
S._usuarios.append({"usuario": "viejo", "clave": SHA, "nombre": "V", "cuenta_id": "cuenta_2", "rol": "operario"})
check("con una sha256 guardada, mandar esa sha como clave NO entra", login("viejo", SHA).status_code == 401)
check("mayúsculas en el usuario siguen funcionando", login("OP1", "op-plano").status_code == 200)

print("3) altas y cambios quedan cifrados")
tok = login("admin", "adm-plano").get_json()["token"]
H = {"X-API-Key": tok}
r = C.post("/api/auth/usuarios", headers=H, json={"usuario": "nuevo1", "clave": "n-1234", "nombre": "N", "cuenta_id": "cuenta_2", "rol": "operario"})
check("alta de usuario ok", r.status_code == 200 and r.get_json().get("ok"), r.data[:120])
check("alta: la clave se guarda cifrada", en_disco()["nuevo1"].startswith("pbkdf2:") and "n-1234" not in open(os.path.join(DATA, "usuarios.json"), encoding="utf-8").read())
check("alta: entra con su clave", login("nuevo1", "n-1234").status_code == 200)
r = C.put("/api/auth/usuarios/nuevo1", headers=H, json={"clave": "n-5678"})
check("edición con clave nueva: cifrada y funciona", r.status_code == 200 and en_disco()["nuevo1"].startswith("pbkdf2:")
      and login("nuevo1", "n-5678").status_code == 200 and login("nuevo1", "n-1234").status_code == 401, r.data[:120])
r = C.put("/api/auth/usuarios/nuevo1/clave", headers=H, json={"clave": "n-9999"})
check("cambio de clave: cifrada (antes sha256) y funciona", r.status_code == 200 and en_disco()["nuevo1"].startswith("pbkdf2:")
      and login("nuevo1", "n-9999").status_code == 200, r.data[:120])
lista = C.get("/api/auth/usuarios", headers=H).get_json().get("usuarios", [])
check("la lista de usuarios no devuelve claves", lista and all("clave" not in u for u in lista))

print("4) panel web: límite de intentos")
P = S.app.test_client()
def panel(u, c, ip="10.9.9.9"):
    return P.post("/admin/usuarios", data={"usuario": u, "clave": c}, headers={"X-Forwarded-For": ip})
cods = [panel("admin", f"mala{i}").status_code for i in range(8)]
check("8 claves equivocadas: rechazadas", all(c == 200 for c in cods), cods)
r = panel("admin", "adm-plano")
check("el 9º intento, aunque sea correcto, se frena (429)", r.status_code == 429 and "Demasiados intentos" in r.get_data(as_text=True), r.status_code)
r = S.app.test_client().post("/admin/usuarios", data={"usuario": "admin", "clave": "adm-plano"}, headers={"X-Forwarded-For": "10.8.8.8"})
check("desde otra IP el admin entra normal", r.status_code in (200, 302) and "Demasiados" not in r.get_data(as_text=True), r.status_code)
check("el límite es compartido con la app (misma IP y usuario)", login("admin", "adm-plano", ip="10.9.9.9").status_code == 429)

print(f"\nRESULTADO claves: {ok} ok, {fa} fallas"); sys.exit(1 if fa else 0)
