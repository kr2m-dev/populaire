#!/usr/bin/env python3
"""

commande de telechargerment via srv-aws 
yt-dlp --proxy http://veoetdka:4et82gbw1zoc@23.95.150.145:6114 \
       --cookies cookies.txt \
       --js-runtimes node \
       --remote-components ejs:github \
       -f "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]" \
       -o "video.mp4" \
       "https://www.youtube.com/live/Tkhlh73I-BE"


highlight_extractor_v2.py
==========================
Pipeline complet d'extraction des moments forts YouTube.
Fusionne 3 sources de signal :

  Signal 1 — Audio/Transcription (AssemblyAI ou Whisper)
             Densité textuelle, mots-clés, rythme de parole

  Signal 2 — Commentaires YouTube (API v3 gratuit)
             Heatmap des timestamps mentionnés + réactions emoji

  Signal 3 — Vision IA (Groq Vision, gratuit)
             Détection de streamers, items rares, moments viraux

Règles de clipping :
  Vidéo < 30 min → Top 5  moments (Shorts ≤60s)
  Vidéo ≥ 30 min → Top 20 moments (clips 60-120s)

Usage :
  # Rapide, zéro téléchargement (commentaires uniquement)
  python highlight_extractor_v2.py --url URL --yt-key CLE_YT

  # Commentaires + Vision IA (recommandé pour gaming/lives)
  python highlight_extractor_v2.py --url URL --yt-key CLE_YT --groq-key CLE_GROQ

  # Pipeline complet avec transcription audio
  python highlight_extractor_v2.py --url URL --yt-key CLE_YT --groq-key CLE_GROQ \\
      --transcribe assemblyai --assembly-key CLE_ASSEMBLY

  # Tout Groq + Whisper local (100% gratuit illimité)
  python highlight_extractor_v2.py --url URL --yt-key CLE_YT --groq-key CLE_GROQ \\
      --transcribe local
"""

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from urllib.request import urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

SHORT_VIDEO_MAX_SEC  = 30 * 60
SHORT_CLIPS_COUNT    = 5
LONG_CLIPS_COUNT     = 20
SHORT_CLIP_DURATION  = 58
LONG_CLIP_DURATION   = 120

ASSEMBLYAI_UPLOAD_URL     = "https://api.assemblyai.com/v2/upload"
ASSEMBLYAI_TRANSCRIPT_URL = "https://api.assemblyai.com/v2/transcript"
YT_BASE                   = "https://www.googleapis.com/youtube/v3"


# ─────────────────────────────────────────────
# UTILITAIRES
# ─────────────────────────────────────────────

def sec_to_ts(s: float) -> str:
    s = int(s)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def extract_video_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else url


def api_get_yt(endpoint: str, params: dict) -> dict:
    url = f"{YT_BASE}/{endpoint}?{urlencode(params)}"
    try:
        with urlopen(url, timeout=20) as r:
            return json.loads(r.read().decode())
    except HTTPError as e:
        msg = json.loads(e.read().decode()).get("error", {}).get("message", "")
        print(f"  ❌ API YouTube ({e.code}): {msg}")
        return {}


def print_step(icon, msg):
    print(f"{icon}  {msg}")


def print_section(title):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# ─────────────────────────────────────────────
# SIGNAL 1 — MÉTADONNÉES ET CHAPITRES
# ─────────────────────────────────────────────

def get_video_info(url: str) -> dict:
    print_step("📡", "Métadonnées YouTube...")
    try:
        import yt_dlp
    except ImportError:
        print("  ❌ pip install yt-dlp"); sys.exit(1)

    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "no_warnings": True}) as ydl:
        return ydl.extract_info(url, download=False)


def get_chapter_moments(info: dict, clip_duration: float) -> list[dict]:
    """Convertit les chapitres YT en moments scorés."""
    chapters = info.get("chapters") or []
    desc     = info.get("description", "")
    duration = float(info.get("duration") or 0)

    if not chapters:
        # Tente les timestamps dans la description
        for ts_str, label in re.findall(r'(\d{1,2}:\d{2}(?::\d{2})?)\s+(.+)', desc):
            parts = [float(p) for p in ts_str.split(":")]
            sec = parts[0]*3600 + parts[1]*60 + (parts[2] if len(parts)==3 else 0)
            chapters.append({"start_time": sec, "end_time": None, "title": label.strip()[:80]})

    moments = []
    for ch in chapters:
        start = ch.get("start_time", 0)
        end   = ch.get("end_time") or (start + clip_duration)
        title = ch.get("title", "")
        # Score basique : titre contient des mots-clés ?
        kw_score = sum(1 for kw in ["highlight","best","moment","clip","insane","crazy","fou","incroyable","record"]
                       if kw in title.lower())
        moments.append({
            "start": start, "duration": min(clip_duration, end - start),
            "score": 1.0 + kw_score * 0.5, "source": "chapter", "title": title,
        })
    return moments


# ─────────────────────────────────────────────
# SIGNAL 2 — COMMENTAIRES
# ─────────────────────────────────────────────

REACTION_RE = re.compile(
    r'🔥|😱|😂|💀|🤣|😭|🤯|👀|🏆|⚡|💥|🎯|👑|🐐'
    r'|\bclip\b|\bOMG\b|\bWTF\b|\bPog\b|\binsane\b|\bcrazy\b'
    r'|\bgoat\b|\bincroyable\b|\bdingue\b|\bfou\b|\bW\b|\bGG\b'
    r'|no way|j\'y crois pas|trop fort',
    re.IGNORECASE,
)
TS_IN_COMMENT_RE = re.compile(
    r'(?:^|[\s\(\[\|])(\d{1,2}:\d{2}(?::\d{2})?)(?:$|[\s\)\]\|,\.])',
    re.MULTILINE,
)

def _ts_to_sec(ts: str) -> float:
    p = [float(x) for x in ts.strip().split(":")]
    return p[0]*3600 + p[1]*60 + (p[2] if len(p)==3 else 0)


def get_comment_moments(video_id: str, yt_key: str, duration_sec: float,
                        clip_duration: float, top_n: int) -> list[dict]:
    """Heatmap des timestamps mentionnés dans les commentaires."""
    print_step("💬", "Analyse des commentaires...")
    comments = []
    page_token = None

    for _ in range(10):
        params = {"part": "snippet", "videoId": video_id,
                  "maxResults": 100, "order": "relevance", "key": yt_key}
        if page_token:
            params["pageToken"] = page_token
        data = api_get_yt("commentThreads", params)
        if not data:
            break
        for item in data.get("items", []):
            s = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            comments.append({"text": s.get("textOriginal",""), "likes": s.get("likeCount",0)})
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    print_step("", f"  → {len(comments)} commentaires récupérés")

    heat = defaultdict(float)
    hits = defaultdict(int)
    bucket = 30   # fenêtre de 30 secondes

    for c in comments:
        text  = c["text"]
        likes = max(c["likes"], 1)
        react = len(REACTION_RE.findall(text))
        for ts in TS_IN_COMMENT_RE.findall(text):
            sec = _ts_to_sec(ts)
            if 0 <= sec <= duration_sec:
                b = int(sec // bucket) * bucket
                heat[b] += likes * (1 + react * 0.5)
                hits[b] += 1

    if not heat:
        print_step("", "  → Aucun timestamp dans les commentaires")
        return []

    max_h = max(heat.values())
    moments = []
    for b, h in heat.items():
        moments.append({
            "start": b, "duration": clip_duration,
            "score": round((h / max_h) * 4, 3),
            "source": "comments",
            "title": f"Réactions public ({hits[b]} mentions)",
            "_mentions": hits[b],
        })

    moments.sort(key=lambda x: x["score"], reverse=True)
    kept = []
    for m in moments:
        if not any(abs(m["start"] - k["start"]) < clip_duration * 0.6 for k in kept):
            kept.append(m)
        if len(kept) >= top_n:
            break

    print_step("", f"  → {len(kept)} zones chaudes identifiées")
    return sorted(kept, key=lambda x: x["start"])


# ─────────────────────────────────────────────
# SIGNAL 3 — VISION IA (GROQ)
# ─────────────────────────────────────────────

def get_vision_moments(url: str, groq_key: str, duration_sec: float,
                       clip_duration: float, top_n: int,
                       interval_sec: int = 120) -> list[dict]:
    """Délègue au module vision_analyzer si disponible."""
    try:
        from vision_analyzer import analyze_video_frames
        print_step("🤖", f"Vision IA — capture toutes les {interval_sec}s...")
        return analyze_video_frames(
            url=url, groq_key=groq_key, duration_sec=duration_sec,
            top_n=top_n, clip_duration=clip_duration, interval_sec=interval_sec,
        )
    except ImportError:
        print_step("⚠️ ", "vision_analyzer.py introuvable — module vision ignoré")
        return []


# ─────────────────────────────────────────────
# SIGNAL 4 — TRANSCRIPTION AUDIO (optionnel)
# ─────────────────────────────────────────────

HIGHLIGHT_KW = [
    "incroyable","fou","choquant","wow","incroyable","j'y crois pas","c'est dingue",
    "regardez","attention","important","secret","révélation","annonce","exclusif","record",
    "amazing","crazy","insane","breaking","exclusive","never seen","oh my god",
    "unbelievable","shocked","viral","clip ça","moment","historique","énorme",
]

def _build_transcript_moments(words: list[dict], duration_sec: float,
                               clip_duration: float) -> list[dict]:
    if not words:
        return []
    total_ms  = words[-1].get("end", 0) or words[-1].get("start", 0)
    window_ms = int(clip_duration * 1000)
    step_ms   = window_ms // 2
    moments   = []
    t = 0
    while t < total_ms:
        seg_words = [w for w in words if w.get("start",0) >= t and w.get("start",0) < t + window_ms]
        if seg_words:
            text  = " ".join(w.get("text","") for w in seg_words)
            tl    = text.lower()
            kw    = sum(1 for k in HIGHLIGHT_KW if k in tl)
            wps   = len(seg_words) / max(clip_duration, 1)
            punct = tl.count("!") + tl.count("?")
            score = kw*3 + min(wps/2.5, 3) + punct*1.5 + min(len(seg_words)/20, 1)
            if score > 0.8:
                moments.append({
                    "start": t/1000, "duration": clip_duration,
                    "score": round(score, 3), "source": "transcript",
                    "title": text[:80] + ("..." if len(text) > 80 else ""),
                })
        t += step_ms

    moments.sort(key=lambda x: x["score"], reverse=True)
    kept = []
    for m in moments:
        if not any(abs(m["start"] - k["start"]) < clip_duration * 0.6 for k in kept):
            kept.append(m)
    return sorted(kept[:20], key=lambda x: x["start"])


def get_transcript_moments(url: str, mode: str, assembly_key: str,
                           duration_sec: float, clip_duration: float) -> list[dict]:
    import tempfile
    words = []
    with tempfile.TemporaryDirectory() as tmpdir:
        # Télécharge l'audio
        try:
            import yt_dlp
        except ImportError:
            return []
        print_step("⬇️ ", "Téléchargement audio pour transcription...")
        with yt_dlp.YoutubeDL({
            "format": "bestaudio/best", "quiet": True, "no_warnings": True,
            "outtmpl": os.path.join(tmpdir, "audio.%(ext)s"),
            "postprocessors": [{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"64"}],
        }) as ydl:
            ydl.download([url])

        audio_path = None
        for f in os.listdir(tmpdir):
            if f.startswith("audio."):
                audio_path = os.path.join(tmpdir, f); break
        if not audio_path:
            return []

        if mode == "assemblyai":
            words = _transcribe_assemblyai(audio_path, assembly_key)
        elif mode == "local":
            words = _transcribe_whisper(audio_path)

    return _build_transcript_moments(words, duration_sec, clip_duration)


def _transcribe_assemblyai(audio_path: str, api_key: str) -> list[dict]:
    from urllib.request import Request
    print_step("🎙️ ", "Transcription AssemblyAI...")
    headers = {"authorization": api_key, "content-type": "application/octet-stream"}
    with open(audio_path, "rb") as f:
        req = Request(ASSEMBLYAI_UPLOAD_URL, data=f.read(), headers=headers, method="POST")
    with urlopen(req, timeout=120) as r:
        upload_url = json.loads(r.read()).get("upload_url")
    if not upload_url:
        return []
    headers2 = {**headers, "content-type": "application/json"}
    req = Request(ASSEMBLYAI_TRANSCRIPT_URL,
                  data=json.dumps({"audio_url": upload_url, "language_detection": True}).encode(),
                  headers=headers2, method="POST")
    with urlopen(req, timeout=30) as r:
        tid = json.loads(r.read()).get("id")
    for _ in range(120):
        time.sleep(5)
        with urlopen(Request(f"{ASSEMBLYAI_TRANSCRIPT_URL}/{tid}",
                              headers={"authorization": api_key}), timeout=30) as r:
            res = json.loads(r.read())
        if res.get("status") == "completed":
            return res.get("words", [])
        if res.get("status") == "error":
            return []
    return []


def _transcribe_whisper(audio_path: str) -> list[dict]:
    print_step("🤖", "Transcription Whisper (local)...")
    try:
        import whisper
    except ImportError:
        print("  ❌ pip install openai-whisper"); return []
    model = whisper.load_model("base")
    result = model.transcribe(audio_path, word_timestamps=True, verbose=False)
    words = []
    for seg in result.get("segments", []):
        for w in seg.get("words", []):
            words.append({"text": w.get("word","").strip(),
                           "start": int(w.get("start",0)*1000),
                           "end":   int(w.get("end",0)*1000)})
    return words


# ─────────────────────────────────────────────
# FUSION DES SIGNAUX
# ─────────────────────────────────────────────

def fuse_moments(all_sources: list[list[dict]], duration_sec: float,
                 clip_duration: float, top_n: int) -> list[dict]:
    """
    Fusionne plusieurs listes de moments en combinant les scores.
    Si deux moments de sources différentes tombent dans la même fenêtre,
    leur score s'additionne → les vrais moments forts ressortent.
    """
    # Grille temporelle : buckets de clip_duration/2 secondes
    bucket = clip_duration / 2
    grid = defaultdict(lambda: {"score": 0, "sources": [], "titles": [], "start": 0})

    for source_moments in all_sources:
        for m in source_moments:
            b = round(int(m["start"] // bucket) * bucket, 1)
            grid[b]["score"]   += m["score"]
            grid[b]["start"]    = b
            grid[b]["sources"].append(m["source"])
            grid[b]["titles"].append(m.get("title", ""))

    # Bonus multi-signal : si ≥2 sources concordent, +50%
    fused = []
    for b, data in grid.items():
        unique_sources = set(data["sources"])
        multi_bonus = 1.5 if len(unique_sources) >= 2 else 1.0
        score = round(data["score"] * multi_bonus, 3)
        best_title = max(data["titles"], key=len) if data["titles"] else ""
        sources_str = "+".join(sorted(unique_sources))
        fused.append({
            "start":    data["start"],
            "duration": clip_duration,
            "score":    score,
            "source":   sources_str,
            "title":    best_title[:100],
        })

    fused.sort(key=lambda x: x["score"], reverse=True)

    # Déduplication + sélection top N
    kept = []
    for m in fused:
        if not any(abs(m["start"] - k["start"]) < clip_duration * 0.7 for k in kept):
            kept.append(m)
        if len(kept) >= top_n:
            break

    return sorted(kept, key=lambda x: x["start"])


# ─────────────────────────────────────────────
# AFFICHAGE ET EXPORT
# ─────────────────────────────────────────────

def display_results(moments: list[dict], title: str, duration: float,
                    video_id: str, is_short: bool):
    print_section(f"MOMENTS FORTS — {title[:45]}")
    mode_label = f"Top {len(moments)} {'Shorts ≤60s' if is_short else 'clips 60-120s'}"
    print(f"  {mode_label}  |  Durée totale : {sec_to_ts(duration)}\n")

    source_icons = {
        "chapter": "📑", "comments": "💬", "transcript": "🎙️",
        "vision": "👁️", "fallback": "📐",
    }

    for i, m in enumerate(moments, 1):
        end  = min(m["start"] + m["duration"], duration)
        srcs = m.get("source","")
        icons = " ".join(source_icons.get(s, "•") for s in srcs.split("+"))
        stars = "🔥" * min(int(m["score"] / 2), 5)

        print(f"  #{i:02d}  {sec_to_ts(m['start'])} → {sec_to_ts(end)}  "
              f"({int(m['duration'])}s)  {stars}")
        print(f"       {icons}  {m.get('title','')[:70]}")
        print(f"       https://youtu.be/{video_id}?t={int(m['start'])}")
        print()

    # Récap des sources utilisées
    sources_used = set()
    for m in moments:
        sources_used.update(m.get("source","").split("+"))
    print(f"  Signaux utilisés : {', '.join(sorted(sources_used))}")
    if is_short:
        print("  Conseil : format Short 9:16, hook en <3s, sous-titres auto")
    else:
        print("  Conseil : clips 60-120s, sous-titres, thumbnail choc")
    print()


def export_results(moments: list[dict], video_url: str, duration: float, base: str):
    vid_id = extract_video_id(video_url)
    data = {
        "video_url": video_url, "video_id": vid_id,
        "duration_sec": duration,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "moments": moments,
    }
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  📄 JSON  → {base}.json")

    with open(base + "_ffmpeg.sh", "w", encoding="utf-8") as f:
        f.write("#!/bin/bash\n# Usage: bash script.sh chemin_video.mp4\nV=\"$1\"\n")
        for i, m in enumerate(moments, 1):
            f.write(f'ffmpeg -i "$V" -ss {int(m["start"])} -t {int(m["duration"])} '
                    f'-c copy "clip_{i:02d}_{sec_to_ts(m["start"]).replace(":","m")}.mp4"\n')
    os.chmod(base + "_ffmpeg.sh", 0o755)
    print(f"  📄 ffmpeg → {base}_ffmpeg.sh")


# ─────────────────────────────────────────────
# PIPELINE PRINCIPAL
# ─────────────────────────────────────────────

def run(args):
    print_section("YouTube Highlight Extractor v2 — Multi-signal")

    # 1. Infos vidéo
    info = get_video_info(args.url)
    title    = info.get("title", "")
    duration = float(info.get("duration") or 0)
    vid_id   = extract_video_id(args.url)

    print_step("🎬", f"Titre  : {title[:55]}")
    print_step("⏱ ", f"Durée  : {sec_to_ts(duration)}")

    is_short     = duration < SHORT_VIDEO_MAX_SEC
    top_n        = SHORT_CLIPS_COUNT if is_short else LONG_CLIPS_COUNT
    clip_dur     = SHORT_CLIP_DURATION if is_short else LONG_CLIP_DURATION

    print_step("📊", f"Mode   : {'< 30min → Short' if is_short else '≥ 30min → Clip long'}"
               f"  |  objectif : {top_n} moments")

    all_sources = []

    # 2. Chapitres / timestamps description
    chapter_moments = get_chapter_moments(info, clip_dur)
    if chapter_moments:
        all_sources.append(chapter_moments)
        print_step("📑", f"{len(chapter_moments)} chapitres/timestamps trouvés")

    # 3. Commentaires
    if args.yt_key:
        comment_moments = get_comment_moments(
            vid_id, args.yt_key, duration, clip_dur, top_n)
        if comment_moments:
            all_sources.append(comment_moments)
    else:
        print_step("ℹ️ ", "Commentaires ignorés (--yt-key non fourni)")

    # 4. Vision IA
    if args.groq_key:
        vision_moments = get_vision_moments(
            args.url, args.groq_key, duration, clip_dur, top_n,
            interval_sec=args.vision_interval)
        if vision_moments:
            all_sources.append(vision_moments)
    else:
        print_step("ℹ️ ", "Vision IA ignorée (--groq-key non fourni)")

    # 5. Transcription audio
    if args.transcribe and args.transcribe != "none":
        if args.transcribe == "assemblyai" and not args.assembly_key:
            print_step("⚠️ ", "--assembly-key requis pour assemblyai")
        else:
            transcript_moments = get_transcript_moments(
                args.url, args.transcribe, args.assembly_key or "",
                duration, clip_dur)
            if transcript_moments:
                all_sources.append(transcript_moments)
    
    # 6. Fallback si aucune source
    if not all_sources:
        print_step("⚠️ ", "Aucune source de signal — génération uniforme")
        step = duration / (top_n + 1)
        fallback = [{"start": step*(i+1), "duration": clip_dur,
                      "score": 0, "source": "fallback",
                      "title": f"Segment {i+1}"} for i in range(top_n)]
        all_sources.append(fallback)

    # 7. Fusion
    print_step("🔀", "Fusion des signaux...")
    moments = fuse_moments(all_sources, duration, clip_dur, top_n)

    # 8. Affichage
    display_results(moments, title, duration, vid_id, is_short)

    # 9. Export
    safe = re.sub(r'[^\w\-]', '_', title[:25])
    base = args.export or f"highlights_{safe}"
    export_results(moments, args.url, duration, base)


# ─────────────────────────────────────────────
# POINT D'ENTRÉE
# ─────────────────────────────────────────────

if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Extraction multi-signal des moments forts YouTube (v2)",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("--url",       required=True, help="URL YouTube")
    p.add_argument("--yt-key",    default=None,  help="Clé YouTube Data API v3 (commentaires)")
    p.add_argument("--groq-key",  default=None,
                   help="Clé API Groq Vision (gratuit : console.groq.com)")
    p.add_argument("--transcribe", choices=["none","assemblyai","local"], default="none",
                   help="Mode transcription audio (défaut: none)")
    p.add_argument("--assembly-key", default=None, help="Clé AssemblyAI")
    p.add_argument("--vision-interval", type=int, default=120,
                   help="Intervalle entre captures vision en secondes (défaut: 120)")
    p.add_argument("--export", default=None, help="Nom de base des fichiers exportés")
    args = p.parse_args()
    run(args)
