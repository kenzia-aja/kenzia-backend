"""Unit test untuk logika scraper yang rawan bug.

Regression terpenting: _clean_episode_url yang dulu memotong "-2" dari
episode LEGIT (the-early-spring-episode-2) sehingga 443 episode hilang.

Jalankan: python -m pytest test_scraper.py -v
"""
import asyncio
import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from scraper import (  # noqa: E402
    _clean_episode_url,
    _server_sort_key,
    dedup_episodes_list,
    extract_episode_number,
    is_recent,
    merge_episodes,
    parse_ep_number,
    slug_from_url,
    infer_series_slug,
    sort_episodes,
    upsert_item,
    OppadramaScraper,
    ScrapeError,
)
from migrate_db import analyze, migrate  # noqa: E402
from restore_from_supabase import build_db  # noqa: E402
from sync_supabase import (  # noqa: E402
    aggregate_counts,
    build_episode_rows,
    build_series_row,
    series_has_video,
    upsert,
)


# ── _clean_episode_url ──


class TestCleanEpisodeUrl:
    def test_episode_legit_tidak_terpotong(self):
        """Regression: -2/-3/-4/-5 adalah NOMOR episode, bukan tanda repost."""
        for n in range(1, 11):
            url = f"http://x.com/the-early-spring-episode-{n}/"
            assert _clean_episode_url(url) == url, f"episode-{n} terpotong!"

    def test_repost_dihapus(self):
        assert (
            _clean_episode_url("http://x.com/xxx-episode-4-2/")
            == "http://x.com/xxx-episode-4/"
        )
        assert (
            _clean_episode_url("http://x.com/a-b-episode-30-2-3/")
            == "http://x.com/a-b-episode-30/"
        )
        # repost berapa pun angkanya (bukan cuma -2..-5)
        assert (
            _clean_episode_url("http://x.com/a-episode-9-7/")
            == "http://x.com/a-episode-9/"
        )

    def test_nomorsesuaiottviu_dihapus(self):
        assert (
            _clean_episode_url("http://x.com/xxx-episode-1-nomorsesuaiottviu/")
            == "http://x.com/xxx-episode-1/"
        )

    def test_nomorsesuaiottviu_plus_repost(self):
        assert (
            _clean_episode_url("http://x.com/xxx-episode-1-nomorsesuaiottviu-2/")
            == "http://x.com/xxx-episode-1/"
        )

    def test_tanpa_trailing_slash(self):
        assert (
            _clean_episode_url("http://x.com/xxx-episode-4-2")
            == "http://x.com/xxx-episode-4/"
        )

    def test_bukan_episode_tetap_utuh(self):
        url = "http://x.com/movie-title-2/"
        assert _clean_episode_url(url) == url

    def test_idempoten(self):
        once = _clean_episode_url("http://x.com/xxx-episode-4-2/")
        assert _clean_episode_url(once) == once


# ── extract_episode_number ──


class TestExtractEpisodeNumber:
    def test_dari_url(self):
        assert (
            extract_episode_number(None, None, "http://x.com/foo-episode-12/")
            == 12
        )

    def test_dari_url_case_insensitive(self):
        assert extract_episode_number(None, None, "http://x.com/foo-Episode-7/") == 7

    def test_dari_title(self):
        assert extract_episode_number(None, "The Early Spring Episode 6", None) == 6

    def test_dari_raw_num(self):
        assert extract_episode_number("36 END", None, None) == 36
        assert extract_episode_number("E5", None, None) == 5
        assert extract_episode_number("7", None, None) == 7

    def test_raw_num_menang_dari_title(self):
        assert extract_episode_number("9", "Episode 3", None) == 9

    def test_priority_title_sebelum_url(self):
        assert (
            extract_episode_number(None, "Episode 3", "http://x.com/foo-episode-99/")
            == 3
        )

    def test_tidak_ada_nomor(self):
        assert extract_episode_number(None, "Movie BluRay", "http://x.com/movie/") is None

    def test_semua_none(self):
        assert extract_episode_number(None, None, None) is None

    def test_string_kosong(self):
        assert extract_episode_number("", "", "") is None

    def test_bukan_digit_pertama_title(self):
        """Angka di title tanpa kata Episode tidak diambil dari jalur title."""
        assert extract_episode_number(None, "Movie 2019 BluRay", None) is None


class TestParseEpNumber:
    def test_label_biasa(self):
        assert parse_ep_number("Ep 8") == 8
        assert parse_ep_number("Episode 12") == 12
        assert parse_ep_number("2") == 2

    def test_label_kosong(self):
        assert parse_ep_number(None) is None
        assert parse_ep_number("") is None


# ── slug helpers ──


class TestSlugHelpers:
    def test_slug_from_url(self):
        assert slug_from_url("http://45.11.57.188/the-early-spring/") == "the-early-spring"

    def test_slug_from_relative(self):
        assert slug_from_url("/the-early-spring/") == "the-early-spring"

    def test_infer_series_slug(self):
        assert infer_series_slug("the-early-spring-episode-7") == "the-early-spring"

    def test_infer_series_slug_movie(self):
        assert infer_series_slug("movie-foo-2020-bluray") is None

    def test_infer_series_slug_bukan_episode(self):
        assert infer_series_slug("the-early-spring") is None


# ── is_recent ──


class TestIsRecent:
    def test_baru_saja(self):
        ts = datetime.now(timezone.utc).isoformat()
        assert is_recent(ts, 48) is True

    def test_lebih_tua_dari_cutoff(self):
        ts = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        assert is_recent(ts, 48) is False

    def test_format_Z(self):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert is_recent(ts, 48) is True

    def test_naive_timestamp(self):
        ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        assert is_recent(ts, 48) is True

    def test_none_dan_sampah(self):
        assert is_recent(None, 48) is False
        assert is_recent("", 48) is False
        assert is_recent("bukan-timestamp", 48) is False


# ── merge_episodes (regression the-early-spring) ──


def _ep(n: int, base: str = "http://45.11.57.188/foo") -> dict:
    return {
        "number": n,
        "title": f"Foo Episode {n}",
        "date": None,
        "url": f"{base}-episode-{n}/",
        "servers": [],
        "embeds": [],
        "stale": False,
    }


class TestMergeEpisodes:
    def test_merge_tidak_membuang_episode_legit(self):
        """Regression the-early-spring: existing ep1+ep4, incoming 6 episode
        → hasil harus 6, bukan 3 (dulu ep 2,3,5 hilang karena URL dibersihkan
        salah sasaran)."""
        current = {"episodes": [_ep(1), _ep(4)]}
        incoming = [_ep(n) for n in (6, 5, 4, 3, 2, 1)]

        merge_episodes(current, incoming)

        numbers = sorted(e["number"] for e in current["episodes"])
        assert numbers == [1, 2, 3, 4, 5, 6]

    def test_episode_repost_tidak_duplikat(self):
        current = {"episodes": [_ep(4)]}
        repost = dict(_ep(4))
        repost["url"] = "http://45.11.57.188/foo-episode-4-2/"
        merge_episodes(current, [repost])
        assert len(current["episodes"]) == 1
        assert current["episodes"][0]["url"] == "http://45.11.57.188/foo-episode-4/"

    def test_swap_url_repost_dengan_trailing_slash(self):
        """Regression: URL sumber diakhiri '/', endswith(REPOST) dulu selalu
        False → URL repost tidak pernah ditukar dengan URL asli."""
        current = {
            "episodes": [
                {
                    **_ep(1),
                    "url": "http://x.com/foo-episode-1-nomorsesuaiottviu/",
                }
            ]
        }
        merge_episodes(current, [_ep(1)])
        assert current["episodes"][0]["url"] == "http://45.11.57.188/foo-episode-1/"

    def test_nomor_null_terisi_dari_incoming(self):
        current = {"episodes": [{**_ep(1), "number": None}]}
        changed = merge_episodes(current, [_ep(1)])
        assert changed is True
        assert current["episodes"][0]["number"] == 1

    def test_tanggal_diisi_bila_kosong(self):
        current = {"episodes": [_ep(1)]}
        inc = {**_ep(1), "date": "2026-08-30"}
        merge_episodes(current, [inc])
        assert current["episodes"][0]["date"] == "2026-08-30"

    def test_tanggal_lama_tidak_ditimpa(self):
        current = {"episodes": [{**_ep(1), "date": "2026-01-01"}]}
        inc = {**_ep(1), "date": "2026-08-30"}
        merge_episodes(current, [inc])
        assert current["episodes"][0]["date"] == "2026-01-01"

    def test_episode_baru_dapat_first_seen(self):
        current = {"episodes": []}
        merge_episodes(current, [_ep(1)])
        assert current["episodes"][0]["first_seen_at"]

    def test_noop_mengembalikan_false(self):
        current = {"episodes": [_ep(1)]}
        assert merge_episodes(current, [_ep(1)]) is False

    def test_gap_episode_tengah_terisi(self):
        """Kasus nyata the-early-spring: 1-6 ada, sumber rilis 8 & 10 → gap 7/9."""
        current = {"episodes": [_ep(n) for n in (1, 2, 3, 4, 5, 6)]}
        merge_episodes(current, [_ep(8)])
        merge_episodes(current, [_ep(10)])
        numbers = sorted(e["number"] for e in current["episodes"])
        assert numbers == [1, 2, 3, 4, 5, 6, 8, 10]
        merge_episodes(current, [_ep(7), _ep(9)])
        numbers = sorted(e["number"] for e in current["episodes"])
        assert numbers == list(range(1, 11))

    def test_incoming_tanpa_url_tetap_aman(self):
        current = {"episodes": [_ep(1)]}
        merge_episodes(current, [{"number": 2, "title": None, "date": None, "url": ""}])
        numbers = sorted(e["number"] for e in current["episodes"] if e["number"])
        assert numbers == [1, 2]

    def test_servers_existing_tidak_tertimpa(self):
        current = {
            "episodes": [{**_ep(1), "servers": [{"name": "A", "embed": "e"}], "embeds": ["e"]}]
        }
        merge_episodes(current, [_ep(1)])
        assert current["episodes"][0]["servers"] == [{"name": "A", "embed": "e"}]
        assert current["episodes"][0]["embeds"] == ["e"]


# ── dedup_episodes_list ──


class TestDedupEpisodesList:
    def test_duplikat_nomor_dibuang(self):
        eps = [_ep(1), dict(_ep(1)), _ep(2)]
        result = dedup_episodes_list(eps)
        assert sorted(e["number"] for e in result) == [1, 2]

    def test_tanpa_nomor_dedup_per_url_bersih(self):
        ep1 = {"number": None, "title": None, "date": None, "url": "http://x.com/xxx-episode-1/"}
        ep1_dup = dict(ep1)
        ep1_dup["url"] = "http://x.com/xxx-episode-1-2/"  # repost → bersih sama
        result = dedup_episodes_list([ep1, ep1_dup])
        assert len(result) == 1

    def test_null_vs_numbered_sama_url(self):
        """Regression: ep bernomor + ep null dengan URL sama harus jadi satu."""
        numbered = _ep(3)
        null_ep = {"number": None, "title": None, "date": None, "url": "http://45.11.57.188/foo-episode-3/"}
        result = dedup_episodes_list([numbered, null_ep])
        assert len(result) == 1
        assert result[0]["number"] == 3

    def test_repost_prioritas_lebih_rendah(self):
        repost = {**_ep(4), "url": "http://45.11.57.188/foo-episode-4-2/"}
        result = dedup_episodes_list([repost, _ep(4)])
        assert len(result) == 1
        assert result[0]["url"] == "http://45.11.57.188/foo-episode-4/"

    def test_urut_menaik_null_di_akhir(self):
        eps = [_ep(3), {"number": None, "title": "X", "date": None, "url": "http://x.com/x/"}, _ep(1)]
        result = dedup_episodes_list(eps)
        numbers = [e["number"] for e in result]
        assert numbers == [1, 3, None]

    def test_list_kosong(self):
        assert dedup_episodes_list([]) == []

    def test_semua_unik_tetap_utuh(self):
        eps = [_ep(n) for n in (5, 1, 3)]
        result = dedup_episodes_list(eps)
        assert [e["number"] for e in result] == [1, 3, 5]


class TestSortEpisodes:
    def test_menaik(self):
        eps = [_ep(3), _ep(1), _ep(2)]
        sort_episodes(eps)
        assert [e["number"] for e in eps] == [1, 2, 3]

    def test_null_di_akhir(self):
        eps = [{"number": None, "url": "u"}, _ep(1)]
        sort_episodes(eps)
        assert eps[0]["number"] == 1


# ── upsert_item ──


class TestUpsertItem:
    def test_series_baru(self):
        db = {}
        upsert_item(db, {"slug": "foo", "url": "http://x/foo/", "title": "Foo", "episodes": [_ep(1)]})
        assert db["foo"]["title"] == "Foo"
        assert [e["number"] for e in db["foo"]["episodes"]] == [1]

    def test_nilai_none_tidak_menimpa(self):
        db = {"foo": {"slug": "foo", "title": "Foo"}}
        upsert_item(db, {"slug": "foo", "title": None, "type": "Drama"})
        assert db["foo"]["title"] == "Foo"
        assert db["foo"]["type"] == "Drama"

    def test_upsert_berulang_idempoten(self):
        db = {}
        data = {"slug": "foo", "url": "http://x/foo/", "title": "Foo", "episodes": [_ep(1)]}
        upsert_item(db, dict(data, episodes=[dict(_ep(1))]))
        upsert_item(db, dict(data, episodes=[dict(_ep(1))]))
        assert len(db["foo"]["episodes"]) == 1


# ── parser HTML (MockTransport, tanpa jaringan) ──


CARD_HTML = """
<article class="bs">
  <a class="tip" href="/the-early-spring/" title="The Early Spring">
    <img src="/p.jpg" data-lazy-src="/lazy.jpg"/>
    <span class="typez">Drama</span>
    <span class="epx">Ep 10</span>
    <span class="tt">The Early Spring</span>
  </a>
</article>
"""


def _make_scraper(responses: dict[str, str]) -> OppadramaScraper:
    """Scraper dengan transport mock — request dipetakan URL → body."""

    async def handler(request: httpx.Request) -> httpx.Response:
        body = responses.get(str(request.url))
        if body is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=body)

    s = OppadramaScraper(base_url="http://test", concurrency=2, retries=2)
    s._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return s


class TestParsing:
    def test_get_latest_parse_card(self):
        """Kartu latest sumber menaut ke URL EPISODE, bukan halaman series."""
        html = (
            '<article class="bs"><a class="tip" href="/the-early-spring-episode-10/" title="The Early Spring Episode 10">'
            '<img src="/p.jpg"/><span class="typez">Drama</span><span class="epx">Episode 10</span>'
            '<span class="tt">The Early Spring Episode 10</span></a></article>'
            '<article class="bs"><a class="tip" href="/movie-x-bluray/"><span class="typez">Movie</span><span class="epx">Movie</span></a></article>'
        )
        s = _make_scraper({"http://test/": html})
        cards = asyncio.run(s.get_latest(1))
        asyncio.run(s.close())
        assert len(cards) == 2
        first = cards[0]
        assert first["slug"] == "the-early-spring-episode-10"
        assert first["url"] == "http://test/the-early-spring-episode-10/"
        assert first["type"] == "Drama"
        assert first["label"] == "Episode 10"
        assert first["series_slug"] == "the-early-spring"
        assert cards[1]["series_slug"] is None  # movie

    def test_parse_card_href_relatif(self):
        from bs4 import BeautifulSoup

        s = OppadramaScraper(base_url="http://test")
        soup = BeautifulSoup('<a class="tip" href="/series/foo/">X</a>', "html.parser")
        card = s._parse_card(soup.select_one("a"), page_url="http://test/jadwal/")
        assert card["url"] == "http://test/series/foo/"
        assert card["slug"] == "foo"  # prefix series/ dibuang

    def test_parse_card_tanpa_href(self):
        from bs4 import BeautifulSoup

        s = OppadramaScraper()
        assert s._parse_card(BeautifulSoup("<a></a>", "html.parser").a) is None
        assert s._parse_card(None) is None

    def test_get_series_list_skip_variety_dilakukan_di_run_cli(self):
        """get_series_list sendiri tidak memfilter tipe; filter di run_cli."""
        html = CARD_HTML
        s = _make_scraper({"http://test/series/?page=1&status=&type=&order=update": html})
        items = asyncio.run(s.get_series_list(1))
        assert len(items) == 1
        asyncio.run(s.close())

    def test_get_series_detail_lengkap(self):
        html = """
        <div class="infox">
          <h1 class="entry-title">The Early Spring</h1>
          <div class="spe">
            <span><b>Status:</b> Ongoing</span>
            <span><b>Network:</b> <a>ABC</a> <a>DEF</a></span>
            <span><b>Dirilis:</b> 2026</span>
            <span><b>Negara:</b> China</span>
            <span><b>Tipe:</b> Drama</span>
            <span><b>Episode:</b> 24</span>
          </div>
          <div class="gentag"><a>Romance</a><a>Romance</a><a>Drama</a></div>
          <div class="mindesc">Sinopsis uji.</div>
        </div>
        <div class="rating">8.5</div>
        <meta property="og:image" content="http://test/poster.jpg"/>
        <div class="eplister"><ul>
          <li><a href="/the-early-spring-episode-1/"><span class="epl-num">1</span><span class="epl-title">Episode 1</span><span class="epl-date">Aug 1</span></a></li>
          <li><a href="/the-early-spring-episode-2/"><span class="epl-num">2</span><span class="epl-title">Episode 2</span><span class="epl-date">Aug 2</span></a></li>
        </ul></div>
        """
        s = _make_scraper({"http://test/the-early-spring/": html})
        detail = asyncio.run(s.get_series_detail("http://test/the-early-spring/"))
        asyncio.run(s.close())
        assert detail["title"] == "The Early Spring"
        assert detail["status"] == "Ongoing"
        assert detail["network"] == "ABC, DEF"
        assert detail["country"] == "China"
        assert detail["type"] == "Drama"
        assert detail["total_episodes"] == "24"
        assert sorted(detail["genres"]) == ["Drama", "Romance"]
        assert detail["synopsis"] == "Sinopsis uji."
        assert detail["rating"] == "8.5"
        assert detail["poster"] == "http://test/poster.jpg"
        assert [e["number"] for e in detail["episodes"]] == [1, 2]

    def test_get_series_detail_single_episode(self):
        html = """
        <div class="infox"><h1 class="entry-title">Movie X</h1></div>
        <div class="eplister"><ul>
          <li><a href="/movie-x/"><span class="epl-num"></span><span class="epl-title">Movie X</span></a></li>
        </ul></div>
        """
        s = _make_scraper({"http://test/movie-x/": html})
        detail = asyncio.run(s.get_series_detail("http://test/movie-x/"))
        asyncio.run(s.close())
        assert detail["episodes"][0]["number"] == 1  # single → nomor 1

    def test_get_series_detail_halaman_tak_kenali(self):
        s = _make_scraper({"http://test/bad/": "<html></html>"})
        with pytest.raises(Exception):
            asyncio.run(s.get_series_detail("http://test/bad/"))
        asyncio.run(s.close())


class TestChallengeFlow:
    def test_challenge_diulang_lalu_sukses(self):
        """Halaman verify_human → verify → halaman asli."""
        real = "<html>" + "x" * 3000 + "</html>"
        calls = {"n": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if "verify_human" in str(request.url):
                return httpx.Response(200, text="ok")
            if calls["n"] == 1:
                return httpx.Response(200, text='<script>verify_human</script>')
            return httpx.Response(200, text=real)

        s = OppadramaScraper(base_url="http://test", retries=3)
        s._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        s._verified = True  # lewati verify awal agar urutan calls terkontrol
        resp = asyncio.run(s._fetch("http://test/page/"))
        assert resp.text == real
        asyncio.run(s.close())

    def test_challenge_persisten_raise(self):
        """Regression: challenge yang tak hilang dulu dikembalikan diam-diam
        → parsing kosong tanpa jejak. Sekarang harus raise."""

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text='<script>verify_human</script>')

        s = OppadramaScraper(base_url="http://test", retries=2)
        s._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        s._verified = True
        with pytest.raises(ScrapeError):
            asyncio.run(s._fetch("http://test/page/"))
        asyncio.run(s.close())

    def test_http_error_diretry_lalu_raise(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="err")

        s = OppadramaScraper(base_url="http://test", retries=2)
        s._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        s._verified = True
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(s._fetch("http://test/page/"))
        asyncio.run(s.close())

    def test_retries_nol_tetap_raise(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="err")

        s = OppadramaScraper(base_url="http://test", retries=0)
        s._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        s._verified = True
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(s._fetch("http://test/page/"))
        asyncio.run(s.close())


class TestGetServers:
    def _opts(self, entries: list[tuple[str, str]]) -> str:
        parts = []
        for name, embed in entries:
            b64 = base64.b64encode(f'<iframe src="{embed}"></iframe>'.encode()).decode()
            parts.append(f'<option value="{b64}" data-index="0">{name}</option>')
        return "".join(parts)

    def _page(self, opts: str, iframes: str = "") -> str:
        return f"<html><body><select>{opts}</select>{iframes}</body></html>"

    def test_server_options_diparse(self):
        html = self._page(self._opts([("Hydrax", "https://h/v/1"), ("TurboVIP", "https://t/v/2")]))
        s = _make_scraper({"http://test/ep1/": html})
        s._verified = True
        servers = asyncio.run(s.get_servers("http://test/ep1/"))
        asyncio.run(s.close())
        assert len(servers) == 2
        assert servers[0]["name"] == "Hydrax"  # preferred paling depan
        assert servers[0]["embed"] == "https://h/v/1"
        assert servers[0]["ads"] is True

    def test_blocked_server_dibuang(self):
        html = self._page(
            self._opts([("FileLions", "https://filelions.example/v/1"), ("OK", "https://ok/v/2")])
        )
        s = _make_scraper({"http://test/ep1/": html})
        s._verified = True
        servers = asyncio.run(s.get_servers("http://test/ep1/"))
        asyncio.run(s.close())
        assert [x["name"] for x in servers] == ["OK"]

    def test_fallback_iframe_bila_tanpa_options(self):
        html = self._page("", '<iframe src="//embed.example/v/1"></iframe><iframe src="https://b/v/2"></iframe>')
        s = _make_scraper({"http://test/ep1/": html})
        s._verified = True
        servers = asyncio.run(s.get_servers("http://test/ep1/"))
        asyncio.run(s.close())
        assert len(servers) == 2
        assert servers[0]["embed"] == "https://embed.example/v/1"  # // → https:
        assert all(x["name"] == "Default" for x in servers)

    def test_working_status(self):
        """200 → True, 404 → False, 500 → None (tidak diketahui)."""
        html = self._page(self._opts([("A", "https://a/v/1"), ("B", "https://b/v/2"), ("C", "https://c/v/3")]))

        async def handler(request: httpx.Request) -> httpx.Response:
            page = self._page(self._opts([("A", "https://a/v/1"), ("B", "https://b/v/2"), ("C", "https://c/v/3")]))
            if str(request.url) == "http://test/ep1/":
                return httpx.Response(200, text=page)
            url = str(request.url)
            if url.startswith("https://a/"):
                return httpx.Response(200, text="ok")
            if url.startswith("https://b/"):
                return httpx.Response(404, text="gone")
            return httpx.Response(500, text="err")

        s = OppadramaScraper(base_url="http://test")
        s._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        s._verified = True
        servers = asyncio.run(s.get_servers("http://test/ep1/"))
        asyncio.run(s.close())
        by_name = {x["name"]: x["working"] for x in servers}
        assert by_name == {"A": True, "B": False, "C": None}

    def test_embed_duplikat_dedup(self):
        html = self._page(self._opts([("A", "https://a/v/1"), ("A2", "https://a/v/1")]))
        s = _make_scraper({"http://test/ep1/": html})
        s._verified = True
        servers = asyncio.run(s.get_servers("http://test/ep1/"))
        asyncio.run(s.close())
        assert len(servers) == 1

    def test_option_tanpa_iframe_dilewati(self):
        b64 = base64.b64encode(b"<div>no iframe</div>").decode()
        b64_bad = "bukan-base64!!"
        html = self._page(f'<option value="{b64}" data-index="0">X</option><option value="{b64_bad}" data-index="1">Y</option>')
        s = _make_scraper({"http://test/ep1/": html})
        s._verified = True
        servers = asyncio.run(s.get_servers("http://test/ep1/"))
        asyncio.run(s.close())
        assert servers == []


# ── migrate_db ──


class TestMigrateDb:
    def test_analyze(self):
        db = {
            "a": {"episodes": [_ep(1), {**_ep(1), "title": "dup"}, _ep(2), {**_ep(3), "number": None}]},
            "b": {"episodes": []},
        }
        stats = analyze(db)
        assert stats == {
            "total_series": 2,
            "total_episodes": 4,
            "null_number": 1,
            "duplicate_numbers": 1,
        }

    def test_migrate_fix_null_dedup_dan_backup(self, tmp_path: Path):
        db_path = tmp_path / "db.json"
        db = {
            "a": {
                "episodes": [
                    {**_ep(1), "title": None},  # null → dari URL
                    dict(_ep(1)),               # duplikat
                    _ep(2),
                ]
            }
        }
        db_path.write_text(json.dumps(db), encoding="utf-8")
        migrate(db_path)
        after = json.loads(db_path.read_text(encoding="utf-8"))
        numbers = [e["number"] for e in after["a"]["episodes"]]
        assert numbers == [1, 2]
        assert (tmp_path / "db.bak.json").exists()

    def test_migrate_tanpa_perubahan_tidak_tulis(self, tmp_path: Path):
        db_path = tmp_path / "db.json"
        db = {"a": {"episodes": [_ep(1)]}}
        db_path.write_text(json.dumps(db), encoding="utf-8")
        migrate(db_path)
        backup = tmp_path / "db.bak.json"
        assert not backup.exists()

    def test_migrate_idempoten(self, tmp_path: Path):
        db_path = tmp_path / "db.json"
        db = {"a": {"episodes": [_ep(1), dict(_ep(1))]}}
        db_path.write_text(json.dumps(db), encoding="utf-8")
        migrate(db_path)
        snap1 = db_path.read_text(encoding="utf-8")
        migrate(db_path)
        snap2 = db_path.read_text(encoding="utf-8")
        assert snap1 == snap2


# ── restore_from_supabase.build_db ──


class TestBuildDb:
    def _series_row(self, **over) -> dict:
        row = {
            "id": 1,
            "slug": "foo",
            "title": "Foo",
            "type": "Drama",
            "rating": 8.5,
            "cast_list": ["A"],
            "genres": ["Romance"],
            "source_url": "http://x/foo/",
        }
        row.update(over)
        return row

    def test_mapping_dan_urutan(self):
        rows = [
            {"series_id": 1, "number": 2, "source_url": "http://x/foo-episode-2/"},
            {"series_id": 1, "number": 1, "source_url": "http://x/foo-episode-1/"},
        ]
        db = build_db([self._series_row()], rows)
        assert [e["number"] for e in db["foo"]["episodes"]] == [1, 2]

    def test_episode_tanpa_series_id_valid_dilewati(self):
        rows = [{"series_id": 99, "number": 1, "source_url": "http://x/bar-episode-1/"}]
        db = build_db([self._series_row()], rows)
        assert db["foo"]["episodes"] == []

    def test_checked_at_dipulihkan(self):
        """Regression: checked_at hilang saat restore → sync menimpa NULL."""
        rows = [
            {
                "series_id": 1,
                "number": 1,
                "source_url": "http://x/foo-episode-1/",
                "checked_at": "2026-08-30T00:00:00+00:00",
                "stale": True,
            }
        ]
        db = build_db([self._series_row()], rows)
        ep = db["foo"]["episodes"][0]
        assert ep["checked_at"] == "2026-08-30T00:00:00+00:00"
        assert ep["stale"] is True

    def test_rating_nol_tidak_jadi_none(self):
        """Regression: rating 0 dulu dianggap falsy → None."""
        db = build_db([self._series_row(rating=0)], [])
        assert db["foo"]["rating"] == "0"

    def test_nulls_sesuai_bentuk_db_json(self):
        db = build_db([self._series_row()], [])
        item = db["foo"]
        assert item["episodes"] == []
        assert item["cast"] == ["A"]
        assert item["poster"] is None


# ── sync_supabase ──


class TestSyncHelpers:
    def test_build_series_row(self):
        row = build_series_row(
            {
                "slug": "foo",
                "title": "Foo",
                "type": "Drama",
                "rating": "8.5",
                "poster": "http://p",
                "total_episodes": "24",
                "cast": ["A"],
                "genres": ["G"],
                "url": "http://x/foo/",
                "first_seen_at": "2026-01-01T00:00:00+00:00",
                "last_update_at": "2026-01-02T00:00:00+00:00",
            }
        )
        assert row["rating"] == 8.5
        assert row["total_episodes"] == "24"
        assert row["source_url"] == "http://x/foo/"
        assert row["first_seen_at"] == "2026-01-01T00:00:00+00:00"
        assert "last_scraped_at" in row

    def test_build_series_row_tanpa_slug(self):
        assert build_series_row({"title": "x"}) is None

    def test_build_series_row_timestamp_kosong_tidak_dikirim(self):
        """first_seen_at/last_update_at null jangan menimpa backfill SQL."""
        row = build_series_row({"slug": "foo"})
        assert "first_seen_at" not in row
        assert "last_update_at" not in row

    def test_build_series_row_rating_sampah(self):
        assert build_series_row({"slug": "foo", "rating": "bukan"})["rating"] is None

    def test_build_episode_rows_skip_tanpa_url(self):
        rows = build_episode_rows(
            7,
            [
                {"number": 1, "url": ""},
                {"number": 2, "url": "http://x/e2/", "embeds": ["e"], "servers": [], "stale": False},
            ],
        )
        assert len(rows) == 1
        assert rows[0]["series_id"] == 7
        assert rows[0]["source_url"] == "http://x/e2/"
        assert rows[0]["embeds"] == ["e"]

    def test_build_episode_rows_first_seen_opsional(self):
        rows = build_episode_rows(1, [{"number": 1, "url": "http://x/e1/"}])
        assert "first_seen_at" not in rows[0]
        rows = build_episode_rows(1, [{"number": 1, "url": "http://x/e1/", "first_seen_at": "t"}])
        assert rows[0]["first_seen_at"] == "t"

    def test_aggregate_counts_list_dan_string(self):
        db = {
            "a": {"genres": ["Drama", "Action"], "country": "China"},
            "b": {"genres": ["Drama"], "country": "China"},
            "c": {"genres": [], "country": None},
        }
        genres = aggregate_counts(db, "genres")
        countries = aggregate_counts(db, "country")
        assert {"name": "Drama", "count": 2} in genres
        assert {"name": "Action", "count": 1} in genres
        assert countries == [{"name": "China", "count": 2}]

    def test_series_has_video(self):
        assert series_has_video({"synopsis": None}) is True  # detail gagal → pertahankan
        assert series_has_video({"synopsis": "ada"}) is False  # detail ok tapi kosong → mati

        alive = {
            "episodes": [
                {"embeds": ["e"], "servers": [], "stale": False},
            ]
        }
        assert series_has_video(alive) is True

        all_dead = {
            "episodes": [
                {"embeds": [], "servers": [{"working": False}], "stale": True},
            ]
        }
        assert series_has_video(all_dead) is False

        unknown = {
            "episodes": [
                {"embeds": [], "servers": [{"working": None}], "stale": False},
            ]
        }
        assert series_has_video(unknown) is True  # 5xx/timeout → jangan dibuang

        unchecked = {"episodes": [{"embeds": [], "servers": [], "stale": False}]}
        assert series_has_video(unchecked) is True

        mixed = {
            "episodes": [
                {"embeds": [], "servers": [{"working": False}], "stale": True},
                {"embeds": ["e"], "servers": [], "stale": False},
            ]
        }
        assert series_has_video(mixed) is True

    def test_series_has_video_tanpa_episodes_key(self):
        assert series_has_video({}) is True

    def test_upsert_kelompokkan_row_berdasar_signature_key(self):
        """Regression: PostgREST menolak batch dengan key berbeda antar baris
        (PGRST102 'All object keys must match') — upsert harus memisah POST
        per kombinasi key, bukan per urutan."""
        batches: list[list[dict]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            batches.append(json.loads(request.content))
            return httpx.Response(201)

        client = httpx.Client(
            base_url="http://test", transport=httpx.MockTransport(handler)
        )
        rows = [
            {"slug": "a", "title": "A"},
            {"slug": "b", "title": "B", "last_update_at": "t"},
            {"slug": "c", "title": "C"},
            {"slug": "d", "title": "D", "last_update_at": "t"},
        ]
        upsert(client, "/series?on_conflict=slug", rows, "upsert series")
        assert len(batches) == 2
        for batch in batches:
            keys = {tuple(sorted(r)) for r in batch}
            assert len(keys) == 1, f"batch campur signature key: {keys}"
        assert {r["slug"] for b in batches for r in b} == {"a", "b", "c", "d"}

    def test_upsert_sertakan_body_error(self):
        """Pesan kegagalan harus memuat body PostgREST (bukan cuma status)."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400, json={"code": "PGRST102", "message": "All object keys must match"}
            )

        client = httpx.Client(
            base_url="http://test", transport=httpx.MockTransport(handler)
        )
        with pytest.raises(RuntimeError, match="PGRST102"):
            upsert(client, "/series?on_conflict=slug", [{"slug": "a"}], "upsert series")


# ── _server_sort_key ──


class TestServerSortKey:
    def test_hydrax_diprioritaskan(self):
        hydrax = {"name": "Hydrax", "stream": None, "working": True}
        turbo = {"name": "TurboVIP", "stream": None, "working": True}
        assert _server_sort_key(hydrax) < _server_sort_key(turbo)

    def test_yang_jalan_dulu_yang_mati_kakhir(self):
        ok = {"name": "A", "stream": None, "working": True}
        dead = {"name": "A", "stream": None, "working": False}
        assert _server_sort_key(ok) < _server_sort_key(dead)

    def test_hls_sebelum_iframe(self):
        hls = {"name": "A", "stream": "https://x/master.m3u8", "working": True}
        iframe = {"name": "A", "stream": None, "working": True}
        assert _server_sort_key(hls) < _server_sort_key(iframe)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
