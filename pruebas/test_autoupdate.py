"""Actualizacion automatica: publicar/consultar/descargar en el servidor LOCAL y verificacion del actualizador
(sha256, zip con Logibot.exe, firma Ed25519). Nada toca produccion."""
import os, sys, io, json, time, shutil, base64, hashlib, zipfile, tempfile, subprocess, threading
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
sys.stdout.reconfigure(encoding="utf-8")
S = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(S)
PORT = "5145"; B = f"http://127.0.0.1:{PORT}"; DATA = os.path.join(S, "data_autoupdate")
ok = fa = 0
def check(n, c, x=""):
    global ok, fa
    print(("  OK    " if c else "  FALLA ") + n + ("" if c else "  -> " + str(x)))
    if c: ok += 1
    else: fa += 1

sys.path.insert(0, REPO)
import logibot_updater as U

# clave de PRUEBA (no la real): se inyecta en el actualizador para firmar zips de prueba
priv = Ed25519PrivateKey.generate()
from cryptography.hazmat.primitives import serialization
U.CLAVE_PUBLICA_FIRMA = base64.b64encode(priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
def firmar(version, sha): return base64.b64encode(priv.sign(U.mensaje_firmado(version, sha))).decode()

def hacer_zip(con_exe=True, marca="x"):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        if con_exe: z.writestr("Logibot.exe", b"MZ fake " + marca.encode())
        z.writestr("_internal/base_library.zip", b"lib " + marca.encode())
    return b.getvalue()

shutil.rmtree(DATA, ignore_errors=True); os.makedirs(DATA)
env = dict(os.environ, PORT=PORT, DATA_DIR=DATA, PICKING_API_KEY="clave-maestra-prueba", REPO=REPO,
           PEDIDOS_SNAP=os.path.join(S, "snap", "pedidos_live.json"), APP_SECRET_KEY="s" * 40,
           ML_API_URL="http://127.0.0.1:1", ML_APP_ID="x", ML_SECRET_KEY="x", PYTHONIOENCODING="utf-8")
env.pop("APP_PUBLICAR_KEY", None)
env["APP_FIRMA_PUB"] = U.CLAVE_PUBLICA_FIRMA     # el servidor local verifica con la clave de PRUEBA
srv = subprocess.Popen([sys.executable, os.path.join(S, "run_srv_pedidos.py")], env=env,
                       stdout=open(os.path.join(S, "autoupdate.log"), "w"), stderr=subprocess.STDOUT)
K = {"X-API-Key": "clave-maestra-prueba"}
def publicar(version, data, firma=None, clave=K):
    sha = hashlib.sha256(data).hexdigest()
    return requests.post(B + "/api/admin/app/publicar", headers=clave,
                         data={"version": version, "notas": f"prueba {version}", "firma": firma if firma is not None else firmar(version, sha)},
                         files={"zip": (f"Logibot_v{version}.zip", data, "application/zip")})
try:
    for _ in range(80):
        try: requests.get(B + "/api/ping", timeout=1); break
        except Exception: time.sleep(.5)

    print("1) SERVIDOR: publicar y consultar")
    check("sin versiones -> 404", requests.get(B + "/api/app/version").status_code == 404)
    z1 = hacer_zip(marca="v1")
    check("publicar sin clave -> 401", publicar("4.1.32", z1, clave={}).status_code == 401)
    check("publicar con la clave heredada (la de los exe) -> 401", publicar("4.1.32", z1, clave={"X-API-Key": "everest2024"}).status_code == 401)
    check("zip invalido -> 400", publicar("4.1.32", b"esto no es un zip").status_code == 400)
    check("zip sin Logibot.exe -> 400", publicar("4.1.32", hacer_zip(con_exe=False)).status_code == 400)
    check("version mal escrita -> 400", publicar("4.1", z1).status_code == 400)
    check("sin firma -> 400", publicar("4.1.32", z1, firma="").status_code == 400)
    otra_k = Ed25519PrivateKey.generate()
    firma_mala = base64.b64encode(otra_k.sign(U.mensaje_firmado("4.1.32", hashlib.sha256(z1).hexdigest()))).decode()
    check("firma de otra clave -> 403 (aunque tenga la clave de la API)", publicar("4.1.32", z1, firma=firma_mala).status_code == 403)
    check("firma de otro archivo -> 403", publicar("4.1.32", z1, firma=firmar("4.1.32", "0" * 64)).status_code == 403)
    check("nada quedo publicado tras los rechazos", requests.get(B + "/api/app/version").status_code == 404)
    r = publicar("4.1.32", z1)
    check("publicar 4.1.32 -> ok", r.status_code == 200 and r.json().get("ok"), r.text[:200])
    v = requests.get(B + "/api/app/version").json()
    check("/api/app/version la informa con sha256 y url de descarga", v.get("version") == "4.1.32" and v.get("sha256") == hashlib.sha256(z1).hexdigest()
          and v.get("url") == "/api/app/descarga/Logibot_v4.1.32.zip" and v.get("firma"), v)
    d = requests.get(B + v["url"])
    check("la descarga es el mismo archivo, byte a byte", d.status_code == 200 and d.content == z1)
    for malo in ("..%2F..%2Fusuarios.json", "releases.json", "Logibot_v4.1.32.zip.tmp", "Logibot_v9.9.9.zip"):
        check(f"descarga de '{malo}' -> 404", requests.get(B + "/api/app/descarga/" + malo).status_code == 404)
    check("publicar una version menor o igual -> 400", publicar("4.1.32", z1).status_code == 400 and publicar("4.1.20", z1).status_code == 400)
    for ver in ("4.1.33", "4.1.34", "4.1.35"):
        publicar(ver, hacer_zip(marca=ver))
    rels = json.load(open(os.path.join(DATA, "app_releases", "releases.json"), encoding="utf-8"))
    archivos = sorted(f for f in os.listdir(os.path.join(DATA, "app_releases")) if f.endswith(".zip"))
    check("se conservan solo las 3 ultimas", [r["version"] for r in rels] == ["4.1.33", "4.1.34", "4.1.35"] and len(archivos) == 3, (rels, archivos))

    print("2) ACTUALIZADOR: consulta al servidor y respaldo en GitHub")
    got = []; ev = threading.Event()
    U.verificar_actualizacion("4.1.31", lambda i: (got.append(i), ev.set()), servidor=B); ev.wait(15)
    i = got[0] if got else {}
    check("encuentra la nueva en NUESTRO servidor, con url completa, sha y firma",
          i.get("origen") == "servidor" and i.get("version") == "4.1.35" and i.get("url_exe") == B + "/api/app/descarga/Logibot_v4.1.35.zip"
          and i.get("sha256") and i.get("firma"), i)
    got.clear(); ev.clear()
    U.verificar_actualizacion("4.1.35", lambda i: (got.append(i), ev.set()), servidor=B); ev.wait(3)
    check("si ya esta al dia no avisa", not got, got)
    orig_get = U._get_json
    def falso(url, timeout=10):
        if "/api/app/version" in url: raise OSError("servidor caido")
        return {"version": "4.1.40", "url_exe": "https://github.com/x.zip", "notas": "gh"}
    U._get_json = falso; got.clear(); ev.clear()
    U.verificar_actualizacion("4.1.31", lambda i: (got.append(i), ev.set()), servidor="http://127.0.0.1:1"); ev.wait(10)
    U._get_json = orig_get
    check("servidor caido -> usa GitHub como antes (sin firma: solo aviso con boton)", got and got[0]["origen"] == "github" and not got[0]["firma"], got)

    print("3) ACTUALIZADOR: nunca deja lista una version rota, ajena o sin firma")
    tmp = tempfile.mkdtemp(); U._dir_instalacion = lambda: tmp
    def bajar(info, **kw):
        res = {}; e = threading.Event()
        U.descargar_e_instalar(info["url_exe"], info["version"], lambda p: None,
                               lambda: (res.setdefault("ok", True), e.set()), lambda m: (res.setdefault("err", m), e.set()),
                               sha256=kw.get("sha256", info.get("sha256", "")), firma=kw.get("firma", info.get("firma", "")),
                               requerir_firma=kw.get("requerir_firma", True))
        e.wait(30); return res
    v = requests.get(B + "/api/app/version").json(); info = {"version": v["version"], "url_exe": B + v["url"], "sha256": v["sha256"], "firma": v["firma"]}
    r = bajar(info)
    check("version firmada y correcta -> lista para instalar", r.get("ok") and os.path.exists(os.path.join(tmp, "_update_pending.txt")), r)
    os.remove(os.path.join(tmp, "_update_pending.txt"))
    r = bajar(info, sha256="0" * 64)
    check("sha256 distinto -> NO se instala", "sha256" in r.get("err", "") and not os.path.exists(os.path.join(tmp, "_update_pending.txt")), r)
    otra = Ed25519PrivateKey.generate()
    firma_ajena = base64.b64encode(otra.sign(U.mensaje_firmado(info["version"], info["sha256"]))).decode()
    r = bajar(info, firma=firma_ajena)
    check("firma de otra clave -> NO se instala", "firma" in r.get("err", ""), r)
    r = bajar(info, firma="")
    check("sin firma y con requerir_firma -> NO se instala", "firma" in r.get("err", ""), r)
    check("la firma de una version no sirve para otra", not U.verificar_firma("4.1.99", info["sha256"], info["firma"]))
    shutil.rmtree(tmp, ignore_errors=True)
finally:
    srv.terminate()
    try: srv.wait(10)
    except Exception: srv.kill()
print(f"\n{ok} OK, {fa} FALLAS"); sys.exit(1 if fa else 0)
