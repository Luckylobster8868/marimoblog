"""Runs inside GitHub Actions (free). Reuses the original backend/main.py as a library: fetches every source in
backend/sources.json plus AniList's lists, then writes plain JSON files into site/data/ for the static site and
embeds the first-screen data into site/index.html so the home page needs no extra requests. Writes changed=true|false
to $GITHUB_OUTPUT so scheduled runs can skip the deploy when nothing visible changed."""
import asyncio, hashlib, json, math, os, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.join(ROOT, "backend")); sys.path.insert(0, os.getcwd())
import main  # the original backend, used only for its feed + AniList code

OUT = os.path.join(ROOT, "site", "data"); os.makedirs(OUT, exist_ok=True)
STATE = os.path.join(ROOT, "backend", ".build-state.json")
MAX_NEW = int(os.environ.get("ANIME_REFRESH_MAX", "24"))  # AniList detail calls per run (it allows 30 to 90 a minute, we pause 1.5 s between calls)
PAUSE = float(os.environ.get("ANIME_PAUSE", "1.5"))

def load(name, default=None):
    try:
        with open(os.path.join(OUT, name), encoding="utf-8") as f: return json.load(f)
    except Exception: return {} if default is None else default

def save(name, obj):
    path = os.path.join(OUT, name); os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f: json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(path + ".tmp", path)

def state():
    try:
        with open(STATE, encoding="utf-8") as f: return json.load(f)
    except Exception: return {}

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
old_news = load("news.json")
if rows: items = [main._pub(r, names) for r in rows]
else: items = old_news.get("items", [])  # never replace good news with an empty list after a bad run

# 2) AniList lists for the home / trending / seasonal pages (a failed list keeps its previous copy)
async def home_lists():
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
home = asyncio.run(home_lists())

# 3) articles, comments and poll: one call to the Worker, saved as static files (so visitors never wait on the Worker)
def worker_url():
    try:
        m = re.search(r'NX_API\s*=\s*"(https?://[^"]+)"', open(os.path.join(ROOT, "site", "config.js"), encoding="utf-8").read())
        return m.group(1).rstrip("/") if m else ""
    except Exception: return ""

def export_articles(prev_fp):
    url = worker_url()
    if not url: print("articles: site/config.js has no Worker address yet, skipping"); return None
    q = url + "/api/export" + ("?fp=" + prev_fp if prev_fp and os.path.exists(os.path.join(OUT, "articles.json")) else "")
    try:
        with urllib.request.urlopen(urllib.request.Request(q, headers={"User-Agent": "marimoblog-build"}), timeout=30) as r: return json.load(r)
    except Exception as e: print("articles export failed (keeping the previous copy):", e); return None

S = state(); ex_data = export_articles(S.get("export_fp")); export_fp = S.get("export_fp"); art_files = {}
if ex_data and not ex_data.get("unchanged"):
    export_fp = ex_data.get("fp"); A, C = ex_data.get("articles", []), ex_data.get("comments", {})
    lst = [{"id": a["id"], "kind": a["kind"], "title": a["title"], "anime_id": a["anime_id"], "created": a["created"], "excerpt": (a["body"] or "")[:160],
            "comments": a["comments"], "min": max(1, math.ceil(len((a["body"] or "").split()) / 200))} for a in A]
    poll = ex_data.get("poll", [])
    art_files = {"a/%d.json" % a["id"]: {"article": {k: a[k] for k in ("id", "kind", "title", "body", "anime_id", "created")}, "comments": C.get(str(a["id"]), [])} for a in A}
else:
    prev = load("articles.json"); lst, poll = prev.get("list", []), prev.get("poll", [])

# 4) static anime details (first screen, trending, season, upcoming and anime linked from articles): visitors read these
# instead of every browser asking AniList. The query is read from the page's own code so the two can never drift apart.
def detail_query():
    js = open(os.path.join(ROOT, "site", "nx-shim.js"), encoding="utf-8").read()
    card = re.search(r"const CARD='([^']*)';", js).group(1)
    expr = re.search(r"const DETAIL=(.*?);\n", js, re.S).group(1)
    return expr.replace("'+CARD+'", card)[1:-1]

trend = home.get("trending") or []
fid = (lst[0].get("anime_id") if lst else None) or (trend[0]["id"] if trend else None)
ids = []
for i in [fid] + [a.get("anime_id") for a in lst[:12]] + [m["id"] for m in trend] + [m["id"] for m in (home.get("browse_trending") or [])] + [m["id"] for m in (home.get("season") or {}).get("media", [])] + [m["id"] for m in (home.get("upcoming") or [])]:
    if i and i not in ids: ids.append(i)
ids = ids[:100]

async def anime_details(ids):
    q = detail_query(); media = {}; stale = []; now = time.time(); fetched = 0; halt = False
    for i in ids:
        d = load("anime/%d.json" % i)
        if d.get("m") and now - d.get("t", 0) < 12 * 3600: media[i] = d["m"]
        else: stale.append((i, d))
    for i, d in stale:
        if halt or fetched >= MAX_NEW:
            if d.get("m"): media[i] = d["m"]
            continue
        try:
            m = ((await main.gql(q, {"id": i}, ttl=0)) or {}).get("Media")
            if m: save("anime/%d.json" % i, {"t": int(now), "m": m}); media[i] = m
            fetched += 1
        except Exception as e:
            print("anime", i, "failed, stopping for this run:", e); halt = True
            if d.get("m"): media[i] = d["m"]
        await asyncio.sleep(PAUSE)
    print("anime details: %d ready, %d fetched this run" % (len(media), fetched))
    return media
media = asyncio.run(anime_details(ids))
cover = lambda m: ((m.get("coverImage") or {}).get("extraLarge") or (m.get("coverImage") or {}).get("large") or "")
anime_index = {str(i): {"n": [x for x in [(m.get("title") or {}).get("english"), (m.get("title") or {}).get("romaji"), *(m.get("synonyms") or [])[:4]] if x],
                        "b": m.get("bannerImage") or "", "c": cover(m)} for i, m in media.items()}

# 5) did anything visitors can see change? (ignores timestamps and fast-moving counters such as popularity)
def calm(o):
    if isinstance(o, dict): return {k: calm(v) for k, v in o.items() if k not in ("popularity", "averageScore", "checked")}
    if isinstance(o, list): return [calm(v) for v in o]
    return o
core = {"items": items, "sources": sources, "home": home, "lst": lst, "poll": poll, "anime": sorted(media), "files": {k: v["comments"] for k, v in art_files.items()}}
fp = hashlib.sha256(json.dumps(calm(core), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
changed = fp != S.get("fp") or not os.path.exists(os.path.join(OUT, "news.json"))
updated = int(time.time()) if changed else S.get("updated", int(time.time()))

# 6) write everything (same content when nothing changed) and embed the first screen into index.html
save("news.json", {"updated": updated, "items": items, "sources": sources})
save("home.json", home)
save("articles.json", {"updated": updated, "poll": poll, "list": lst})
for k, v in art_files.items(): save(k, v)
if art_files:
    keep = {os.path.basename(k) for k in art_files}
    for f in os.listdir(os.path.join(OUT, "a")):
        if f.endswith(".json") and f not in keep: os.remove(os.path.join(OUT, "a", f))
save("anime/index.json", anime_index)
def nrm(x): return re.sub(r"[^a-z0-9]+", " ", (x or "").lower()).strip()
def name_keys(names):  # mirrors keys() in nx-shim.js: full name and the part before ":" / " - ", at least 5 characters
    ks = []
    for n in names:
        n = n or ""
        for v in (n, re.split(r"\s*[:\u2013\u2014]\s*|\s+-\s+", n)[0]):
            k = " " + nrm(v) + " "
            if len(k) >= 7 and k not in ks: ks.append(k)
    return ks
texts = {r["id"]: " " + nrm((r.get("title") or "") + " " + (r.get("excerpt") or "")) + " " for r in items}
matches = {str(i): [rid for rid, t in texts.items() if any(k in t for k in name_keys(anime_index[str(i)]["n"]))] for i in media}
save("anime/matches.json", matches)
adir = os.path.join(OUT, "anime"); live = {"%d.json" % i for i in media} | {"index.json", "matches.json"}
for f in os.listdir(adir):
    p = os.path.join(adir, f)
    if f not in live and f.endswith(".json") and time.time() - os.path.getmtime(p) > 3 * 86400: os.remove(p)

def news_resp(types, per):
    L = [r for r in items if not types or r["kind"] in types]; total = len(L)
    return {"items": L[:per], "total": total, "page": 1, "pages": max(1, math.ceil(total / per)), "updated": updated}
art_ids = [a["anime_id"] for a in lst[:12] if a.get("anime_id") in media]
boot = {"t": updated, "articles": lst, "trending": trend, "season": home.get("season"), "poll": poll,
        "news": {"": {"per": 6, "resp": news_resp([], 6)}, "video,podcast": {"per": 3, "resp": news_resp(["video", "podcast"], 3)}},
        "anime": sorted(media), "feat": {"id": fid, "m": media[fid]} if fid in media else None,
        "art": {str(i): {"b": anime_index[str(i)]["b"], "c": anime_index[str(i)]["c"]} for i in art_ids}}
payload = json.dumps(boot, separators=(",", ":"), ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
ip = os.path.join(ROOT, "site", "index.html"); html = open(ip, encoding="utf-8").read()
pat = re.compile(r'(<script id="boot" type="application/json">)(.*?)(</script>)', re.S)
if pat.search(html):
    with open(ip, "w", encoding="utf-8") as f: f.write(pat.sub(lambda m: m.group(1) + payload + m.group(3), html, count=1))
else: print("index.html has no boot placeholder, skipping the first-screen data")

with open(STATE, "w", encoding="utf-8") as f: json.dump({"fp": fp, "updated": updated, "export_fp": export_fp}, f)
if os.environ.get("GITHUB_OUTPUT"):
    with open(os.environ["GITHUB_OUTPUT"], "a") as f: f.write("changed=%s\n" % ("true" if changed else "false"))
print("news items: %d | sources: %d | articles: %d | boot: %d bytes | changed: %s" % (len(items), len(sources), len(lst), len(payload), changed))
