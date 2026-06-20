from datetime import datetime, timedelta
import os
from pathlib import Path
import re
import threading
import time
from typing import Dict

import requests
from requests.auth import HTTPBasicAuth
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from database import get_config, get_db, init_db, set_config, sync_personas_from_folders
from notifier import send_notification

app = FastAPI(title="FaceGuard Dashboard")
ESP32_TIMEOUT = 2.0
BASE_IMAGES_DIR = Path("base_datos")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
CAMERA_MONITOR_STARTED = False
CAMERA_LAST_OK = None

Path("static").mkdir(parents=True, exist_ok=True)
Path("static/uploads").mkdir(parents=True, exist_ok=True)
Path("static/captures").mkdir(parents=True, exist_ok=True)
BASE_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/base_datos", StaticFiles(directory="base_datos"), name="base_datos")


class PersonCaptureRequest(BaseModel):
    nombre: str
    apellido: str = ""
    email: str = ""
    telefono: str = ""
    rol: str = "empleado"
    notas: str = ""
    quality: int = 10
    size: str = "vga"


class NotificationSettingsRequest(BaseModel):
    values: Dict[str, str]


class NotificationTestRequest(BaseModel):
    event_type: str = "unknown_attempt"
    message: str = "Prueba de notificacion FaceGuard"


@app.on_event("startup")
def startup():
    init_db()
    sync_personas_from_folders()
    start_camera_monitor()


def rows_to_dicts(rows):
    return [dict(row) for row in rows]


def query_all(sql, params=()):
    conn = get_db()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def query_one(sql, params=()):
    conn = get_db()
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return dict(row) if row else {}


def person_name(row) -> str:
    full_name = f"{row.get('nombre') or ''} {row.get('apellido') or ''}".strip()
    return full_name or row.get("nombre_detectado") or "Desconocido"


def initials(name: str) -> str:
    clean = [part for part in name.replace("-", " ").split() if part]
    if not clean:
        return "?"
    return "".join(part[0] for part in clean[:2]).upper()


def normalize_decision(value: str | None) -> str:
    decision = (value or "").upper()
    if decision in {"PERMITIDO", "GRANTED", "OK"}:
        return "PERMITIDO"
    return "DENEGADO"


def safe_folder_name(nombre: str, apellido: str = "") -> str:
    raw_name = f"{nombre} {apellido}".strip()
    cleaned = re.sub(r"[^A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ _-]+", "", raw_name)
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._-")
    return cleaned or "Persona"


def get_or_create_persona(payload: PersonCaptureRequest) -> int:
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM personas WHERE nombre=? AND apellido=?",
        (payload.nombre.strip(), payload.apellido.strip()),
    ).fetchone()
    if row:
        persona_id = row["id"]
        conn.execute(
            """
            UPDATE personas
            SET email=COALESCE(NULLIF(?, ''), email),
                telefono=COALESCE(NULLIF(?, ''), telefono),
                rol=COALESCE(NULLIF(?, ''), rol),
                notas=COALESCE(NULLIF(?, ''), notas),
                activo=1
            WHERE id=?
            """,
            (payload.email.strip(), payload.telefono.strip(), payload.rol.strip(), payload.notas.strip(), persona_id),
        )
    else:
        cursor = conn.execute(
            """
            INSERT INTO personas (nombre, apellido, email, telefono, rol, activo, notas)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (
                payload.nombre.strip(),
                payload.apellido.strip(),
                payload.email.strip() or None,
                payload.telefono.strip() or None,
                payload.rol.strip() or "empleado",
                payload.notas.strip() or None,
            ),
        )
        persona_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return persona_id


def add_person_image(persona_id: int, image_path: Path):
    normalized = str(image_path).replace("\\", "/")
    conn = get_db()
    conn.execute(
        "INSERT INTO imagenes_persona (persona_id, ruta) VALUES (?, ?)",
        (persona_id, normalized),
    )
    conn.execute(
        "UPDATE personas SET foto_perfil=COALESCE(foto_perfil, ?) WHERE id=?",
        (normalized, persona_id),
    )
    conn.commit()
    conn.close()


def capture_from_esp32(quality: int, size: str) -> bytes:
    ip = get_config_value("esp32_ip", "192.168.0.50")
    quality = max(4, min(int(quality), 40))
    size = size if size in {"qvga", "vga", "svga", "xga"} else "vga"
    url = f"http://{ip}/capture?quality={quality}&size={size}&fast=1"
    try:
        response = requests.get(url, auth=get_esp32_auth(), timeout=6)
        response.raise_for_status()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo capturar desde el ESP32-CAM en {ip}: {exc}",
        ) from exc
    content_type = response.headers.get("content-type", "")
    if "image" not in content_type.lower():
        raise HTTPException(status_code=502, detail="El ESP32-CAM no devolvio una imagen valida")
    return response.content


def get_config_value(clave: str, default: str) -> str:
    row = query_one("SELECT valor FROM configuracion WHERE clave=?", (clave,))
    return row.get("valor") or default


def get_esp32_auth() -> HTTPBasicAuth:
    user = os.getenv("ESP32_AUTH_USER") or get_config_value("esp32_auth_user", "admi1")
    password = os.getenv("ESP32_AUTH_PASS") or get_config_value("esp32_auth_pass", "123456789")
    return HTTPBasicAuth(user, password)


def esp32_request(path: str):
    ip = get_config_value("esp32_ip", "192.168.0.50")
    try:
        response = requests.get(f"http://{ip}{path}", auth=get_esp32_auth(), timeout=ESP32_TIMEOUT)
        response.raise_for_status()
        return {"ok": True, "ip": ip, "data": response.json()}
    except Exception as exc:
        return {"ok": False, "ip": ip, "error": str(exc)}


def esp32_version_info():
    result = esp32_request("/version")
    if result.get("ok"):
        return result

    status = esp32_request("/status")
    if status.get("ok"):
        data = status.get("data") or {}
        data.setdefault("firmwareVersion", "Firmware sin endpoint /version")
        data.setdefault("apiVersion", "1")
        data.setdefault("streamPort", 81)
        data.setdefault("captureUrl", "/capture?quality=10&size=vga&fast=1")
        return {
            "ok": True,
            "ip": status.get("ip"),
            "data": data,
            "warning": result.get("error"),
        }

    return result


def esp32_command(path: str):
    ip = get_config_value("esp32_ip", "192.168.0.50")
    try:
        response = requests.get(f"http://{ip}{path}", auth=get_esp32_auth(), timeout=ESP32_TIMEOUT)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            data = {"text": response.text}
        return {"ok": True, "ip": ip, "data": data}
    except Exception as exc:
        return {"ok": False, "ip": ip, "error": str(exc)}


def esp32_stream_response():
    ip = get_config_value("esp32_ip", "192.168.0.50")
    url = f"http://{ip}:81/stream"
    try:
        upstream = requests.get(url, stream=True, timeout=(3, None))
        upstream.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo abrir el stream del ESP32-CAM en {ip}: {exc}") from exc

    def iter_stream():
        try:
            for chunk in upstream.iter_content(chunk_size=4096):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    media_type = upstream.headers.get("content-type", "multipart/x-mixed-replace; boundary=frame")
    return StreamingResponse(iter_stream(), media_type=media_type)


NOTIFICATION_KEYS = [
    "notify_unknown_enabled",
    "notify_access_granted_enabled",
    "notify_camera_down_enabled",
    "notify_manual_relay_enabled",
    "telegram_enabled",
    "telegram_bot_token",
    "telegram_chat_id",
    "whatsapp_enabled",
    "whatsapp_token",
    "whatsapp_phone_number_id",
    "whatsapp_to",
    "email_enabled",
    "smtp_host",
    "smtp_port",
    "smtp_user",
    "smtp_password",
    "smtp_from",
    "smtp_to",
    "mobile_enabled",
    "mobile_webhook_url",
    "camera_monitor_enabled",
    "camera_monitor_interval",
]


def notification_settings_dict():
    return {key: str(get_config(key, "") or "") for key in NOTIFICATION_KEYS}


def camera_monitor_loop():
    global CAMERA_LAST_OK
    while True:
        interval = int(float(get_config_value("camera_monitor_interval", "30") or "30"))
        interval = max(10, interval)
        if str(get_config("camera_monitor_enabled", "1")) in {"1", "true", "on", "yes"}:
            result = esp32_request("/status")
            ok = bool(result.get("ok"))
            if CAMERA_LAST_OK is not False and not ok:
                send_notification(
                    "camera_down",
                    "Camara ESP32-CAM caida",
                    f"No hay respuesta de la camara en {result.get('ip')}: {result.get('error', 'sin detalle')}",
                    result,
                )
            CAMERA_LAST_OK = ok
        time.sleep(interval)


def start_camera_monitor():
    global CAMERA_MONITOR_STARTED
    if CAMERA_MONITOR_STARTED:
        return
    CAMERA_MONITOR_STARTED = True
    thread = threading.Thread(target=camera_monitor_loop, daemon=True)
    thread.start()


@app.get("/", response_class=HTMLResponse)
async def index():
    return Path("templates/index.html").read_text(encoding="utf-8")


@app.get("/api/personas")
def api_personas():
    rows = query_all(
        """
        SELECT
          p.*,
          COUNT(DISTINCT i.id) AS total_fotos,
          COUNT(DISTINCT a.id) AS total_accesos
        FROM personas p
        LEFT JOIN imagenes_persona i ON i.persona_id = p.id
        LEFT JOIN accesos a ON a.persona_id = p.id AND UPPER(a.decision) = 'PERMITIDO'
        GROUP BY p.id
        ORDER BY p.activo DESC, p.nombre COLLATE NOCASE
        """
    )
    for row in rows:
        row["nombre_completo"] = person_name(row)
        row["iniciales"] = initials(row["nombre_completo"])
        row["activo"] = bool(row["activo"])
    return rows


@app.get("/api/imagenes")
def api_imagenes():
    sync_personas_from_folders()
    rows = query_all(
        """
        SELECT
          p.id AS persona_id,
          p.nombre,
          p.apellido,
          i.id AS imagen_id,
          i.ruta,
          i.created_at
        FROM personas p
        LEFT JOIN imagenes_persona i ON i.persona_id = p.id
        WHERE p.activo=1
        ORDER BY p.nombre COLLATE NOCASE, p.apellido COLLATE NOCASE, i.created_at DESC, i.ruta COLLATE NOCASE
        """
    )

    people = {}
    for row in rows:
        persona_id = row["persona_id"]
        if persona_id not in people:
            full_name = person_name(row)
            people[persona_id] = {
                "persona_id": persona_id,
                "nombre_completo": full_name,
                "iniciales": initials(full_name),
                "imagenes": [],
            }
        if row.get("ruta"):
            ruta = row["ruta"].replace("\\", "/")
            people[persona_id]["imagenes"].append(
                {
                    "id": row["imagen_id"],
                    "ruta": ruta,
                    "url": "/" + ruta,
                    "archivo": Path(ruta).name,
                    "created_at": row["created_at"],
                }
            )
    return list(people.values())


@app.post("/api/personas/captura-esp32")
def api_persona_captura_esp32(payload: PersonCaptureRequest):
    if not payload.nombre.strip():
        raise HTTPException(status_code=400, detail="El nombre es obligatorio")

    init_db()
    image_bytes = capture_from_esp32(payload.quality, payload.size)
    persona_id = get_or_create_persona(payload)

    folder = BASE_IMAGES_DIR / safe_folder_name(payload.nombre, payload.apellido)
    folder.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    image_path = folder / f"esp32_{timestamp}.jpg"
    image_path.write_bytes(image_bytes)
    add_person_image(persona_id, image_path)
    sync_personas_from_folders()

    return {
        "ok": True,
        "persona_id": persona_id,
        "ruta": str(image_path).replace("\\", "/"),
        "url": "/" + str(image_path).replace("\\", "/"),
        "quality": payload.quality,
        "size": payload.size,
    }


@app.get("/api/accesos")
def api_accesos(limit: int = 25):
    limit = max(1, min(limit, 100))
    rows = query_all(
        """
        SELECT
          a.*,
          p.nombre,
          p.apellido
        FROM accesos a
        LEFT JOIN personas p ON p.id = a.persona_id
        ORDER BY a.timestamp DESC
        LIMIT ?
        """,
        (limit,),
    )
    for row in rows:
        row["decision"] = normalize_decision(row.get("decision"))
        row["nombre_completo"] = person_name(row)
        row["iniciales"] = initials(row["nombre_completo"])
    return rows


@app.get("/api/desconocidos")
def api_desconocidos():
    return query_all(
        """
        SELECT * FROM desconocidos
        WHERE estado = 'pendiente'
        ORDER BY timestamp DESC
        LIMIT 30
        """
    )


@app.get("/api/alertas")
def api_alertas():
    return query_all("SELECT * FROM alertas ORDER BY timestamp DESC LIMIT 30")


@app.get("/api/esp32/version")
def api_esp32_version():
    return esp32_version_info()


@app.get("/api/esp32/status")
def api_esp32_status():
    return esp32_request("/status")


@app.get("/api/esp32/stream")
def api_esp32_stream():
    return esp32_stream_response()


@app.get("/api/esp32/capture")
def api_esp32_capture(quality: int = 10, size: str = "vga"):
    image_bytes = capture_from_esp32(quality, size)
    return Response(
        content=image_bytes,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.post("/api/esp32/relay/manual-on")
def api_esp32_manual_relay_on(ms: int = 5000):
    ms = max(1000, min(int(ms), 15000))
    result = esp32_request(f"/access/open?ms={ms}")
    if result.get("ok"):
        send_notification(
            "manual_relay",
            "Relay activado manualmente",
            f"Se activo manualmente el relay por {ms} ms desde el dashboard.",
            result,
        )
    return result


@app.post("/api/esp32/relay/manual-off")
def api_esp32_manual_relay_off():
    return esp32_request("/access/close")


@app.post("/api/esp32/flash/on")
def api_esp32_flash_on():
    return esp32_command("/flash/on")


@app.post("/api/esp32/flash/off")
def api_esp32_flash_off():
    return esp32_command("/flash/off")


@app.post("/api/esp32/pir/arm")
def api_esp32_pir_arm():
    return esp32_command("/pir/arm")


@app.post("/api/esp32/pir/disarm")
def api_esp32_pir_disarm():
    return esp32_command("/pir/disarm")


@app.post("/api/esp32/verify/ack")
def api_esp32_verify_ack():
    return esp32_command("/verify/ack")


@app.get("/api/notificaciones/config")
def api_notificaciones_config():
    return notification_settings_dict()


@app.post("/api/notificaciones/config")
def api_notificaciones_save(payload: NotificationSettingsRequest):
    for key, value in payload.values.items():
        if key in NOTIFICATION_KEYS:
            set_config(key, value)
    return {"ok": True, "config": notification_settings_dict()}


@app.post("/api/notificaciones/test")
def api_notificaciones_test(payload: NotificationTestRequest):
    results = send_notification(
        payload.event_type,
        "Prueba de notificacion",
        payload.message,
        {"source": "dashboard"},
    )
    return {"ok": True, "results": results}


@app.get("/api/notificaciones/logs")
def api_notificaciones_logs(limit: int = 30):
    limit = max(1, min(limit, 100))
    return query_all(
        "SELECT * FROM notification_logs ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    )


@app.get("/api/dashboard")
def api_dashboard():
    today = datetime.now().strftime("%Y-%m-%d")
    stats = {
        "accesos_hoy": query_one(
            "SELECT COUNT(*) AS total FROM accesos WHERE date(timestamp)=? AND UPPER(decision)='PERMITIDO'",
            (today,),
        ).get("total", 0),
        "denegados_hoy": query_one(
            "SELECT COUNT(*) AS total FROM accesos WHERE date(timestamp)=? AND UPPER(decision)!='PERMITIDO'",
            (today,),
        ).get("total", 0),
        "personas": query_one(
            "SELECT COUNT(*) AS total FROM personas WHERE activo=1"
        ).get("total", 0),
        "desconocidos": query_one(
            "SELECT COUNT(*) AS total FROM desconocidos WHERE estado='pendiente'"
        ).get("total", 0),
        "alertas": query_one(
            "SELECT COUNT(*) AS total FROM alertas WHERE leida=0"
        ).get("total", 0),
    }

    recent_accesses = api_accesos(limit=6)
    personas = api_personas()
    imagenes = api_imagenes()
    desconocidos = api_desconocidos()
    alertas = api_alertas()

    top_personas = query_all(
        """
        SELECT
          p.nombre,
          p.apellido,
          COUNT(a.id) AS total
        FROM personas p
        LEFT JOIN accesos a ON a.persona_id = p.id AND UPPER(a.decision)='PERMITIDO'
        GROUP BY p.id
        ORDER BY total DESC, p.nombre COLLATE NOCASE
        LIMIT 5
        """
    )
    for row in top_personas:
        row["nombre_completo"] = person_name(row)

    hourly_rows = query_all(
        """
        SELECT CAST(strftime('%H', timestamp) AS INTEGER) AS hour, COUNT(*) AS total
        FROM accesos
        WHERE date(timestamp)=?
        GROUP BY hour
        """,
        (today,),
    )
    by_hour = {row["hour"]: row["total"] for row in hourly_rows}
    hourly = {
        "labels": [str(hour) for hour in range(6, 23)],
        "data": [by_hour.get(hour, 0) for hour in range(6, 23)],
    }

    week_days = [datetime.now().date() - timedelta(days=offset) for offset in range(6, -1, -1)]
    weekly_allowed = []
    weekly_denied = []
    for day in week_days:
        day_text = day.strftime("%Y-%m-%d")
        weekly_allowed.append(
            query_one(
                "SELECT COUNT(*) AS total FROM accesos WHERE date(timestamp)=? AND UPPER(decision)='PERMITIDO'",
                (day_text,),
            ).get("total", 0)
        )
        weekly_denied.append(
            query_one(
                "SELECT COUNT(*) AS total FROM accesos WHERE date(timestamp)=? AND UPPER(decision)!='PERMITIDO'",
                (day_text,),
            ).get("total", 0)
        )

    return {
        "stats": stats,
        "personas": personas,
        "imagenes": imagenes,
        "recent_accesses": recent_accesses,
        "desconocidos": desconocidos,
        "alertas": alertas,
        "top_personas": top_personas,
        "charts": {
            "hourly": hourly,
            "weekly": {
                "labels": [day.strftime("%d/%m") for day in week_days],
                "permitidos": weekly_allowed,
                "denegados": weekly_denied,
            },
            "decision": [stats["accesos_hoy"], stats["denegados_hoy"]],
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=5000, reload=False)
