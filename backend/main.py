# marimoblog backend: serves the page and the /api it talks to.
# Anime data comes from AniList, articles/comments/poll live in a small sqlite file (nexus.db).
# Run:  python main.py   then open http://localhost:8000
import threading
import asyncio, copy, json, os, re, secrets, sqlite3, time, urllib.request
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.middleware.gzip import GZipMiddleware

app = FastAPI()
app.add_middleware(GZipMiddleware, minimum_size=800)  # the page is 44 KB, gzip sends about a third of that
_CACHED = ("/api/trending", "/api/season", "/api/upcoming", "/api/calendar", "/api/browse", "/api/anime/", "/api/similar/")


@app.middleware("http")
async def _headers(req, call_next):
    r = await call_next(req)
    p = req.url.path
    if r.status_code == 200 and (p == "/api/art" or p.startswith(_CACHED)):  # anime data changes slowly, keep it 5 minutes
        r.headers["Cache-Control"] = "public, max-age=300"
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return r
_D = os.path.dirname(os.path.abspath(__file__))
_DB = os.path.join(_D, "nexus.db")
_CACHE = {}
CARD = ("id title{english romaji} coverImage{large} averageScore genres seasonYear format popularity "
        "startDate{month day} nextAiringEpisode{airingAt episode}")


_FLIGHT = {}


async def _ask(query, variables, key, old):
    def go():
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "marimoblog/1.0"}
        with urllib.request.urlopen(urllib.request.Request("https://graphql.anilist.co", body, h), timeout=20) as r:
            return json.load(r)

    try:
        data = (await asyncio.to_thread(go)).get("data")
    except Exception:
        data = None
    finally:
        _FLIGHT.pop(key, None)
    if not data:
        if old:
            return old[1]  # AniList is down or rate limiting, show the last good answer
        raise HTTPException(502, "AniList is not answering right now")
    if len(_CACHE) > 400:
        _CACHE.clear()
    _CACHE[key] = (time.time(), data)
    return data


async def gql(query, variables=None, ttl=600):
    key = query + json.dumps(variables or {}, sort_keys=True)
    hit = _CACHE.get(key)
    age = time.time() - hit[0] if hit else None
    if hit and age < ttl:
        return hit[1]
    if key not in _FLIGHT:  # many visitors asking the same thing at once share one call
        _FLIGHT[key] = asyncio.ensure_future(_ask(query, variables, key, hit))
    if hit and age < 21600:  # stale but under 6 hours old: answer now, the refresh finishes in the background
        return hit[1]
    return await asyncio.shield(_FLIGHT[key])


async def _media(args, per=24):
    q = "query($n:Int){Page(perPage:$n){media(%s,type:ANIME,isAdult:false){%s}}}" % (args, CARD)
    return (await gql(q, {"n": per}))["Page"]["media"]


@app.get("/api/trending")
async def trending():
    return await _media("sort:TRENDING_DESC", 8)


@app.get("/api/season")
async def season():
    t = time.gmtime()
    s = ["WINTER", "SPRING", "SUMMER", "FALL"][(t.tm_mon - 1) // 3]
    return {"season": s.title(), "year": t.tm_year,
            "media": await _media("season:%s,seasonYear:%d,sort:POPULARITY_DESC" % (s, t.tm_year), 24)}


@app.get("/api/upcoming")
async def upcoming():
    return await _media("status:NOT_YET_RELEASED,sort:POPULARITY_DESC", 12)


@app.get("/api/calendar")
async def schedule():
    end = time.time() + 7 * 86400
    rows = [m for m in await _media("status:RELEASING,sort:POPULARITY_DESC", 50)
            if m.get("nextAiringEpisode") and m["nextAiringEpisode"]["airingAt"] < end]
    return sorted(rows, key=lambda m: m["nextAiringEpisode"]["airingAt"])


@app.get("/api/browse")
async def browse(q: str = "", genre: str = "", year: int = 0, sort: str = "popular", page: int = 1):
    s = {"popular": "POPULARITY_DESC", "trending": "TRENDING_DESC", "score": "SCORE_DESC", "new": "START_DATE_DESC"}.get(sort, "POPULARITY_DESC")
    q, genre = q.strip()[:100], genre.strip()[:30]
    d, a, v = ["$p:Int", "$n:Int"], ["sort:" + ("SEARCH_MATCH" if q and sort == "popular" else s)], {"p": max(page, 1), "n": 24}
    for val, name, typ, arg in ((q, "s", "String", "search"), (genre, "g", "String", "genre"), (year, "y", "Int", "seasonYear")):
        if val:
            d.append("$%s:%s" % (name, typ))
            a.append("%s:$%s" % (arg, name))
            v[name] = val
    qq = "query(%s){Page(page:$p,perPage:$n){media(%s,type:ANIME,isAdult:false){%s}}}" % (",".join(d), ",".join(a), CARD)
    return (await gql(qq, v))["Page"]["media"]


DETAIL = ("query($id:Int){Media(id:$id,type:ANIME){" + CARD + " description episodes status bannerImage "
          "studios(isMain:true){nodes{name}} tags{name rank isMediaSpoiler} "
          "characters(perPage:8,sort:[ROLE,RELEVANCE]){edges{role node{name{full} image{medium}}}} "
          "staff(perPage:8){edges{role node{name{full}}}} "
          "relations{edges{relationType node{type " + CARD + "}}} "
          "stats{statusDistribution{status amount}} "
          "recommendations(perPage:8,sort:RATING_DESC){nodes{mediaRecommendation{" + CARD + "}}}}}")


def _recs(m):
    return [n["mediaRecommendation"] for n in m.pop("recommendations")["nodes"] if n["mediaRecommendation"]]


@app.get("/api/anime/{mid}")
async def anime(mid: int):
    m = copy.deepcopy((await gql(DETAIL, {"id": mid}))["Media"])  # copy, the cache keeps the original
    if not m:
        raise HTTPException(404, "not found")
    m["description"] = re.sub(r"<[^>]+>", "", (m["description"] or "").replace("<br>", "\n"))
    m["relations"]["edges"] = [e for e in m["relations"]["edges"] if e["node"]["type"] == "ANIME"]
    m["recs"] = _recs(m)
    c = _db()
    m["articles"] = [dict(r) for r in c.execute("select id,title,kind,created,(select count(*) from comments where article_id=articles.id) as comments from articles where anime_id=? order by id desc", (mid,))]
    c.close()
    return m


@app.get("/api/similar/{mid}")
async def similar(mid: int):
    q = "query($id:Int){Media(id:$id){recommendations(perPage:10,sort:RATING_DESC){nodes{mediaRecommendation{%s}}}}}" % CARD
    m = copy.deepcopy((await gql(q, {"id": mid}))["Media"] or {"recommendations": {"nodes": []}})
    return [dict(r, why="Fans of this also liked it") for r in _recs(m)]


@app.get("/api/art")
async def art(ids: str = ""):
    n = [int(x) for x in ids.split(",") if x.strip().isdigit()][:40]
    if not n:
        return []
    q = "query($i:[Int]){Page(perPage:40){media(id_in:$i,type:ANIME){id bannerImage coverImage{extraLarge}}}}"
    return [{"id": m["id"], "best": m["bannerImage"] or m["coverImage"]["extraLarge"], "stills": [m["coverImage"]["extraLarge"]]}
            for m in (await gql(q, {"i": n}, 3600))["Page"]["media"]]


D1 = [os.environ.get(k, "") for k in ("D1_ACCOUNT", "D1_DB", "D1_TOKEN")]
D1_URL = os.environ.get("D1_URL") or "https://api.cloudflare.com/client/v4/accounts/%s/d1/database/%s/query" % (D1[0], D1[1])
_RC = {}  # D1 reads are remembered for 20 seconds, any write clears them


class _Cur:
    def __init__(self, rows, last=None):
        self.rows, self.lastrowid = rows, last

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class _D1:  # just enough of the sqlite3 connection for the code below
    def execute(self, sql, params=()):
        sql = sql.strip()
        if sql.lower().startswith("pragma"):
            return _Cur([])
        read, key = sql.lower().startswith("select"), (sql, tuple(params))
        hit = _RC.get(key) if read else None
        if hit and time.time() - hit[0] < 20:
            return _Cur(hit[1])
        req = urllib.request.Request(D1_URL, json.dumps({"sql": sql, "params": list(params)}).encode(),
                                     {"Authorization": "Bearer " + D1[2], "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                res = json.load(r)["result"][0]
        except Exception:
            raise HTTPException(503, "the database is not answering right now") from None
        rows = res.get("results") or []
        if read:
            _RC[key] = (time.time(), rows)
            if len(_RC) > 300:
                _RC.clear()
        else:
            _RC.clear()
        return _Cur(rows, (res.get("meta") or {}).get("last_row_id"))

    def executemany(self, sql, seq):
        for p in seq:
            self.execute(sql, p)

    def executescript(self, script):
        for st in script.split(";"):
            if st.strip():
                self.execute(st)

    def commit(self):
        pass

    def close(self):
        pass


def _db():
    if all(D1) or os.environ.get("D1_URL"):
        return _D1()
    c = sqlite3.connect(_DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _init():
    c = _db()
    c.executescript("""
        pragma journal_mode=wal;
        create table if not exists articles(id integer primary key, kind text, title text, body text, anime_id integer, created text default (datetime('now')));
        create table if not exists comments(id integer primary key, article_id integer, name text, text text, created text default (datetime('now')));
        create table if not exists poll(option text primary key, votes integer default 0);
        create index if not exists comments_by_article on comments(article_id);
        create index if not exists articles_by_anime on articles(anime_id);""")
    if not c.execute("select 1 from articles").fetchone():  # placeholder articles, same ones the demo page shows
        body = ("Gear 5 is the spectacle, but the real engine is how the story frames freedom and who chooses to follow him.\n\n"
                "Egghead quietly sets up a different kind of power, and the characters who understand Luffy matter more than the transformation.")
        c.executemany("insert into articles(kind,title,body) values(?,?,?)", [(k, t, body) for k, t in (
            ("Analysis", "Why Luffy's real power isn't Gear 5"), ("Theory", "Every foreshadowing detail in Egghead, ranked"),
            ("Character", "The quiet cruelty of the best side characters"),
            ("Review", "Magilumiere Magical Girls Inc. S2: a clever, optimistic comeback"),
            ("News", "Hideaki Anno co-directs Yamato 2199 film at I.G"))])
    if not c.execute("select 1 from poll").fetchone():
        c.executemany("insert into poll(option) values(?)",
                      [("Monogatari: Off & Monster Season",), ("Liberated from Paradise",), ("Horror Collector",)])
    c.commit()
    c.close()


try:
    _init()
except Exception as ex:  # e.g. the free database is unreachable at boot, the site still starts
    print("database setup failed:", ex)


@app.get("/api/articles")
def articles():
    c = _db()
    r = [dict(x) for x in c.execute("select id,kind,title,anime_id,created,substr(body,1,160) as excerpt,(select count(*) from comments where article_id=articles.id) as comments from articles order by id desc")]
    c.close()
    return r


@app.get("/api/articles/{aid}")
def article(aid: int):
    c = _db()
    r = c.execute("select * from articles where id=?", (aid,)).fetchone()
    c.close()
    if not r:
        raise HTTPException(404, "not found")
    return dict(r)


@app.get("/api/articles/{aid}/comments")
def comments(aid: int):
    c = _db()
    r = [dict(x) for x in c.execute("select name,text,created from comments where article_id=? order by id desc", (aid,))]
    c.close()
    return r


@app.post("/api/articles/{aid}/comments")
async def add_comment(aid: int, req: Request):
    _limit(req, "comment", 5, 60)
    d = await _json(req)
    name, text = str(d.get("name", "")).strip()[:40], str(d.get("text", "")).strip()[:1000]
    if not name or not text:
        raise HTTPException(400, "name and text needed")
    c = _db()
    if not c.execute("select 1 from articles where id=?", (aid,)).fetchone():
        c.close()
        raise HTTPException(404, "not found")
    c.execute("insert into comments(article_id,name,text) values(?,?,?)", (aid, name, text))
    c.commit()
    c.close()
    return {"ok": 1}


async def _json(req):
    try:
        d = await req.json()
    except Exception:
        raise HTTPException(400, "send json") from None
    if not isinstance(d, dict):
        raise HTTPException(400, "send a json object")
    return d


_HITS = {}


def _limit(req, what, n, secs):  # n requests per visitor per window, enough to stop spam
    ip = req.headers.get("x-forwarded-for", "").split(",")[0].strip() or (req.client.host if req.client else "?")
    now = time.time()
    hits = [t for t in _HITS.get((what, ip), []) if now - t < secs]
    if len(hits) >= n:
        raise HTTPException(429, "slow down a little")
    _HITS[(what, ip)] = hits + [now]
    if len(_HITS) > 5000:
        _HITS.clear()


def _admin(req):  # publishing needs the ADMIN_KEY env var, sent as the x-admin-key header
    key = os.environ.get("ADMIN_KEY", "")
    if not key or not secrets.compare_digest(req.headers.get("x-admin-key", "").encode(), key.encode()):
        raise HTTPException(403, "wrong or missing admin key (set the ADMIN_KEY env var first)")


@app.post("/api/articles")
async def add_article(req: Request):
    _admin(req)
    d = await _json(req)
    if not str(d.get("title", "")).strip() or not str(d.get("body", "")).strip():
        raise HTTPException(400, "title and body needed")
    c = _db()
    cur = c.execute("insert into articles(kind,title,body,anime_id) values(?,?,?,?)",
                    (str(d.get("kind") or "News")[:30], str(d["title"]).strip()[:200], str(d["body"]).strip(), d.get("anime_id") or None))
    new = cur.lastrowid
    c.commit()
    c.close()
    return {"id": new}


@app.delete("/api/articles/{aid}")
def delete_article(aid: int, req: Request):
    _admin(req)
    c = _db()
    c.execute("delete from comments where article_id=?", (aid,))
    c.execute("delete from articles where id=?", (aid,))
    c.commit()
    c.close()
    return {"ok": 1}


def _poll():
    c = _db()
    r = [dict(x) for x in c.execute("select option,votes from poll order by rowid")]
    c.close()
    return r


@app.get("/api/poll")
def poll():
    return _poll()


@app.post("/api/poll/vote")
async def vote(req: Request):
    _limit(req, "vote", 3, 60)
    d = await _json(req)
    c = _db()
    c.execute("update poll set votes=votes+1 where option=?", (d.get("option"),))
    c.commit()
    c.close()
    return _poll()


# nexus-news v3
import asyncio, calendar, hashlib, html as _html, json, os, re, sqlite3, time
import urllib.error, urllib.parse, urllib.request
try:
    import feedparser
except ImportError:
    feedparser = None

_DIR = os.path.dirname(os.path.abspath(__file__))
_NDB, _NSRC = os.path.join(_DIR, "nexus_news.db"), os.path.join(_DIR, "sources.json")
_UA = "marimoblog/1.0 (anime news reader; every item links back to its original source)"
_ITUNES = "https://itunes.apple.com/search"
_NS = {"started": False, "last": 0.0, "busy": False, "ready": False}
_DBI_INIT_LOCK = threading.Lock()

_DEFAULT = [
    {"key": "ann", "name": "Anime News Network", "kind": "news", "home": "https://www.animenewsnetwork.com", "feeds": ["https://www.animenewsnetwork.com/all/rss.xml"]},
    {"key": "mal", "name": "MyAnimeList News", "kind": "news", "home": "https://myanimelist.net/news", "feeds": ["https://myanimelist.net/rss/news.xml"]},
    {"key": "crunchyroll", "name": "Crunchyroll News", "kind": "news", "home": "https://www.crunchyroll.com/news", "feeds": ["https://cr-news-api-service.prd.crunchyrollsvc.com/v1/en-US/rss"]},
    {"key": "animecorner", "name": "Anime Corner", "kind": "news", "home": "https://animecorner.me", "backfill": "wp"},
    {"key": "glorio", "name": "The Glorio Blog", "kind": "blog", "home": "https://theglorioblog.com", "backfill": "wp"},
    {"key": "animefeminist", "name": "Anime Feminist", "kind": "blog", "home": "https://www.animefeminist.com", "backfill": "wp"},
    {"key": "tangles", "name": "Beneath the Tangles", "kind": "blog", "home": "https://beneaththetangles.com", "backfill": "wp"},
    {"key": "starcrossed", "name": "Star Crossed Anime", "kind": "blog", "home": "https://starcrossedanime.com", "backfill": "wp"},
    {"key": "glorio-chat", "name": "The GLORIO Chat Podcast", "kind": "podcast", "home": "https://theglorioblog.com", "itunes": "GLORIO Chat Podcast"},
    {"key": "anncast", "name": "ANNCast", "kind": "podcast", "home": "https://www.animenewsnetwork.com", "itunes": "ANNCast Anime News Network"},
]

# starter YouTube channels (videos play inside your page from YouTube's own player); swap in the creators you like
_DEFAULT_VIDEO = [
    {"key": "yt-ann", "name": "Anime News Network (YouTube)", "kind": "video", "home": "https://www.youtube.com/@AnimeNewsNetwork"},
    {"key": "yt-crunchyroll", "name": "Crunchyroll (YouTube)", "kind": "video", "home": "https://www.youtube.com/@Crunchyroll"},
    {"key": "yt-gigguk", "name": "Gigguk (YouTube)", "kind": "video", "home": "https://www.youtube.com/@Gigguk"},
    {"key": "yt-sew", "name": "Super Eyepatch Wolf (YouTube)", "kind": "video", "home": "https://www.youtube.com/@SuperEyepatchWolf"},
]

def _save(data):
    with open(_NSRC, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)

def _sources_read():
    if not os.path.exists(_NSRC):
        _save(_DEFAULT + _DEFAULT_VIDEO)
    try:
        with open(_NSRC, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = None
    if not isinstance(data, list):
        data = _DEFAULT + _DEFAULT_VIDEO
    seeded = _NSRC + ".videos"
    if not os.path.exists(seeded):  # one time: add the starter channels if there are no video sources yet
        if not any(x.get("kind") == "video" for x in data):
            data = data + _DEFAULT_VIDEO
            try:
                _save(data)
            except Exception:
                pass
        try:
            open(seeded, "w").close()
        except Exception:
            pass
    return [x for x in data if x.get("key") and not x.get("off")]

_SRC = {"mt": None, "data": []}

def _sources():
    try:
        mt = os.path.getmtime(_NSRC)
    except OSError:
        mt = None
    if mt is None or mt != _SRC["mt"]:
        _SRC["data"] = _sources_read()
        try:
            _SRC["mt"] = os.path.getmtime(_NSRC)
        except OSError:
            _SRC["mt"] = None
    return _SRC["data"]


def _dbi():
    c = sqlite3.connect(_NDB, timeout=30)
    c.row_factory = sqlite3.Row

    try:
        if not _NS["ready"]:
            with _DBI_INIT_LOCK:
                if not _NS["ready"]:
                    c.execute("PRAGMA journal_mode=WAL")
                    c.executescript("""
                        CREATE TABLE IF NOT EXISTS items(
                            id TEXT PRIMARY KEY, source TEXT, kind TEXT,
                            title TEXT, excerpt TEXT, link TEXT, image TEXT,
                            audio TEXT, published INTEGER, added INTEGER
                        );
                        CREATE INDEX IF NOT EXISTS ix_items_pub
                            ON items(published DESC);
                        CREATE INDEX IF NOT EXISTS ix_items_src
                            ON items(source, published DESC);
                        CREATE TABLE IF NOT EXISTS srcs(
                            key TEXT PRIMARY KEY, feed TEXT, etag TEXT,
                            modified TEXT, checked INTEGER DEFAULT 0,
                            error TEXT DEFAULT '', backfilled INTEGER DEFAULT 0,
                            title TEXT DEFAULT '', home TEXT DEFAULT '',
                            art TEXT DEFAULT ''
                        );
                    """)
                    _NS["ready"] = True

        return c
    except Exception:
        c.close()
        raise


def _get(url, etag=None, modified=None, limit=4000000):
    if not url.startswith(("http://", "https://")):
        raise ValueError("not an http(s) url")
    h = {"User-Agent": _UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, application/json, text/html;q=0.8, */*;q=0.5"}
    if etag:
        h["If-None-Match"] = etag
    if modified:
        h["If-Modified-Since"] = modified
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=15) as r:
            return r.status, r.read(limit), r.headers
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, b"", e.headers
        raise

_TAG = re.compile(r"<[^>]+>")
_IMG = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.I)
_JUNK = re.compile(r"pixel|1x1|spacer|feedburner|/ads?/|emoji|gravatar|badge", re.I)

def _ok(u):
    return u if isinstance(u, str) and u.startswith(("http://", "https://")) else ""

def _clean(s, n=300):
    s = re.sub(r"\s+", " ", _html.unescape(_TAG.sub(" ", s or ""))).strip()
    s = re.sub(r"\s*The post .{0,220}? appeared first on .{0,120}$", "", s)
    s = re.sub(r"\s*\[\u2026\]$|\s*\[\.\.\.\]$", "", s)
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0].rstrip(".,;:") + "\u2026"

def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def _image(e, base):
    for k in ("media_thumbnail", "media_content"):
        for m in e.get(k) or []:
            kind = m.get("medium") or m.get("type") or "image"
            if _ok(m.get("url")) and kind.startswith("image"):
                return m["url"]
    for l in e.get("links") or []:
        if l.get("rel") == "enclosure" and (l.get("type") or "").startswith("image") and _ok(l.get("href")):
            return l["href"]
    im = e.get("image")
    if isinstance(im, dict) and _ok(im.get("href")):
        return im["href"]
    blob = ((e.get("content") or [{}])[0].get("value") or "") + (e.get("summary") or "")
    for u in _IMG.findall(blob):
        u = urllib.parse.urljoin(base, _html.unescape(u))
        if _ok(u) and not _JUNK.search(u):
            return u
    return ""

def _audio(e):
    for l in e.get("enclosures") or []:
        h = l.get("href") or ""
        if _ok(h) and ((l.get("type") or "").startswith("audio") or h.lower().split("?")[0].endswith((".mp3", ".m4a", ".ogg"))):
            return h
    return ""

def _ts(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return calendar.timegm(t) if t else int(time.time())

def _rows(src, d, art=""):
    out, now = [], int(time.time())
    for e in d.entries[:200]:
        link, title = _ok(e.get("link")), _clean(e.get("title"), 200)
        if not link or not title:
            continue
        body = (e.get("content") or [{}])[0].get("value") or ""
        img = _image(e, link) or (art if src["kind"] == "podcast" else "")
        out.append((hashlib.sha1(link.encode()).hexdigest()[:16], src["key"], src["kind"], title,
                    _clean(e.get("summary") or body, 300), link, img, _audio(e), min(_ts(e), now + 3600), now))
    return out

def _upsert(c, rows):
    c.executemany("INSERT INTO items(id,source,kind,title,excerpt,link,image,audio,published,added) VALUES(?,?,?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET title=excluded.title, excerpt=excluded.excerpt, "
                  "image=CASE WHEN excluded.image<>'' THEN excluded.image ELSE items.image END", rows)

def _wp_backfill(src):  # one-time: pull older posts from a WordPress site's public API (up to 200)
    out, now, base = [], int(time.time()), src["home"].rstrip("/")
    for page in range(1, 5):
        try:
            raw = _get("%s/wp-json/wp/v2/posts?per_page=50&page=%d&_embed=wp:featuredmedia&_fields=link,date_gmt,title,excerpt,_links,_embedded" % (base, page))[1]
            posts = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            break
        for p in posts:
            try:
                link, title = _ok(p["link"]), _clean(p["title"]["rendered"], 200)
                try:
                    img = _ok(p["_embedded"]["wp:featuredmedia"][0]["source_url"])
                except Exception:
                    img = ""
                ts = calendar.timegm(time.strptime(p["date_gmt"][:19], "%Y-%m-%dT%H:%M:%S"))
                if link and title:
                    out.append((hashlib.sha1(link.encode()).hexdigest()[:16], src["key"], src["kind"], title,
                                _clean(p["excerpt"]["rendered"], 300), link, img, "", min(ts, now), now))
            except Exception:
                continue
        if len(posts) < 50:
            break
        time.sleep(2)
    return out

def _candidates(src):  # explicit feeds first, then whatever the home page advertises, then common paths
    for u in src.get("feeds") or []:
        yield u
    home = src.get("home")
    if not home:
        return
    try:
        raw = _get(home)[1].decode("utf-8", "replace")
        for m in re.finditer(r"<link[^>]+>", raw, re.I):
            t = m.group(0)
            if re.search(r"alternate", t, re.I) and re.search(r"(rss|atom)\+xml", t, re.I):
                h = re.search(r"""href=["']([^"']+)""", t)
                if h:
                    yield urllib.parse.urljoin(home, _html.unescape(h.group(1)))
        if "youtube.com" in home:  # channel pages: build the channel's video feed from its id
            ym = re.search(r'(?:"channelId"|"externalId"|"browseId"):"(UC[\w-]{22})"|youtube\.com/channel/(UC[\w-]{22})', raw)
            if ym:
                yield "https://www.youtube.com/feeds/videos.xml?channel_id=" + (ym.group(1) or ym.group(2))
    except Exception:
        pass
    for p in ("feed/", "feed", "rss", "rss.xml", "atom.xml", "index.xml"):
        yield urllib.parse.urljoin(home.rstrip("/") + "/", p)

def _refresh_one(src):
    key, c = src["key"], _dbi()
    try:
        c.execute("INSERT INTO srcs(key) VALUES(?) ON CONFLICT(key) DO NOTHING", (key,))
        st = c.execute("SELECT * FROM srcs WHERE key=?", (key,)).fetchone()
        if st["error"] and time.time() - st["checked"] < 900:
            return
        feed, etag, mod, art, title, home, err = st["feed"], st["etag"], st["modified"], st["art"], st["title"], st["home"], ""
        if not feed and src.get("itunes"):  # podcasts: Apple's free search API tells us the show's real RSS feed
            try:
                q = urllib.parse.urlencode({"term": src["itunes"], "media": "podcast", "entity": "podcast", "limit": 1})
                hit = json.loads(_get(_ITUNES + "?" + q)[1].decode("utf-8", "replace"))["results"][0]
                feed, art = hit["feedUrl"], hit.get("artworkUrl600") or hit.get("artworkUrl100") or ""
                title, home = hit.get("collectionName") or "", hit.get("collectionViewUrl") or ""
            except Exception as ex:
                err = "podcast lookup failed: %s" % ex

        def urls():
            if feed:
                yield feed
            for u in _candidates(src):
                if u != feed:
                    yield u

        parsed = None
        for u in urls():
            try:
                code, raw, hdr = _get(u, etag if u == feed else None, mod if u == feed else None)
            except Exception as ex:
                err = "%s: %s" % (u, ex)
                continue
            if code == 304:
                parsed, err = "same", ""
                break
            p = feedparser.parse(raw)
            if p.entries:
                parsed, feed, etag, mod, err = p, u, hdr.get("ETag"), hdr.get("Last-Modified"), ""
                break
            err = "%s: no articles found in this feed" % u
        if parsed not in (None, "same"):
            _upsert(c, _rows(src, parsed, art))
            if src.get("backfill") == "wp" and not st["backfilled"] and src.get("home"):
                _upsert(c, _wp_backfill(src))
                c.execute("UPDATE srcs SET backfilled=1 WHERE key=?", (key,))
        c.execute("UPDATE srcs SET feed=?, etag=?, modified=?, checked=?, error=?, title=COALESCE(NULLIF(?,''),title), "
                  "home=COALESCE(NULLIF(?,''),home), art=COALESCE(NULLIF(?,''),art) WHERE key=?",
                  (feed, etag, mod, int(time.time()), err, title, home, art, key))
        c.commit()
    except Exception as ex:
        try:
            c.execute("UPDATE srcs SET checked=?, error=? WHERE key=?", (int(time.time()), str(ex)[:200], key))
            c.commit()
        except Exception:
            pass
    finally:
        c.close()

async def _refresh_all():
    if _NS["busy"] or feedparser is None:
        return
    _NS["busy"] = True
    try:
        await asyncio.to_thread(lambda: _dbi().close())  # create the tables once before the workers start
        sem = asyncio.Semaphore(4)

        async def one(src):
            async with sem:
                await asyncio.to_thread(_refresh_one, src)

        await asyncio.gather(*(one(s) for s in await asyncio.to_thread(_sources)), return_exceptions=True)
        _NT.clear()
        _NS["last"] = time.time()
    finally:
        _NS["busy"] = False

async def _news_loop():
    while True:
        try:
            await _refresh_all()
        except Exception as ex:
            print("news refresh failed:", ex)
        await asyncio.sleep(300)  # look for new articles every 5 minutes

async def _ensure():
    if not _NS["started"]:
        _NS["started"] = True
        _NS["task"] = asyncio.create_task(_news_loop())

try:
    app.router.on_startup.append(_ensure)  # start fetching as soon as the server starts (also starts on first request)
except Exception:
    pass

_TIT, _LC, _NT = {}, {}, {}
TIT_Q = "query($ids:[Int]){Page(perPage:50){media(id_in:$ids,type:ANIME){id title{romaji english} synonyms}}}"

async def _titles(ids):
    need = [i for i in ids if i not in _TIT or time.time() - _TIT[i][0] > 43200]
    if need:
        try:
            for m in (await gql(TIT_Q, {"ids": need}))["Page"]["media"]:
                names = [m["title"].get("english"), m["title"].get("romaji")] + (m.get("synonyms") or [])[:4]
                ks = []
                for n in names:
                    for v in (n or "", re.split(r"\s*[:\u2013\u2014]\s*|\s+-\s+", n or "")[0]):  # full title and short form
                        k = " " + _norm(v) + " "
                        if len(k) >= 7 and k not in ks:
                            ks.append(k)
                _TIT[m["id"]] = (time.time(), ks)
        except Exception:
            pass
    return {i: _TIT[i][1] for i in ids if i in _TIT and _TIT[i][1]}

def _query(kind, source, q, tit, page, per):
    c = _dbi()
    try:
        where, args = [], []
        ks = [k for k in (kind or "").split(",") if k][:5]
        if ks:
            where.append("kind IN (%s)" % ",".join("?" * len(ks))); args += ks
        if source:
            where.append("source=?"); args.append(source)
        if q:
            where.append("(title LIKE ? ESCAPE '\\' OR excerpt LIKE ? ESCAPE '\\')"); args += ["%" + re.sub(r"([\\%_])", r"\\\1", q) + "%"] * 2
        sql = "SELECT * FROM items" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY published DESC"
        if not tit:
            total = c.execute("SELECT COUNT(*) FROM items" + (" WHERE " + " AND ".join(where) if where else ""), args).fetchone()[0]
            rows = c.execute(sql + " LIMIT ? OFFSET ?", args + [per, (page - 1) * per]).fetchall()
            return total, [(r, []) for r in rows]
        hits = []
        for r in c.execute(sql + " LIMIT 3000", args).fetchall():
            text = _NT.get(r["id"]) or _NT.setdefault(r["id"], " " + _norm(r["title"] + " " + r["excerpt"]) + " ")
            m = [i for i, ts in tit.items() if any(t in text for t in ts)]
            if m:
                hits.append((r, m))
        return len(hits), hits[(page - 1) * per: page * per]
    finally:
        c.close()

def _count():
    c = _dbi()
    try:
        return c.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    finally:
        c.close()

_YT = re.compile(r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})")

def _pub(r, names, anime=None):
    m = _YT.search(r["link"] or "")
    return {"id": r["id"], "source": r["source"], "sourceName": names.get(r["source"], r["source"]), "kind": r["kind"],
            "title": r["title"], "excerpt": r["excerpt"], "link": r["link"], "image": r["image"], "audio": r["audio"],
            "video": m.group(1) if m else "", "published": r["published"], "anime": anime or []}

@app.get("/api/news/item/{iid}")
async def nx_item(iid: str):
    def go():
        c = _dbi()
        try:
            r = c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
            more = c.execute("SELECT * FROM items WHERE source=? AND id<>? ORDER BY published DESC LIMIT 4", (r["source"], iid)).fetchall() if r else []
            return r, more
        finally:
            c.close()
    r, more = await asyncio.to_thread(go)
    if not r:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="not found")
    names = {s["key"]: s["name"] for s in await asyncio.to_thread(_sources)}
    return {**_pub(r, names), "more": [_pub(x, names) for x in more]}

@app.get("/api/news")
async def nx_news(type: str = "", source: str = "", q: str = "", ids: str = "", page: int = 1, per: int = 12):
    await _ensure()
    if feedparser is None:
        return {"items": [], "total": 0, "page": 1, "pages": 1, "error": "pip install feedparser"}
    page, per = max(1, page), max(1, min(per, 48))
    if not _NS["last"] and await asyncio.to_thread(_count) == 0:  # very first run: give the first feeds a few seconds to land
        for _ in range(12):
            await asyncio.sleep(1)
            if await asyncio.to_thread(_count):
                break
    idl = [int(i) for i in ids.split(",") if i.strip().isdigit()][:50]
    tit = await _titles(idl) if idl else {}
    if idl and not tit:
        total, rows = 0, []
    else:
        total, rows = await asyncio.to_thread(_query, type, source, q.strip(), tit, page, per)
    names = {s["key"]: s["name"] for s in await asyncio.to_thread(_sources)}
    items = [_pub(r, names, m) for r, m in rows]
    return {"items": items, "total": total, "page": page, "pages": max(1, -(-total // per)), "updated": int(_NS["last"])}

@app.get("/api/sources")
async def nx_sources():
    await _ensure()
    def go():
        c = _dbi()
        try:
            cnt = dict(c.execute("SELECT source, COUNT(*) FROM items GROUP BY source").fetchall())
            st = {r["key"]: r for r in c.execute("SELECT * FROM srcs")}
        finally:
            c.close()
        out = []
        for s in _sources():
            r = st.get(s["key"])
            out.append({"key": s["key"], "name": s["name"], "kind": s["kind"], "home": s.get("home") or (r["home"] if r else "") or "",
                        "count": cnt.get(s["key"], 0), "checked": (r["checked"] if r else 0), "error": ((r["error"] if r else "") or ""),
                        "matched": ((r["title"] if r else "") or "") if s["kind"] == "podcast" else ""})
        return out
    return await asyncio.to_thread(go)

@app.get("/api/news/refresh")
async def nx_refresh():
    await _ensure()
    if feedparser is None:
        return {"started": False, "error": "pip install feedparser"}
    if not _NS["busy"] and time.time() - _NS["last"] > 300:
        _NS["manual"] = asyncio.create_task(_refresh_all())
        return {"started": True}
    return {"started": False, "busy": _NS["busy"], "last": int(_NS["last"])}

LNK_Q = "query($id:Int){Media(id:$id){siteUrl idMal title{romaji english} externalLinks{site url type}}}"

@app.get("/api/links/{mid}")
async def nx_links(mid: int):
    hit = _LC.get(mid)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    m = (await gql(LNK_Q, {"id": mid}))["Media"]
    if not m:
        raise HTTPException(404, "not found")
    name = m["title"].get("english") or m["title"].get("romaji") or ""
    out = [{"label": "AniList", "url": m["siteUrl"], "kind": "database"}]
    if m.get("idMal"):
        out.append({"label": "MyAnimeList", "url": "https://myanimelist.net/anime/%d" % m["idMal"], "kind": "database"})
    out.append({"label": "Fandom wiki search", "url": "https://www.fandom.com/search?q=" + urllib.parse.quote(name), "kind": "wiki"})
    for e in m.get("externalLinks") or []:
        if _ok(e.get("url")):
            out.append({"label": {"Twitter": "X / Twitter"}.get(e["site"], e["site"]), "url": e["url"], "kind": (e.get("type") or "INFO").lower()})
    if len(_LC) > 500:
        _LC.clear()
    _LC[mid] = (time.time(), out)
    return out

@app.get("/")
def home():
    for n in ("index.html", "index-10.html"):
        if os.path.exists(os.path.join(_D, n)):
            return FileResponse(os.path.join(_D, n))
    raise HTTPException(404, "put index.html next to main.py")


@app.get("/admin")
def admin():
    p = os.path.join(_D, "admin.html")
    if not os.path.exists(p):
        raise HTTPException(404, "admin.html is missing")
    return FileResponse(p)


@app.get("/health")
def health():  # cheap ping target for the uptime monitor
    return {"ok": 1}


async def _warm():
    while True:
        for f in (trending, season, upcoming, schedule):
            try:
                await f()
            except Exception as ex:
                print("warm-up failed:", ex)
        await asyncio.sleep(540)


_TASKS = {}


def _start_warm():
    _TASKS["warm"] = asyncio.create_task(_warm())


try:
    app.router.on_startup.append(_start_warm)
except AttributeError:
    pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
