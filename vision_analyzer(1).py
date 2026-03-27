#!/usr/bin/env python3
"""
vision_analyzer.py
==================
Module 2 — Capture d'images depuis une vidéo YouTube + analyse Groq Vision.

Stratégie :
  1. Télécharge la vidéo en basse qualité (360p) avec yt-dlp
  2. Extrait une capture toutes les N secondes avec ffmpeg
  3. Envoie chaque capture à Groq llama-3.2-11b-vision (gratuit)
  4. Compare avec la base locale iconic_db.json
  5. Retourne les moments avec un bonus de score pour les éléments iconiques

Groq Vision gratuit :
  → Crée un compte sur https://console.groq.com
  → Clé API gratuite, 14 400 req/jour, pas de CB requise

Usage standalone :
    python vision_analyzer.py --url URL --groq-key CLE_GROQ

Import :
    from vision_analyzer import analyze_video_frames
"""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError

GROQ_API_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL     = "llama-3.2-11b-vision-preview"
DB_PATH        = os.path.join(os.path.dirname(__file__), "iconic_db.json")

# Intervalle entre captures : 1 capture toutes les N secondes de vidéo
# Plus petit = plus précis mais plus de requêtes Groq
CAPTURE_INTERVAL_SEC = 120   # 1 capture / 2 minutes → ~30 captures pour 1h

# Résolution de capture (basse = moins de données à envoyer)
CAPTURE_WIDTH = 640


def load_iconic_db(db_path: str = DB_PATH) -> dict:
    """Charge la base de données locale des éléments iconiques."""
    if not os.path.exists(db_path):
        print(f"  ⚠️  Base iconic_db.json introuvable à {db_path}")
        print("       Télécharge-la depuis le projet ou crée-en une vide.")
        return {}
    with open(db_path, encoding="utf-8") as f:
        return json.load(f)


def download_video_low_quality(url: str, output_dir: str,
                               cookies: str = None,
                               proxy: str = None,
                               po_token: str = None) -> str:
    """Télécharge la vidéo en 360p pour minimiser le temps + espace disque."""
    print("  ⬇️  Téléchargement vidéo (360p pour captures)...")
    try:
        import yt_dlp
    except ImportError:
        print("  ❌ yt-dlp manquant : pip install yt-dlp")
        sys.exit(1)

    output_path = os.path.join(output_dir, "video.%(ext)s")
    ydl_opts = {
        "format": "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best[height<=360]",
        "outtmpl": output_path,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        # JS solver (requis sur AWS)
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        "js_runtimes": {"node": {}},
        "remote_components": "ejs:github",
    }
    if cookies:
        ydl_opts["cookiefile"] = cookies
    if proxy:
        ydl_opts["proxy"] = proxy
    if po_token:
        ydl_opts["extractor_args"]["youtube"]["po_token"] = [f"WEB+{po_token}"]
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # Trouve le fichier téléchargé
    for f in os.listdir(output_dir):
        if f.startswith("video.") and not f.endswith(".part"):
            fp = os.path.join(output_dir, f)
            size_mb = os.path.getsize(fp) / 1_048_576
            print(f"     ✅ Vidéo téléchargée : {size_mb:.0f} MB")
            return fp

    print("  ❌ Fichier vidéo introuvable après téléchargement")
    sys.exit(1)


def extract_frames(video_path: str, output_dir: str, interval_sec: int, duration_sec: float) -> list[tuple[float, str]]:
    """
    Extrait des captures avec ffmpeg toutes les interval_sec secondes.
    Retourne une liste de (timestamp_sec, chemin_image).
    """
    print(f"  🖼️  Extraction des captures (1 toutes les {interval_sec}s)...")

    # Vérifie que ffmpeg est disponible
    result = subprocess.run(["ffmpeg", "-version"], capture_output=True)
    if result.returncode != 0:
        print("  ❌ ffmpeg non installé. Installe-le : https://ffmpeg.org/download.html")
        return []

    frames = []
    timestamps = [i for i in range(0, int(duration_sec), interval_sec)]

    for ts in timestamps:
        frame_path = os.path.join(output_dir, f"frame_{ts:06d}.jpg")
        cmd = [
            "ffmpeg", "-ss", str(ts), "-i", video_path,
            "-frames:v", "1",
            "-vf", f"scale={CAPTURE_WIDTH}:-1",
            "-q:v", "5",         # qualité JPEG 1-31, 5 = bon compromis
            "-y",                # overwrite
            frame_path,
            "-loglevel", "quiet",
        ]
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode == 0 and os.path.exists(frame_path):
            frames.append((float(ts), frame_path))

    print(f"     → {len(frames)} captures extraites")
    return frames


def image_to_base64(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def build_groq_prompt(db: dict) -> str:
    """Construit le prompt système pour Groq Vision basé sur la DB locale."""
    streamers = ", ".join(db.get("streamers_gaming", [])[:20])
    roblox    = ", ".join(db.get("roblox_items_and_games", [])[:15])
    minecraft = ", ".join(db.get("minecraft_elements", [])[:15])
    viral     = ", ".join(db.get("viral_visual_moments", [])[:15])
    music     = ", ".join(db.get("music_artists_popular", [])[:10])

    return f"""Tu analyses une capture d'écran de vidéo gaming/divertissement YouTube.

Réponds UNIQUEMENT en JSON valide, sans texte autour, avec exactement ces champs :
{{
  "game_detected": "nom du jeu ou null",
  "streamer_recognized": "nom si reconnu parmi ({streamers}) ou null",
  "iconic_elements": ["liste des éléments importants visibles"],
  "roblox_items": ["items Roblox si applicable : {roblox}"],
  "minecraft_elements": ["éléments Minecraft si applicable : {minecraft}"],
  "viral_moment_type": "type si applicable : {viral} ou null",
  "music_artist": "artiste si visible/affiché : {music} ou null",
  "excitement_level": 0,
  "clip_worthy": false,
  "reason": "explication courte en 1 phrase"
}}

excitement_level : entier 0-10 (0=rien d'intéressant, 10=moment viral incontournable)
clip_worthy : true si ce moment mérite d'être clippé, false sinon"""


def analyze_frame_groq(image_path: str, groq_key: str, prompt: str,
                       proxy: str = None) -> dict | None:
    """Envoie une capture à Groq Vision et retourne l'analyse JSON."""
    img_b64 = image_to_base64(image_path)

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{img_b64}",
                            "detail": "low",    # low = moins de tokens, assez pour la détection
                        },
                    },
                ],
            }
        ],
        "max_tokens": 400,
        "temperature": 0.1,   # réponses déterministes
    }

    headers = {
        "Authorization": f"Bearer {groq_key}",
        "Content-Type": "application/json",
    }

    try:
        import requests as _req
        proxies = {"https": proxy, "http": proxy} if proxy else None
        r = _req.post(GROQ_API_URL, json=payload, headers=headers,
                      proxies=proxies, timeout=30)
        if r.status_code == 429:
            print("     ⏳ Rate limit Groq atteint — pause 60s...")
            time.sleep(60)
            return None
        if r.status_code != 200:
            print(f"     ⚠️  Erreur Groq {r.status_code}: {r.text[:200]}")
            return None
        content = r.json()["choices"][0]["message"]["content"].strip()
        content = re.sub(r"```json\s*|\s*```", "", content).strip()
        return json.loads(content)
    except (json.JSONDecodeError, KeyError):
        return None
    except Exception as e:
        print(f"     ⚠️  Erreur Groq inattendue : {e}")
        return None


def score_frame_result(analysis: dict, db: dict) -> float:
    """
    Calcule un score pour une frame basé sur l'analyse Groq + DB locale.
    """
    if not analysis:
        return 0.0

    boosts = db.get("score_boosts", {})
    score = 0.0

    # Base : niveau d'excitation déclaré par le modèle
    score += analysis.get("excitement_level", 0) * 0.5

    # Bonus streamer reconnu
    if analysis.get("streamer_recognized"):
        score += boosts.get("streamer_recognized", 5.0)

    # Bonus items rares Roblox/Minecraft
    roblox_items = analysis.get("roblox_items", [])
    mc_elements  = analysis.get("minecraft_elements", [])
    if roblox_items or mc_elements:
        score += boosts.get("rare_item_detected", 4.0)

    # Bonus moment viral identifié
    if analysis.get("viral_moment_type"):
        score += boosts.get("viral_moment_type", 3.5)

    # Bonus artiste musique
    if analysis.get("music_artist"):
        score += boosts.get("music_artist", 3.0)

    # Bonus éléments iconiques généraux
    iconic = analysis.get("iconic_elements", [])
    if len(iconic) >= 2:
        score += boosts.get("game_element_iconic", 2.0)

    # clip_worthy déclaré par le modèle
    if analysis.get("clip_worthy"):
        score += 2.0

    return round(score, 2)


def analyze_video_frames(
    url: str,
    groq_key: str,
    duration_sec: float,
    top_n: int = 20,
    clip_duration: float = 60,
    interval_sec: int = CAPTURE_INTERVAL_SEC,
    db_path: str = DB_PATH,
    cookies: str = None,
    proxy: str = None,
    po_token: str = None,
) -> list[dict]:
    """
    Pipeline complet : téléchargement → captures → analyse Groq → scoring.
    Retourne les top_n moments les plus intéressants visuellement.
    """
    db = load_iconic_db(db_path)
    prompt = build_groq_prompt(db)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Téléchargement
        video_path = download_video_low_quality(url, tmpdir,
                                                cookies=cookies,
                                                proxy=proxy,
                                                po_token=po_token)

        # Extraction des frames
        frames = extract_frames(video_path, tmpdir, interval_sec, duration_sec)
        if not frames:
            print("  ⚠️  Aucune capture extraite")
            return []

        # Analyse Groq frame par frame
        print(f"  🤖 Analyse Groq Vision ({len(frames)} captures)...")
        results = []

        for i, (ts, frame_path) in enumerate(frames):
            analysis = analyze_frame_groq(frame_path, groq_key, prompt, proxy=proxy)
            score = score_frame_result(analysis, db)

            # Affichage compact
            game = (analysis or {}).get("game_detected") or "?"
            streamer = (analysis or {}).get("streamer_recognized") or ""
            exc = (analysis or {}).get("excitement_level", 0)
            reason = (analysis or {}).get("reason", "")

            status = "🔥" if score >= 5 else ("⚡" if score >= 2 else "·")
            print(f"     {status} {_sec_to_ts(ts)}  exc:{exc}  score:{score:.1f}  {game}"
                  f"{' | ' + streamer if streamer else ''}")

            if score > 0 or (analysis or {}).get("clip_worthy"):
                # Construit le titre du moment
                parts = []
                if (analysis or {}).get("streamer_recognized"):
                    parts.append((analysis or {})["streamer_recognized"])
                if (analysis or {}).get("viral_moment_type"):
                    parts.append((analysis or {})["viral_moment_type"])
                if (analysis or {}).get("roblox_items"):
                    parts.append(", ".join((analysis or {})["roblox_items"][:2]))
                if reason:
                    parts.append(reason[:60])
                title = " | ".join(parts) if parts else game

                results.append({
                    "start":       ts,
                    "duration":    clip_duration,
                    "score":       score,
                    "source":      "vision",
                    "title":       title or "Moment visuel intéressant",
                    "game":        game,
                    "analysis":    analysis,
                })

            # Petite pause pour ne pas dépasser le rate limit Groq
            time.sleep(0.3)

    if not results:
        return []

    # Tri + déduplication
    results.sort(key=lambda x: x["score"], reverse=True)
    kept = []
    for r in results:
        too_close = any(abs(r["start"] - k["start"]) < clip_duration * 0.6 for k in kept)
        if not too_close:
            kept.append(r)
        if len(kept) >= top_n:
            break

    kept.sort(key=lambda x: x["start"])

    clip_worthy_count = sum(1 for r in kept if (r.get("analysis") or {}).get("clip_worthy"))
    print(f"     → {len(kept)} moments visuels détectés ({clip_worthy_count} clip-worthy)")
    return kept


def _sec_to_ts(s: float) -> str:
    s = int(s)
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


# ─── CLI standalone ───────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Analyse visuelle des moments forts via Groq Vision (gratuit)"
    )
    parser.add_argument("--url",       required=True, help="URL YouTube")
    parser.add_argument("--groq-key",  required=True, help="Clé API Groq (gratuit sur console.groq.com)")
    parser.add_argument("--duration",  type=float, default=3600)
    parser.add_argument("--interval",  type=int, default=CAPTURE_INTERVAL_SEC,
                        help=f"Intervalle entre captures en secondes (défaut: {CAPTURE_INTERVAL_SEC})")
    parser.add_argument("--top",       type=int, default=20)
    parser.add_argument("--db",        default=DB_PATH, help="Chemin vers iconic_db.json")
    args = parser.parse_args()

    moments = analyze_video_frames(
        url=args.url,
        groq_key=args.groq_key,
        duration_sec=args.duration,
        top_n=args.top,
        interval_sec=args.interval,
        db_path=args.db,
    )

    print(f"\n{'─'*55}")
    print(f"  Vision IA — {len(moments)} moments détectés")
    print(f"{'─'*55}")
    for i, m in enumerate(moments, 1):
        link = f"https://youtu.be/?t={int(m['start'])}"
        print(f"  #{i:02d}  {_sec_to_ts(m['start'])}  score:{m['score']:.1f}  {m['title'][:60]}")
        print(f"       {link}")
    print()
