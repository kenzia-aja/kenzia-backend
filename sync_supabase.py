import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

DB_FILE = Path("data/db.json")
BATCH_SIZE = 400


def fail(message: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"ERROR: {message}")
    sys.exit(1)


def to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_series_row(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    slug = item.get("slug")
    if not slug:
        return None
    row = {
        "slug": slug,
        "title": item.get("title"),
        "type": item.get("type"),
        "status": item.get("status"),
        "country": item.get("country"),
        "released": item.get("released"),
        "rating": to_float(item.get("rating")),
        "poster_url": item.get("poster"),
        "network": item.get("network"),
        "director": item.get("director"),
        "total_episodes": str(item.get("total_episodes")) if item.get("total_episodes") else None,
        "synopsis": item.get("synopsis"),
        "cast_list": item.get("cast") or [],
        "genres": item.get("genres") or [],
        "source_url": item.get("url"),
        "last_scraped_at": item.get("last_scraped_at") or datetime.now(timezone.utc).isoformat(),
    }
    # first_seen_at hanya dikirim bila ada — jangan menimpa nilai backfill SQL dengan null
    if item.get("first_seen_at"):
        row["first_seen_at"] = item["first_seen_at"]
    if item.get("last_update_at"):
        row["last_update_at"] = item["last_update_at"]
    return row


def build_episode_rows(
    series_id: int, episodes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows = []
    for ep in episodes:
        if not ep.get("url"):
            continue
        row = {
            "series_id": series_id,
            "number": ep.get("number"),
            "title": ep.get("title"),
            "release_date": ep.get("date"),
            "source_url": ep["url"],
            "embeds": [e for e in (ep.get("embeds") or []) if e],
            "servers": ep.get("servers") or [],
            "stale": bool(ep.get("stale")),
            "checked_at": ep.get("checked_at"),
        }
        if ep.get("first_seen_at"):
            row["first_seen_at"] = ep["first_seen_at"]
        rows.append(row)
    return rows


def aggregate_counts(db: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Hitung frekuensi nilai (genre/negara) dari semua series.

    Nilai bisa berupa list (genres) atau string tunggal (country).
    """
    counter: dict[str, int] = {}
    for item in db.values():
        value = item.get(key)
        names = value if isinstance(value, list) else [value]
        for name in names:
            name = (name or "").strip()
            if name:
                counter[name] = counter.get(name, 0) + 1
    return [{"name": name, "count": count} for name, count in sorted(counter.items())]


def series_has_video(item: dict[str, Any]) -> bool:
    """True bila series layak tayang.

    - TANPA episode:
        * detail belum pernah sukses (tanpa synopsis) → PERTAHANKAN
          (detail scrape bisa saja gagal masa 503 — jangan buang!)
        * detail sukses tapi tetap kosong → MATI (sumber tak punya video)
    - DENGAN episode:
        * definitive = ada bukti pemeriksaan (embeds / stale / working True-False)
        * working=None (5xx/timeout) tidak dihitung mati
        * mati = semua episode tercek tapi tidak ada yang jalan
    """
    eps = item.get("episodes") or []
    if not eps:
        return not bool(item.get("synopsis"))
    checked = 0
    working = 0
    for ep in eps:
        servers = ep.get("servers") or []
        embeds = ep.get("embeds") or []
        definitive = (
            bool(embeds)
            or bool(ep.get("stale"))
            or any(s.get("working") in (True, False) for s in servers)
        )
        if definitive:
            checked += 1
        if embeds or any(s.get("working") is True for s in servers):
            working += 1
    if checked == 0:
        return True  # belum dicek → jangan dibuang
    return working > 0


def chunked(items: list[Any], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def main() -> int:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    db_path_arg = None
    if "--db" in args:
        db_path_arg = args[args.index("--db") + 1]
    db_path = Path(db_path_arg) if db_path_arg else DB_FILE

    if not db_path.exists():
        fail(f"File {db_path} tidak ditemukan. Jalankan scraper.py dulu.")

    db = json.loads(db_path.read_text(encoding="utf-8"))
    if not db:
        fail(
            "db.json KOSONG — kemungkinan state hilang/rusak. "
            "JANGAN sync dengan state kosong (bisa menghapus genres/countries). "
            "Pulihkan dulu: python restore_from_supabase.py"
        )
    supabase_url = os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_KEY")

    if dry_run:
        total_eps = sum(len(it.get("episodes") or []) for it in db.values())
        with_embeds = sum(
            1 for it in db.values() for e in it.get("episodes") or [] if e.get("embeds")
        )
        print(f"[DRY-RUN] Akan upsert {len(db)} series dan ~{total_eps} episode ({with_embeds} punya embeds)")
        print("[DRY-RUN] Contoh row series:")
        sample = next(iter(db.values()), {})
        print(json.dumps(build_series_row(sample), ensure_ascii=False, indent=2)[:600])
        return 0

    if not supabase_url or not service_key:
        fail("Set environment SUPABASE_URL dan SUPABASE_SERVICE_KEY (service_role).")

    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }

    with httpx.Client(base_url=f"{supabase_url.rstrip('/')}/rest/v1", headers=headers, timeout=60.0) as client:
        # ── Filter series mati + variety show ──
        alive: dict[str, dict[str, Any]] = {}
        dead_slugs: list[str] = []
        for slug, item in db.items():
            tipe = (item.get("type") or "").strip().lower()
            if any(b == tipe for b in ("tv show", "variety show", "variety", "special")):
                dead_slugs.append(slug)  # variety dibuang + dihapus dari Supabase
            elif series_has_video(item):
                alive[slug] = item
            else:
                dead_slugs.append(slug)
        dropped = len(dead_slugs)

        series_rows = [row for row in (build_series_row(it) for it in alive.values()) if row]
        for batch in chunked(series_rows, BATCH_SIZE):
            resp = client.post("/series?on_conflict=slug", json=batch)
            resp.raise_for_status()
        print(f"Series di-upsert: {len(series_rows)} (dibuang mati: {dropped})")

        # Hapus series mati yang sebelumnya sudah terlanjur di Supabase
        # (cascade menghapus episodes-nya juga)
        resp = client.get("/series", params={"select": "slug"})
        resp.raise_for_status()
        existing_slugs = [row["slug"] for row in resp.json()]
        to_delete = [s for s in existing_slugs if s in set(dead_slugs)]
        for i in range(0, len(to_delete), BATCH_SIZE):
            batch = to_delete[i : i + BATCH_SIZE]
            quoted = ",".join(f'"{s}"' for s in batch)
            resp = client.delete("/series", params={"slug": f"in.({quoted})"})
            resp.raise_for_status()
        if to_delete:
            print(f"Series mati dihapus dari Supabase: {len(to_delete)}")

        # id_map SEMUA series — dengan pagination (default PostgREST cuma 1000)
        id_map: dict[str, int] = {}
        offset = 0
        while True:
            resp = client.get("/series", params={"select": "id,slug", "limit": 1000, "offset": offset})
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                break
            for row in rows:
                id_map[row["slug"]] = row["id"]
            if len(rows) < 1000:
                break
            offset += 1000

        episode_rows: list[dict[str, Any]] = []
        skipped = 0
        for slug, item in alive.items():
            sid = id_map.get(slug)
            if sid is None:
                skipped += 1
                continue
            episode_rows.extend(build_episode_rows(sid, item.get("episodes") or []))

        for batch in chunked(episode_rows, BATCH_SIZE):
            resp = client.post("/episodes?on_conflict=source_url", json=batch)
            resp.raise_for_status()

        # Agregat genre & negara (dipakai endpoint /api/genres & /api/countries)
        # Hapus dulu agar entri lama yang tidak ada lagi ikut bersih.
        # PostgREST menolak DELETE tanpa filter → pakai id=not.is.null.
        client.delete("/genres", params={"name": "not.is.null"}).raise_for_status()
        client.delete("/countries", params={"name": "not.is.null"}).raise_for_status()

        genre_rows = aggregate_counts(db, "genres")
        for batch in chunked(genre_rows, BATCH_SIZE):
            resp = client.post("/genres?on_conflict=name", json=batch)
            resp.raise_for_status()

        country_rows = aggregate_counts(db, "country")
        for batch in chunked(country_rows, BATCH_SIZE):
            resp = client.post("/countries?on_conflict=name", json=batch)
            resp.raise_for_status()

        # Jadwal rilis mingguan (scrape langsung; 1 halaman, cepat)
        try:
            from scraper import OppadramaScraper, BASE_URL

            async def _sync_schedule() -> int:
                scraper = OppadramaScraper(base_url=BASE_URL, concurrency=2, retries=2)
                try:
                    days = await scraper.get_schedule()
                finally:
                    await scraper.close()
                client.delete(
                    "/schedule", params={"day": "not.is.null"}
                ).raise_for_status()
                rows = [
                    {"day": d["day"], "items": d["items"], "updated_at": now}
                    for d in days
                ]
                for batch in chunked(rows, BATCH_SIZE):
                    client.post("/schedule?on_conflict=day", json=batch).raise_for_status()
                return len(rows)

            import asyncio

            from datetime import datetime, timezone as _tz

            now = datetime.now(_tz.utc).isoformat()
            n_days = asyncio.run(_sync_schedule())
            print(f"Jadwal rilis: {n_days} hari")
        except Exception as exc:  # jadwal gagal tidak boleh menggagalkan sync utama
            print(f"WARNING: gagal sync jadwal: {exc}")

        print(f"Episode di-upsert: {len(episode_rows)} (dilewati: {skipped} series tanpa id)")
        print(f"Genres: {len(genre_rows)} | Countries: {len(country_rows)}")
        print("Sinkronisasi Supabase selesai.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
