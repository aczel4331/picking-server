#!/usr/bin/env python3
"""
Script para diagnosticar sincronización en tiempo real
Verifica que el endpoint /api/estado-picking está retornando datos correctamente
"""

import requests
import json
import time

# URL del servidor (cambiar si es diferente)
SERVIDOR = "https://picking-server-production.up.railway.app"
ENDPOINT = f"{SERVIDOR}/api/estado-picking"

print("=" * 70)
print("DIAGNÓSTICO DE SINCRONIZACIÓN EN TIEMPO REAL")
print("=" * 70)

print(f"\n[1] Conectando a: {ENDPOINT}")

try:
    response = requests.get(ENDPOINT, timeout=5)
    print(f"    ✅ Conexión OK (status: {response.status_code})")
    
    data = response.json()
    print(f"\n[2] Datos recibidos:")
    print(f"    - Lote cargado: {data.get('cargado', False)}")
    
    if data.get("cargado"):
        colecta = data.get("colecta", {})
        grupos = data.get("grupos", [])
        
        print(f"    - SKUs en colecta: {len(colecta)}")
        print(f"    - Grupos: {len(grupos)}")
        print(f"    - Colecta completa: {data.get('colecta_completa', False)}")
        
        if colecta:
            print(f"\n[3] Estado de colecta actual:")
            for sku, qty in list(colecta.items())[:5]:
                print(f"    {sku}: {qty} unidades")
            if len(colecta) > 5:
                print(f"    ... y {len(colecta) - 5} más")
        
        if grupos:
            print(f"\n[4] Primeros grupo:")
            for grupo in grupos[:2]:
                print(f"    - {grupo.get('nombre', 'Sin nombre')}: {len(grupo.get('items', []))} items")
                for item in grupo.get('items', [])[:2]:
                    print(f"      {item.get('sku')}: {item.get('collected')}/{item.get('req')}")
        
        print(f"\n[5] Polling en tiempo real (presiona Ctrl+C para parar):")
        print(f"    Consultando cada 2 segundos...\n")
        
        colecta_anterior = colecta.copy()
        count = 0
        
        while count < 15:  # 30 segundos máximo
            time.sleep(2)
            count += 1
            
            try:
                r = requests.get(ENDPOINT, timeout=5)
                d = r.json()
                
                if d.get("cargado"):
                    colecta_nueva = d.get("colecta", {})
                    
                    # Detectar cambios
                    cambios = []
                    for sku in set(list(colecta_anterior.keys()) + list(colecta_nueva.keys())):
                        anterior = colecta_anterior.get(sku, 0)
                        nuevo = colecta_nueva.get(sku, 0)
                        if anterior != nuevo:
                            cambios.append(f"{sku}: {anterior}→{nuevo}")
                    
                    if cambios:
                        print(f"    ✅ Cambios detectados ({count*2}s):")
                        for cambio in cambios:
                            print(f"       {cambio}")
                        colecta_anterior = colecta_nueva.copy()
                    else:
                        print(f"    ⏳ Sin cambios ({count*2}s) - esperando escaneos en móvil...")
                else:
                    print(f"    ⚠️  No hay lote cargado")
                    
            except Exception as e:
                print(f"    ❌ Error en consulta: {e}")
    else:
        print("    ⚠️  No hay lote cargado actualmente")
        print("\n    Para probar sincronización:")
        print("    1. Abre app móvil")
        print("    2. Genera/carga un picking")
        print("    3. Escanea SKUs")
        print("    4. Vuelve a ejecutar este script")

except requests.exceptions.ConnectionError:
    print(f"    ❌ No se puede conectar a {SERVIDOR}")
    print("    Verifica que:")
    print("    - La URL es correcta")
    print("    - Railway está online")
    print("    - Hay conexión a internet")

except Exception as e:
    print(f"    ❌ Error: {e}")

print("\n" + "=" * 70)
print("FIN DEL DIAGNÓSTICO")
print("=" * 70)