"""Migrasi db.json: re-parse nomor episode null, hapus duplikat, urutkan episode.

Idempotent: aman dijalankan berulang kali.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from scraper import dedup_episodes_list, extract_episode_number  # noqa: E402


def analyze(db: dict) -> dict:
    total_eps = null_eps = dup_nums = 0
    for item in db.values():
        eps = item.get("episodes") or []
        total_eps += len(eps)
        nums = [e.get("number") for e in eps]
        null_eps += sum(1 for n in nums if n is None)
        seen = set()
        for n in nums:
            if n is not None:
                if n in seen:
                    dup_nums += 1
                seen.add(n)
    return {
        "total_series": len(db),
        "total_episodes": total_eps,
        "null_number": null_eps,
        "duplicate_numbers": dup_nums,
    }


def migrate(db_path: Path) -> None:
    db = json.loads(db_path.read_text(encoding="utf-8"))
    before = analyze(db)
    print("SEBELUM :", before)

    fixed_numbers = removed_dups = reordered = 0
    for slug, item in db.items():
        eps = item.get("episodes") or []
        if not eps:
            continue

        # 1. Re-parse semua nomor episode yang null (mis. "36 END", "37 Extra")
        for ep in eps:
            if ep.get("number") is None:
                num = extract_episode_number(
                    None,
                    ep.get("title"),
                    ep.get("url"),
                ) or extract_episode_number(ep.get("title"), ep.get("url"))
                if num is not None:
                    ep["number"] = num
                    fixed_numbers += 1

        # 2. Deduplikasi + urutkan menaik
        after_eps = dedup_episodes_list(eps)
        if len(after_eps) != len(eps):
            removed_dups += len(eps) - len(after_eps)
        if after_eps != eps:  # mendeteksi duplikat ATAU perubahan urutan
            reordered += 1
        item["episodes"] = after_eps

    after = analyze(db)
    print("SESUDAH :", after)
    print(f"Nomor diperbaiki : {fixed_numbers}")
    print(f"Duplikat dihapus : {removed_dups}")

    if fixed_numbers or removed_dups or reordered or before != after:
        backup = db_path.with_suffix(".bak.json")
        backup.write_text(db_path.read_text(encoding="utf-8"), encoding="utf-8")
        db_path.write_text(
            json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Database disimpan. Backup lama: {backup.name}")
    else:
        print("Tidak ada perubahan yang diperlukan.")

    # Laporan detail series kunci untuk verifikasi
    for key in ("blossoms-of-power",):
        item = db.get(key)
        if item:
            nums = [e.get("number") for e in item.get("episodes") or []]
            print(f"{key}: {len(nums)} eps -> {nums}")


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/db.json")
    if not path.exists():
        print(f"File {path} tidak ditemukan.")
        sys.exit(1)
    migrate(path)