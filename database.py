import sqlite3
import os
from pathlib import Path
from datetime import datetime

DB_PATH = "access_control.db"
UPLOADS_DIR = Path("static/uploads")
CAPTURES_DIR = Path("static/captures")

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
    """)
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


def get_config(clave, default=None):
    conn = get_db()
    row = conn.execute("SELECT valor FROM configuracion WHERE clave=?", (clave,)).fetchone()
    conn.close()
    return row["valor"] if row else default


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
