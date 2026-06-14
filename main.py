import os
os.environ["TF_USE_LEGACY_KERAS"] = "1"

import time
import shutil
from pathlib import Path
import requests
from deepface import DeepFace

# ================= CONFIG =================
ESP32_IP = "192.168.0.50"   # cambia si tu ESP32 imprime otra IP
DB_PATH = Path("base_datos")
TEMP_DIR = Path("temp_captures")

BURST_COUNT = 5
BURST_DELAY = 0.35
MIN_HITS_TO_OPEN = 3
RELAY_OPEN_MS = 5000

MODEL_NAME = "ArcFace"
DETECTOR_BACKEND = "opencv"

STATUS_URL = f"http://{ESP32_IP}/status"
CAPTURE_URL = f"http://{ESP32_IP}/capture?quality=5&size=xga"
OPEN_URL = f"http://{ESP32_IP}/access/open?ms=5000"
CLOSE_URL = f"http://{ESP32_IP}/access/close"
ACK_URL = f"http://{ESP32_IP}/verify/ack"

last_request_id = -1

def ensure_temp_dir():
    TEMP_DIR.mkdir(exist_ok=True)

def clear_temp_dir():
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

def get_status():
    r = requests.get(STATUS_URL, timeout=5)
    r.raise_for_status()
    return r.json()

def capture_burst():
    clear_temp_dir()
    files = []

    for i in range(BURST_COUNT):
        try:
            r = requests.get(CAPTURE_URL, timeout=12)
            r.raise_for_status()
            img_path = TEMP_DIR / f"frame_{i+1}.jpg"
            with open(img_path, "wb") as f:
                f.write(r.content)
            files.append(img_path)
        except Exception as e:
            print(f"[CAPTURE ERROR] {e}")
        time.sleep(BURST_DELAY)

    return files

def identify_face(img_path):
    try:
        dfs = DeepFace.find(
            img_path=str(img_path),
            db_path=str(DB_PATH),
            model_name=MODEL_NAME,
            detector_backend=DETECTOR_BACKEND,
            enforce_detection=False,
            silent=True
        )

        if isinstance(dfs, list) and len(dfs) > 0 and len(dfs[0]) > 0:
            top = dfs[0].iloc[0]
            identity = str(top["identity"])
            distance = float(top.get("distance", 999))
            return True, identity, distance

        return False, None, None

    except Exception as e:
        print(f"[IDENTIFY ERROR] {img_path}: {e}")
        return False, None, None

def majority_vote(files):
    hits = 0
    identities = {}
    details = []

    for img in files:
        ok, identity, distance = identify_face(img)
        details.append((str(img), ok, identity, distance))
        if ok and identity:
            hits += 1
            identities[identity] = identities.get(identity, 0) + 1

    best_identity = None
    best_count = 0
    for identity, count in identities.items():
        if count > best_count:
            best_count = count
            best_identity = identity

    granted = hits >= MIN_HITS_TO_OPEN and best_identity is not None
    return granted, hits, best_identity, best_count, details

def open_relay():
    r = requests.get(OPEN_URL, timeout=5)
    print("[ESP32 OPEN]", r.text)

def close_relay():
    r = requests.get(CLOSE_URL, timeout=5)
    print("[ESP32 CLOSE]", r.text)

def ack_verify():
    try:
        requests.get(ACK_URL, timeout=5)
    except Exception as e:
        print("[ACK ERROR]", e)

def main():
    global last_request_id
    ensure_temp_dir()

    print("Servidor DeepFace iniciado")
    print("ESP32:", ESP32_IP)
    print("Base:", DB_PATH.resolve())

    while True:
        try:
            status = get_status()
            request_id = int(status.get("verificationRequestId", -1))
            verify_requested = bool(status.get("verificationRequested", False))

            if verify_requested and request_id != last_request_id:
                print(f"\n[NUEVO EVENTO] request_id={request_id}")
                last_request_id = request_id

                files = capture_burst()
                print(f"[CAPTURAS] total={len(files)}")

                if len(files) == 0:
                    print("[RESULTADO] No se pudieron capturar fotos")
                    close_relay()
                    ack_verify()
                    time.sleep(1)
                    continue

                granted, hits, best_identity, best_count, details = majority_vote(files)

                for row in details:
                    print("[FRAME]", row)

                print(f"[VOTO] hits={hits} mejor={best_identity} repeticiones={best_count}")

                if granted:
                    print("[ACCESO] CONCEDIDO")
                    open_relay()
                else:
                    print("[ACCESO] DENEGADO")
                    close_relay()

                ack_verify()

            time.sleep(0.7)

        except Exception as e:
            print("[ERROR GENERAL]", e)
            time.sleep(2)

if __name__ == "__main__":
    main()