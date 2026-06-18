# Sistema de Control de Acceso — ESP32-CAM + DeepFace

Sistema de reconocimiento facial para control de acceso físico.  
El hardware es una **ESP32-CAM (sensor OV3660)** y el backend corre en **Python** usando el modelo **ArcFace** de DeepFace.

---

## Arquitectura general

```
[ESP32-CAM]  ──HTTP──►  [Python backend]  ──HTTP──►  [Relay físico]
  captura                  main.py                   abre/cierra puerta
  frames                   DeepFace
  PIR sensor               ArcFace model
```

El backend hace polling al ESP32, captura una ráfaga de frames cuando detecta movimiento, filtra los frames de baja calidad, compara contra la base de datos y decide si abrir el relay.

---

## Requisitos

- Python 3.11 (recomendado para compatibilidad con TensorFlow)
- ESP32-CAM con el firmware `Codigo_Arquitectura_CAMERA.ino` flasheado

### Instalación rápida (Windows)

```bat
instalar_proyecto.bat
```

### Instalación manual

```bash
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Linux/macOS
pip install -r requirements.txt
```

---

## Configuración rápida

Abre `main.py` y edita el bloque `SETTINGS` al inicio del archivo:

```python
ESP32_IP    = "192.168.0.50"   # ← cambia a la IP que imprime tu ESP32 en Serial
BURST_COUNT = 10               # frames por evento (recomendado: 8–12)
```

### Todos los parámetros ajustables

| Variable | Descripción | Default |
|---|---|---|
| `ESP32_IP` | IP local de la ESP32-CAM | `192.168.0.50` |
| `BURST_COUNT` | Frames capturados por evento | `10` |
| `BURST_DELAY` | Segundos entre frames | `0.30` |
| `MODEL_NAME` | Modelo DeepFace | `ArcFace` |
| `DETECTOR_BACKEND` | Detector de caras | `opencv` |
| `MAX_DISTANCE` | Umbral máximo de distancia coseno | `0.52` |
| `MIN_BLUR` | Varianza Laplaciana mínima | `60.0` |
| `MIN_BRIGHTNESS` | Brillo promedio mínimo (0–255) | `40.0` |
| `MAX_BRIGHTNESS` | Brillo promedio máximo (sobreexpuesto) | `230.0` |
| `MIN_FACE_AREA` | Fracción mínima del frame que ocupa la cara | `0.03` |
| `MIN_VALID_FRAMES` | Frames válidos mínimos para decidir | `2` |
| `MIN_HITS` | Matches mínimos para conceder acceso | `2` |
| `MIN_SUPPORT_PCT` | % mínimo de frames que deben apoyar a la misma persona | `0.30` |
| `MIN_SCORE` | Score combinado mínimo para acceso | `0.40` |

---

## Base de datos de personas

La base de datos está en la carpeta `base_datos/`, organizada por persona:

```
base_datos/
├── Jhon/
│   ├── jhon_foto_1.jpeg
│   ├── jhon_foto_2.jpeg
│   └── jhon_foto_3.jpeg
├── Sally/
│   └── ...
└── Yele/
    └── ...
```

**Recomendaciones para mejores resultados:**
- Mínimo 3 fotos por persona, idealmente 5–8
- Fotos con diferente iluminación, ángulo y expresión
- Evita fotos muy oscuras o borrosas en la base de datos
- Resolución mínima recomendada: 160×160 px con la cara visible

### Herramienta de gestión (`db_tools.py`)

```bash
# Ver todas las personas registradas
python db_tools.py list

# Agregar fotos a una persona (crea la carpeta si no existe)
python db_tools.py add --person "NombrePersona" --images foto1.jpg foto2.jpg

# Eliminar una persona completa
python db_tools.py remove --person "NombrePersona"

# Reconstruir el cache de embeddings desde cero
python db_tools.py rebuild

# Probar una imagen contra la base de datos
python db_tools.py test --image ruta/a/imagen.jpg

# Calibrar el umbral MAX_DISTANCE con una imagen de referencia
python db_tools.py tune --image ruta/a/imagen.jpg
```

---

## Ejecución

```bash
python main.py
```

Salida esperada al arrancar:

```
=======================================================
 Servidor DeepFace — Control de Acceso con ESP32-CAM
=======================================================
 ESP32 IP   : 192.168.0.50
 Base datos : /ruta/base_datos
 Modelo     : ArcFace | Detector: opencv
 Frames/evento: 10 | Max dist: 0.52
=======================================================
[INIT] Cargando embeddings de la base de datos...
[DB] Jhon: 3 embedding(s)
[DB] Sally: 3 embedding(s)
[INIT] Base lista: 2 persona(s), 6 embedding(s) total
```

Salida durante un evento de verificación:

```
[EVENTO] request_id=5
[CAPTURAS] obtenidas=10
[FRAME 1] rostro=NO  blur=28.1  brillo=35.2  area=0.000  valido=NO
         descartado: blur_bajo=28.1
[FRAME 3] rostro=SI  blur=124.2 brillo=88.1  area=0.190  valido=SI  match=Jhon dist=0.4812
[FRAME 5] rostro=SI  blur=98.7  brillo=91.0  area=0.210  valido=SI  match=Jhon dist=0.5100
[FRAMES] total=10 validos=6
[VOTO] persona=Jhon hits=4/6 mejor=0.4812 promedio=0.5050 apoyo=66.7% score=0.818

[RESULTADO] {
  "request_id": 5,
  "total_frames": 10,
  "total_frames_validos": 6,
  "persona_ganadora": "Jhon",
  "score_final": 0.818,
  "mejor_distancia": 0.4812,
  "decision": "PERMITIDO",
  "motivo": "4 frames válidos apoyan a Jhon, score=0.818, mejor_dist=0.4812"
}

[ACCESO] PERMITIDO — 4 frames válidos apoyan a Jhon
```

---

## Algoritmo de decisión

```
Para cada evento de verificación:

1. Capturar BURST_COUNT frames del ESP32

2. Por cada frame:
   a. Medir blur (Laplacian variance)
   b. Medir brillo promedio
   c. Detectar cara y medir su área
   d. Si pasa todos los filtros → frame válido

3. Si frames válidos < MIN_VALID_FRAMES → DENEGAR

4. Por cada frame válido:
   a. Calcular embedding (ArcFace) — del cache si existe
   b. Comparar contra todos los embeddings de la BD
   c. Si mejor distancia <= MAX_DISTANCE → registrar hit

5. Agrupar hits por persona:
   - total_hits, mejor_distancia, distancia_promedio, % apoyo

6. Score final por persona:
   score = 0.60 × (1 - mejor_dist/MAX_DISTANCE) + 0.40 × apoyo

7. Persona ganadora = mayor score

8. CONCEDER acceso solo si:
   - hits >= MIN_HITS
   - apoyo >= MIN_SUPPORT_PCT
   - score >= MIN_SCORE
```

---

## Ajustes recomendados para ESP32-CAM de baja calidad

Si tienes **muchos falsos rechazos** (personas autorizadas denegadas):
1. Baja `MIN_SCORE` de `0.40` → `0.30`
2. Baja `MIN_HITS` de `2` → `1`
3. Baja `MIN_BLUR` de `60` → `40`
4. Sube `MAX_DISTANCE` de `0.52` → `0.58`

Si tienes **falsos positivos** (gente no autorizada que pasa):
1. Sube `MIN_SCORE` de `0.40` → `0.55`
2. Sube `MIN_HITS` de `2` → `3`
3. Baja `MAX_DISTANCE` de `0.52` → `0.45`

Para **calibrar el umbral exacto** con tus imágenes reales:
```bash
python db_tools.py tune --image ruta/a/foto_real_del_esp32.jpg
```

---

## Estructura del proyecto

```
proyecto/
├── main.py                              # Backend principal
├── db_tools.py                          # CLI de gestión de personas
├── requirements.txt                     # Dependencias Python
├── instalar_proyecto.bat                # Setup automático Windows
├── README.md                            # Esta documentación
├── base_datos/                          # Personas autorizadas
│   ├── Persona1/
│   │   └── foto1.jpg
│   └── Persona2/
│       └── foto1.jpg
├── temp_captures/                       # Frames temporales (auto-generado)
├── embeddings_cache.pkl                 # Cache de embeddings (auto-generado)
└── Codigo_Arquitectura_CAMERA/
    └── Codigo_Arquitectura_CAMERA.ino   # Firmware ESP32-CAM
```

---

## Cache de embeddings

El archivo `embeddings_cache.pkl` se genera automáticamente la primera vez que corre `main.py`.  
En ejecuciones siguientes, los embeddings de la base de datos se reutilizan sin recalcular.

Si modificas la base de datos (agregas/eliminas fotos), ejecuta:
```bash
python db_tools.py rebuild
```

---

## Dependencias

| Paquete | Versión | Uso |
|---|---|---|
| `deepface` | 0.0.95 | Reconocimiento facial |
| `tensorflow` | 2.20.0 | Backend de modelos |
| `tf-keras` | 2.20.1 | Keras para TF 2.x |
| `opencv-python` | 4.10.0.84 | Procesamiento de imagen |
| `numpy` | 1.26.4 | Álgebra vectorial |
| `pandas` | 2.2.2 | Manejo de resultados |
| `pillow` | 10.4.0 | Lectura de imágenes |
| `requests` | 2.32.3 | Comunicación con ESP32 |
| `fastapi` | 0.111.1 | (disponible para extensión futura) |
| `uvicorn` | 0.30.3 | (disponible para extensión futura) |
| `retina-face` | 0.0.17 | Detector alternativo |

---

## Firmware ESP32-CAM

El archivo `.ino` está en `Codigo_Arquitectura_CAMERA/`.  
Flashear con Arduino IDE o PlatformIO.  
La ESP32-CAM debe estar en la misma red local que el backend Python.

### Cargar cambios desde Arduino IDE

Cada vez que se edite el dashboard/backend y el flujo del ESP32 cambie, abre en Arduino IDE:

```text
Codigo_Arquitectura_CAMERA/Codigo_Arquitectura_CAMERA.ino
```

Luego selecciona la placa ESP32-CAM correcta, el puerto COM y pulsa **Subir**.

La versión esperada de este firmware es:

```text
faceguard-fastapi-sqlite-2026-06-17
```

Después de subirlo, verifica en el navegador:

```text
http://192.168.0.50/version
```

O desde el dashboard/backend:

```text
http://127.0.0.1:5000/api/esp32/version
```

Si la versión no aparece, el ESP32-CAM todavía no tiene cargados los cambios del Arduino IDE.

---

## Acceso local con administradores

El panel, el stream y los endpoints sensibles del ESP32 usan HTTP Basic Auth.
El administrador inicial se crea automaticamente al flashear el firmware:

| Usuario | Contraseña |
|---|---|
| `admi1` | `123456789` |

El panel web permite agregar administradores, eliminar administradores y cambiar
contraseñas. No se puede eliminar el ultimo administrador.

Si cambias la contraseña del administrador que usa el backend Python, inicia
`main.py` con las mismas credenciales:

```powershell
$env:ESP32_AUTH_USER="admi1"
$env:ESP32_AUTH_PASS="nueva-contraseña"
python main.py
```

---

## Licencia

Proyecto personal. Uso libre para fines educativos y personales.
