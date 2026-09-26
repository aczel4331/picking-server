# Tablero de tareas — Logibot (mantenimiento/calidad)

Backlog de mantenimiento y calidad (no una feature nueva). Cada tarea está
acotada a los archivos de su área para que los agentes no se pisen entre sí.

| ID | Área      | Tarea                                                                                                          | Estado    | Agente   |
|----|-----------|------------------------------------------------------------------------------------------------------------------|-----------|----------|
| F1 | Frontend  | Extraer los colores de canal (violeta Flex / azul Colecta) hardcodeados en `movil.html` a variables CSS reutilizables en vez de valores hex repetidos en JS/CSS | pendiente | frontend |
| F2 | Frontend  | Mensaje claro en pantalla cuando se niega el permiso de cámara al escanear (hoy no hay feedback visible)          | pendiente | frontend |
| F3 | Frontend  | Auditar y unificar el indicador de "sin conexión" (`setConexion('warn')`) en todos los puntos de fetch fallido de `movil.html` | pendiente | frontend |
| F4 | Frontend  | Unificar estilos inline repetidos de `movil.html` en clases CSS reutilizables, sin cambiar comportamiento         | pendiente | frontend |
| B1 | Backend   | `/api/pedidos/impreso/<order_id>` (`server.py`) usa el alias fijo `_estado` (= canal colecta) en vez de `_get_estado(canal)` — no encuentra etiquetas impresas del canal Flex | pendiente | backend  |
| B2 | Backend   | El alias `_estado` queda apuntando a un dict viejo/vacío tras reiniciar el server si se restaura el canal "colecta" desde disco (`_startup`, ~línea 6504) | pendiente | backend  |
| B3 | Backend   | Validar payloads de `/api/subir_estado`, `/api/escanear`, `/api/reset_sku` — hoy un payload malformado puede tirar un 500 en vez de un 400 claro | pendiente | backend  |
| B4 | Backend   | Documentar (comentarios/README corto) el modelo de estado por canal: `_estados_canal`, `_get_estado`, `_actualizar_sync_estado` | pendiente | backend  |
| T1 | Pruebas   | `tests/test_canales.py` — aislamiento entre canales + detección de lote nuevo vía `lote_id` bajo concurrencia (basarse en el harness ya probado en esta sesión) | pendiente | tests    |
| T2 | Pruebas   | `tests/test_escaneo.py` — `/api/escanear`: SKU no encontrado, SKU ya completo, colecta completa dispara Fase 2   | pendiente | tests    |
| T3 | Pruebas   | `tests/test_regresion_canal.py` — fijar en piedra los bugs ya arreglados esta sesión (lote_id único, canal "default" separado de "colecta") para que no puedan reaparecer sin que un test falle | pendiente | tests    |
| T4 | Pruebas   | `tests/README.md` — cómo correr los tests localmente (env vars necesarias: `DATA_DIR`, `PICKING_API_KEY`)         | pendiente | tests    |

## Reglas para los agentes

- Cada agente sólo toca los archivos de su área (frontend: `templates/movil.html`, `ui_theme.py`; backend: `server.py`; tests: directorio `tests/` nuevo).
- Al arrancar una tarea, marcar su fila como `en progreso` en este archivo. Al terminar, marcar `hecho` y agregar una línea breve de qué se hizo/dónde, en el mismo commit del cambio.
- No reasignar ni tocar filas de otra área.
- Si una tarea resulta más grande de lo esperado o depende de algo fuera de su área, dejarlo anotado en la fila en vez de tocar archivos de otro agente.
