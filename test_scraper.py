"""Unit test untuk logika scraper yang rawan bug.

Regression terpenting: _clean_episode_url yang dulu memotong "-2" dari
episode LEGIT (the-early-spring-episode-2) sehingga 443 episode hilang.

Jalankan: python -m pytest test_scraper.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from scraper import (  # noqa: E402
    _clean_episode_url,
    _server_sort_key,
    dedup_episodes_list,
    extract_episode_number,
    merge_episodes,
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

    def test_nomorsesuaiottviu_dihapus(self):
        assert (
            _clean_episode_url("http://x.com/xxx-episode-1-nomorsesuaiottviu/")
            == "http://x.com/xxx-episode-1/"
        )

    def test_bukan_episode_tetap_utuh(self):
        url = "http://x.com/movie-title-2/"
        assert _clean_episode_url(url) == url


# ── extract_episode_number ──


class TestExtractEpisodeNumber:
    def test_dari_url(self):
        assert (
            extract_episode_number(None, None, "http://x.com/foo-episode-12/")
            == 12
        )

    def test_dari_title(self):
        assert extract_episode_number(None, "The Early Spring Episode 6", None) == 6

    def test_tidak_ada_nomor(self):
        assert extract_episode_number(None, "Movie BluRay", "http://x.com/movie/") is None


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

    def test_urut_menaik_null_di_akhir(self):
        eps = [_ep(3), {"number": None, "title": "X", "date": None, "url": "http://x.com/x/"}, _ep(1)]
        result = dedup_episodes_list(eps)
        numbers = [e["number"] for e in result]
        assert numbers == [1, 3, None]


# ── _server_sort_key (prioritas pengisian server) ──


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
