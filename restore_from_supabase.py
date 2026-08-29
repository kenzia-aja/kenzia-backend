"""Pulihkan db.json dari Supabase (kebalikan sync_supabase.py).

Dipakai saat state lokal hilang/rusak: semua series + episode (termasuk
servers yang sudah terisi cron) diunduh dari Supabase dan ditulis ke db.json.

Pemakaian:
    python restore_from_supabase.py [--output data/db.json]
Env: SUPABASE_URL, SUPABASE_SERVICE_KEY
"""
import json
import os
import sys
from pathlib import Path

import httpx

PAGE = 1000


def fail(msg: str):
    print(f"ERROR: {msg}")
    sys.exit(1)


def fetch_all(client: httpx.Client, table: str) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        resp = client.get(
            f"/{table}",
            params={"select": "*", "limit": PAGE, "offset": offset},
        )
        resp.raise_for_status()
        batch = resp.json()
        rows.extend(batch)
        if len(batch) < PAGE:
            return rows
        offset += PAGE


def main() -> int:
    args = sys.argv[1:]
    out = Path(args[args.index("--output") + 1]) if "--output" in args else Path("data/db.json")

    supabase_url = os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not service_key:
        fail("Set SUPABASE_URL dan SUPABASE_SERVICE_KEY.")

    headers = {"apikey": service_key, "Authorization": f"Bearer {service_key}"}

    with httpx.Client(
        base_url=f"{supabase_url.rstrip('/')}/rest/v1",
        headers=headers,
        timeout=60.0,
    ) as client:
        print("Mengunduh seriesâ€¦")
        series_rows = fetch_all(client, "series")
        print(f"  {len(series_rows)} series")
        print("Mengunduh episodesâ€¦")
        episode_rows = fetch_all(client, "episodes")
        print(f"  {len(episode_rows)} episodes")

    id_to_slug = {row["id"]: row["slug"] for row in series_rows}
    db: dict[str, dict] = {}
    for row in series_rows:
        db[row["slug"]] = {
            "slug": row["slug"],
            "url": row.get("source_url"),
            "title": row.get("title"),
            "type": row.get("type"),
            "status": row.get("status"),
            "country": row.get("country"),
            "released": row.get("released"),
            "rating": str(row["rating"]) if row.get("rating") else None,
            "poster": row.get("poster_url"),
            "network": row.get("network"),
            "director": row.get("director"),
            "total_episodes": row.get("total_episodes"),
            "synopsis": row.get("synopsis"),
            "cast": row.get("cast_list") or [],
            "genres": row.get("genres") or [],
            "episodes": [],
            "last_scraped_at": row.get("last_scraped_at"),
            "first_seen_at": row.get("first_seen_at"),
        }

    for ep in episode_rows:
        slug = id_to_slug.get(ep.get("series_id"))
        if slug is None or slug not in db:
            continue
        db[slug]["episodes"].append(
            {
                "number": ep.get("number"),
                "title": ep.get("title"),
                "date": ep.get("release_date"),
                "url": ep.get("source_url"),
                "servers": ep.get("servers") or [],
                "embeds": ep.get("embeds") or [],
                "stale": bool(ep.get("stale")),
                "first_seen_at": ep.get("first_seen_at"),
            }
        )

    for item in db.values():
        item["episodes"].sort(key=lambda e: (e.get("number") is None, e.get("number") or 0))

    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")

    filled = sum(1 for v in db.values() for e in v["episodes"] if e.get("servers"))
    print(f"db.json dipulihkan: {len(db)} series, "
          f"{sum(len(v['episodes']) for v in db.values())} episode, {filled} punya servers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
