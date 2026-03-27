#!/usr/bin/env python3
"""
YouTube Viral Detector — Clip Finder Optimisé
=============================================
Détecte chaque matin les vidéos à fort potentiel à clipper,
classées par score viral (vues/heure × ratio engagement).

Usage:
    python youtube_viral_detector.py --api-key YOUR_KEY
    python youtube_viral_detector.py --api-key YOUR_KEY --hours 12 --region FR
    python youtube_viral_detector.py --api-key YOUR_KEY --export csv
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.request import urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError

# ─────────────────────────────────────────────
# CONFIG PAR DÉFAUT — modifie ici si besoin
# ─────────────────────────────────────────────
DEFAULT_CONFIG = {
    "hours_back": 24,           # Fenêtre temporelle (vidéos publiées depuis X heures)
    "region_code": "FR",        # Code pays : FR, SN, US, etc.
    "language": "fr",           # Langue principale du contenu en (us)
    "max_channel_subs": 100_000_000,  # Exclure les très grandes chaînes (trop compétitif)
    "min_views": 10_000,        # Vues minimum pour être éligible
    "min_engagement_rate": 0.02,    # 2% = (likes + comments) / vues
    "top_n_results": 20,        # Nombre de vidéos à afficher
    "categories": {             # Catégories YouTube à inclure (laisser vide = toutes)
        # "1": "Film & Animation",
        # "10": "Music",
        # "20": "Gaming",
        # "22": "People & Blogs",
        # "23": "Comedy",
        # "24": "Entertainment",  ← pertinent pour divertissement général
        # "25": "News & Politics",
        # "26": "Howto & Style",
        # "28": "Science & Technology",
    },
}

BASE_URL = "https://www.googleapis.com/youtube/v3"


# ─────────────────────────────────────────────
# FONCTIONS UTILITAIRES
# ─────────────────────────────────────────────

def api_get(endpoint: str, params: dict) -> dict:
    """Appel GET à l'API YouTube v3."""
    url = f"{BASE_URL}/{endpoint}?{urlencode(params)}"
    try:
        with urlopen(url, timeout=15) as r:
            return json.loads(r.read().decode())
    except HTTPError as e:
        body = e.read().decode()
        err = json.loads(body).get("error", {})
        print(f"\n❌ Erreur API ({e.code}): {err.get('message', body)}")
        if e.code == 403:
            print("   → Vérifie que YouTube Data API v3 est activée sur ton projet Google Cloud.")
            print("   → Ou que ta clé API est correcte.")
        sys.exit(1)


def hours_ago(n: int) -> str:
    """Retourne une date ISO 8601 pour 'il y a N heures'."""
    dt = datetime.now(timezone.utc) - timedelta(hours=n)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_duration_to_seconds(iso: str) -> int:
    """Convertit PT4M13S → 253 secondes."""
    import re
    h = int((re.search(r'(\d+)H', iso) or type('', (), {'group': lambda s, x: '0'})()).group(1))
    m = int((re.search(r'(\d+)M', iso) or type('', (), {'group': lambda s, x: '0'})()).group(1))
    s = int((re.search(r'(\d+)S', iso) or type('', (), {'group': lambda s, x: '0'})()).group(1))
    return h * 3600 + m * 60 + s


def viral_score(views: int, hours_old: float, likes: int, comments: int) -> float:
    """
    Score viral = vitesse de croissance × qualité d'engagement.
    
    Formule :
        views_per_hour = vues / heures_depuis_publication
        engagement     = (likes + comments) / vues
        score          = views_per_hour × (1 + engagement × 10)
    
    Le multiplicateur d'engagement booste les vidéos qui suscitent
    de vraies réactions, pas juste des vues passives.
    """
    if hours_old < 0.1:
        hours_old = 0.1  # éviter division par zéro
    views_per_hour = views / hours_old
    engagement = (likes + comments) / views if views > 0 else 0
    return views_per_hour * (1 + engagement * 10)


def format_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


def clip_duration_advice(seconds: int) -> str:
    """Conseil sur la durée idéale du clip."""
    if seconds < 60:
        return "Short natif — clippe l'intégralité"
    if seconds < 300:
        return "Clippe les 45-60s les plus fortes"
    if seconds < 900:
        return "Clippe 2-3 séquences de 45s max"
    return "Long format — cherche le moment clé (peak)"


# ─────────────────────────────────────────────
# ÉTAPE 1 — Récupérer les vidéos récentes
# ─────────────────────────────────────────────

def fetch_recent_videos(api_key: str, cfg: dict) -> list[dict]:
    """
    Utilise videos.list avec chart=mostPopular + publishedAfter pour
    cibler uniquement les vidéos fraîches dans la fenêtre temporelle.
    """
    print(f"🔍 Recherche des vidéos publiées dans les {cfg['hours_back']}h...")

    published_after = hours_ago(cfg["hours_back"])
    all_items = []
    page_token = None

    for _ in range(5):  # max 5 pages = 250 vidéos candidates
        params = {
            "part": "id,snippet",
            "chart": "mostPopular",
            "regionCode": cfg["region_code"],
            "relevanceLanguage": cfg["language"],
            "maxResults": 50,
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token

        data = api_get("videos", params)
        items = data.get("items", [])

        # Filtrer par date de publication
        for item in items:
            published = item["snippet"].get("publishedAt", "")
            if published >= published_after:
                all_items.append(item)

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    print(f"   → {len(all_items)} vidéos récentes trouvées")
    return all_items


# ─────────────────────────────────────────────
# ÉTAPE 2 — Enrichir avec les stats
# ─────────────────────────────────────────────

def fetch_video_stats(api_key: str, video_ids: list[str]) -> dict:
    """Récupère les statistiques et détails pour une liste d'IDs."""
    stats_map = {}

    # L'API accepte max 50 IDs par requête
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i+50]
        params = {
            "part": "statistics,contentDetails,snippet",
            "id": ",".join(batch),
            "key": api_key,
        }
        data = api_get("videos", params)
        for item in data.get("items", []):
            stats_map[item["id"]] = item

    return stats_map


# ─────────────────────────────────────────────
# ÉTAPE 3 — Filtrer les chaînes trop grandes
# ─────────────────────────────────────────────

def fetch_channel_subs(api_key: str, channel_ids: list[str]) -> dict:
    """Récupère le nombre d'abonnés par chaîne."""
    subs_map = {}
    unique_ids = list(set(channel_ids))

    for i in range(0, len(unique_ids), 50):
        batch = unique_ids[i:i+50]
        params = {
            "part": "statistics",
            "id": ",".join(batch),
            "key": api_key,
        }
        data = api_get("channels", params)
        for item in data.get("items", []):
            subs = int(item["statistics"].get("subscriberCount", 0))
            subs_map[item["id"]] = subs

    return subs_map


# ─────────────────────────────────────────────
# ÉTAPE 4 — Scorer et trier
# ─────────────────────────────────────────────

def score_and_filter(stats_map: dict, subs_map: dict, cfg: dict) -> list[dict]:
    """Applique les filtres et calcule le score viral."""
    now = datetime.now(timezone.utc)
    results = []

    for vid_id, item in stats_map.items():
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        details = item.get("contentDetails", {})

        views = int(stats.get("viewCount", 0))
        likes = int(stats.get("likeCount", 0))
        comments = int(stats.get("commentCount", 0))
        channel_id = snippet.get("channelId", "")
        published_str = snippet.get("publishedAt", "")
        duration_iso = details.get("duration", "PT0S")

        # Filtre : vues minimum
        if views < cfg["min_views"]:
            continue

        # Filtre : engagement minimum
        engagement = (likes + comments) / views if views > 0 else 0
        if engagement < cfg["min_engagement_rate"]:
            continue

        # Filtre : taille de chaîne
        channel_subs = subs_map.get(channel_id, 0)
        if channel_subs > cfg["max_channel_subs"]:
            continue

        # Calcul de l'ancienneté
        try:
            pub_dt = datetime.strptime(published_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            hours_old = (now - pub_dt).total_seconds() / 3600
        except Exception:
            continue

        # Durée
        duration_s = parse_duration_to_seconds(duration_iso)
        is_short = duration_s <= 60

        # Score viral
        score = viral_score(views, hours_old, likes, comments)

        results.append({
            "id": vid_id,
            "title": snippet.get("title", ""),
            "channel": snippet.get("channelTitle", ""),
            "channel_subs": channel_subs,
            "views": views,
            "likes": likes,
            "comments": comments,
            "engagement_pct": engagement * 100,
            "hours_old": hours_old,
            "duration_s": duration_s,
            "is_short": is_short,
            "score": score,
            "url": f"https://youtube.com/watch?v={vid_id}",
            "clip_advice": clip_duration_advice(duration_s),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:cfg["top_n_results"]]


# ─────────────────────────────────────────────
# AFFICHAGE
# ─────────────────────────────────────────────

def print_results(results: list[dict], cfg: dict):
    """Affiche le rapport terminal."""
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    print(f"\n{'='*65}")
    print(f"  🎬 YOUTUBE VIRAL DETECTOR — Rapport du {now_str}")
    print(f"  Fenêtre : {cfg['hours_back']}h | Région : {cfg['region_code']}")
    print(f"{'='*65}\n")

    if not results:
        print("⚠️  Aucune vidéo ne correspond aux critères.")
        print("   → Essaie d'augmenter 'hours_back' ou de baisser 'min_views'.")
        return

    for i, v in enumerate(results, 1):
        badge = "📱 SHORT" if v["is_short"] else "🎬 LONG "
        print(f"#{i:02d} {badge} | Score: {v['score']:,.0f}")
        print(f"     📌 {v['title'][:70]}")
        print(f"     📺 {v['channel']} ({format_number(v['channel_subs'])} abos)")
        print(f"     👁  {format_number(v['views'])} vues "
              f"| ❤️  {format_number(v['likes'])} "
              f"| 💬 {format_number(v['comments'])} "
              f"| 📊 {v['engagement_pct']:.1f}% engagement")
        print(f"     ⏱  Publiée il y a {v['hours_old']:.1f}h "
              f"| Durée : {v['duration_s']//60}m{v['duration_s']%60:02d}s")
        print(f"     ✂️  {v['clip_advice']}")
        print(f"     🔗 {v['url']}")
        print()

    print(f"{'='*65}")
    print(f"  ✅ {len(results)} vidéos à clipper aujourd'hui")
    print(f"  💡 Priorise les #1 à #5 pour le maximum d'impact")
    print(f"{'='*65}\n")


# ─────────────────────────────────────────────
# EXPORT CSV (optionnel)
# ─────────────────────────────────────────────

def export_csv(results: list[dict], filename: str = "clips_du_jour.csv"):
    """Exporte les résultats en CSV."""
    import csv
    fields = ["id", "title", "channel", "channel_subs", "views", "likes",
              "comments", "engagement_pct", "hours_old", "duration_s",
              "is_short", "score", "clip_advice", "url"]
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    print(f"📄 Export CSV → {filename}")


def export_json(results: list[dict], filename: str = "clips_du_jour.json"):
    """Exporte les résultats en JSON."""
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"📄 Export JSON → {filename}")


# ─────────────────────────────────────────────
# PIPELINE PRINCIPAL
# ─────────────────────────────────────────────

def run(api_key: str, cfg: dict, export: Optional[str] = None):
    print(f"\n🚀 Démarrage du scan YouTube Viral Detector...")

    # 1. Vidéos récentes populaires
    recent = fetch_recent_videos(api_key, cfg)
    if not recent:
        print("⚠️  Aucune vidéo récente trouvée. Essaie d'augmenter hours_back.")
        return

    # 2. Stats détaillées
    print("📊 Récupération des statistiques...")
    video_ids = [v["id"] for v in recent]
    stats_map = fetch_video_stats(api_key, video_ids)

    # 3. Abonnés des chaînes
    print("👤 Vérification des tailles de chaînes...")
    channel_ids = [v["snippet"]["channelId"] for v in recent if "snippet" in v]
    subs_map = fetch_channel_subs(api_key, channel_ids)

    # 4. Score + filtres
    print("🧮 Calcul des scores viraux...")
    results = score_and_filter(stats_map, subs_map, cfg)

    # 5. Affichage
    print_results(results, cfg)

    # 6. Export si demandé
    if export == "csv":
        export_csv(results)
    elif export == "json":
        export_json(results)


# ─────────────────────────────────────────────
# POINT D'ENTRÉE
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Détecte les vidéos YouTube virales à clipper aujourd'hui"
    )
    parser.add_argument("--api-key", required=True, help="Clé API YouTube Data v3")
    parser.add_argument("--hours", type=int, default=DEFAULT_CONFIG["hours_back"],
                        help="Fenêtre temporelle en heures (défaut: 24)")
    parser.add_argument("--region", default=DEFAULT_CONFIG["region_code"],
                        help="Code région (ex: FR, SN, US — défaut: FR)")
    parser.add_argument("--lang", default=DEFAULT_CONFIG["language"],
                        help="Langue (ex: fr, en — défaut: fr)")
    parser.add_argument("--max-subs", type=int, default=DEFAULT_CONFIG["max_channel_subs"],
                        help="Abonnés max de la chaîne source (défaut: 1M)")
    parser.add_argument("--min-views", type=int, default=DEFAULT_CONFIG["min_views"],
                        help="Vues minimum (défaut: 10000)")
    parser.add_argument("--top", type=int, default=DEFAULT_CONFIG["top_n_results"],
                        help="Nombre de résultats (défaut: 20)")
    parser.add_argument("--export", choices=["csv", "json"],
                        help="Exporter les résultats (csv ou json)")

    args = parser.parse_args()

    cfg = {
        **DEFAULT_CONFIG,
        "hours_back": args.hours,
        "region_code": args.region,
        "language": args.lang,
        "max_channel_subs": args.max_subs,
        "min_views": args.min_views,
        "top_n_results": args.top,
    }

    run(api_key=args.api_key, cfg=cfg, export=args.export)
