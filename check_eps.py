import json

db = json.load(open("data/db.json", encoding="utf-8"))
episodes = db.get("bloody-smart", {}).get("episodes", [])
print(f"Total episodes: {len(episodes)}")
for i, ep in enumerate(episodes):
    url = ep.get("url", "")
    number = ep.get("number")
    title = ep.get("title", "")
    print(f"  [{i}] number={number} title=\"{title}\" url=\"{url}\"")
    # Check if this is episode 1 URL
    if "episode-1/" in url:
        print(f"       ^^^ THIS IS EPISODE 1 URL")
