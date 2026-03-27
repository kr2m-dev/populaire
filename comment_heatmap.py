#!/usr/bin/env python3
"""
comment_heatmap.py
==================
Module 1 — Analyse des commentaires YouTube pour détecter
les moments forts via les réactions du public.

Stratégie :
  1. Récupère tous les commentaires via YouTube Data API v3
  2. Extrait les timestamps mentionnés (ex: "1:23 🔥", "ce moment à 4:30")
  3. Mesure la densité de réactions emoji/mots forts par tranche de temps
  4. Génère une heatmap temporelle → liste de timestamps "chauds"

Usage standalone :
    python comment_heatmap.py --video-id VIDEO_ID --api-key CLE_YT

Import dans highlight_extractor :
    from comment_heatmap import get_comment_hotspots
"""

import json
import re
import sys
from collections import defaultdict
from urllib.request import urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError

YT_BASE = "https://www.googleapis.com/youtube/v3"

# Emojis et mots qui signalent une forte réaction
REACTION_PATTERNS = [
    r'🔥', r'😱', r'😂', r'💀', r'🤣', r'😭', r'🤯', r'👀',
    r'🏆', r'⚡', r'💥', r'🎯', r'👑', r'🐐',
    r'\bclip\b', r'\bclipe\b', r'\bOMG\b', r'\bWTF\b', r'\bPog\b',
    r'\binsane\b', r'\bcrazy\b', r'\bgoat\b', r'\bgodlike\b',
    r'\bincroyable\b', r'\bdingue\b', r'\bfou\b', r'\blégende\b',
    r'\bLFG\b', r'\bW\b', r'\bEZ\b', r'\bGG\b',
    r'no way', r'let\'s go', r'j\'y crois pas', r'trop fort',
]
REACTION_RE = re.compile('|'.join(REACTION_PATTERNS), re.IGNORECASE)

# Patterns pour extraire les timestamps dans le texte d'un commentaire
TIMESTAMP_RE = re.compile(
    r'(?:^|[\s\(\[\|])(\d{1,2}:\d{2}(?::\d{2})?)(?:$|[\s\)\]\|,\.])',
    re.MULTILINE,
)


def _api_get(endpoint: str, params: dict) -> dict:
    url = f"{YT_BASE}/{endpoint}?{urlencode(params)}"
    try:
        with urlopen(url, timeout=20) as r:
            return json.loads(r.read().decode())
    except HTTPError as e:
        body = e.read().decode()
        print(f"  ❌ Erreur API YouTube ({e.code}): {json.loads(body).get('error', {}).get('message', body)}")
        return {}


def _ts_to_sec(ts: str) -> float:
    parts = ts.strip().split(":")
    parts = [float(p) for p in parts]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return parts[0] * 60 + parts[1]


def fetch_comments(video_id: str, api_key: str, max_pages: int = 10) -> list[dict]:
    """Récupère les commentaires top-level (jusqu'à ~1000)."""
    comments = []
    page_token = None

    for page in range(max_pages):
        params = {
            "part": "snippet",
            "videoId": video_id,
            "maxResults": 100,
            "order": "relevance",   # les plus likés en premier
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token

        data = _api_get("commentThreads", params)
        if not data:
            break

        for item in data.get("items", []):
            snippet = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            comments.append({
                "text":      snippet.get("textOriginal", ""),
                "likes":     snippet.get("likeCount", 0),
                "published": snippet.get("publishedAt", ""),
            })

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return comments


def build_heatmap(comments: list[dict], duration_sec: float, bucket_sec: float = 30) -> dict:
    """
    Construit une heatmap temporelle.
    Retourne un dict {bucket_start_sec: heat_score}.
    
    Le heat_score combine :
    - Nombre de commentaires mentionnant ce timestamp
    - Likes sur ces commentaires
    - Présence de mots/emojis de réaction forte
    """
    n_buckets = max(1, int(duration_sec / bucket_sec) + 1)
    heat = defaultdict(float)
    timestamp_hits = defaultdict(int)  # nb de fois qu'un timestamp est mentionné

    for c in comments:
        text = c["text"]
        likes = max(c["likes"], 1)  # au moins 1 pour éviter multiplication par 0
        reaction_count = len(REACTION_RE.findall(text))

        # Cherche des timestamps dans le commentaire
        ts_found = TIMESTAMP_RE.findall(text)
        for ts in ts_found:
            sec = _ts_to_sec(ts)
            if 0 <= sec <= duration_sec:
                bucket = int(sec // bucket_sec) * bucket_sec
                # Score = mentions × likes × boost réaction
                reaction_boost = 1 + reaction_count * 0.5
                heat[bucket] += likes * reaction_boost
                timestamp_hits[bucket] += 1

        # Même sans timestamp : si le commentaire a des réactions fortes,
        # on ne peut pas localiser — on l'ignore (pas de signal temporel)

    return dict(heat), dict(timestamp_hits)


def get_hotspot_moments(
    video_id: str,
    api_key: str,
    duration_sec: float,
    top_n: int = 20,
    bucket_sec: float = 30,
    clip_duration: float = 60,
) -> list[dict]:
    """
    Pipeline complet : fetch commentaires → heatmap → top N moments.
    
    Retourne une liste de dicts :
      { start, duration, score, source, title, timestamp_mentions }
    """
    print("  💬 Récupération des commentaires YouTube...")
    comments = fetch_comments(video_id, api_key)
    print(f"     → {len(comments)} commentaires analysés")

    if not comments:
        return []

    heat, ts_hits = build_heatmap(comments, duration_sec, bucket_sec)

    if not heat:
        print("     → Aucun timestamp trouvé dans les commentaires")
        return []

    # Normalisation du score
    max_heat = max(heat.values()) if heat else 1
    moments = []
    for bucket_start, score in heat.items():
        normalized = score / max_heat
        mentions = ts_hits.get(bucket_start, 0)
        moments.append({
            "start":               bucket_start,
            "duration":            clip_duration,
            "score":               round(normalized * 4, 3),  # scale 0-4
            "source":              "comments",
            "title":               f"Réactions commentaires ({mentions} mentions)",
            "timestamp_mentions":  mentions,
            "raw_heat":            round(score, 2),
        })

    # Tri par score décroissant
    moments.sort(key=lambda x: x["score"], reverse=True)

    # Déduplication : évite deux moments trop proches
    kept = []
    for m in moments:
        too_close = any(abs(m["start"] - k["start"]) < clip_duration * 0.6 for k in kept)
        if not too_close:
            kept.append(m)
        if len(kept) >= top_n:
            break

    # Retri chronologique
    kept.sort(key=lambda x: x["start"])

    total_mentions = sum(m["timestamp_mentions"] for m in kept)
    print(f"     → {len(kept)} moments chauds identifiés ({total_mentions} mentions totales)")

    return kept


def seconds_to_hms(s: float) -> str:
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def print_hotspots(moments: list[dict], video_id: str):
    print(f"\n{'─'*55}")
    print(f"  Heatmap commentaires — {len(moments)} moments chauds")
    print(f"{'─'*55}")
    for i, m in enumerate(moments, 1):
        ts = seconds_to_hms(m["start"])
        link = f"https://youtu.be/{video_id}?t={int(m['start'])}"
        print(f"  #{i:02d}  {ts}  score:{m['score']:.2f}  mentions:{m['timestamp_mentions']}")
        print(f"       {link}")
    print()


# ─── CLI standalone ───────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Heatmap des moments forts via commentaires YouTube")
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--api-key",  required=True)
    parser.add_argument("--duration", type=float, default=3600,
                        help="Durée de la vidéo en secondes (défaut: 3600)")
    parser.add_argument("--top",      type=int, default=20)
    args = parser.parse_args()

    moments = get_hotspot_moments(
        video_id=args.video_id,
        api_key=args.api_key,
        duration_sec=args.duration,
        top_n=args.top,
    )
    print_hotspots(moments, args.video_id)
