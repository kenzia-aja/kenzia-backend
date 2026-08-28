import json
db = json.load(open("data/db.json", encoding="utf-8"))
total = len(db)
with_poster = sum(1 for v in db.values() if v.get("poster"))
without_poster = total - with_poster
movies = [v for v in db.values() if (v.get("type") or "").lower() == "movie"]
movies_with_poster = sum(1 for v in movies if v.get("poster"))
movies_without = sum(1 for v in movies if not v.get("poster"))
dramas = [v for v in db.values() if (v.get("type") or "").lower() == "drama"]
dramas_with = sum(1 for v in dramas if v.get("poster"))
print(f"Total: {total} series")
print(f"With poster: {with_poster}, Without: {without_poster}")
print(f"Movies: {len(movies)} (with poster: {movies_with_poster}, without: {movies_without})")
print(f"Dramas: {len(dramas)} (with poster: {dramas_with})")
no_poster_movies = [v for v in movies if not v.get("poster")][:5]
for m in no_poster_movies:
    print(f"  No poster: {m['slug']} | title={m.get('title')}")
