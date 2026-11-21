# Run from anywhere:  python3 tests/matching.test.py   (needs Python 3 and Node)
# Cross-language check: the name-matching rules in build_data.py (Python) and nx-shim.js (JavaScript) must produce identical keys,
# and the prebuilt matches.json must equal a fresh computation from news.json + index.json.
import os, re, json, subprocess, sys
root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..") + "/"
b = open(root + "scripts/build_data.py", encoding="utf-8").read()
src = b[b.index("def nrm(x)"):b.index("texts = {")]
ns = {"re": re}; exec(src, ns); name_keys = ns["name_keys"]; nrm = ns["nrm"]
js = open(root + "site/nx-shim.js", encoding="utf-8").read()
fn = re.search(r"const norm=.*?\n", js).group(0) + re.search(r"const keys=.*?\n", js).group(0)
samples = [["Re:Zero \u2212 Starting Life in Another World", "Re:Zero kara Hajimeru Isekai Seikatsu"], ["Sword Art Online - Alicization", "SAO"], ["Fate/stay night: Heaven's Feel", None],
  ["Kaguya-sama: Love Is War", "Kaguya-sama wa Kokurasetai"], ["\u307c\u3063\u3061\u30fb\u3056\u30fb\u308d\u3063\u304f\uff01", "Bocchi the Rock!"], ["86 -Eighty Six-", "86: Eighty-Six"], ["K", "Z"], ["Mob Psycho 100 II", ""],
  ["Gintama\u00b0", "Gintama: Silver Soul Arc \u2014 Second Part"], ["  Spaced   Out  ", "Dr. STONE: New World"], ["Boku no Hero Academia 7th Season", "My Hero Academia Season 7"]]
out = subprocess.run(["node", "-e", fn + "\nconst S=JSON.parse(process.argv[1]);console.log(JSON.stringify(S.map(n=>keys(n))))", json.dumps(samples)], capture_output=True, text=True)
jsk = json.loads(out.stdout); bad = 0
for n, a in zip(samples, jsk):
    p = name_keys(n)
    if p != a: bad += 1; print("MISMATCH", n, "\n   py:", p, "\n   js:", a)
print("keys: %d name sets compared, %d mismatches" % (len(samples), bad))
D = root + "site/data/"
diff = 0
if os.path.exists(D + "anime/matches.json"):  # only when a build has produced the data
    items = json.load(open(D + "news.json"))["items"]; idx = json.load(open(D + "anime/index.json")); mm = json.load(open(D + "anime/matches.json"))
    texts = {r["id"]: " " + nrm((r.get("title") or "") + " " + (r.get("excerpt") or "")) + " " for r in items}
    diff = sum(1 for i, e in idx.items() if mm[i] != [rid for rid, t in texts.items() if any(k in t for k in name_keys(e["n"]))])
    print("matches.json: %d anime, %d disagree with a fresh computation" % (len(mm), diff))
else: print("(no site/data/anime/matches.json yet: run the build first to check it too)")
sys.exit(1 if bad or diff else 0)
