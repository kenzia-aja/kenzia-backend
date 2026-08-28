import json
import re
import sys
from pathlib import Path


def clean_url(url: str) -> str:
    """Remove common suffixes that indicate duplicate/repost episodes."""
    cleaned = url.rstrip("/")
    for suffix in ("-nomorsesuaiottviu", "-2", "-3", "-4", "-5"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
    return cleaned + "/"


def dedup_episodes(episodes: list[dict]) -> list[dict]:
    """Remove duplicate episodes by number, prefer clean URLs."""
    seen = {}
    for ep in episodes:
        num = ep.get("number")
        url = ep.get("url", "")
        if num is not None:
            if num in seen:
                existing = seen[num]
                existing_clean = clean_url(existing["url"])
                new_clean = clean_url(url)
                if existing["url"].endswith("-nomorsesuaiottviu") and not url.endswith("-nomorsesuaiottviu"):
                    seen[num] = ep
                elif existing_clean != existing["url"] and new_clean == url:
                    seen[num] = ep
            else:
                seen[num] = ep
    return list(seen.values())


def main():
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/db.json")
    if not db_path.exists():
        print(f"File {db_path} tidak ditemukan.")
        return 1

    db = json.loads(db_path.read_text(encoding="utf-8"))
    total_removed = 0

    for slug, item in db.items():
        eps = item.get("episodes", [])
        if not eps:
            continue
        before = len(eps)
        deduped = dedup_episodes(eps)
        after = len(deduped)
        if before > after:
            removed = before - after
            total_removed += removed
            item["episodes"] = deduped
            print(f"  {slug}: {before} -> {after} (-{removed})")

    if total_removed > 0:
        db_path.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nTotal episode dihapus: {total_removed}")
    else:
        print("Tidak ada duplikat ditemukan.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
