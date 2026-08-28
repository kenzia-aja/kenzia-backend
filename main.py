import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from scraper import (
    OppadramaScraper,
    _server_sort_key,
    dedup_episodes_list,
    extract_episode_number,
    now_iso,
    upsert_item,
)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DB_FILE = DATA_DIR / "db.json"

LATEST_TTL_SECONDS = 300.0
SCHEDULE_TTL_SECONDS = 3600.0

db: dict[str, dict[str, Any]] = {}
scraper: OppadramaScraper
_latest_cache: dict[str, Any] = {"ts": 0.0, "pages": {}}
_schedule_cache: dict[str, Any] = {"ts": 0.0, "data": None}


def load_db() -> None:
    global db
    if DB_FILE.exists():
        try:
            db = json.loads(DB_FILE.read_text(encoding="utf-8"))
            for item in db.values():
                eps = item.get("episodes") or []
                for ep in eps:
                    if ep.get("number") is None:
                        ep["number"] = extract_episode_number(None, ep.get("title"), ep.get("url"))
                item["episodes"] = dedup_episodes_list(eps)
            return
        except json.JSONDecodeError:
            pass
    db = {}


def save_db() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    tmp = DB_FILE.with_suffix(".tmp")
    content = json.dumps(db, ensure_ascii=False, indent=2)
    tmp.write_text(content, encoding="utf-8")
    try:
        tmp.replace(DB_FILE)
    except PermissionError:
        # Windows: file may be locked by another process / race condition
        import time as _time

        for _ in range(5):
            _time.sleep(0.2)
            try:
                if DB_FILE.exists():
                    try:
                        DB_FILE.unlink()
                    except PermissionError:
                        continue
                tmp.replace(DB_FILE)
                break
            except PermissionError:
                continue
        else:
            # last resort: overwrite directly so request doesn't crash with 500
            try:
                DB_FILE.write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8")
                try:
                    tmp.unlink()
                except Exception:
                    pass
            except Exception:
                pass
    except Exception:
        # never let save_db crash request handling; log instead
        import traceback

        traceback.print_exc()


def _sanitize_servers(servers: list) -> list:
    """Buang server yang diblokir (FileLions dkk) atau yang jelas tak bisa diputar.

    Diterapkan pada hasil cache & hasil scrape agar konsisten walau state lama kotor.
    Diakhir dengan urutan prioritas yang sama dengan scraper (Hydrax dulu).
    """
    from urllib.parse import urlparse

    blocked_hosts = ("minochinos.com", "filelions", "filelions.com")
    out = []
    for s in servers or []:
        name = (s.get("name") or "").lower()
        embed = s.get("embed") or ""
        host = (urlparse(embed).hostname or "").lower()
        if any(h in host for h in blocked_hosts) or any(b in name for b in ("filelions",)):
            continue
        # server yang sudah jelas mati (working False) jangan dikirim
        if s.get("working") is False:
            continue
        out.append(s)
    out.sort(key=_server_sort_key)
    return out


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global scraper
    DATA_DIR.mkdir(exist_ok=True)
    load_db()
    scraper = OppadramaScraper()
    yield
    save_db()
    await scraper.close()


app = FastAPI(
    title="Streaming Film API",
    version="0.3.0",
    description=(
        "REST API hasil scraping katalog film/drama. "
        "Data series disimpan di cache lokal (db.json); link video per episode di-cache otomatis."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


WESTERN_COUNTRIES = {
    "united states", "united kingdom", "canada", "australia", "ireland",
    "new zealand", "germany", "france", "spain", "finland", "belgium",
    "luxembourg", "hungary", "poland", "brazil", "mexico", "south africa",
    "iraq", "qatar", "singapore",
}
COUNTRY_ALIASES = {
    "usa": "united states",
    "us": "united states",
    "amerika": "united states",
    "inggris": "united kingdom",
    "uk": "united kingdom",
}


def country_matches(item_country: Optional[str], query: str) -> bool:
    q = query.strip().lower()
    q = COUNTRY_ALIASES.get(q, q)
    if q in ("barat", "west", "western"):
        parts = _country_parts(item_country)
        return any(p in WESTERN_COUNTRIES for p in parts)
    parts = _country_parts(item_country)
    return any(q == p or q in p for p in parts)


def _country_parts(value: Optional[str]) -> list[str]:
    parts = []
    for raw in (value or "").split(","):
        p = COUNTRY_ALIASES.get(raw.strip().lower(), raw.strip().lower())
        if p:
            parts.append(p)
    return parts


@app.get("/")
async def root():
    return {
        "message": "Welcome to Streaming Film API",
        "docs_url": "/docs",
        "health_check": "/health"
    }


@app.get("/health")
async def health() -> dict:
    total_eps = sum(len(it.get("episodes") or []) for it in db.values())
    eps_with_embeds = sum(
        1
        for it in db.values()
        for e in it.get("episodes") or []
        if e.get("embeds")
    )
    return {
        "status": "ok",
        "cached_series": len(db),
        "cached_episodes": total_eps,
        "episodes_with_embeds": eps_with_embeds,
    }


@app.get("/api/latest")
async def api_latest(page: int = Query(1, ge=1)) -> dict:
    """Episode terbaru dari homepage (live scrape dengan cache TTL 5 menit)."""
    now = time.time()
    key = str(page)
    if now - _latest_cache["ts"] > LATEST_TTL_SECONDS or key not in _latest_cache["pages"]:
        items = await scraper.get_latest(page)
        _latest_cache["ts"] = now
        _latest_cache["pages"] = {key: items}
    return {"page": page, "results": _latest_cache["pages"][key]}


@app.get("/api/schedule")
async def api_schedule() -> dict:
    """Jadwal rilis mingguan (cache TTL 1 jam)."""
    now = time.time()
    if _schedule_cache["data"] is None or now - _schedule_cache["ts"] > SCHEDULE_TTL_SECONDS:
        data = await scraper.get_schedule()
        _schedule_cache["ts"] = now
        _schedule_cache["data"] = data
    return {"days": _schedule_cache["data"]}


SOURCE_PAGE_SIZE = 20  # jumlah item per halaman pada situs sumber


async def _fetch_live_window(
    fetcher, page: int, limit: int
) -> Optional[tuple[dict, list[dict[str, Any]]]]:
    """Ambil satu window `limit` item dari sumber yang berpagination 20 item/halaman.

    Beberapa halaman sumber digabungkan agar satu halaman API selalu terisi penuh
    sesuai `limit` (grid frontend tidak kurang item di baris terakhir), lalu
    dipotong tepat `limit` item. Mengembalikan (payload, semua_item_terkumpul).
    """
    first_idx = (page - 1) * limit
    src_first = first_idx // SOURCE_PAGE_SIZE + 1
    src_last = (page * limit + SOURCE_PAGE_SIZE - 1) // SOURCE_PAGE_SIZE

    collected: list[dict[str, Any]] = []
    exhausted = False
    for p in range(src_first, src_last + 1):
        items = await fetcher(p)
        if not items:
            exhausted = True
            break
        collected.extend(items)
        if len(items) < SOURCE_PAGE_SIZE:
            exhausted = True
            break

    if not collected:
        return None

    offset = first_idx - (src_first - 1) * SOURCE_PAGE_SIZE
    window = collected[offset : offset + limit]
    if exhausted:
        # Ujung katalog tercapai: total persis diketahui.
        total = (src_first - 1) * SOURCE_PAGE_SIZE + len(collected)
    else:
        # Belum ujung: minimal masih ada satu halaman sumber lagi.
        total = src_last * SOURCE_PAGE_SIZE + SOURCE_PAGE_SIZE
    payload = {
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": max(1, (total + limit - 1) // limit),
        "results": window,
    }
    return payload, collected


COUNTRY_CANONICAL = {
    "united states": "United States",
    "united kingdom": "United Kingdom",
    "south korea": "South Korea",
    "china": "China",
    "japan": "Japan",
    "taiwan": "Taiwan",
    "thailand": "Thailand",
    "indonesia": "Indonesia",
    "india": "India",
    "philippines": "Philippines",
    "hong kong": "Hong Kong",
    "canada": "Canada",
    "australia": "Australia",
}


@app.get("/api/series")
async def api_series_list(
    page: int = Query(1, ge=1),
    limit: int = Query(24, ge=1, le=100),
    q: Optional[str] = None,
    type: Optional[str] = Query(None, description="Drama | Movie | TV Show"),
    status: Optional[str] = Query(None, description="Ongoing | Completed"),
    country: Optional[str] = None,
    genre: Optional[str] = None,
) -> dict:
    """Katalog series/film lengkap: live fetch ke sumber asli saat filter aktif, dengan fallback & auto-cache."""
    has_filter = bool(q or type or status or country or genre)

    # 1. Jika ada pencarian query `q`
    if q:
        try:

            async def _search_fetch(p: int) -> list[dict[str, Any]]:
                return await scraper.search(q, page=p)

            fetched = await _fetch_live_window(_search_fetch, page, limit)
            if fetched is not None:
                payload, all_items = fetched
                for it in all_items:
                    upsert_item(db, it)
                try:
                    save_db()
                except Exception:
                    pass
                if payload["results"]:
                    return payload
        except Exception:
            pass

    # 2. Jika ada filter (tipe, status, negara, genre)
    if has_filter:
        try:

            async def _list_fetch(p: int) -> list[dict[str, Any]]:
                return await scraper.get_series_list(
                    page=p,
                    status=status,
                    type_=type,
                    country=country,
                    genre=genre,
                )

            fetched = await _fetch_live_window(_list_fetch, page, limit)
            if fetched is not None:
                payload, all_items = fetched
                for it in all_items:
                    upsert_item(db, it)
                try:
                    save_db()
                except Exception:
                    pass
                if payload["results"]:
                    return payload
        except Exception:
            pass

    # 3. Fallback ke database lokal
    items = list(db.values())
    items.reverse()

    def match(it: dict) -> bool:
        if q and q.lower() not in (it.get("title") or "").lower():
            return False
        if type and (it.get("type") or "").lower() != type.lower():
            return False
        if status and (it.get("status") or "").lower() != status.lower():
            return False
        if country and not country_matches(it.get("country"), country):
            return False
        if genre and genre.lower() not in [g.lower() for g in it.get("genres") or []]:
            return False
        return True

    filtered = [it for it in items if match(it)]
    total = len(filtered)
    start = (page - 1) * limit
    results = filtered[start : start + limit]

    # Jika halaman melebihi data lokal dan tanpa filter spesifik, coba ambil live catalog
    if not results and not has_filter and page > 1:
        try:

            async def _catalog_fetch(p: int) -> list[dict[str, Any]]:
                return await scraper.get_series_list(page=p)

            fetched = await _fetch_live_window(_catalog_fetch, page, limit)
            if fetched is not None:
                payload, all_items = fetched
                for it in all_items:
                    upsert_item(db, it)
                try:
                    save_db()
                except Exception:
                    pass
                if payload["results"]:
                    return payload
        except Exception:
            pass

    return {
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": max(1, (total + limit - 1) // limit),
        "results": results,
    }


@app.get("/api/genres")
async def api_genres() -> dict:
    counts: dict[str, int] = {}
    for item in db.values():
        for g in item.get("genres") or []:
            counts[g] = counts.get(g, 0) + 1
    genres = [
        {"name": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return {"total": len(genres), "genres": genres}


@app.get("/api/countries")
async def api_countries() -> dict:
    counts: dict[str, int] = {}
    for item in db.values():
        raw_country = item.get("country") or ""
        for part in _country_parts(raw_country):
            name = COUNTRY_CANONICAL.get(part, part.title())
            if name:
                counts[name] = counts.get(name, 0) + 1

    top_defaults = [
        "South Korea", "China", "Japan", "Thailand", "Taiwan",
        "United States", "United Kingdom", "Indonesia", "Hong Kong", "Philippines"
    ]
    for d in top_defaults:
        if d not in counts:
            counts[d] = 0

    countries = [
        {"name": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return {"total": len(countries), "countries": countries}


@app.get("/api/series/{slug}")
async def api_series_detail(slug: str, refresh: bool = False) -> dict:
    """Detail series + daftar episode. Otomatis scrape jika belum ada di cache."""
    item = db.get(slug)
    if item is not None and not refresh and item.get("synopsis"):
        return item

    url = (item or {}).get("url") or f"{scraper.base_url}/{slug}/"
    try:
        detail = await scraper.get_series_detail(url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gagal scraping: {exc}") from exc

    merged = upsert_item(db, detail)
    try:
        save_db()
    except Exception:
        pass
    return merged


@app.get("/api/series/{slug}/sources")
async def api_series_sources(
    slug: str,
    ep: Optional[int] = Query(None, description="Nomor episode (opsional untuk movie)"),
    refresh: bool = False,
) -> dict:
    """Daftar server video untuk episode tertentu atau halaman movie.

    Setiap server: {name, embed, stream, working}.
    `stream` = URL .m3u8 langsung (putar dengan HLS.js); `embed` = iframe player.
    """
    item = db.get(slug)
    if item is None:
        try:
            item = await api_series_detail(slug)
        except Exception:
            raise HTTPException(status_code=404, detail=f"'{slug}' tidak ditemukan")

    episodes = item.get("episodes") or []
    target = None
    if episodes:
        if ep is not None:
            target = next((e for e in episodes if e.get("number") == ep), None)
            if target is None:
                idx = ep - 1
                if 0 <= idx < len(episodes):
                    target = episodes[idx]
        if target is None:
            target = episodes[0]
            ep = target.get("number") or 1
        target_url = target.get("url") or item.get("url", "")
    else:
        target = item
        target_url = item.get("url", "")
        ep = None

    if not target_url:
        target_url = f"{scraper.base_url}/{slug}/"

    cached_servers = target.get("servers") or []
    has_valid = any(s.get("working") is not False for s in cached_servers)
    if not refresh and cached_servers and has_valid and not target.get("stale"):
        servers = _sanitize_servers(cached_servers)
        return {"slug": slug, "episode": ep, "url": target_url, "servers": servers, "cached": True}

    try:
        servers = await scraper.get_servers(target_url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gagal scraping sumber video: {exc}") from exc

    servers = _sanitize_servers(servers)
    target["servers"] = servers
    target["embeds"] = [
        s.get("stream") or s["embed"] for s in servers if s.get("working") is not False
    ]
    target["stale"] = not target["embeds"]
    target["checked_at"] = now_iso()
    try:
        save_db()
    except Exception:
        pass
    return {"slug": slug, "episode": ep, "url": target_url, "servers": servers, "cached": False}


@app.post("/api/scrape")
async def api_scrape(
    pages: int = Query(1, ge=1, le=50, description="Jumlah halaman katalog /series/"),
    details: bool = Query(True, description="Scrape juga halaman detail tiap series"),
    force: bool = Query(False, description="Refresh ulang detail yang sudah ada"),
) -> dict:
    """Crawl katalog /series/ dan isi cache lokal."""
    added = 0
    slugs: list[str] = []

    for p in range(1, pages + 1):
        items = await scraper.get_series_list(p)
        for it in items:
            existed = it["slug"] in db
            upsert_item(db, it)
            if not existed:
                added += 1
            slugs.append(it["slug"])
    save_db()

    details_scraped = 0
    failures: list[dict] = []
    if details:
        targets = []
        seen = set()
        for slug in slugs:
            if slug in seen:
                continue
            seen.add(slug)
            item = db[slug]
            needs = force or not item.get("synopsis") or not item.get("last_scraped_at")
            if needs:
                targets.append(item)

        async def enrich(entry: dict[str, Any]) -> None:
            nonlocal details_scraped
            try:
                detail = await scraper.get_series_detail(entry["url"])
                detail["slug"] = entry["slug"]
                upsert_item(db, detail)
                details_scraped += 1
            except Exception as exc:
                failures.append({"slug": entry["slug"], "error": str(exc)})

        await asyncio.gather(*(enrich(t) for t in targets))
        save_db()

    return {
        "status": "done",
        "pages_scraped": pages,
        "items_found": len(slugs),
        "added": added,
        "details_scraped": details_scraped,
        "failures": failures,
        "total_cached": len(db),
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
