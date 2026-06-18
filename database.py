import sqlite3
import os
from pathlib import Path
from datetime import datetime

DB_PATH = "access_control.db"
UPLOADS_DIR = Path("static/uploads")
CAPTURES_DIR = Path("static/captures")
BASE_IMAGES_DIR = Path("base_datos")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)

    conn = get_db()
    c = conn.cursor()

    c.executescript("""
        CREATE TABLE IF NOT EXISTS personas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            apellido TEXT NOT NULL,
            email TEXT,
            telefono TEXT,
            rol TEXT DEFAULT 'empleado',
            activo INTEGER DEFAULT 1,
            foto_perfil TEXT,
            notas TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS imagenes_persona (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            persona_id INTEGER NOT NULL,
            ruta TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS horarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            persona_id INTEGER NOT NULL,
            lunes INTEGER DEFAULT 0,
            martes INTEGER DEFAULT 0,
            miercoles INTEGER DEFAULT 0,
            jueves INTEGER DEFAULT 0,
            viernes INTEGER DEFAULT 0,
            sabado INTEGER DEFAULT 0,
            domingo INTEGER DEFAULT 0,
            hora_inicio TEXT DEFAULT '08:00',
            hora_fin TEXT DEFAULT '18:00',
            activo INTEGER DEFAULT 1,
            FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS accesos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            persona_id INTEGER,
            nombre_detectado TEXT,
            decision TEXT NOT NULL,
            score REAL,
            distancia REAL,
            frames_validos INTEGER,
            frames_total INTEGER,
            motivo TEXT,
            foto_captura TEXT,
            timestamp TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS desconocidos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            foto TEXT,
            estado TEXT DEFAULT 'pendiente',
            persona_id INTEGER,
            timestamp TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS configuracion (
            clave TEXT PRIMARY KEY,
            valor TEXT,
            descripcion TEXT
        );

        CREATE TABLE IF NOT EXISTS alertas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tipo TEXT NOT NULL,
            mensaje TEXT NOT NULL,
            leida INTEGER DEFAULT 0,
            timestamp TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS notification_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            channel TEXT NOT NULL,
            ok INTEGER NOT NULL,
            message TEXT,
            response TEXT,
            timestamp TEXT DEFAULT (datetime('now','localtime'))
        );

        INSERT OR IGNORE INTO configuracion VALUES ('esp32_ip', '192.168.0.50', 'IP del ESP32-CAM');
        INSERT OR IGNORE INTO configuracion VALUES ('burst_count', '5', 'Frames por captura');
        INSERT OR IGNORE INTO configuracion VALUES ('max_distance', '0.68', 'Umbral de distancia coseno');
        INSERT OR IGNORE INTO configuracion VALUES ('capture_quality', '10', 'Calidad JPEG (1-63, menor=mejor)');
        INSERT OR IGNORE INTO configuracion VALUES ('capture_size', 'vga', 'Resolución de captura');
        INSERT OR IGNORE INTO configuracion VALUES ('poll_interval', '0.15', 'Intervalo de polling (segundos)');
        INSERT OR IGNORE INTO configuracion VALUES ('early_grant', '1', 'Decision temprana activada');
        INSERT OR IGNORE INTO configuracion VALUES ('min_blur', '20.0', 'Blur mínimo aceptable');
        INSERT OR IGNORE INTO configuracion VALUES ('min_score', '0.20', 'Score mínimo para acceso');
        INSERT OR IGNORE INTO configuracion VALUES ('alert_email', '', 'Email para alertas');
        INSERT OR IGNORE INTO configuracion VALUES ('notify_unknown_enabled', '1', 'Notificar intentos desconocidos');
        INSERT OR IGNORE INTO configuracion VALUES ('notify_access_granted_enabled', '1', 'Notificar accesos concedidos');
        INSERT OR IGNORE INTO configuracion VALUES ('notify_camera_down_enabled', '1', 'Notificar camara caida');
        INSERT OR IGNORE INTO configuracion VALUES ('notify_manual_relay_enabled', '1', 'Notificar relay manual');
        INSERT OR IGNORE INTO configuracion VALUES ('telegram_enabled', '0', 'Enviar alertas por Telegram');
        INSERT OR IGNORE INTO configuracion VALUES ('telegram_bot_token', '', 'Token del bot de Telegram');
        INSERT OR IGNORE INTO configuracion VALUES ('telegram_chat_id', '', 'Chat ID de Telegram');
        INSERT OR IGNORE INTO configuracion VALUES ('whatsapp_enabled', '0', 'Enviar alertas por WhatsApp Cloud API');
        INSERT OR IGNORE INTO configuracion VALUES ('whatsapp_token', '', 'Token de WhatsApp Cloud API');
        INSERT OR IGNORE INTO configuracion VALUES ('whatsapp_phone_number_id', '', 'ID del numero emisor en WhatsApp Cloud API');
        INSERT OR IGNORE INTO configuracion VALUES ('whatsapp_to', '', 'Numero destino en formato internacional');
        INSERT OR IGNORE INTO configuracion VALUES ('email_enabled', '0', 'Enviar alertas por correo');
        INSERT OR IGNORE INTO configuracion VALUES ('smtp_host', '', 'Servidor SMTP');
        INSERT OR IGNORE INTO configuracion VALUES ('smtp_port', '587', 'Puerto SMTP');
        INSERT OR IGNORE INTO configuracion VALUES ('smtp_user', '', 'Usuario SMTP');
        INSERT OR IGNORE INTO configuracion VALUES ('smtp_password', '', 'Password SMTP');
        INSERT OR IGNORE INTO configuracion VALUES ('smtp_from', '', 'Remitente del correo');
        INSERT OR IGNORE INTO configuracion VALUES ('smtp_to', '', 'Destinatarios separados por coma');
        INSERT OR IGNORE INTO configuracion VALUES ('mobile_enabled', '0', 'Enviar alertas a app movil por webhook');
        INSERT OR IGNORE INTO configuracion VALUES ('mobile_webhook_url', '', 'Webhook de app movil');
        INSERT OR IGNORE INTO configuracion VALUES ('camera_monitor_enabled', '1', 'Monitoreo automatico de camara');
        INSERT OR IGNORE INTO configuracion VALUES ('camera_monitor_interval', '30', 'Intervalo de monitoreo de camara en segundos');
    """)
    conn.commit()
    conn.close()


def _split_person_name(folder_name: str) -> tuple[str, str]:
    parts = folder_name.replace("_", " ").split()
    if not parts:
        return folder_name, ""
    return parts[0], " ".join(parts[1:])


def sync_personas_from_folders():
    """Mantiene SQLite alineado con las carpetas de fotos en base_datos/."""
    if not BASE_IMAGES_DIR.exists():
        return

    conn = get_db()
    for person_dir in sorted(BASE_IMAGES_DIR.iterdir()):
        if not person_dir.is_dir():
            continue

        nombre, apellido = _split_person_name(person_dir.name)
        row = conn.execute(
            "SELECT id FROM personas WHERE nombre=? AND apellido=?",
            (nombre, apellido),
        ).fetchone()

        images = [
            img for img in sorted(person_dir.iterdir())
            if img.is_file() and img.suffix.lower() in IMAGE_EXTENSIONS
        ]
        foto_perfil = str(images[0]).replace("\\", "/") if images else None

        if row:
            persona_id = row["id"]
            conn.execute(
                "UPDATE personas SET foto_perfil=COALESCE(foto_perfil, ?) WHERE id=?",
                (foto_perfil, persona_id),
            )
        else:
            cursor = conn.execute(
                """
                INSERT INTO personas (nombre, apellido, rol, activo, foto_perfil, notas)
                VALUES (?, ?, 'empleado', 1, ?, ?)
                """,
                (nombre, apellido, foto_perfil, f"Sincronizado desde {person_dir.as_posix()}"),
            )
            persona_id = cursor.lastrowid

        for image in images:
            image_path = str(image).replace("\\", "/")
            exists = conn.execute(
                "SELECT 1 FROM imagenes_persona WHERE persona_id=? AND ruta=?",
                (persona_id, image_path),
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO imagenes_persona (persona_id, ruta) VALUES (?, ?)",
                    (persona_id, image_path),
                )

    conn.commit()
    conn.close()


def log_acceso(persona_id, nombre_detectado, decision, score, distancia,
               frames_validos, frames_total, motivo, foto_captura=None):
    conn = get_db()
    conn.execute("""
        INSERT INTO accesos
          (persona_id, nombre_detectado, decision, score, distancia,
           frames_validos, frames_total, motivo, foto_captura)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (persona_id, nombre_detectado, decision, score, distancia,
          frames_validos, frames_total, motivo, foto_captura))
    conn.commit()
    conn.close()


def log_desconocido(foto):
    conn = get_db()
    conn.execute("INSERT INTO desconocidos (foto) VALUES (?)", (foto,))
    conn.commit()
    conn.close()


def add_alerta(tipo, mensaje):
    conn = get_db()
    conn.execute("INSERT INTO alertas (tipo, mensaje) VALUES (?, ?)", (tipo, mensaje))
    conn.commit()
    conn.close()


def log_notification(event_type, channel, ok, message="", response=""):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO notification_logs (event_type, channel, ok, message, response)
        VALUES (?, ?, ?, ?, ?)
        """,
        (event_type, channel, 1 if ok else 0, message, response),
    )
    conn.commit()
    conn.close()


def get_config(clave, default=None):
    conn = get_db()
    row = conn.execute("SELECT valor FROM configuracion WHERE clave=?", (clave,)).fetchone()
    conn.close()
    return row["valor"] if row else default


def set_config(clave, valor, descripcion=None):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO configuracion (clave, valor, descripcion)
        VALUES (?, ?, COALESCE(?, ''))
        ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor
        """,
        (clave, str(valor), descripcion),
    )
    conn.commit()
    conn.close()


def verificar_horario(persona_id) -> bool:
    conn = get_db()
    horario = conn.execute(
        "SELECT * FROM horarios WHERE persona_id=? AND activo=1", (persona_id,)
    ).fetchone()
    conn.close()

    if not horario:
        return True

    now = datetime.now()
    dias = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
    dia_actual = dias[now.weekday()]

    if not horario[dia_actual]:
        return False

    hora_actual = now.strftime("%H:%M")
    return horario["hora_inicio"] <= hora_actual <= horario["hora_fin"]


def get_persona_by_nombre(nombre) -> dict:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM personas WHERE nombre=? AND activo=1", (nombre,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None
