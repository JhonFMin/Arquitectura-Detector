import os
os.environ["TF_USE_LEGACY_KERAS"] = "1"
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"

import time
import json
import shutil
import pickle
import hashlib
from pathlib import Path

import cv2
import numpy as np
import requests
from requests.auth import HTTPBasicAuth
from deepface import DeepFace

try:
    from database import get_persona_by_nombre, init_db, log_acceso, log_desconocido
except Exception:
    get_persona_by_nombre = None
    init_db = None
    log_acceso = None
    log_desconocido = None

try:
    from notifier import send_notification
except Exception:
    send_notification = None

# ===================== SETTINGS =====================
ESP32_IP          = "192.168.0.50"   # <-- cambia a la IP de tu ESP32-CAM
ESP32_AUTH_USER   = os.getenv("ESP32_AUTH_USER", "admi1")
ESP32_AUTH_PASS   = os.getenv("ESP32_AUTH_PASS", "123456789")

# Rutas
DB_PATH           = Path("base_datos")
TEMP_DIR          = Path("temp_captures")
CACHE_FILE        = Path("embeddings_cache.pkl")

# Captura rapida
BURST_COUNT       = 5
CAPTURE_QUALITY   = 10              # menor archivo que quality=5, buena para WiFi
CAPTURE_SIZE      = "vga"           # VGA suele ser suficiente para ArcFace y es mucho mas rapido que XGA
SAVE_DEBUG_FRAMES = False           # ponlo en True si necesitas guardar temp_captures/
EARLY_GRANT       = True            # abre en cuanto se cumplan los umbrales

# Tiempos HTTP
STATUS_TIMEOUT    = 1.0
CAPTURE_TIMEOUT   = 4.0
COMMAND_TIMEOUT   = 1.5
POLL_INTERVAL     = 0.15
ERROR_RETRY_DELAY = 0.7

# Modelo
MODEL_NAME        = "ArcFace"
DETECTOR_BACKEND  = os.getenv("DETECTOR_BACKEND", "yunet")
FALLBACK_DETECTOR_BACKEND = os.getenv("FALLBACK_DETECTOR_BACKEND", "opencv")
DISTANCE_METRIC   = "cosine"

# Umbrales de calidad de frame (permisivos para ESP32-CAM)
MIN_BLUR          = 20.0
MIN_BRIGHTNESS    = 20.0
MAX_BRIGHTNESS    = 245.0
MIN_FACE_AREA     = 0.01

# Umbrales de decision (permisivos para ESP32-CAM)
MAX_DISTANCE      = 0.68
MIN_VALID_FRAMES  = 1
MIN_HITS          = 1
MIN_SUPPORT_PCT   = 0.10
MIN_SCORE         = 0.20

# ESP32 URLs
STATUS_URL  = f"http://{ESP32_IP}/status"
VERSION_URL = f"http://{ESP32_IP}/version"
CAPTURE_URL = f"http://{ESP32_IP}/capture?quality={CAPTURE_QUALITY}&size={CAPTURE_SIZE}&fast=1"
OPEN_URL    = f"http://{ESP32_IP}/access/open?ms=5000"
CLOSE_URL   = f"http://{ESP32_IP}/access/close"
ACK_URL     = f"http://{ESP32_IP}/verify/ack"
# ====================================================

last_request_id = -1
embeddings_cache: dict = {}
http = requests.Session()
http.headers.update({"Connection": "keep-alive"})
http.auth = HTTPBasicAuth(ESP32_AUTH_USER, ESP32_AUTH_PASS)

FACE_CASCADE = cv2.CascadeClassifier(
    str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
)


# ------------------------- CACHE DE EMBEDDINGS -------------------------

def _file_hash(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _is_path(value) -> bool:
    return isinstance(value, (str, Path))


def _source_label(source) -> str:
    if _is_path(source):
        return Path(source).name
    return "frame_memoria"


def _deepface_input(source):
    return str(source) if _is_path(source) else source


def _load_image_bgr(source):
    if _is_path(source):
        return cv2.imread(str(source))
    if isinstance(source, np.ndarray):
        return source
    return None


def load_cache():
    if not CACHE_FILE.exists():
        return
    try:
        with open(CACHE_FILE, "rb") as f:
            loaded = pickle.load(f)
        embeddings_cache.clear()
        embeddings_cache.update(loaded)
        print(f"[CACHE] Cargados {len(embeddings_cache)} embeddings desde disco")
    except Exception as e:
        print(f"[CACHE] No se pudo cargar cache: {e}")
        embeddings_cache.clear()


def save_cache():
    try:
        with open(CACHE_FILE, "wb") as f:
            pickle.dump(embeddings_cache, f)
    except Exception as e:
        print(f"[CACHE] Error guardando cache: {e}")


def get_embedding(source):
    cache_key = None
    current_hash = None

    if _is_path(source):
        img_path = Path(source)
        cache_key = str(img_path)
        current_hash = _file_hash(img_path)
        cached = embeddings_cache.get(cache_key)
        if cached and cached.get("hash") == current_hash:
            return cached["embedding"]

    backends = [DETECTOR_BACKEND]
    if FALLBACK_DETECTOR_BACKEND and FALLBACK_DETECTOR_BACKEND not in backends:
        backends.append(FALLBACK_DETECTOR_BACKEND)

    for backend in backends:
        try:
            result = DeepFace.represent(
                img_path=_deepface_input(source),
                model_name=MODEL_NAME,
                detector_backend=backend,
                enforce_detection=False,
            )
            if result and len(result) > 0:
                emb = result[0]["embedding"]
                if cache_key:
                    embeddings_cache[cache_key] = {"embedding": emb, "hash": current_hash}
                return emb
        except Exception as e:
            print(f"[EMBEDDING ERROR] {_source_label(source)} backend={backend}: {e}")
    return None


def cosine_distance(a, b) -> float:
    va, vb = np.array(a), np.array(b)
    norm_a, norm_b = np.linalg.norm(va), np.linalg.norm(vb)
    if norm_a == 0 or norm_b == 0:
        return 1.0
    return float(1.0 - np.dot(va, vb) / (norm_a * norm_b))


# ------------------------- BASE DE DATOS LOCAL -------------------------

def build_db_embeddings() -> dict:
    db: dict = {}
    exts = {".jpg", ".jpeg", ".png", ".bmp"}
    dirty = False

    if not DB_PATH.exists():
        return db

    for person_dir in sorted(DB_PATH.iterdir()):
        if not person_dir.is_dir():
            continue
        person = person_dir.name
        imgs = [p for p in person_dir.iterdir() if p.suffix.lower() in exts]
        embs = []
        for img in imgs:
            emb = get_embedding(img)
            if emb is not None:
                embs.append(emb)
                dirty = True
        if embs:
            db[person] = embs
            print(f"[DB] {person}: {len(embs)} foto(s) cargada(s)")

    if dirty:
        save_cache()
    return db


def _normalized_matrix(embeddings) -> np.ndarray:
    matrix = np.asarray(embeddings, dtype=np.float32)
    if matrix.size == 0:
        return np.empty((0, 0), dtype=np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def build_db_index(db_embeddings: dict) -> dict:
    return {
        person: _normalized_matrix(embs)
        for person, embs in db_embeddings.items()
        if len(embs) > 0
    }


# ------------------------- CALIDAD DE FRAME -------------------------

def measure_frame_quality(source) -> dict:
    metrics = {
        "blur": 0.0,
        "brightness": 0.0,
        "face_area": 0.0,
        "has_face": False,
        "valid": False,
        "reason": "",
    }
    img_bgr = _load_image_bgr(source)
    if img_bgr is None:
        metrics["reason"] = "no_image"
        return metrics

    if img_bgr.ndim == 2:
        gray = img_bgr
    else:
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

    h, w = gray.shape
    metrics["blur"] = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    metrics["brightness"] = float(gray.mean())

    if metrics["blur"] < MIN_BLUR:
        metrics["reason"] = f"blur_bajo={metrics['blur']:.1f}"
        return metrics
    if metrics["brightness"] < MIN_BRIGHTNESS:
        metrics["reason"] = f"oscuro={metrics['brightness']:.1f}"
        return metrics
    if metrics["brightness"] > MAX_BRIGHTNESS:
        metrics["reason"] = f"sobreexpuesto={metrics['brightness']:.1f}"
        return metrics

    if FACE_CASCADE.empty():
        metrics["face_area"] = MIN_FACE_AREA
        metrics["has_face"] = True
    else:
        min_side = max(24, int(min(h, w) * 0.07))
        faces = FACE_CASCADE.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=4,
            minSize=(min_side, min_side),
        )
        if len(faces) > 0:
            best = max(faces, key=lambda box: box[2] * box[3])
            face_pixels = int(best[2]) * int(best[3])
            metrics["face_area"] = face_pixels / (w * h) if (w * h) > 0 else 0.0
            metrics["has_face"] = metrics["face_area"] >= MIN_FACE_AREA

    if not metrics["has_face"]:
        metrics["reason"] = f"cara_ausente_o_pequena={metrics['face_area']:.3f}"
        return metrics

    metrics["valid"] = True
    return metrics


# ------------------------- IDENTIFICACION -------------------------

def identify_embedding(frame_embedding, db_embeddings: dict) -> dict:
    result = {"match": False, "person": None, "distance": None, "all_distances": {}}
    frame_vec = np.asarray(frame_embedding, dtype=np.float32)
    frame_norm = np.linalg.norm(frame_vec)
    if frame_norm == 0:
        return result
    frame_vec = frame_vec / frame_norm

    best_person = None
    best_dist = float("inf")
    all_dists = {}

    for person, embs in db_embeddings.items():
        matrix = _normalized_matrix(embs)
        if matrix.size == 0:
            continue
        similarities = matrix @ frame_vec
        min_dist = float(1.0 - np.max(similarities))
        all_dists[person] = round(min_dist, 4)
        if min_dist < best_dist:
            best_dist = min_dist
            best_person = person

    result["all_distances"] = all_dists

    if best_person and best_dist <= MAX_DISTANCE:
        result["match"] = True
        result["person"] = best_person
        result["distance"] = round(best_dist, 4)

    return result


def identify_frame(source, db_embeddings: dict) -> dict:
    frame_emb = get_embedding(source)
    if frame_emb is None:
        return {"match": False, "person": None, "distance": None, "all_distances": {}}
    return identify_embedding(frame_emb, db_embeddings)


# ------------------------- CAPTURA -------------------------

def ensure_temp_dir():
    TEMP_DIR.mkdir(exist_ok=True)


def clear_temp_dir():
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)


def get_status():
    r = http.get(STATUS_URL, timeout=STATUS_TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_firmware_version() -> dict:
    try:
        r = http.get(VERSION_URL, timeout=STATUS_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def capture_frame(frame_number: int) -> dict:
    started = time.perf_counter()
    r = http.get(CAPTURE_URL, timeout=CAPTURE_TIMEOUT)
    r.raise_for_status()

    raw = r.content
    img_arr = np.frombuffer(raw, dtype=np.uint8)
    img_bgr = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)
    if img_bgr is None:
        raise ValueError("jpeg_invalido")

    name = f"frame_{frame_number}.jpg"
    if SAVE_DEBUG_FRAMES:
        ensure_temp_dir()
        with open(TEMP_DIR / name, "wb") as f:
            f.write(raw)

    return {
        "name": name,
        "image": img_bgr,
        "bytes": len(raw),
        "capture_ms": (time.perf_counter() - started) * 1000.0,
    }


def capture_burst() -> list:
    if SAVE_DEBUG_FRAMES:
        clear_temp_dir()

    frames = []
    for i in range(1, BURST_COUNT + 1):
        try:
            frames.append(capture_frame(i))
        except Exception as e:
            print(f"[CAPTURA ERROR] frame_{i}: {e}")
    return frames


# ------------------------- LOGICA DE DECISION -------------------------

def _frame_name_and_source(frame, index: int):
    if isinstance(frame, dict):
        return frame.get("name", f"frame_{index}.jpg"), frame.get("image"), frame
    if _is_path(frame):
        return Path(frame).name, frame, {}
    return f"frame_{index}.jpg", frame, {}


def process_single_frame(frame, index: int, db_embeddings: dict) -> dict:
    frame_name, source, meta = _frame_name_and_source(frame, index)
    quality = measure_frame_quality(source)
    match_info = {"match": False, "person": None, "distance": None, "all_distances": {}}

    if quality["valid"]:
        match_info = identify_frame(source, db_embeddings)

    tag_rostro = "SI" if quality["has_face"] else "NO"
    tag_valido = "SI" if quality["valid"] else "NO"
    tag_match = (
        f"{match_info['person']} dist={match_info['distance']}"
        if match_info["match"]
        else "NINGUNO"
    )
    capture_ms = meta.get("capture_ms")
    capture_tag = f" captura={capture_ms:.0f}ms" if capture_ms is not None else ""
    bytes_tag = f" bytes={meta.get('bytes')}" if meta.get("bytes") is not None else ""

    print(
        f"[FRAME {index}] rostro={tag_rostro} blur={quality['blur']:.1f} "
        f"brillo={quality['brightness']:.1f} area={quality['face_area']:.3f} "
        f"valido={tag_valido} match={tag_match}{capture_tag}{bytes_tag}"
    )
    if not quality["valid"]:
        print(f"          descartado: {quality['reason']}")

    return {
        "frame": frame_name,
        "quality": quality,
        "match": match_info,
        "capture_ms": capture_ms,
        "bytes": meta.get("bytes"),
    }


def evaluate_decision(frame_results: list, log_votes: bool = True) -> dict:
    valid_frames = [fr for fr in frame_results if fr["quality"]["valid"]]
    n_valid = len(valid_frames)

    if log_votes:
        print(f"[FRAMES] total={len(frame_results)} validos={n_valid}")

    if n_valid < MIN_VALID_FRAMES:
        return {
            "granted": False,
            "reason": f"pocos_frames_validos ({n_valid} < {MIN_VALID_FRAMES})",
            "winner": None,
            "scores": {},
            "n_valid": n_valid,
            "frames": frame_results,
        }

    scores: dict = {}
    for fr in valid_frames:
        mi = fr["match"]
        if not mi["match"]:
            continue
        person = mi["person"]
        dist = mi["distance"]
        if person not in scores:
            scores[person] = {"hits": 0, "distances": [], "best_distance": dist}
        scores[person]["hits"] += 1
        scores[person]["distances"].append(dist)
        if dist < scores[person]["best_distance"]:
            scores[person]["best_distance"] = dist

    results = {}
    for person, s in scores.items():
        hits = s["hits"]
        best_dist = s["best_distance"]
        avg_dist = sum(s["distances"]) / len(s["distances"])
        support = hits / n_valid
        dist_score = max(0.0, 1.0 - (best_dist / MAX_DISTANCE))
        final_score = 0.60 * dist_score + 0.40 * support

        results[person] = {
            "total_frames_validos": n_valid,
            "total_hits": hits,
            "mejor_distancia": round(best_dist, 4),
            "distancia_promedio": round(avg_dist, 4),
            "porcentaje_apoyo": round(support * 100, 1),
            "score_final": round(final_score, 4),
        }
        if log_votes:
            print(
                f"[VOTO] persona={person} hits={hits}/{n_valid} "
                f"mejor={best_dist:.4f} promedio={avg_dist:.4f} "
                f"apoyo={support*100:.1f}% score={final_score:.3f}"
            )

    winner = None
    winner_score = None
    if results:
        winner = max(results, key=lambda p: results[p]["score_final"])
        winner_score = results[winner]["score_final"]

    granted = False
    reason = ""
    if winner is None:
        reason = "sin_matches_validos"
    elif results[winner]["total_hits"] < MIN_HITS:
        reason = f"pocos_hits ({results[winner]['total_hits']} < {MIN_HITS})"
    elif results[winner]["porcentaje_apoyo"] / 100 < MIN_SUPPORT_PCT:
        reason = f"apoyo_insuficiente ({results[winner]['porcentaje_apoyo']}% < {MIN_SUPPORT_PCT*100}%)"
    elif winner_score < MIN_SCORE:
        reason = f"score_bajo ({winner_score:.3f} < {MIN_SCORE})"
    else:
        granted = True
        reason = (
            f"{results[winner]['total_hits']} frames apoyan a {winner}, "
            f"score={winner_score:.3f}, mejor_dist={results[winner]['mejor_distancia']}"
        )

    return {
        "granted": granted,
        "reason": reason,
        "winner": winner,
        "scores": results,
        "n_valid": n_valid,
        "frames": frame_results,
    }


def process_event(frames: list, db_embeddings: dict, early_grant: bool = False) -> dict:
    frame_results = []
    for i, frame in enumerate(frames, 1):
        frame_results.append(process_single_frame(frame, i, db_embeddings))
        if early_grant:
            partial = evaluate_decision(frame_results, log_votes=False)
            if partial["granted"]:
                print(f"[RAPIDO] decision temprana con {len(frame_results)} frame(s)")
                return evaluate_decision(frame_results, log_votes=True)
    return evaluate_decision(frame_results, log_votes=True)


def capture_and_process_event(db_embeddings: dict) -> dict:
    if SAVE_DEBUG_FRAMES:
        clear_temp_dir()

    frame_results = []
    for i in range(1, BURST_COUNT + 1):
        try:
            frame = capture_frame(i)
        except Exception as e:
            print(f"[CAPTURA ERROR] frame_{i}: {e}")
            continue

        frame_results.append(process_single_frame(frame, i, db_embeddings))

        if EARLY_GRANT:
            partial = evaluate_decision(frame_results, log_votes=False)
            if partial["granted"]:
                print(f"[RAPIDO] decision temprana en frame {i}/{BURST_COUNT}")
                break

    return evaluate_decision(frame_results, log_votes=True)


# ------------------------- ESP32 ACTIONS -------------------------

def open_relay():
    r = http.get(OPEN_URL, timeout=COMMAND_TIMEOUT)
    print("[ESP32 OPEN]", r.text)


def close_relay():
    r = http.get(CLOSE_URL, timeout=COMMAND_TIMEOUT)
    print("[ESP32 CLOSE]", r.text)


def ack_verify():
    try:
        http.get(ACK_URL, timeout=COMMAND_TIMEOUT)
    except Exception as e:
        print("[ACK ERROR]", e)


# ------------------------- MAIN LOOP -------------------------

def main():
    global last_request_id

    ensure_temp_dir()
    load_cache()
    if init_db:
        init_db()

    print("=" * 55)
    print(" Servidor DeepFace - Control de Acceso con ESP32-CAM")
    print("=" * 55)
    print(f" ESP32 IP      : {ESP32_IP}")
    print(f" Base de datos : {DB_PATH.resolve()}")
    print(f" Modelo        : {MODEL_NAME} | Detector: {DETECTOR_BACKEND} (fallback: {FALLBACK_DETECTOR_BACKEND})")
    print(f" Captura       : {CAPTURE_SIZE} quality={CAPTURE_QUALITY} fast=1")
    print(f" Frames max    : {BURST_COUNT} | Early grant: {EARLY_GRANT}")
    print(f" Max dist      : {MAX_DISTANCE}")
    firmware_info = get_firmware_version()
    if firmware_info.get("error"):
        print(f" Firmware ESP32: no verificado ({firmware_info['error']})")
    else:
        print(f" Firmware ESP32: {firmware_info.get('firmwareVersion', 'sin_version')}")
    print("=" * 55)

    print("\n[INIT] Leyendo fotos de base_datos/ ...")
    db_embeddings = build_db_embeddings()
    if not db_embeddings:
        print("[INIT] ADVERTENCIA: base_datos/ esta vacia o sin imagenes validas.")
        print("[INIT] Asegurate de tener carpetas como base_datos/Jhon/foto1.jpg")
    else:
        total_embs = sum(len(v) for v in db_embeddings.values())
        db_embeddings = build_db_index(db_embeddings)
        print(f"[INIT] Listo: {len(db_embeddings)} persona(s), {total_embs} foto(s) procesada(s)\n")

    while True:
        try:
            status = get_status()
            request_id = int(status.get("verificationRequestId", -1))
            verify_requested = bool(status.get("verificationRequested", False))

            if verify_requested and request_id != last_request_id:
                print(f"\n{'='*55}")
                print(f"[EVENTO] request_id={request_id}")
                print(f"{'='*55}")
                last_request_id = request_id

                result = capture_and_process_event(db_embeddings)

                if not result["frames"]:
                    print("[RESULTADO] Sin capturas. Cerrando relay.")
                    close_relay()
                    ack_verify()
                    time.sleep(POLL_INTERVAL)
                    continue

                summary = {
                    "request_id": request_id,
                    "total_frames": len(result["frames"]),
                    "total_frames_validos": result["n_valid"],
                    "persona_ganadora": result["winner"],
                    "score_final": result["scores"].get(result["winner"] or "", {}).get("score_final"),
                    "mejor_distancia": result["scores"].get(result["winner"] or "", {}).get("mejor_distancia"),
                    "decision": "PERMITIDO" if result["granted"] else "DENEGADO",
                    "motivo": result["reason"],
                }
                print("\n[RESULTADO]", json.dumps(summary, ensure_ascii=False, indent=2))

                if log_acceso:
                    persona = (
                        get_persona_by_nombre(result["winner"])
                        if result["winner"] and get_persona_by_nombre
                        else None
                    )
                    winner_score = result["scores"].get(result["winner"] or "", {})
                    log_acceso(
                        persona["id"] if persona else None,
                        result["winner"],
                        summary["decision"],
                        winner_score.get("score_final"),
                        winner_score.get("mejor_distancia"),
                        result["n_valid"],
                        len(result["frames"]),
                        result["reason"],
                    )
                    if not result["granted"] and log_desconocido:
                        log_desconocido(None)

                if result["granted"]:
                    print(f"\n[ACCESO] PERMITIDO - {result['reason']}")
                    if send_notification:
                        send_notification(
                            "access_granted",
                            "Acceso concedido",
                            f"Se concedio acceso a {result['winner']}. {result['reason']}",
                            summary,
                        )
                    open_relay()
                else:
                    print(f"\n[ACCESO] DENEGADO  - {result['reason']}")
                    if send_notification:
                        send_notification(
                            "unknown_attempt",
                            "Intento de acceso desconocido",
                            f"Acceso denegado. Motivo: {result['reason']}",
                            summary,
                        )
                    close_relay()

                ack_verify()

            time.sleep(POLL_INTERVAL)

        except Exception as e:
            print(f"[ERROR GENERAL] {e}")
            time.sleep(ERROR_RETRY_DELAY)


if __name__ == "__main__":
    main()
