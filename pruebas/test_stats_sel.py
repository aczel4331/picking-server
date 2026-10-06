"""Selector de tienda en /estadisticas (solo admin general)."""
import os, sys, json, time, shutil, subprocess, base64, io
import requests
from PIL import Image
sys.stdout.reconfigure(encoding="utf-8")
S=os.path.dirname(os.path.abspath(__file__)); REPO=os.path.dirname(S)
PORT="5133"; B=f"http://127.0.0.1:{PORT}"; DATA=os.path.join(S,"data_stats")
ok=fa=0
def check(n,c,x=""):
    global ok,fa
    print(("  OK    " if c else "  FALLA ")+n+("" if c else "  -> "+str(x)))
    if c: ok+=1
    else: fa+=1
shutil.rmtree(DATA,ignore_errors=True); os.makedirs(DATA)
json.dump([{"usuario":"admin","clave":"1234","nombre":"Admin","cuenta_id":"todas","rol":"admin"},
 {"usuario":"sup2","clave":"abcd","nombre":"Sup","cuenta_id":"cuenta_2","rol":"supervisor"}],open(os.path.join(DATA,"usuarios.json"),"w"))
env=dict(os.environ,PORT=PORT,DATA_DIR=DATA,PICKING_API_KEY="everest2024",REPO=REPO,PEDIDOS_SNAP=os.path.join(S,"snap","pedidos_live.json"),
 APP_SECRET_KEY="s"*40,ML_API_URL="http://127.0.0.1:1",ML_APP_ID="x",ML_SECRET_KEY="x",PYTHONIOENCODING="utf-8",MULTI_TIENDA="1")
srv=subprocess.Popen([sys.executable,os.path.join(S,"run_srv_pedidos.py")],env=env,stdout=open(os.path.join(S,"stats.log"),"w"),stderr=subprocess.STDOUT)
try:
    for _ in range(80):
        try: requests.get(B+"/api/ping",timeout=1); break
        except Exception: time.sleep(.5)
    hoy=time.strftime("%Y-%m-%d")
    b=io.BytesIO(); Image.new("RGBA",(60,30),(1,2,3,255)).save(b,"PNG")
    ADM={"X-Admin-Usuario":"admin","X-Admin-Clave":"1234"}
    TID=requests.post(B+"/api/admin/tiendas",headers=ADM,json={"nombre":"Tienda Beta","logo_b64":base64.b64encode(b.getvalue()).decode()}).json()["tienda"]["id"]
    ses=requests.Session(); ses.post(B+"/admin/usuarios",data={"usuario":"admin","clave":"1234"})
    ses.post(B+"/api/auth/usuarios",headers={"X-API-Key":"everest2024"},json={"usuario":"bop","clave":"bpw2","nombre":"Op Beta","cuenta_id":TID,"rol":"operario"})
    tok=requests.post(B+"/api/auth/login",json={"usuario":"bop","clave":"bpw2"}).json()["token"]
    requests.post(B+"/api/metricas/subir",headers={"X-API-Key":tok},json={"metricas":[{"ts":hoy+" 10:00","canal":"colecta","operario":"OPERARIO-BETA-X","n_pedidos":7,"cuenta_id":TID}]})
    requests.post(B+"/api/metricas/subir",headers={"X-API-Key":"everest2024"},json={"metricas":[{"ts":hoy+" 11:00","canal":"colecta","operario":"OPERARIO-EVEREST-Y","n_pedidos":3,"cuenta_id":"cuenta_2"}]})
    def pagina(user,clave,q=""):
        s=requests.Session(); s.post(B+"/admin/usuarios",data={"usuario":user,"clave":clave})
        r=s.get(B+"/estadisticas"+q); return r.status_code,r.text
    c,h=pagina("admin","1234")
    check("admin general: ve Everest por defecto, no Beta","OPERARIO-EVEREST-Y" in h and "OPERARIO-BETA-X" not in h)
    check("admin general: ve el selector con la tienda",'name="tienda"' in h and "Tienda Beta" in h)
    c,h=pagina("admin","1234","?tienda="+TID)
    check("admin general eligiendo Beta: ve Beta, no Everest","OPERARIO-BETA-X" in h and "OPERARIO-EVEREST-Y" not in h)
    check("titulo con el nombre de la tienda","Tienda Beta" in h)
    c,h=pagina("admin","1234","?tienda=cuenta_2")
    check("tienda invalida/primaria: cae a Everest","OPERARIO-EVEREST-Y" in h and c==200)
    c,h=pagina("sup2","abcd","?tienda="+TID)
    check("supervisor de Everest NO puede ver Beta","OPERARIO-BETA-X" not in h and 'name="tienda"' not in h)
    c,h=pagina("admin","1234","?tienda=%7B%7B7*7%7D%7D")
    check("valor raro no rompe",c==200 and "49" not in h.split("Tienda")[0][-0:])
finally:
    srv.kill()
print(f"\nRESULTADO stats: {ok} ok, {fa} fallas"); sys.exit(1 if fa else 0)
