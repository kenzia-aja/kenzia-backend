"""Verifikasi rencana perbaikan: parsing episode, dedup, urutan, dan API."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402

import scraper  # noqa: E402
from main import app  # noqa: E402

PASSED = []
FAILED = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  PASS: {name} {extra}")
    else:
        FAILED.append(name)
        print(f"  FAIL: {name} {extra}")


print("=" * 60)
print("1. TEST PARSING NOMOR EPISODE")
print("=" * 60)
check("'36 END' -> 36", scraper.extract_episode_number("36 END") == 36)
check("'37 Extra' -> 37", scraper.extract_episode_number("37 Extra") == 37)
check("'Episode 12' -> 12", scraper.extract_episode_number(None, "Episode 12") == 12)
check(
    "URL '-episode-25/' -> 25",
    scraper.extract_episode_number(None, None, "http://x/blossoms-of-power-episode-25/") == 25,
)
check("'Ep 8' -> 8", scraper.parse_ep_number("Ep 8") == 8)

print()
print("=" * 60)
print("2. TEST DEDUPLIKASI & URUTAN")
print("=" * 60)
eps = [
    {"number": 3, "url": "http://x/s-episode-3/", "title": None},
    {"number": 1, "url": "http://x/s-episode-1-nomorsesuaiottviu/", "title": None},
    {"number": 1, "url": "http://x/s-episode-1/", "title": None},
    {"number": 2, "url": "http://x/s-episode-2/", "title": None},
]
deduped = scraper.dedup_episodes_list(eps)
nums = [e["number"] for e in deduped]
check("Duplikat ep 1 dihapus (kanonikal dipertahankan)", nums == [1, 2, 3], f"-> {nums}")
urls = [e["url"] for e in deduped]
check("URL kanonikal (bukan -nomorsesuaiottviu)", all("-nomorsesuaiottviu" not in u for u in urls))

print()
print("=" * 60)
print("3. TEST DATABASE LOKAL")
print("=" * 60)
import json  # noqa: E402

db = json.loads(Path("data/db.json").read_text(encoding="utf-8"))
null_count = sum(
    1 for it in db.values() for e in (it.get("episodes") or []) if e.get("number") is None
)
dup_count = 0
for it in db.values():
    seen = set()
    for e in it.get("episodes") or []:
        n = e.get("number")
        if n is not None:
            if n in seen:
                dup_count += 1
            seen.add(n)
check("Tidak ada episode number=null", null_count == 0, f"(null={null_count})")
check("Tidak ada nomor duplikat", dup_count == 0, f"(dup={dup_count})")

bop = db.get("blossoms-of-power")
if bop:
    bnums = [e.get("number") for e in bop.get("episodes") or []]
    check("Blossoms of Power = Ep 1..37 runtut", bnums == list(range(1, 38)), f"({len(bnums)} eps)")
else:
    check("Blossoms of Power ada di db", False)

print()
print("=" * 60)
print("4. TEST API ENDPOINT (TestClient, offline-safe)")
print("=" * 60)
with TestClient(app) as client:
    r = client.get("/health")
    check("GET /health", r.status_code == 200, str(r.json()) if r.status_code == 200 else "")

    r = client.get("/api/series/blossoms-of-power")
    ok = r.status_code == 200
    data = r.json() if ok else {}
    eps_list = data.get("episodes") or []
    ep_nums = [e.get("number") for e in eps_list]
    check("GET /api/series/blossoms-of-power", ok)
    check(
        "Detail: episode 1..37 urut & unik",
        ep_nums == sorted(set(ep_nums)) and len(ep_nums) == 37,
        f"({len(ep_nums)} eps)",
    )

    # Fallback lokal: filter type=Drama pada db lokal (live fetch dinonaktifkan
    # dengan monkeypatch agar test tidak bergantung jaringan)
    import main as main_mod  # noqa: E402

    async def _offline(*a, **k):
        return []

    original = main_mod.scraper.get_series_list
    try:
        main_mod.scraper.get_series_list = _offline  # type: ignore[method-assign]
        r = client.get("/api/series", params={"type": "Drama", "country": "China", "page": 1})
        ok = r.status_code == 200
        body = r.json() if ok else {}
        results = body.get("results") or []
        china_ok = all(
            "china" in ((it.get("country") or "").lower()) for it in results if it.get("country")
        )
        check("GET /api/series?type=Drama&country=China (fallback lokal)", ok)
        check(f"Hasil filter China > 0 ({len(results)} item)", len(results) > 0)
        check("Semua hasil berasal dari China", china_ok)
    finally:
        main_mod.scraper.get_series_list = original  # type: ignore[method-assign]

    r = client.get("/api/countries")
    ok = r.status_code == 200
    names = [c["name"] for c in (r.json().get("countries") or [])] if ok else []
    no_composite = all("," not in n for n in names)
    check("GET /api/countries", ok)
    check("Negara majemuk sudah dipisah (tanpa koma)", no_composite, f"total={len(names)}")

print()
print("=" * 60)
print(f"HASIL: {len(PASSED)} PASS, {len(FAILED)} FAIL")
if FAILED:
    print("Gagal:", FAILED)
    sys.exit(1)
print("SEMUA VERIFIKASI BERHASIL ✓")