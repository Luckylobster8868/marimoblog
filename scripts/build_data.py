"""Runs inside GitHub Actions (free). Reuses the original backend/main.py as a library: fetches every source in
backend/sources.json plus AniList's home-page lists, then writes plain JSON files into site/data/ for the static site."""
import asyncio, json, os, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.join(ROOT, "backend")); sys.path.insert(0, os.getcwd())
import main  # the original backend, used only for its feed + AniList code

OUT = os.path.join(ROOT, "site", "data"); os.makedirs(OUT, exist_ok=True)

def load(name):
    try:
        with open(os.path.join(OUT, name), encoding="utf-8") as f: return json.load(f)
    except Exception: return {}

def save(name, obj):
    tmp = os.path.join(OUT, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f: json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, os.path.join(OUT, name))

# 1) news, blogs, podcasts, videos
srcs = main._sources()
with ThreadPoolExecutor(4) as ex: list(ex.map(main._refresh_one, srcs))
names = {s["key"]: s["name"] for s in srcs}
c = main._dbi()
rows = c.execute("SELECT * FROM items ORDER BY published DESC LIMIT 500").fetchall()
cnt = dict(c.execute("SELECT source, COUNT(*) FROM items GROUP BY source").fetchall())
st = {r["key"]: r for r in c.execute("SELECT * FROM srcs")}
c.close()
sources = []
for s in srcs:
    r = st.get(s["key"])
    sources.append({"key": s["key"], "name": s["name"], "kind": s["kind"], "home": s.get("home") or (r["home"] if r else "") or "",
                    "count": cnt.get(s["key"], 0), "checked": r["checked"] if r else 0, "error": (r["error"] if r else "") or "",
                    "matched": ((r["title"] if r else "") or "") if s["kind"] == "podcast" else ""})
if rows or not load("news.json"):  # never replace good news with an empty list after a bad run
    save("news.json", {"updated": int(time.time()), "items": [main._pub(r, names) for r in rows], "sources": sources})

# 2) AniList lists for the home / trending / seasonal pages (a failed list keeps its previous copy)
async def home():
    old, new = load("home.json"), {}
    jobs = {"trending": main.trending, "season": main.season, "upcoming": main.upcoming, "calendar": main.schedule}
    for k, fn in jobs.items():
        try: new[k] = await fn()
        except Exception as e: print("anilist", k, "failed:", e); new[k] = old.get(k)
    for s in ("trending", "popular", "score", "new"):
        try: new["browse_" + s] = await main.browse(sort=s)
        except Exception as e: print("anilist browse", s, "failed:", e); new["browse_" + s] = old.get("browse_" + s)
    new["trending"] = new.get("trending") or []; new["upcoming"] = new.get("upcoming") or []; new["calendar"] = new.get("calendar") or []
    new["season"] = new.get("season") or {"season": "", "year": 0, "media": []}
    return {k: v for k, v in new.items() if v is not None}
save("home.json", asyncio.run(home()))

# 3) articles, comments and poll: one call to the Worker, saved as static files (so visitors never wait on the Worker)
def worker_url():
    try:
        m = re.search(r'NX_API\s*=\s*"(https?://[^"]+)"', open(os.path.join(ROOT, "site", "config.js"), encoding="utf-8").read())
        return m.group(1).rstrip("/") if m else ""
    except Exception: return ""

def export_articles():
    url = worker_url()
    if not url: print("articles: site/config.js has no Worker address yet, skipping"); return
    try:
        with urllib.request.urlopen(urllib.request.Request(url + "/api/export", headers={"User-Agent": "marimoblog-build"}), timeout=30) as r: d = json.load(r)
    except Exception as e: print("articles export failed (keeping the previous copy):", e); return
    A, C = d.get("articles", []), d.get("comments", {})
    save("articles.json", {"updated": int(time.time()), "poll": d.get("poll", []),
         "list": [{"id": a["id"], "kind": a["kind"], "title": a["title"], "anime_id": a["anime_id"], "created": a["created"], "excerpt": (a["body"] or "")[:160], "comments": a["comments"]} for a in A]})
    os.makedirs(os.path.join(OUT, "a"), exist_ok=True); keep = set()
    for a in A:
        save("a/%d.json" % a["id"], {"article": {k: a[k] for k in ("id", "kind", "title", "body", "anime_id", "created")}, "comments": C.get(str(a["id"]), [])}); keep.add("%d.json" % a["id"])
    for f in os.listdir(os.path.join(OUT, "a")):
        if f not in keep and f.endswith(".json"): os.remove(os.path.join(OUT, "a", f))
    print("articles:", len(A))
export_articles()
print("news items:", len(rows), "| sources:", len(sources), "| files:", sorted(os.listdir(OUT)))
