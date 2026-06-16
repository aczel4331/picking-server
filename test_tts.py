#!/usr/bin/env python3
"""
Script para probar la funcionalidad de text-to-speech
Ejecutar: python test_tts.py
"""

import io
import sys

print("=" * 60)
print("PRUEBA DE TEXT-TO-SPEECH")
print("=" * 60)

# Test 1: gTTS
print("\n[1/3] Probando gTTS...")
try:
    from gtts import gTTS
    print("    ✅ gTTS importado OK")
    
    texto = "Sillón Infantil Mora, pasillo A3"
    tts = gTTS(text=texto, lang="es", slow=False)
    print(f"    ✅ gTTS creado para: '{texto}'")
    
    buffer = io.BytesIO()
    tts.write_to_fp(buffer)
    tamaño = len(buffer.getvalue())
    print(f"    ✅ Audio generado: {tamaño} bytes")
    
    # Guardar a archivo para prueba
    with open('/tmp/test_audio.mp3', 'wb') as f:
        f.write(buffer.getvalue())
    print(f"    ✅ Guardado en /tmp/test_audio.mp3")
    
except ImportError as e:
    print(f"    ❌ gTTS NO instalado: {e}")
    print("    Instalar: pip install gtts")
except Exception as e:
    print(f"    ❌ Error gTTS: {e}")

# Test 2: pyttsx3 (fallback)
print("\n[2/3] Probando pyttsx3 (fallback)...")
try:
    import pyttsx3
    print("    ✅ pyttsx3 importado OK")
    
    engine = pyttsx3.init()
    engine.setProperty('rate', 150)
    print("    ✅ Motor de TTS inicializado")
    
    # Nota: pyttsx3 requiere archivo real, no buffer
    print("    ℹ️  pyttsx3 disponible como fallback")
    
except ImportError as e:
    print(f"    ❌ pyttsx3 NO instalado: {e}")
    print("    Instalar: pip install pyttsx3")
except Exception as e:
    print(f"    ❌ Error pyttsx3: {e}")

# Test 3: Flask y endpoint
print("\n[3/3] Información del endpoint...")
print(f"    Ruta: POST /api/leer-producto")
print(f"    Parámetros JSON esperados:")
print(f"      - sku: string")
print(f"      - producto: string (obligatorio)")
print(f"      - pasillo: string (opcional)")
print(f"    Respuesta: audio/mpeg (MP3)")

print("\n" + "=" * 60)
print("PRÓXIMOS PASOS:")
print("=" * 60)
print("""
1. En Railway, ejecuta en el terminal:
   pip install gtts pyttsx3

2. Redeploy la app:
   git push (si está conectado a GitHub)
   O redeploy manual en Railway

3. Prueba en el navegador:
   - Abre la app móvil
   - Carga un picking
   - Escanea un SKU
   - Verifica la consola del navegador (F12 > Console)
   - Deberías ver logs [VOZ] indicando el progreso

4. Si aún no funciona, comprueba:
   - La consola de Railway (logs)
   - Que gTTS está instalado (pip list | grep gtts)
   - Que el navegador permite reproducir audio
""")

print("\nSi quieres generar un archivo MP3 de prueba:")
print("  python test_tts.py > /tmp/test_audio.mp3")
