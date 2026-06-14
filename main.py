import os
os.environ["TF_USE_LEGACY_KERAS"] = "1"
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"

import time
import json
import shutil
import pickle
import hashlib
from pathlib import Path
import requests
import numpy as np
import cv2
from deepface import DeepFace

# ===================== SETTINGS =====================
ESP32_IP          = "192.168.0.50"   # <-- cambia a la IP de tu ESP32-CAM

# Rutas
DB_PATH           = Path("base_datos")       # tus carpetas con fotos por persona
TEMP_DIR          = Path("temp_captures")
CACHE_FILE        = Path("embeddings_cache.pkl")

# Captura
BURST_COUNT       = 5


# Modelo
MODEL_NAME        = "ArcFace"
DETECTOR_BACKEND  = "opencv"
DISTANCE_METRIC   = "cosine"

# Umbrales de calidad de frame (permisivos para ESP32-CAM)
MIN_BLUR          = 20.0
MIN_BRIGHTNESS    = 20.0
MAX_BRIGHTNESS    = 245.0
MIN_FACE_AREA     = 0.01

# Umbrales de decisión (permisivos para ESP32-CAM)
MAX_DISTANCE      = 0.68
MIN_VALID_FRAMES  = 1
MIN_HITS          = 1
MIN_SUPPORT_PCT   = 0.10
MIN_SCORE         = 0.20

# ESP32 URLs
STATUS_URL  = f"http://{ESP32_IP}/status"
CAPTURE_URL = f"http://{ESP32_IP}/capture?quality=5&size=xga"
OPEN_URL    = f"http://{ESP32_IP}/access/open?ms=5000"
CLOSE_URL   = f"http://{ESP32_IP}/access/close"
ACK_URL     = f"http://{ESP32_IP}/verify/ack"
# ====================================================

last_request_id = -1
embeddings_cache: dict = {}


# ───────────────────────── CACHE DE EMBEDDINGS ──────────────────────────

def _file_hash(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()

def load_cache():
    global embeddings_cache
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "rb") as f:
                embeddings_cache = pickle.load(f)
            print(f"[CACHE] Cargados {len(embeddings_cache)} embeddings desde disco")
        except Exception as e:
            print(f"[CACHE] No se pudo cargar cache: {e}")
            embeddings_cache = {}

def save_cache():
    try:
        with open(CACHE_FILE, "wb") as f:
            pickle.dump(embeddings_cache, f)
    except Exception as e:
        print(f"[CACHE] Error guardando cache: {e}")

def get_embedding(img_path: Path):
    key = str(img_path)
    current_hash = _file_hash(img_path)
    cached = embeddings_cache.get(key)
    if cached and cached.get("hash") == current_hash:
        return cached["embedding"]
    try:
        result = DeepFace.represent(
            img_path=str(img_path),
            model_name=MODEL_NAME,
            detector_backend=DETECTOR_BACKEND,
            enforce_detection=False,
        )
        if result and len(result) > 0:
            emb = result[0]["embedding"]
            embeddings_cache[key] = {"embedding": emb, "hash": current_hash}
            return emb
    except Exception as e:
        print(f"[EMBEDDING ERROR] {img_path.name}: {e}")
    return None

def cosine_distance(a, b) -> float:
    va, vb = np.array(a), np.array(b)
    norm_a, norm_b = np.linalg.norm(va), np.linalg.norm(vb)
    if norm_a == 0 or norm_b == 0:
        return 1.0
    return float(1.0 - np.dot(va, vb) / (norm_a * norm_b))


# ───────────────────────── BASE DE DATOS LOCAL ──────────────────────────
# Lee TODAS las fotos de base_datos/NombrePersona/*.jpg
# y genera un embedding por foto para comparar contra los frames

def build_db_embeddings() -> dict:
    db: dict = {}
    exts = {".jpg", ".jpeg", ".png", ".bmp"}
    dirty = False

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


# ───────────────────────── CALIDAD DE FRAME ──────────────────────────

def measure_frame_quality(img_path: Path) -> dict:
    metrics = {
        "blur": 0.0, "brightness": 0.0, "face_area": 0.0,
        "has_face": False, "valid": False, "reason": "",
    }
    img_bgr = cv2.imread(str(img_path))
    if img_bgr is None:
        metrics["reason"] = "no_image"
        return metrics

    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    metrics["blur"]       = float(cv2.Laplacian(gray, cv2.CV_64F).var())
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

    try:
        faces = DeepFace.extract_faces(
            img_path=str(img_path),
            detector_backend=DETECTOR_BACKEND,
            enforce_detection=False,
        )
        if faces and faces[0].get("facial_area"):
            fa = faces[0]["facial_area"]
            face_pixels = fa.get("w", 0) * fa.get("h", 0)
            metrics["face_area"] = face_pixels / (w * h) if (w * h) > 0 else 0.0
            metrics["has_face"]  = metrics["face_area"] >= MIN_FACE_AREA
    except Exception:
        pass

    if not metrics["has_face"]:
        metrics["reason"] = f"cara_ausente_o_pequeña={metrics['face_area']:.3f}"
        return metrics

    metrics["valid"] = True
    return metrics


# ───────────────────────── IDENTIFICACIÓN ──────────────────────────
# Compara el frame contra TODAS las fotos de TODAS las personas en base_datos
# y elige la persona con menor distancia

def identify_frame(frame_path: Path, db_embeddings: dict) -> dict:
    result = {"match": False, "person": None, "distance": None, "all_distances": {}}
    frame_emb = get_embedding(frame_path)
    if frame_emb is None:
        return result

    best_person = None
    best_dist   = float("inf")
    all_dists   = {}

    for person, embs in db_embeddings.items():
        # compara contra cada foto de esa persona, toma la menor distancia
        dists    = [cosine_distance(frame_emb, e) for e in embs]
        min_dist = min(dists)
        all_dists[person] = round(min_dist, 4)
        if min_dist < best_dist:
            best_dist   = min_dist
            best_person = person

    result["all_distances"] = all_dists

    if best_person and best_dist <= MAX_DISTANCE:
        result["match"]    = True
        result["person"]   = best_person
        result["distance"] = round(best_dist, 4)

    return result


# ───────────────────────── CAPTURA ──────────────────────────

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

def capture_burst() -> list:
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
            print(f"[CAPTURA ERROR] frame_{i+1}: {e}")
    return files


# ───────────────────────── LÓGICA DE DECISIÓN ──────────────────────────

def process_event(files: list, db_embeddings: dict) -> dict:
    frame_results = []
    valid_frames  = []

    for i, fp in enumerate(files, 1):
        quality    = measure_frame_quality(fp)
        match_info = {"match": False, "person": None, "distance": None, "all_distances": {}}

        if quality["valid"]:
            match_info = identify_frame(fp, db_embeddings)

        tag_rostro = "SI" if quality["has_face"] else "NO"
        tag_valido = "SI" if quality["valid"]    else "NO"
        tag_match  = f"{match_info['person']} dist={match_info['distance']}" if match_info["match"] else "NINGUNO"

        print(
            f"[FRAME {i}] rostro={tag_rostro} blur={quality['blur']:.1f} "
            f"brillo={quality['brightness']:.1f} area={quality['face_area']:.3f} "
            f"valido={tag_valido} match={tag_match}"
        )
        if not quality["valid"]:
            print(f"         descartado: {quality['reason']}")

        frame_results.append({"frame": fp.name, "quality": quality, "match": match_info})
        if quality["valid"]:
            valid_frames.append(frame_results[-1])

    n_valid = len(valid_frames)
    print(f"[FRAMES] total={len(files)} validos={n_valid}")

    if n_valid < MIN_VALID_FRAMES:
        return {
            "granted": False,
            "reason": f"pocos_frames_validos ({n_valid} < {MIN_VALID_FRAMES})",
            "winner": None, "scores": {}, "n_valid": n_valid, "frames": frame_results,
        }

    scores: dict = {}
    for fr in valid_frames:
        mi = fr["match"]
        if not mi["match"]:
            continue
        person = mi["person"]
        dist   = mi["distance"]
        if person not in scores:
            scores[person] = {"hits": 0, "distances": [], "best_distance": dist}
        scores[person]["hits"] += 1
        scores[person]["distances"].append(dist)
        if dist < scores[person]["best_distance"]:
            scores[person]["best_distance"] = dist

    results = {}
    for person, s in scores.items():
        hits       = s["hits"]
        best_dist  = s["best_distance"]
        avg_dist   = sum(s["distances"]) / len(s["distances"])
        support    = hits / n_valid
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
        print(
            f"[VOTO] persona={person} hits={hits}/{n_valid} "
            f"mejor={best_dist:.4f} promedio={avg_dist:.4f} "
            f"apoyo={support*100:.1f}% score={final_score:.3f}"
        )

    winner       = None
    winner_score = None
    if results:
        winner       = max(results, key=lambda p: results[p]["score_final"])
        winner_score = results[winner]["score_final"]

    granted = False
    reason  = ""
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
        reason  = (
            f"{results[winner]['total_hits']} frames apoyan a {winner}, "
            f"score={winner_score:.3f}, mejor_dist={results[winner]['mejor_distancia']}"
        )

    return {
        "granted": granted, "reason": reason, "winner": winner,
        "scores": results, "n_valid": n_valid, "frames": frame_results,
    }


# ───────────────────────── ESP32 ACTIONS ──────────────────────────

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


# ───────────────────────── MAIN LOOP ──────────────────────────

def main():
    global last_request_id

    ensure_temp_dir()
    load_cache()

    print("=" * 55)
    print(" Servidor DeepFace — Control de Acceso con ESP32-CAM")
    print("=" * 55)
    print(f" ESP32 IP      : {ESP32_IP}")
    print(f" Base de datos : {DB_PATH.resolve()}")
    print(f" Modelo        : {MODEL_NAME} | Detector: {DETECTOR_BACKEND}")
    print(f" Frames/evento : {BURST_COUNT} | Max dist: {MAX_DISTANCE}")
    print("=" * 55)

    print("\n[INIT] Leyendo fotos de base_datos/ ...")
    db_embeddings = build_db_embeddings()
    if not db_embeddings:
        print("[INIT] ADVERTENCIA: base_datos/ está vacía o sin imágenes válidas.")
        print("[INIT] Asegúrate de tener carpetas como base_datos/Jhon/foto1.jpg")
    else:
        total_embs = sum(len(v) for v in db_embeddings.values())
        print(f"[INIT] Listo: {len(db_embeddings)} persona(s), {total_embs} foto(s) procesada(s)\n")

    while True:
        try:
            status           = get_status()
            request_id       = int(status.get("verificationRequestId", -1))
            verify_requested = bool(status.get("verificationRequested", False))

            if verify_requested and request_id != last_request_id:
                print(f"\n{'='*55}")
                print(f"[EVENTO] request_id={request_id}")
                print(f"{'='*55}")
                last_request_id = request_id

                files = capture_burst()
                print(f"[CAPTURAS] obtenidas={len(files)}")

                if not files:
                    print("[RESULTADO] Sin capturas. Cerrando relay.")
                    close_relay()
                    ack_verify()
                    time.sleep(1)
                    continue

                result = process_event(files, db_embeddings)

                summary = {
                    "request_id"           : request_id,
                    "total_frames"         : len(files),
                    "total_frames_validos" : result["n_valid"],
                    "persona_ganadora"     : result["winner"],
                    "score_final"          : result["scores"].get(result["winner"] or "", {}).get("score_final"),
                    "mejor_distancia"      : result["scores"].get(result["winner"] or "", {}).get("mejor_distancia"),
                    "decision"             : "PERMITIDO" if result["granted"] else "DENEGADO",
                    "motivo"               : result["reason"],
                }
                print("\n[RESULTADO]", json.dumps(summary, ensure_ascii=False, indent=2))

                if result["granted"]:
                    print(f"\n[ACCESO] PERMITIDO — {result['reason']}")
                    open_relay()
                else:
                    print(f"\n[ACCESO] DENEGADO  — {result['reason']}")
                    close_relay()

                ack_verify()

            time.sleep(0.7)

        except Exception as e:
            print(f"[ERROR GENERAL] {e}")
            time.sleep(2)


if __name__ == "__main__":
    main()