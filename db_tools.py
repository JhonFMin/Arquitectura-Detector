"""
db_tools.py — Herramientas de gestión de la base de datos facial
================================================================
Uso:
    python db_tools.py --help
    python db_tools.py list
    python db_tools.py rebuild
    python db_tools.py add    --person Nombre --images foto1.jpg foto2.jpg
    python db_tools.py remove --person Nombre
    python db_tools.py test   --image ruta/a/imagen.jpg
    python db_tools.py tune   --image ruta/a/imagen.jpg
"""

import os
os.environ["TF_USE_LEGACY_KERAS"] = "1"
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"

import argparse
import pickle
import shutil
import hashlib
from pathlib import Path

import numpy as np

# ── importar configuración desde main ──────────────────────────────────────
from main import (
    DB_PATH, CACHE_FILE, MODEL_NAME, DETECTOR_BACKEND, DISTANCE_METRIC,
    MAX_DISTANCE, MIN_BLUR, MIN_BRIGHTNESS, MAX_BRIGHTNESS, MIN_FACE_AREA,
    load_cache, save_cache, get_embedding, build_db_embeddings,
    cosine_distance, measure_frame_quality, identify_frame,
    embeddings_cache,
)


# ───────────────────────── HELPERS ─────────────────────────────────────────

def _sep(char="─", n=55):
    print(char * n)


# ───────────────────────── COMANDOS ────────────────────────────────────────

def cmd_list(_args):
    """Lista todas las personas y cuántas imágenes tienen."""
    _sep()
    print("  BASE DE DATOS DE PERSONAS AUTORIZADAS")
    _sep()
    exts = {".jpg", ".jpeg", ".png", ".bmp"}
    if not DB_PATH.exists() or not any(DB_PATH.iterdir()):
        print("  (vacía)")
    else:
        total_imgs = 0
        for person_dir in sorted(DB_PATH.iterdir()):
            if not person_dir.is_dir():
                continue
            imgs = [p for p in person_dir.iterdir() if p.suffix.lower() in exts]
            print(f"  • {person_dir.name:20s}  {len(imgs)} imagen(es)")
            for img in sorted(imgs):
                print(f"      - {img.name}")
            total_imgs += len(imgs)
        _sep()
        print(f"  Total: {len(list(DB_PATH.iterdir()))} persona(s), {total_imgs} imagen(es)")
    _sep()


def cmd_rebuild(_args):
    """Borra el cache y recalcula todos los embeddings desde cero."""
    global embeddings_cache
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()
        print(f"[REBUILD] Cache anterior eliminado: {CACHE_FILE}")

    # Limpiar también el .pkl legacy de DeepFace.find()
    for pkl in DB_PATH.glob("*.pkl"):
        pkl.unlink()
        print(f"[REBUILD] PKL legacy eliminado: {pkl.name}")

    print("[REBUILD] Recalculando embeddings...")
    db = build_db_embeddings()
    total = sum(len(v) for v in db.values())
    print(f"[REBUILD] Listo: {len(db)} persona(s), {total} embedding(s) guardados en cache.")


def cmd_add(args):
    """Agrega imágenes a una persona (o crea la carpeta si no existe)."""
    person_dir = DB_PATH / args.person
    person_dir.mkdir(parents=True, exist_ok=True)

    added = 0
    exts = {".jpg", ".jpeg", ".png", ".bmp"}
    for src in args.images:
        src = Path(src)
        if not src.exists():
            print(f"[ADD] ADVERTENCIA: no se encontró {src}")
            continue
        if src.suffix.lower() not in exts:
            print(f"[ADD] ADVERTENCIA: formato no soportado: {src.name}")
            continue
        dst = person_dir / src.name
        shutil.copy2(src, dst)
        print(f"[ADD] Copiado: {src.name} → {person_dir}")
        added += 1

    if added > 0:
        print(f"[ADD] {added} imagen(es) agregadas a '{args.person}'. Reconstruyendo embeddings...")
        cmd_rebuild(None)
    else:
        print("[ADD] No se agregó ninguna imagen.")


def cmd_remove(args):
    """Elimina a una persona completa de la base de datos."""
    person_dir = DB_PATH / args.person
    if not person_dir.exists():
        print(f"[REMOVE] No existe '{args.person}' en la base de datos.")
        return

    confirm = input(f"¿Eliminar a '{args.person}' y todas sus imágenes? (s/N): ").strip().lower()
    if confirm != "s":
        print("[REMOVE] Operación cancelada.")
        return

    shutil.rmtree(person_dir)
    print(f"[REMOVE] '{args.person}' eliminado.")

    # Limpiar sus embeddings del cache
    load_cache()
    keys_to_del = [k for k in embeddings_cache if args.person in k]
    for k in keys_to_del:
        del embeddings_cache[k]
    save_cache()
    print(f"[REMOVE] {len(keys_to_del)} entrada(s) eliminadas del cache.")


def cmd_test(args):
    """
    Prueba una imagen contra la base de datos y muestra el resultado completo,
    incluyendo distancias a todas las personas.
    """
    img_path = Path(args.image)
    if not img_path.exists():
        print(f"[TEST] No se encontró la imagen: {img_path}")
        return

    print(f"\n[TEST] Imagen: {img_path}")
    _sep()

    # Calidad
    quality = measure_frame_quality(img_path)
    print(f"  Blur       : {quality['blur']:.2f}  (min recomendado: {MIN_BLUR})")
    print(f"  Brillo     : {quality['brightness']:.2f}  (rango: {MIN_BRIGHTNESS}–{MAX_BRIGHTNESS})")
    print(f"  Área cara  : {quality['face_area']:.4f}  (min: {MIN_FACE_AREA})")
    print(f"  Tiene cara : {'SÍ' if quality['has_face'] else 'NO'}")
    print(f"  Frame válido: {'SÍ ✓' if quality['valid'] else 'NO ✗  — ' + quality['reason']}")
    _sep()

    load_cache()
    db = build_db_embeddings()

    if not db:
        print("[TEST] Base de datos vacía.")
        return

    # Embedding del frame
    frame_emb = get_embedding(img_path)
    if frame_emb is None:
        print("[TEST] No se pudo calcular embedding.")
        return

    print("\n  Distancias por persona:")
    print(f"  {'Persona':<20}  {'Min dist':>10}  {'Avg dist':>10}  {'Fotos':>6}  {'Match?':>7}")
    _sep()
    for person, embs in sorted(db.items()):
        dists = [cosine_distance(frame_emb, e) for e in embs]
        min_d = min(dists)
        avg_d = sum(dists) / len(dists)
        match = "SÍ ✓" if min_d <= MAX_DISTANCE else "NO"
        print(f"  {person:<20}  {min_d:>10.4f}  {avg_d:>10.4f}  {len(embs):>6}  {match:>7}")

    _sep()
    result = identify_frame(img_path, db)
    if result["match"]:
        print(f"\n  → RESULTADO: {result['person']}  (dist={result['distance']})")
    else:
        print("\n  → RESULTADO: sin match (ninguna persona superó el umbral)")
    _sep()


def cmd_tune(args):
    """
    Muestra qué umbral máximo de distancia sería necesario para que
    esta imagen haga match con cada persona. Útil para calibrar MAX_DISTANCE.
    """
    img_path = Path(args.image)
    if not img_path.exists():
        print(f"[TUNE] No se encontró: {img_path}")
        return

    load_cache()
    db = build_db_embeddings()

    frame_emb = get_embedding(img_path)
    if frame_emb is None:
        print("[TUNE] No se pudo calcular embedding.")
        return

    print(f"\n[TUNE] Imagen: {img_path}")
    print(f"  MAX_DISTANCE actual: {MAX_DISTANCE}")
    _sep()
    print(f"  {'Persona':<20}  {'Min dist':>10}  {'¿Pasa umbral actual?':>22}")
    _sep()
    for person, embs in sorted(db.items()):
        dists = [cosine_distance(frame_emb, e) for e in embs]
        min_d = min(dists)
        pasa = "SÍ ✓" if min_d <= MAX_DISTANCE else f"NO  (necesitaría MAX_DISTANCE >= {min_d:.4f})"
        print(f"  {person:<20}  {min_d:>10.4f}  {pasa}")
    _sep()
    print("  Tip: si ves muchos falsos rechazos, sube MAX_DISTANCE en main.py.")
    print("  Tip: si ves falsos positivos, bájalo.")
    _sep()


# ───────────────────────── CLI ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Herramientas de gestión de la base de datos facial",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list",    help="Lista personas y sus imágenes")
    sub.add_parser("rebuild", help="Reconstruye el cache de embeddings desde cero")

    p_add = sub.add_parser("add", help="Agrega imágenes a una persona")
    p_add.add_argument("--person", required=True, help="Nombre de la persona")
    p_add.add_argument("--images", nargs="+", required=True, help="Rutas de imágenes a agregar")

    p_rm = sub.add_parser("remove", help="Elimina una persona de la base de datos")
    p_rm.add_argument("--person", required=True, help="Nombre de la persona a eliminar")

    p_test = sub.add_parser("test", help="Prueba una imagen contra la base de datos")
    p_test.add_argument("--image", required=True, help="Ruta de la imagen a probar")

    p_tune = sub.add_parser("tune", help="Calibra el umbral MAX_DISTANCE con una imagen")
    p_tune.add_argument("--image", required=True, help="Ruta de la imagen de referencia")

    args = parser.parse_args()

    cmds = {
        "list":    cmd_list,
        "rebuild": cmd_rebuild,
        "add":     cmd_add,
        "remove":  cmd_remove,
        "test":    cmd_test,
        "tune":    cmd_tune,
    }
    cmds[args.cmd](args)


if __name__ == "__main__":
    main()
