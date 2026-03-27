#!/usr/bin/env python3
"""
YouTube Highlight Extractor
============================
Extrait les N meilleurs moments d'une vidéo YouTube pour faire des clips.

- Vidéo < 30 min  → Top 5  moments  (format Shorts, ≤60s chacun)
- Vidéo ≥ 30 min  → Top 20 moments  (clips longs, lives, etc.)

Deux modes de transcription :
  --mode local      : Whisper installé localement (gratuit, illimité)
  --mode assemblyai : API AssemblyAI (gratuit 5h/mois, plus rapide)

Usage :
    python highlight_extractor.py --url "https://youtube.com/watch?v=XXXX"
    python highlight_extractor.py --url "..." --mode assemblyai --api-key TON_KEY
    python highlight_extractor.py --url "..." --mode local
    python highlight_extractor.py --url "..." --chapters-only
"""

import argparse
import json
import os
import re
import sys
import time
import math
import subprocess
import tempfile
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

SHORT_VIDEO_MAX_SECONDS = 30 * 60   # 30 minutes
SHORT_CLIPS_COUNT  = 5              # Top N pour vidéos courtes
LONG_CLIPS_COUNT   = 20             # Top N pour vidéos longues / lives

SHORT_CLIP_DURATION = 58            # Durée cible d'un Short (secondes)
LONG_CLIP_DURATION  = 120           # Durée cible d'un clip long (secondes)

ASSEMBLYAI_UPLOAD_URL     = "https://api.assemblyai.com/v2/upload"
ASSEMBLYAI_TRANSCRIPT_URL = "https://api.assemblyai.com/v2/transcript"

# Mots qui signalent souvent un moment fort dans une transcription
HIGHLIGHT_KEYWORDS = [
    "incroyable", "fou", "choquant", "wow", "waow", "omg", "incroyable",
    "j'y crois pas", "c'est dingue", "regardez", "attention", "important",
    "secret", "révélation", "annonce", "exclusif", "jamais vu", "record",
    "amazing", "crazy", "insane", "breaking", "exclusive", "never seen",
    "oh my god", "look at this", "unbelievable", "shocked", "viral",
    "clip ça", "moment", "historique", "énorme", "trop fort",
]


# ─────────────────────────────────────────────
# UTILITAIRES
# ─────────────────────────────────────────────

def seconds_to_hms(s: float) -> str:
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def hms_to_seconds(t: str) -> float:
    parts = t.strip().split(":")
    parts = [float(p) for p in parts]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0]


def extract_video_id(url: str) -> str:
    patterns = [
        r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})",
        r"^([A-Za-z0-9_-]{11})$",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return url


def print_header(title: str):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


def print_step(icon: str, msg: str):
    print(f"{icon}  {msg}")


# ─────────────────────────────────────────────
# ÉTAPE 1 : INFOS + CHAPITRES VIA yt-dlp
# ─────────────────────────────────────────────

def get_video_info(url: str) -> dict:
    """Récupère les métadonnées et chapitres sans télécharger."""
    print_step("📡", "Récupération des métadonnées...")
    try:
        import yt_dlp
    except ImportError:
        print("❌ yt-dlp non installé. Lance : pip install yt-dlp")
        sys.exit(1)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    return info


def parse_chapters(info: dict) -> list[dict]:
    """Extrait les chapitres YouTube si disponibles."""
    chapters = info.get("chapters") or []
    result = []
    for ch in chapters:
        result.append({
            "start": ch.get("start_time", 0),
            "end":   ch.get("end_time", 0),
            "title": ch.get("title", ""),
        })
    return result


def parse_description_timestamps(description: str) -> list[dict]:
    """Cherche des timestamps manuels dans la description (ex: 1:23 Moment fort)."""
    pattern = r'(\d{1,2}:\d{2}(?::\d{2})?)\s+(.+)'
    matches = re.findall(pattern, description or "")
    result = []
    for ts, title in matches:
        result.append({
            "start": hms_to_seconds(ts),
            "end":   None,
            "title": title.strip()[:80],
        })
    # Recalcule les fins à partir des débuts suivants
    for i in range(len(result) - 1):
        result[i]["end"] = result[i + 1]["start"]
    return result


# ─────────────────────────────────────────────
# ÉTAPE 2A : TÉLÉCHARGEMENT AUDIO (pour transcription)
# ─────────────────────────────────────────────

def download_audio(url: str, output_dir: str) -> str:
    """Télécharge uniquement l'audio en MP3."""
    print_step("⬇️ ", "Téléchargement de l'audio (cela peut prendre quelques minutes)...")
    try:
        import yt_dlp
    except ImportError:
        sys.exit(1)

    output_path = os.path.join(output_dir, "audio.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_path,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "64",
        }],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    mp3_path = os.path.join(output_dir, "audio.mp3")
    if not os.path.exists(mp3_path):
        # Cherche tout fichier audio dans le dossier
        for f in os.listdir(output_dir):
            if f.startswith("audio."):
                return os.path.join(output_dir, f)
        print("❌ Fichier audio introuvable après téléchargement.")
        sys.exit(1)

    size_mb = os.path.getsize(mp3_path) / 1_048_576
    print_step("✅", f"Audio téléchargé : {size_mb:.1f} MB")
    return mp3_path


# ─────────────────────────────────────────────
# ÉTAPE 2B : TRANSCRIPTION — AssemblyAI
# ─────────────────────────────────────────────

def transcribe_assemblyai(audio_path: str, api_key: str) -> list[dict]:
    """
    Transcrit avec AssemblyAI et retourne les mots avec timestamps.
    Clé API gratuite : https://www.assemblyai.com (5h/mois offerts)
    """
    print_step("🔑", "Upload vers AssemblyAI...")

    headers = {"authorization": api_key, "content-type": "application/octet-stream"}

    # Upload du fichier audio
    with open(audio_path, "rb") as f:
        audio_data = f.read()

    req = Request(ASSEMBLYAI_UPLOAD_URL, data=audio_data,
                  headers=headers, method="POST")
    with urlopen(req, timeout=120) as r:
        upload_resp = json.loads(r.read())

    upload_url = upload_resp.get("upload_url")
    if not upload_url:
        print("❌ Échec upload AssemblyAI:", upload_resp)
        sys.exit(1)

    print_step("🎙️ ", "Transcription en cours (AssemblyAI)...")

    # Demande de transcription avec word-level timestamps
    transcript_req = {
        "audio_url": upload_url,
        "language_detection": True,
        "punctuate": True,
        "format_text": True,
        "word_boost": HIGHLIGHT_KEYWORDS[:20],
    }
    req = Request(
        ASSEMBLYAI_TRANSCRIPT_URL,
        data=json.dumps(transcript_req).encode(),
        headers={**headers, "content-type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=30) as r:
        transcript_resp = json.loads(r.read())

    transcript_id = transcript_resp.get("id")
    poll_url = f"{ASSEMBLYAI_TRANSCRIPT_URL}/{transcript_id}"

    # Polling
    poll_headers = {"authorization": api_key}
    for attempt in range(120):
        time.sleep(5)
        req = Request(poll_url, headers=poll_headers)
        with urlopen(req, timeout=30) as r:
            result = json.loads(r.read())

        status = result.get("status")
        if status == "completed":
            break
        if status == "error":
            print(f"❌ Erreur AssemblyAI : {result.get('error')}")
            sys.exit(1)
        if attempt % 6 == 0:
            print_step("⏳", f"Transcription en attente... ({attempt * 5}s)")

    words = result.get("words", [])
    print_step("✅", f"Transcription terminée : {len(words)} mots")
    return words


# ─────────────────────────────────────────────
# ÉTAPE 2C : TRANSCRIPTION — Whisper local
# ─────────────────────────────────────────────

def transcribe_whisper(audio_path: str) -> list[dict]:
    """
    Transcrit avec OpenAI Whisper en local.
    Installation : pip install openai-whisper
    """
    print_step("🤖", "Transcription avec Whisper (local)...")
    try:
        import whisper
    except ImportError:
        print("❌ Whisper non installé.")
        print("   Lance : pip install openai-whisper")
        print("   (Nécessite ~2GB d'espace et quelques minutes la première fois)")
        sys.exit(1)

    model = whisper.load_model("base")
    result = model.transcribe(audio_path, word_timestamps=True, verbose=False)

    words = []
    for segment in result.get("segments", []):
        for w in segment.get("words", []):
            words.append({
                "text":  w.get("word", "").strip(),
                "start": int(w.get("start", 0) * 1000),
                "end":   int(w.get("end",   0) * 1000),
            })

    print_step("✅", f"Transcription terminée : {len(words)} mots")
    return words


# ─────────────────────────────────────────────
# ÉTAPE 3 : SCORING DES MOMENTS FORTS
# ─────────────────────────────────────────────

def build_segments_from_words(words: list[dict], window_sec: float = 60.0) -> list[dict]:
    """
    Découpe la transcription en fenêtres glissantes de window_sec secondes.
    Retourne des segments avec leur texte et leurs timestamps en ms.
    """
    if not words:
        return []

    total_ms = words[-1].get("end", 0) or words[-1].get("start", 0)
    window_ms = int(window_sec * 1000)
    step_ms   = window_ms // 2   # 50% overlap pour ne rater aucun moment

    segments = []
    t = 0
    while t < total_ms:
        seg_words = [
            w for w in words
            if w.get("start", 0) >= t and w.get("start", 0) < t + window_ms
        ]
        if seg_words:
            text = " ".join(w.get("text", "") for w in seg_words)
            segments.append({
                "start_ms": t,
                "end_ms":   min(t + window_ms, total_ms),
                "text":     text,
                "words":    seg_words,
            })
        t += step_ms

    return segments


def score_segment(seg: dict, duration_sec: float) -> float:
    """
    Score un segment sur plusieurs critères :
    1. Densité de mots-clés "highlight"
    2. Rythme de parole (mots/seconde) — les pics = excitation
    3. Ponctuation forte (! ?)
    4. Longueur du texte (segments trop courts = peu informatifs)
    """
    text_lower = seg["text"].lower()
    words_list = seg["words"]
    dur_sec = (seg["end_ms"] - seg["start_ms"]) / 1000

    # 1. Mots-clés highlight
    kw_score = sum(1 for kw in HIGHLIGHT_KEYWORDS if kw in text_lower)
    kw_score *= 3.0

    # 2. Rythme de parole (mots par seconde)
    wps = len(words_list) / max(dur_sec, 1)
    # Normalise par rapport à ~2.5 mots/sec (vitesse normale)
    rhythm_score = min(wps / 2.5, 3.0)

    # 3. Ponctuation forte
    punct_score = (text_lower.count("!") + text_lower.count("?")) * 1.5

    # 4. Longueur minimale (évite les silences)
    length_score = min(len(words_list) / 20, 1.0)

    # 5. Boost si présence de chiffres / stats (souvent = annonce importante)
    has_numbers = bool(re.search(r'\b\d+\b', seg["text"]))
    number_score = 1.0 if has_numbers else 0

    total = kw_score + rhythm_score + punct_score + length_score + number_score
    return round(total, 3)


def score_chapters(chapters: list[dict], duration_sec: float) -> list[dict]:
    """Score des chapitres basé sur leur titre (mots-clés) et position."""
    scored = []
    for i, ch in enumerate(chapters):
        title_lower = ch["title"].lower()
        kw = sum(1 for kw in HIGHLIGHT_KEYWORDS if kw in title_lower)

        # Boost les chapitres du milieu (souvent le coeur du contenu)
        position = (ch["start"] / max(duration_sec, 1))
        position_boost = 1.0 if 0.15 < position < 0.85 else 0.3

        score = kw * 2.5 + position_boost
        scored.append({**ch, "score": score, "source": "chapter"})

    return scored


def remove_overlapping(moments: list[dict], min_gap_sec: float = 30) -> list[dict]:
    """Supprime les moments trop proches (évite les doublons)."""
    if not moments:
        return []

    kept = [moments[0]]
    for m in moments[1:]:
        last = kept[-1]
        gap = m["start"] - (last["start"] + last.get("duration", 60))
        if gap >= min_gap_sec:
            kept.append(m)

    return kept


def extract_highlights(
    words: list[dict],
    chapters: list[dict],
    duration_sec: float,
    top_n: int,
    clip_duration: int,
) -> list[dict]:
    """Pipeline complet de scoring et sélection des moments forts."""

    all_moments = []

    # Source A : chapitres YouTube (si disponibles)
    if chapters:
        print_step("📑", f"{len(chapters)} chapitres YouTube trouvés — scoring...")
        scored_ch = score_chapters(chapters, duration_sec)
        for ch in scored_ch:
            all_moments.append({
                "start":    ch["start"],
                "duration": min(clip_duration, (ch["end"] or ch["start"] + clip_duration) - ch["start"]),
                "title":    ch["title"],
                "score":    ch["score"],
                "source":   "chapter",
            })

    # Source B : transcription (si disponible)
    if words:
        print_step("📝", "Analyse de la transcription par fenêtres glissantes...")
        window = min(clip_duration, 60)
        segments = build_segments_from_words(words, window_sec=window)
        print_step("🧮", f"{len(segments)} segments analysés...")

        for seg in segments:
            score = score_segment(seg, duration_sec)
            if score > 0.5:
                all_moments.append({
                    "start":    seg["start_ms"] / 1000,
                    "duration": clip_duration,
                    "title":    seg["text"][:80] + ("..." if len(seg["text"]) > 80 else ""),
                    "score":    score,
                    "source":   "transcript",
                })

    if not all_moments:
        print_step("⚠️ ", "Aucun signal trouvé — génération de moments uniformes de secours")
        step = duration_sec / (top_n + 1)
        for i in range(1, top_n + 1):
            all_moments.append({
                "start":    step * i,
                "duration": clip_duration,
                "title":    f"Segment {i}",
                "score":    0,
                "source":   "fallback",
            })

    # Tri par score
    all_moments.sort(key=lambda x: x["score"], reverse=True)

    # Déduplication des overlaps
    unique = remove_overlapping(all_moments, min_gap_sec=clip_duration * 0.7)

    # Sélection du top N
    top = unique[:top_n]

    # Retri par ordre chronologique pour l'affichage
    top.sort(key=lambda x: x["start"])

    return top


# ─────────────────────────────────────────────
# AFFICHAGE DES RÉSULTATS
# ─────────────────────────────────────────────

def display_results(moments: list[dict], video_title: str, duration_sec: float, video_url: str):
    print_header(f"MOMENTS FORTS — {video_title[:50]}")
    print(f"  Durée totale : {seconds_to_hms(duration_sec)}  |  {len(moments)} clips identifiés\n")

    for i, m in enumerate(moments, 1):
        start = m["start"]
        end   = start + m["duration"]
        end   = min(end, duration_sec)
        ts    = seconds_to_hms(start)
        src_badge = {"chapter": "📑 chapitre", "transcript": "🎙️  transcription", "fallback": "📐 uniforme"}.get(m["source"], "")

        print(f"  #{i:02d}  ⏱  {ts}  →  {seconds_to_hms(end)}   ({int(m['duration'])}s)   score: {m['score']:.2f}  [{src_badge}]")
        print(f"       {m['title'][:75]}")

        # Lien deep-link YouTube avec timestamp
        vid_id = extract_video_id(video_url)
        t_sec = int(start)
        yt_link = f"https://youtu.be/{vid_id}?t={t_sec}"
        print(f"       {yt_link}")
        print()

    print("─"*60)
    print("  💡 Conseil clips :")
    if duration_sec < SHORT_VIDEO_MAX_SECONDS:
        print("     → Vidéo courte : fais des Shorts (≤60s, ratio 9:16)")
        print("     → Garde le hook dans les 3 premières secondes")
    else:
        print("     → Vidéo longue/live : fais des clips de 60-120s")
        print("     → Ajoute des sous-titres auto pour +40% de rétention")
    print()


def export_results(moments: list[dict], video_url: str, duration_sec: float, output_file: str):
    """Exporte les moments en JSON et en liste de commandes ffmpeg."""
    vid_id = extract_video_id(video_url)

    data = {
        "video_url":    video_url,
        "video_id":     vid_id,
        "duration_sec": duration_sec,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "moments":      moments,
    }

    json_file = output_file + ".json"
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"📄 Export JSON → {json_file}")

    # Script ffmpeg pour couper les clips (si la vidéo est déjà téléchargée)
    ffmpeg_file = output_file + "_ffmpeg.sh"
    with open(ffmpeg_file, "w", encoding="utf-8") as f:
        f.write("#!/bin/bash\n")
        f.write(f"# Commandes ffmpeg pour couper les clips de : {video_url}\n")
        f.write("# Remplace VIDEO_FILE par le chemin de ta vidéo téléchargée\n\n")
        f.write("VIDEO_FILE=\"$1\"\n\n")
        for i, m in enumerate(moments, 1):
            start = int(m["start"])
            dur   = int(m["duration"])
            f.write(f'ffmpeg -i "$VIDEO_FILE" -ss {start} -t {dur} '
                    f'-c copy "clip_{i:02d}_{seconds_to_hms(start).replace(":", "-")}.mp4"\n')
    os.chmod(ffmpeg_file, 0o755)
    print(f"📄 Script ffmpeg → {ffmpeg_file}")


# ─────────────────────────────────────────────
# PIPELINE PRINCIPAL
# ─────────────────────────────────────────────

def run(args):
    print_header("YouTube Highlight Extractor")

    with tempfile.TemporaryDirectory() as tmpdir:

        # 1. Métadonnées
        info = get_video_info(args.url)
        title    = info.get("title", "")
        duration = float(info.get("duration") or 0)
        desc     = info.get("description", "")

        print_step("🎬", f"Titre    : {title}")
        print_step("⏱ ", f"Durée    : {seconds_to_hms(duration)} ({duration:.0f}s)")

        is_short_video = duration < SHORT_VIDEO_MAX_SECONDS
        top_n          = SHORT_CLIPS_COUNT if is_short_video else LONG_CLIPS_COUNT
        clip_dur       = SHORT_CLIP_DURATION if is_short_video else LONG_CLIP_DURATION

        print_step("📊", f"Mode     : {'< 30 min → Top ' + str(top_n) + ' Shorts' if is_short_video else '≥ 30 min → Top ' + str(top_n) + ' clips'}")

        # 2. Chapitres et timestamps description
        chapters = parse_chapters(info)
        desc_ts  = parse_description_timestamps(desc)

        if chapters:
            print_step("📑", f"{len(chapters)} chapitres YouTube détectés")
        elif desc_ts:
            chapters = desc_ts
            print_step("📝", f"{len(chapters)} timestamps trouvés dans la description")
        else:
            print_step("ℹ️ ", "Aucun chapitre ni timestamp — analyse audio requise")

        # 3. Mode chapters-only (pas de téléchargement)
        words = []
        if args.chapters_only:
            print_step("⚡", "Mode rapide : chapitres uniquement (pas de téléchargement)")
            if not chapters:
                print("⚠️  Aucun chapitre disponible. Lance sans --chapters-only pour analyser l'audio.")

        elif args.mode == "assemblyai":
            if not args.api_key:
                print("❌ --api-key requis pour le mode assemblyai")
                print("   Clé gratuite sur : https://www.assemblyai.com")
                sys.exit(1)
            audio_path = download_audio(args.url, tmpdir)
            words = transcribe_assemblyai(audio_path, args.api_key)

        elif args.mode == "local":
            audio_path = download_audio(args.url, tmpdir)
            words = transcribe_whisper(audio_path)

        # 4. Scoring et extraction des moments forts
        print_step("🧮", "Calcul des scores des moments forts...")
        moments = extract_highlights(
            words=words,
            chapters=chapters,
            duration_sec=duration,
            top_n=top_n,
            clip_duration=clip_dur,
        )

        # 5. Affichage
        display_results(moments, title, duration, args.url)

        # 6. Export
        if args.export:
            out_base = args.export
        else:
            safe_title = re.sub(r'[^\w\-]', '_', title[:30])
            out_base = f"highlights_{safe_title}"

        export_results(moments, args.url, duration, out_base)


# ─────────────────────────────────────────────
# POINT D'ENTRÉE
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Extrait les meilleurs moments d'une vidéo YouTube pour des clips",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--url", required=True,
                        help="Lien YouTube (vidéo, short, live replay)")
    parser.add_argument("--mode", choices=["assemblyai", "local", "none"], default="none",
                        help=(
                            "Mode de transcription :\n"
                            "  none        → chapitres + description uniquement (rapide, sans téléchargement)\n"
                            "  assemblyai  → API AssemblyAI (gratuit 5h/mois, précis)\n"
                            "  local       → Whisper local (gratuit illimité, nécessite pip install openai-whisper)\n"
                        ))
    parser.add_argument("--api-key",
                        help="Clé API AssemblyAI (https://www.assemblyai.com)")
    parser.add_argument("--chapters-only", action="store_true",
                        help="Utilise uniquement les chapitres YT, sans télécharger l'audio")
    parser.add_argument("--export", default=None,
                        help="Nom de base pour les fichiers exportés (sans extension)")

    args = parser.parse_args()

    # Raccourci : --chapters-only si mode none
    if args.mode == "none":
        args.chapters_only = True

    run(args)
