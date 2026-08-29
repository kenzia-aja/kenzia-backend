import asyncio
import base64
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

BASE_URL = "http://45.11.57.188"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
CHALLENGE_MARKER = "verify_human"
EPISODE_SLUG_RE = re.compile(r"^(?P<series>.+)-episode-\d+$")
# Server yang diblokir (embed-nya sering error/rusak) â€” disingkirkan dari daftar.
BLOCKED_HOSTS = ("minochinos.com", "filelions", "filelions.com")
BLOCKED_NAMES = ("filelions",)
# Tipe series yang diblokir: variety show (ratusan episode, tidak fokus) & special.
# Website hanya fokus: movie / drama (series) / anime.
BLOCKED_SERIES_TYPES = ("tv show", "variety show", "variety", "special")
# Server yang dijadikan default (paling depan saat dipilih user).
PREFERRED_SERVERS = ("hydrax",)


def _server_sort_key(s: dict) -> tuple:
    """Kunci urutan server: preferred (Hydrax) -> bebas iklan (HLS) -> lainnya; mati paling akhir."""
    name = (s.get("name") or "").lower()
    return (
        0 if any(p in name for p in PREFERRED_SERVERS) else 1,
        s.get("stream") is None,
        s.get("working") is False,
    )

log = logging.getLogger("scraper")


class ScrapeError(Exception):
    pass


def slug_from_url(url: str) -> str:
    return urlparse(url).path.strip("/")


def infer_series_slug(episode_slug: str) -> Optional[str]:
    m = EPISODE_SLUG_RE.match(episode_slug)
    return m.group("series") if m else None


def extract_episode_number(
    raw_num: Optional[str] = None,
    raw_title: Optional[str] = None,
    url: Optional[str] = None,
) -> Optional[int]:
    """Extract integer episode number using regex across multiple fields."""
    if raw_num:
        m = re.search(r"(\d+)", raw_num)
        if m:
            return int(m.group(1))
    if raw_title:
        m = re.search(r"(?:Episode|Ep\.?|E)\s*(\d+)", raw_title, re.IGNORECASE)
        if m:
            return int(m.group(1))
    if url:
        m = re.search(r"-episode-(\d+)", url, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def parse_ep_number(label: Optional[str]) -> Optional[int]:
    if not label:
        return None
    return extract_episode_number(raw_num=label, raw_title=label)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OppadramaScraper:
    def __init__(self, base_url: str = BASE_URL, concurrency: int = 4, retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
        )
        self._sem = asyncio.Semaphore(concurrency)
        self._verified = False
        self.retries = retries

    async def close(self) -> None:
        await self._client.aclose()

    async def _verify(self) -> None:
        r1 = await self._client.get(f"{self.base_url}/")
        if CHALLENGE_MARKER in r1.text:
            await self._client.get(f"{self.base_url}/?verify_human=1")
        self._verified = True

    async def _fetch(self, url: str, params: Optional[dict] = None) -> httpx.Response:
        delay = 1.0
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                async with self._sem:
                    if not self._verified:
                        await self._verify()
                    resp = await self._client.get(url, params=params)
                    resp.raise_for_status()
                    if len(resp.text) < 2000 and CHALLENGE_MARKER in resp.text:
                        await self._verify()
                        resp = await self._client.get(url, params=params)
                        resp.raise_for_status()
                    return resp
            except httpx.HTTPError as exc:
                last_exc = exc
                log.warning("Percobaan %d/%d gagal untuk %s: %s", attempt, self.retries, url, exc)
                if attempt < self.retries:
                    await asyncio.sleep(delay)
                    delay *= 2
        raise last_exc  # type: ignore[misc]

    async def get_soup(self, url: str, params: Optional[dict] = None) -> BeautifulSoup:
        resp = await self._fetch(url, params=params)
        return BeautifulSoup(resp.text, "html.parser")

    @staticmethod
    def _parse_card(a_tag) -> Optional[dict[str, Any]]:
        if a_tag is None:
            return None
        href = a_tag.get("href")
        if not href:
            return None
        img = a_tag.select_one("img")
        poster = None
        if img is not None:
            poster = img.get("src") or img.get("data-lazy-src")
        typez = a_tag.select_one(".typez")
        epx = a_tag.select_one(".epx")
        tt = a_tag.select_one(".tt")
        title = a_tag.get("title") or (tt.get_text(" ", strip=True) if tt else "")
        slug = slug_from_url(href)
        return {
            "slug": slug,
            "url": href,
            "title": title.strip(),
            "type": typez.get_text(strip=True) if typez else None,
            "label": epx.get_text(strip=True) if epx else None,
            "poster": poster,
            "series_slug": infer_series_slug(slug),
        }

    async def get_schedule(self) -> list[dict[str, Any]]:
        """Jadwal rilis mingguan dari /jadwal/: daftar hari berisi series."""
        soup = await self.get_soup(f"{self.base_url}/jadwal/")
        schedule: list[dict[str, Any]] = []
        for box in soup.select(".bixbox"):
            h = box.select_one(".releases h3 span, .releases h1 span")
            if h is None:
                continue
            day = h.get_text(strip=True)
            if not day or day.lower() == "jadwal":
                continue
            items = []
            for card in box.select(".bsx"):
                a = card.find("a", href=True)
                if a is None:
                    continue
                slug = slug_from_url(a["href"])
                if slug.startswith("series/"):
                    slug = slug[len("series/"):]
                img = card.select_one("img")
                epx = card.select_one(".epx")
                sb = card.select_one(".sb")
                items.append(
                    {
                        "slug": slug,
                        "url": a["href"],
                        "title": a.get("title") or a.get_text(strip=True),
                        "poster": (img.get("src") or img.get("data-lazy-src")) if img else None,
                        "release_status": epx.get_text(strip=True) if epx else None,
                        "episode": sb.get_text(strip=True) if sb else None,
                    }
                )
            schedule.append({"day": day, "items": items})
        return schedule

    async def get_latest(self, page: int = 1) -> list[dict[str, Any]]:
        url = f"{self.base_url}/" if page <= 1 else f"{self.base_url}/page/{page}/"
        soup = await self.get_soup(url)
        results = []
        for art in soup.select("article.bs"):
            card = self._parse_card(art.select_one("a.tip"))
            if card:
                timeago = art.select_one(".timeago")
                card["posted"] = timeago.get_text(strip=True) if timeago else None
                results.append(card)
        return results

    async def get_series_list(
        self,
        page: int = 1,
        status: Optional[str] = None,
        type_: Optional[str] = None,
        country: Optional[str] = None,
        genre: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "page": page,
            "status": status or "",
            "type": type_ or "",
            "order": "update",
        }
        if country:
            c = country.strip().lower()
            if c in ("barat", "west", "western", "usa", "us", "amerika"):
                params["country[]"] = "united-states"
            else:
                params["country[]"] = c.replace(" ", "-")
        if genre:
            params["genre[0]"] = genre.strip().lower().replace(" ", "-")
        soup = await self.get_soup(f"{self.base_url}/series/", params=params)
        results = []
        for art in soup.select("article.bs"):
            card = self._parse_card(art.select_one("a.tip"))
            if card:
                results.append(card)
        return results

    async def search(self, q: str, page: int = 1) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"s": q}
        if page > 1:
            params["page"] = page
        soup = await self.get_soup(f"{self.base_url}/", params=params)
        results = []
        for art in soup.select("article.bs"):
            card = self._parse_card(art.select_one("a.tip"))
            if card:
                results.append(card)
        return results

    @staticmethod
    def _spe_map(spe_el) -> dict[str, str]:
        result = {}
        for span in spe_el.find_all("span"):
            b = span.find("b")
            if b is None:
                continue
            key = b.get_text(strip=True).rstrip(":").lower()
            links = [a.get_text(strip=True) for a in span.find_all("a")]
            if links:
                value = ", ".join(links)
            else:
                value = span.get_text(" ", strip=True)
                value = value.replace(b.get_text(strip=True), "").strip()
            if value:
                result[key] = value
        return result

    async def get_series_detail(self, url: str) -> dict[str, Any]:
        soup = await self.get_soup(url)
        infox = soup.select_one(".infox")
        if infox is None:
            raise ScrapeError(f"Halaman series tidak dikenali: {url}")

        h1 = soup.select_one(".entry-title") or infox.find("h1")
        title = h1.get_text(strip=True) if h1 else None

        spe_el = soup.select_one(".spe")
        spe = self._spe_map(spe_el) if spe_el else {}

        genres = [
            a.get_text(strip=True)
            for sel in (".gentag a", ".genxed a", 'a[href*="/genre/"]')
            for a in soup.select(sel)
        ]

        synopsis_el = (
            soup.select_one(".mindesc")
            or soup.select_one(".entry-content")
            or soup.select_one(".synopsis")
        )
        synopsis = synopsis_el.get_text(" ", strip=True) if synopsis_el else None

        poster = None
        og = soup.find("meta", attrs={"property": "og:image"})
        if og and og.get("content"):
            poster = og["content"]

        rating = None
        rating_el = soup.select_one(".rating")
        if rating_el:
            m = re.search(r"\d+(?:\.\d+)?", rating_el.get_text())
            if m:
                rating = m.group(0)

        items = soup.select(".eplister ul li")
        single = len(items) == 1
        episodes = []
        for idx, li in enumerate(items, start=1):
            a = li.find("a", href=True)
            if a is None:
                continue
            num_el = li.select_one(".epl-num")
            t_el = li.select_one(".epl-title")
            d_el = li.select_one(".epl-date")
            raw_num = num_el.get_text(strip=True) if num_el else ""
            raw_title = t_el.get_text(strip=True) if t_el else ""
            ep_url = a["href"]

            number = extract_episode_number(raw_num, raw_title, ep_url)
            if number is None and single:
                number = 1
            episodes.append(
                {
                    "number": number,
                    "title": raw_title if raw_title else None,
                    "date": d_el.get_text(strip=True) if d_el else None,
                    "url": ep_url,
                    "servers": [],
                    "embeds": [],
                    "stale": False,
                }
            )

        cast_raw = spe.get("artis")
        return {
            "slug": slug_from_url(url),
            "url": url,
            "title": title,
            "poster": poster,
            "rating": rating,
            "status": spe.get("status"),
            "network": spe.get("network"),
            "released": spe.get("dirilis"),
            "country": spe.get("negara"),
            "type": spe.get("tipe"),
            "total_episodes": spe.get("episode"),
            "director": spe.get("sutradara"),
            "cast": [c.strip() for c in cast_raw.split(",")] if cast_raw else [],
            "genres": sorted(set(g for g in genres if g)),
            "synopsis": synopsis,
            "episodes": episodes,
            "last_scraped_at": now_iso(),
        }

    @staticmethod
    def _parse_server_options(soup: BeautifulSoup, page_url: str) -> list[dict[str, Any]]:
        servers: list[dict[str, Any]] = []
        seen: set[str] = set()
        for opt in soup.select("option[value][data-index]"):
            raw = opt.get("value") or ""
            name = opt.get_text(strip=True) or f"Server {opt.get('data-index')}"
            try:
                decoded = base64.b64decode(raw).decode("utf-8", "ignore")
            except Exception:
                continue
            m = re.search(r'src=["\']([^"\']+)["\']', decoded, re.I)
            if not m:
                continue
            embed = urljoin(page_url, m.group(1))
            if embed in seen:
                continue
            seen.add(embed)
            servers.append({"name": name, "embed": embed, "stream": None, "working": None})
        return servers

    @staticmethod
    def _parse_plain_iframe(soup: BeautifulSoup, page_url: str) -> list[dict[str, Any]]:
        servers: list[dict[str, Any]] = []
        for frame in soup.select("iframe"):
            src = frame.get("src") or frame.get("data-litespeed-src") or frame.get("data-src")
            if not src:
                continue
            if src.startswith("//"):
                src = "https:" + src
            embed = urljoin(page_url, src)
            servers.append({"name": "Default", "embed": embed, "stream": None, "working": None})
        return servers

    @staticmethod
    def _is_blocked(name: str, embed: str) -> bool:
        """True bila server berada dalam daftar blokir (FileLions dkk)."""
        host = (urlparse(embed).hostname or "").lower()
        low_name = (name or "").lower()
        return any(h in host for h in BLOCKED_HOSTS) or any(b in low_name for b in BLOCKED_NAMES)

    async def get_servers(self, url: str) -> list[dict[str, Any]]:
        """Kembalikan daftar server video untuk satu halaman episode/movie.

        Setiap item: {name, embed, stream, working}.
        stream = URL .m3u8 langsung (saat ini TIDAK ADA yang bisa di-resolve â€”
                 m3u8 `data-hash` TurboVIP ternyata decoy anti-scrape yang
                 menunjuk file PNG, bukan segmen video. Maka semua server
                 diputar sebagai embed iframe).
        working = None bila belum dicek, True/False hasil pengecekan.
        """
        soup = await self.get_soup(url)
        servers = self._parse_server_options(soup, url)
        if not servers:
            servers = self._parse_plain_iframe(soup, url)

        # Singkirkan server yang diblokir (FileLions dkk) sebelum diproses/ditimpa.
        servers = [s for s in servers if not self._is_blocked(s.get("name", ""), s.get("embed", ""))]

        for server in servers:
            try:
                # request langsung (tanpa raise_for_status) agar status bisa dinilai
                async with self._sem:
                    resp = await self._client.get(server["embed"])
                if resp.status_code == 200:
                    server["working"] = True
                elif resp.status_code in (404, 410):
                    server["working"] = False  # pasti mati
                else:
                    # 403 (bot protection), 5xx, dsb: tidak diketahui â€” jangan dianggap mati
                    server["working"] = None
            except Exception:
                # timeout / koneksi gagal: tidak diketahui â€” akan di-retry run berikutnya
                server["working"] = None

        # Tandai risiko iklan per server:
        #   - server dengan `stream` (HLS) diputar via hls.js/Plyr milik kita sendiri -> BEBAS iklan.
        #   - server embed iframe pihak ketiga (TurboVip, Hydrax, dll) -> potensial BERIKLAN.
        for server in servers:
            server["ads"] = server.get("stream") is None

        # Urutkan: server pilihan user (Hydrax) paling depan, lalu bebas iklan (HLS),
        # lalu iframe lain; yang mati di belakang.
        servers.sort(key=_server_sort_key)
        return servers


def upsert_item(db: dict[str, dict], data: dict[str, Any]) -> dict[str, Any]:
    current = db.setdefault(data["slug"], {})
    episodes_incoming = data.pop("episodes", None)
    for key, value in data.items():
        if value is not None:
            current[key] = value
    current.setdefault("episodes", [])
    if episodes_incoming is not None:
        merge_episodes(current, episodes_incoming)
    return current


def _clean_episode_url(url: str) -> str:
    """Remove common suffixes that indicate duplicate/repost episodes.

    Hanya hapus suffix repost yang benar-benar duplikat:
      - `-nomorsesuaiottviu`
      - angka `-2..-5` yang MENGIKUTI nama episode (re-post), contoh:
        `xxx-episode-2-2` -> `xxx-episode-2`, karena `-2` kedua adalah tanda repost.
      - setelah `-episode-{n}` yang juga diakhiri angka runut (mis. `xxx-episode-4-2`).
    Jangan pernah memotong angka dari nama episode legit (`-episode-2` tetap utuh).
    """
    cleaned = url.rstrip("/")
    if cleaned.endswith("-nomorsesuaiottviu"):
        cleaned = cleaned[: -len("-nomorsesuaiottviu")]
    else:
        # tanda repost: URL berbentuk `...-episode-{n}-{repost}` (contoh
        # `xxx-episode-4-2` = duplikat postingan episode 4). Hapus segmen repost
        # saja, jangan menyentuh nama episode.
        cleaned = re.sub(r"(-episode-\d+)(?:-\d+)+$", r"\1", cleaned)
        # jatuhkan `-nomorsesuaiottviu` bila masih tersisa (mis. trailing setelah repost)
        if cleaned.endswith("-nomorsesuaiottviu"):
            cleaned = cleaned[: -len("-nomorsesuaiottviu")]
    return cleaned + "/"


def merge_episodes(series: dict[str, Any], incoming: list[dict[str, Any]]) -> bool:
    eps = series.setdefault("episodes", [])
    changed = False

    for ep in eps + incoming:
        if ep.get("number") is None:
            ep["number"] = extract_episode_number(None, ep.get("title"), ep.get("url"))

    by_clean_url = {_clean_episode_url(e.get("url", "")): i for i, e in enumerate(eps)}
    by_num: dict[int, int] = {}
    for i, e in enumerate(eps):
        n = e.get("number")
        if n is not None:
            by_num[n] = i

    for inc in incoming:
        inc_url = inc.get("url", "")
        inc_clean = _clean_episode_url(inc_url)
        inc_num = inc.get("number")

        existing_idx = None
        if inc_num is not None and inc_num in by_num:
            existing_idx = by_num[inc_num]
        elif inc_clean in by_clean_url:
            existing_idx = by_clean_url[inc_clean]

        if existing_idx is not None:
            existing = eps[existing_idx]
            if existing.get("number") is None and inc_num is not None:
                existing["number"] = inc_num
                changed = True
            if not existing.get("date") and inc.get("date"):
                existing["date"] = inc["date"]
            if existing.get("url", "").endswith("-nomorsesuaiottviu") and not inc_url.endswith("-nomorsesuaiottviu"):
                existing["url"] = inc_url
                changed = True
        else:
            new_ep = {
                "number": inc_num,
                "title": inc.get("title"),
                "date": inc.get("date"),
                "url": inc_url,
                "servers": [],
                "embeds": [],
                "stale": False,
                "first_seen_at": now_iso(),
            }
            eps.append(new_ep)
            by_clean_url[inc_clean] = len(eps) - 1
            if inc_num is not None:
                by_num[inc_num] = len(eps) - 1
            changed = True

    deduped = dedup_episodes_list(eps)
    if len(deduped) != len(eps):
        series["episodes"] = deduped
        changed = True
    else:
        series["episodes"] = deduped
    return changed


def dedup_episodes_list(episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_num: dict[int, dict[str, Any]] = {}
    seen_url: set[str] = set()
    result: list[dict[str, Any]] = []

    def sort_score(e: dict) -> int:
        """Skor 1 = URL repost/duplikat (berubah setelah dibersihkan), prioritas lebih rendah."""
        u = e.get("url", "")
        if _clean_episode_url(u) != u.rstrip("/") + "/":
            return 1
        return 0

    for ep in sorted(episodes, key=sort_score):
        num = ep.get("number")
        url = ep.get("url", "")
        clean_u = _clean_episode_url(url)

        if num is not None:
            if num in seen_num:
                continue
            seen_num[num] = ep
            result.append(ep)
        else:
            if clean_u in seen_url:
                continue
            seen_url.add(clean_u)
            result.append(ep)

    sort_episodes(result)
    return result


def sort_episodes(eps: list[dict[str, Any]]) -> None:
    """Urutkan episode menaik (1, 2, ..., 36, 37); tanpa nomor di akhir."""
    eps.sort(key=lambda e: (e.get("number") is None, e.get("number") or 0))


async def run_cli(args) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    output_path = Path(args.output)
    db: dict[str, dict[str, Any]] = {}
    if output_path.exists():
        try:
            db = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("Output lama tidak valid, mulai dari kosong")

    scraper = OppadramaScraper(base_url=args.base_url, concurrency=args.concurrency)
    report: dict[str, Any] = {"started_at": now_iso(), "failures": []}
    failures = report["failures"]

    try:
        added = 0
        catalog_slugs: list[str] = []
        empty_pages = 0
        for p in range(1, args.catalog_pages + 1):
            items = await scraper.get_series_list(p)
            log.info("Katalog halaman %d: %d item", p, len(items))
            if not items:
                empty_pages += 1
                if empty_pages >= 3:
                    log.info("Katalog berakhir di halaman %d (3 halaman kosong berturut)", p - 3)
                    break
                continue
            empty_pages = 0
            for it in items:
                # Lewati variety show / special (website fokus movie-drama-anime)
                if (it.get("type") or "").strip().lower() in BLOCKED_SERIES_TYPES:
                    continue
                is_new = it["slug"] not in db
                if is_new:
                    added += 1
                    it["first_seen_at"] = now_iso()
                upsert_item(db, it)
                catalog_slugs.append(it["slug"])

        # Series homepage (16 Drama + 16 Movie terbaru) â€” dipakai untuk detail
        # re-scrape di bawah dan prioritas pengisian server.
        def _newest_slugs(ttype: str, n: int) -> set[str]:
            rows = [
                s
                for s in db.values()
                if (s.get("type") or "").strip().lower() == ttype
            ]
            rows.sort(
                key=lambda s: s.get("first_seen_at")
                or s.get("last_scraped_at")
                or "",
                reverse=True,
            )
            return {s.get("slug") for s in rows[:n]}

        def _top_rated_slugs(ttype: str, n: int) -> set[str]:
            """Untuk section Rekomendasi (rating tertinggi) di homepage."""
            rows = [
                s
                for s in db.values()
                if (s.get("type") or "").strip().lower() == ttype
            ]
            rows.sort(
                key=lambda s: float(s.get("rating") or 0),
                reverse=True,
            )
            return {s.get("slug") for s in rows[:n]}

        homepage_slugs = (
            _newest_slugs("drama", 16)
            | _newest_slugs("movie", 16)
            | _top_rated_slugs("movie", 15)
            | _top_rated_slugs("drama", 15)
        )

        details_scraped = 0
        targets = []
        seen = set()
        for slug in catalog_slugs:
            if slug in seen:
                continue
            seen.add(slug)
            item = db[slug]
            needs = args.force_details or not item.get("synopsis") or not item.get("last_scraped_at")
            if needs:
                targets.append(item)

        # Series homepage yang belum punya episode â†’ paksa re-scrape detail,
        # agar baris "Update Series/Film" di homepage selalu bisa diputar.
        for slug in homepage_slugs:
            item = db.get(slug)
            if item and slug not in seen and not item.get("episodes"):
                seen.add(slug)
                targets.append(item)

        async def enrich(item: dict[str, Any]) -> None:
            nonlocal details_scraped
            try:
                detail = await scraper.get_series_detail(item["url"])
                detail["slug"] = item["slug"]
                incoming_eps = detail.pop("episodes", None)
                for key, value in detail.items():
                    if value is not None and key != "slug":
                        item[key] = value
                if incoming_eps:
                    merge_episodes(item, incoming_eps)
                details_scraped += 1
            except Exception as exc:
                failures.append({"stage": "detail", "slug": item["slug"], "error": str(exc)})
                log.warning("Detail gagal: %s (%s)", item["slug"], exc)

        await asyncio.gather(*(enrich(t) for t in targets))
        log.info("Detail di-scrape: %d item", details_scraped)

        episodes_merged = 0
        if args.latest_pages > 0:
            for p in range(1, args.latest_pages + 1):
                latest = await scraper.get_latest(p)
                log.info("Latest halaman %d: %d item", p, len(latest))
                for card in latest:
                    sslug = card.get("series_slug")
                    num = parse_ep_number(card.get("label"))
                    series = db.get(sslug) if sslug else None
                    if series is None or num is None:
                        continue
                    before = sum(1 for e in series.get("episodes", []) if e.get("url") == card["url"])
                    merged = merge_episodes(
                        series,
                        [{"number": num, "title": card["title"], "date": None, "url": card["url"]}],
                    )
                    if merged and before == 0:
                        episodes_merged += 1
            log.info("Episode baru dari latest: %d", episodes_merged)

        sources_fetched = 0
        if args.sources_newest > 0:
            # homepage_slugs sudah dihitung di atas (16 Drama + 16 Movie terbaru).
            # Untuk series homepage (termasuk variety show ratusan episode),
            # hanya episode TERBARU (maks 6) yang diprioritaskan agar budget
            # tidak habis di episode lama yang jarang ditonton.
            _fresh_cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
            homepage_recent: dict[str, set[str]] = {}
            for s in db.values():
                if s.get("slug") in homepage_slugs:
                    eps_desc = sorted(
                        s.get("episodes") or [],
                        key=lambda e: -(e.get("number") or 0),
                    )
                    homepage_recent[s.get("slug")] = {
                        e.get("url") for e in eps_desc[:6]
                    }

            def _server_priority(series: dict[str, Any], ep: dict[str, Any]) -> tuple:
                """Prioritas pengisian server:
                0 = series homepage / episode BARU (<=48 jam) â€” langsung bisa ditonton
                1 = drama pendek (<40 episode) â€” China/Korea/Japan/dll
                2 = movie (1 video)
                3 = series panjang (40+ episode) â€” paling akhir
                """
                eps_count = len(series.get("episodes") or [])
                stype = (series.get("type") or "").lower()
                if series.get("slug") in homepage_slugs:
                    return (0,)
                if stype == "movie" or eps_count <= 1:
                    g = 2
                elif eps_count < 40:
                    g = 1
                else:
                    g = 3
                # Episode yang baru ditemukan (<=48 jam) naik ke prioritas tertinggi
                fs = ep.get("first_seen_at") or ""
                if fs and fs >= _fresh_cutoff:
                    return (0,)
                return (g,)

            pending: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for series in db.values():
                slug = series.get("slug")
                only_recent = homepage_recent.get(slug)
                for ep in sorted(
                    series.get("episodes", []),
                    key=lambda e: (e.get("number") is None, -(e.get("number") or 0)),
                ):
                    if ep.get("embeds") or ep.get("stale"):
                        continue
                    # series homepage: lewati episode lama (bukan 6 terbaru)
                    if only_recent is not None and ep.get("url") not in only_recent:
                        continue
                    pending.append((series, ep))
            # urutkan: homepage -> drama pendek -> movie -> series panjang
            pending.sort(key=lambda p: _server_priority(p[0], p[1]))
            pending = pending[: args.sources_newest]
            log.info("Mengambil embed untuk %d episode", len(pending))

            async def fetch_embeds(pair: tuple[dict[str, Any], dict[str, Any]]) -> None:
                nonlocal sources_fetched
                _, ep = pair
                try:
                    servers = await scraper.get_servers(ep["url"])
                    ep["servers"] = servers
                    # embeds hanya server yang DEFINITIF jalan (True).
                    # working=None (5xx/timeout) â†’ dikeluarkan agar episode
                    # tetap pending dan di-retry run berikutnya.
                    ep["embeds"] = [
                        s.get("stream") or s["embed"]
                        for s in servers
                        if s.get("working") is True
                    ]
                    # stale (mati) HANYA bila semua server dicek dan semuanya
                    # definitif gagal (False) â€” 5xx/timeout TIDAK membuat stale
                    if servers:
                        ep["stale"] = all(s.get("working") is False for s in servers)
                    else:
                        ep["stale"] = True  # halaman tanpa opsi server = mati
                    ep["checked_at"] = now_iso()
                    if ep["embeds"]:
                        sources_fetched += 1
                except Exception as exc:
                    failures.append({"stage": "sources", "slug": ep.get("url"), "error": str(exc)})

            # Probe: coba hingga 3 episode berbeda. Baru skip tahap bila SEMUA gagal
            # (satu 503 sesaat tidak boleh membuang seluruh run).
            if pending:
                candidates = [pending[0]]
                if len(pending) > 2:
                    candidates.append(pending[len(pending) // 2])
                if len(pending) > 1:
                    candidates.append(pending[-1])

                def _apply(pair: tuple[dict[str, Any], dict[str, Any]], servers: list[dict[str, Any]]) -> None:
                    _, ep = pair
                    ep["servers"] = servers
                    ep["embeds"] = [
                        s.get("stream") or s["embed"]
                        for s in servers
                        if s.get("working") is True
                    ]
                    if servers:
                        ep["stale"] = all(s.get("working") is False for s in servers)
                    else:
                        ep["stale"] = True
                    ep["checked_at"] = now_iso()

                probe_ok = False
                for ps, pe in candidates:
                    try:
                        probe = await scraper.get_servers(pe["url"])
                        if probe:
                            _apply((ps, pe), probe)
                            if any(s.get("working") is True for s in probe):
                                sources_fetched += 1
                            probe_ok = True
                            log.info("Probe sukses pada %s â€” lanjut tahap sources", pe["url"])
                            break
                        log.warning("Probe: 0 server dari %s â€” coba kandidat lain", pe["url"])
                    except Exception as exc:
                        log.warning("Probe gagal untuk %s: %s", pe["url"], exc)
                if not probe_ok:
                    log.warning("Semua probe gagal â€” lewati tahap sources run ini")
                    pending = []

            await asyncio.gather(*(fetch_embeds(pair) for pair in pending))

        report.update(
            {
                "finished_at": now_iso(),
                "total_series": len(db),
                "added": added,
                "details_scraped": details_scraped,
                "episodes_merged": episodes_merged,
                "sources_fetched": sources_fetched,
            }
        )

        output_path.parent.mkdir(exist_ok=True)
        output_path.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
        report_path = output_path.parent / "scrape_report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        log.info(
            "SELESAI | total=%d baru=%d detail=%d ep_baru=%d embeds=%d gagal=%d",
            len(db), added, details_scraped, episodes_merged, sources_fetched, len(failures),
        )
        return 0
    finally:
        await scraper.close()


def build_cli() -> "argparse.ArgumentParser":
    import argparse

    parser = argparse.ArgumentParser(description="Scraper katalog film (mode CLI untuk cron/CI)")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--catalog-pages", type=int, default=3, help="Jumlah halaman katalog /series/")
    parser.add_argument("--latest-pages", type=int, default=1, help="Halaman homepage untuk deteksi episode baru")
    parser.add_argument("--sources-newest", type=int, default=0, help="Ambil embed untuk N episode terbaru yang belum ada")
    parser.add_argument("--force-details", action="store_true", help="Refresh semua detail meski sudah ada")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output", default="data/db.json")
    return parser


if __name__ == "__main__":
    sys.exit(asyncio.run(run_cli(build_cli().parse_args())))
