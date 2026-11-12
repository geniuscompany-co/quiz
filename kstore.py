"""Storage, accounts, quiz library, progress, homework and AI helpers for Brainrush!
Standard library only. Uses local files, plus an optional Upstash Redis REST database for durable storage."""
import base64, hashlib, hmac, json, os, queue, random, re, secrets, threading, time, zlib
import urllib.error, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(ROOT, ".data")


# ---------------------------------------------------------------- Store
class Store:
    """Key/value store: in-memory cache + local files + optional Upstash REST (async write-behind)."""

    def __init__(self):
        self.url = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
        self.token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
        self.remote = bool(self.url and self.token)
        self.cache = {}
        self.lock = threading.Lock()
        self.last_error = ""
        self.last_ok = 0
        self.pending = {}
        self._last_q = {}
        self._deferred = {}
        self.q = queue.Queue()
        if self.remote:
            threading.Thread(target=self._writer, daemon=True).start()

    # --- local files
    def _dir(self):
        d = os.path.join(DATA_DIR, "kv")
        os.makedirs(d, exist_ok=True)
        return d

    def _path(self, key):
        return os.path.join(self._dir(), urllib.parse.quote(key, safe="") + ".json")

    def _local_get(self, key):
        try:
            with open(self._path(key)) as fp:
                return fp.read()
        except Exception:
            return None

    def _local_set(self, key, val):
        try:
            tmp = self._path(key) + ".tmp"
            with open(tmp, "w") as fp:
                fp.write(val)
            os.replace(tmp, self._path(key))
        except Exception as e:
            self.last_error = "local write failed: %s" % e

    # --- remote
    def _req(self, method, path, data=None):
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + self.token})
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read().decode())

    def _writer(self):
        while True:
            key = self.q.get()
            with self.lock:
                item = self.pending.pop(key, None)
            if item is None:
                continue
            op, val = item
            for attempt in range(3):
                try:
                    if op == "set":
                        self._req("POST", "/set/" + urllib.parse.quote(key, safe=""), val.encode())
                    else:
                        self._req("POST", "/del/" + urllib.parse.quote(key, safe=""))
                    self.last_ok = time.time()
                    self.last_error = ""
                    break
                except Exception as e:
                    self.last_error = "cloud write failed: %s" % e
                    time.sleep(1.5)

    def _queue(self, key, op, val=None):
        if not self.remote:
            return
        with self.lock:
            self.pending[key] = (op, val)
        self.q.put(key)

    # --- public API
    def get(self, key):
        with self.lock:
            if key in self.cache:
                return self.cache[key]
        val = None
        if self.remote:
            try:
                r = self._req("GET", "/get/" + urllib.parse.quote(key, safe=""))
                val = r.get("result")
                self.last_ok = time.time()
                self.last_error = ""
            except Exception as e:
                self.last_error = "cloud read failed: %s" % e
        if val is None:
            val = self._local_get(key)
        with self.lock:
            self.cache[key] = val
        return val

    def set(self, key, val, remote_every=0):
        with self.lock:
            self.cache[key] = val
        self._local_set(key, val)
        if remote_every:
            now = time.time()
            if now - self._last_q.get(key, 0) >= remote_every:
                self._last_q[key] = now
                self._deferred.pop(key, None)
                self._queue(key, "set", val)
            else:
                self._deferred[key] = val
        else:
            self._deferred.pop(key, None)
            self._queue(key, "set", val)

    def flush_deferred(self, every):
        now = time.time()
        for key, val in list(self._deferred.items()):
            if now - self._last_q.get(key, 0) >= every:
                self._last_q[key] = now
                self._deferred.pop(key, None)
                self._queue(key, "set", val)

    def delete(self, key):
        with self.lock:
            self.cache[key] = None
        try:
            os.remove(self._path(key))
        except Exception:
            pass
        self._queue(key, "del")

    def get_json(self, key, default=None):
        v = self.get(key)
        if v is None:
            return default
        try:
            if v.startswith("z:"):
                return json.loads(zlib.decompress(base64.b64decode(v[2:])).decode())
            return json.loads(v)
        except Exception:
            return default

    def set_json(self, key, obj, compress=False, remote_every=0):
        raw = json.dumps(obj)
        if compress:
            raw = "z:" + base64.b64encode(zlib.compress(raw.encode(), 6)).decode()
        self.set(key, raw, remote_every)

    def status(self):
        return {
            "backend": "cloud" if self.remote else "local",
            "ok": (not self.last_error),
            "error": self.last_error,
            "last_ok": int(self.last_ok),
            "persistent": bool(self.remote),
        }


store = Store()
_lock = threading.Lock()


# ---------------------------------------------------------------- Accounts
def _secret():
    s = os.environ.get("SECRET_KEY")
    if s:
        return s.encode()
    v = store.get("secret")
    if not v:
        v = secrets.token_hex(32)
        store.set("secret", v)
    return v.encode()


def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 100000).hex()


def register(username, password, code=""):
    username = re.sub(r"[^A-Za-z0-9_.-]", "", str(username or ""))[:24]
    if len(username) < 3 or len(str(password or "")) < 6:
        return None, "Username needs 3+ characters and password 6+ characters"
    need = os.environ.get("SIGNUP_CODE") or os.environ.get("HOST_PASSWORD") or ""
    if need and not hmac.compare_digest(str(code or ""), need):
        return None, "Teacher signup code required (ask the server admin)"
    with _lock:
        accts = store.get_json("accounts", {})
        if username.lower() in accts:
            return None, "That username is taken"
        salt = secrets.token_hex(16)
        accts[username.lower()] = {"user": username, "salt": salt, "hash": _hash(password, salt), "created": int(time.time())}
        store.set_json("accounts", accts)
    return username, None


def login(username, password):
    accts = store.get_json("accounts", {})
    a = accts.get(str(username or "").lower())
    if not a or not hmac.compare_digest(a["hash"], _hash(str(password or ""), a["salt"])):
        return None
    return a["user"]


def make_token(user, days=30):
    payload = "%s|%d" % (user, int(time.time()) + days * 86400)
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode((payload + "|" + sig).encode()).decode()


def check_token(token):
    try:
        raw = base64.urlsafe_b64decode(str(token).encode()).decode()
        user, exp, sig = raw.rsplit("|", 2)
        good = hmac.new(_secret(), ("%s|%s" % (user, exp)).encode(), hashlib.sha256).hexdigest()[:32]
        if hmac.compare_digest(sig, good) and int(exp) > time.time():
            return user
    except Exception:
        pass
    return None


def has_accounts():
    return bool(store.get_json("accounts", {}))


# ---------------------------------------------------------------- User quizzes
def uq_load(owner):
    return store.get_json("uq:" + owner, {})


def uq_save(owner, title, questions, folder="", tags=None, qid=None):
    slug = re.sub(r"[^a-z0-9]+", "_", (title or "quiz").lower()).strip("_") or "quiz"
    qid = qid or "uq:%s:%s" % (owner, slug)
    with _lock:
        d = uq_load(owner)
        d[qid] = {"title": title, "folder": (folder or "").strip()[:40], "tags": [str(t).strip()[:24] for t in (tags or []) if str(t).strip()][:8],
                  "questions": questions, "updated": int(time.time())}
        store.set_json("uq:" + owner, d)
    return qid


def uq_get(qid):
    try:
        owner = qid.split(":", 2)[1]
    except Exception:
        return None
    return uq_load(owner).get(qid)


def uq_delete(owner, qid):
    with _lock:
        d = uq_load(owner)
        if qid in d:
            del d[qid]
            store.set_json("uq:" + owner, d)
            return True
    return False


def estimate_duration_sec(questions, qtime=15, preview=5):
    tot = 0
    for q in questions or []:
        if not isinstance(q, dict):
            continue
        t = q.get("type") or "mc"
        qt = q.get("t")
        thk = int(q.get("think") or 0)
        if t == "info":
            tot += 45
        elif t == "poll":
            tot += (qt or 45) + 35 + thk
        elif t == "rank":
            tot += (qt or 60) + 25 + thk
        elif t in ("open", "brainstorm"):
            tot += (qt or 120) + 35 + thk
        elif t == "scale":
            tot += (qt or 25) + 15 + thk
        else:
            tot += (qt or qtime) + preview + thk + 10
    return max(30, int(tot))


def uq_list(owners):
    items = []
    for o in owners:
        for qid, v in uq_load(o).items():
            qs = v.get("questions") or []
            es = estimate_duration_sec(qs)
            items.append({"file": qid, "title": "%s (%d Qs)" % (v["title"], len(qs)), "count": len(qs),
                          "folder": v.get("folder") or "My Quizzes", "tags": v.get("tags", []), "owner": o, "user": True,
                          "case": any(x.get("type") in ("poll", "open", "rank", "info") for x in qs),
                          "est_sec": es, "est_min": max(1, int(round(es / 60.0)))})
    return items


# ---------------------------------------------------------------- Uploaded Images
ALLOWED_IMG_MIMES = {"image/webp", "image/jpeg", "image/png", "image/gif"}


def img_save(owner, data_b64, mime="image/webp"):
    raw_s = str(data_b64 or "").strip()
    if raw_s.startswith("data:"):
        hdr, _, raw_s = raw_s.partition(",")
        m = re.match(r"data:(image/[a-z0-9.+-]+);base64", hdr, re.I)
        if m:
            mime = m.group(1).lower()
    mime = str(mime or "image/webp").lower().strip()
    if mime not in ALLOWED_IMG_MIMES:
        return None, "Unsupported image type"
    try:
        raw_bytes = base64.b64decode(raw_s, validate=False)
    except Exception:
        return None, "Invalid image data"
    if not raw_bytes or len(raw_bytes) > 320000:
        return None, "Image too large (max 300 KB after compression)"
    clean_b64 = base64.b64encode(raw_bytes).decode("ascii")
    iid = "".join(random.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(12))
    store.set_json("img:" + iid, {"mime": mime, "b64": clean_b64, "owner": owner or "_shared", "ts": int(time.time())})
    return "/api/img/" + iid, None


def img_get(iid):
    safe = re.sub(r"[^a-z0-9_-]", "", str(iid or "").lower())
    if not safe:
        return None, None
    rec = store.get_json("img:" + safe)
    if not rec or not rec.get("b64"):
        return None, None
    try:
        return base64.b64decode(rec["b64"]), rec.get("mime") or "image/webp"
    except Exception:
        return None, None


# ---------------------------------------------------------------- History + progress
def hist_add(entry):
    with _lock:
        h = store.get_json("history", [])
        h.append(entry)
        store.set_json("history", h[-300:])


def hist_get(owner):
    return [e for e in store.get_json("history", []) if e.get("owner", "_shared") == owner]


def hist_clear(owner):
    with _lock:
        h = [e for e in store.get_json("history", []) if e.get("owner", "_shared") != owner]
        store.set_json("history", h)


def progress_update(owner, name, entry):
    with _lock:
        d = store.get_json("progress:" + owner, {})
        rec = d.setdefault(name.lower(), {"name": name, "games": []})
        rec["name"] = name
        rec["games"].append(entry)
        rec["games"] = rec["games"][-40:]
        store.set_json("progress:" + owner, d)


def progress_report(owner):
    d = store.get_json("progress:" + owner, {})
    out = []
    for rec in d.values():
        g = rec["games"]
        if not g:
            continue
        pcts = [x["pct"] for x in g]
        avg = round(sum(pcts) / len(pcts))
        last3 = pcts[-3:]
        prev = pcts[:-3]
        l3 = round(sum(last3) / len(last3))
        trend = 0
        if prev:
            trend = l3 - round(sum(prev) / len(prev))
        topics = {}
        for x in g:
            for t, (c, n) in (x.get("topics") or {}).items():
                a = topics.setdefault(t, [0, 0])
                a[0] += c
                a[1] += n
        weak = sorted(([t, round(100.0 * c / n), n] for t, (c, n) in topics.items() if n >= 2), key=lambda r: r[1])[:5]
        status = "ok"
        if l3 < 55 or avg < 60:
            status = "struggling"
        elif trend <= -12:
            status = "declining"
        elif trend >= 12:
            status = "improving"
        out.append({"name": rec["name"], "games": len(g), "avg": avg, "recent": l3, "trend": trend, "status": status,
                    "weak": weak, "history": [{"ts": x["ts"], "quiz": x.get("quiz", ""), "pct": x["pct"]} for x in g[-10:]]})
    out.sort(key=lambda r: ({"struggling": 0, "declining": 1}.get(r["status"], 2), r["avg"]))
    return out


# ---------------------------------------------------------------- Homework
def hw_create(owner, title, questions, deadline, roster):
    hid = "".join(random.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(6))
    doc = {"id": hid, "owner": owner, "title": (title or "Homework")[:60], "created": int(time.time()),
           "deadline": int(deadline or 0), "roster": roster or [], "questions": questions, "subs": {}}
    store.set_json("hw:" + hid, doc)
    with _lock:
        lst = store.get_json("hwlist:" + owner, [])
        lst.append(hid)
        store.set_json("hwlist:" + owner, lst[-60:])
    return doc


def hw_get(hid):
    return store.get_json("hw:" + re.sub(r"[^a-z0-9]", "", str(hid).lower()))


def hw_list(owner):
    out = []
    for hid in reversed(store.get_json("hwlist:" + owner, [])):
        d = hw_get(hid)
        if d:
            subs = d["subs"].values()
            out.append({"id": d["id"], "title": d["title"], "created": d["created"], "deadline": d["deadline"],
                        "questions": len(d["questions"]), "started": len(d["subs"]), "done": sum(1 for s in subs if s.get("done")),
                        "roster": len(d.get("roster", []))})
    return out


def hw_open(doc):
    return not doc.get("deadline") or time.time() <= doc["deadline"]


def hw_perm(doc, name, i, n):
    rnd = random.Random("%s|%s|%d" % (doc["id"], name.lower(), i))
    p = list(range(n))
    for _ in range(12):
        rnd.shuffle(p)
        if p != list(range(n)):
            break
    return p


def hw_start(doc, name):
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:24]
    if not name:
        return None, "Enter your name"
    if doc.get("roster"):
        m = next((r for r in doc["roster"] if r.lower() == name.lower()), None)
        if not m:
            return None, "Pick your name from the class list"
        name = m
    if not hw_open(doc):
        sub = doc["subs"].get(name.lower())
        if not (sub and sub.get("done")):
            return None, "This assignment is closed"
    with _lock:
        d = hw_get(doc["id"])
        sub = d["subs"].get(name.lower())
        if not sub:
            sub = {"name": name, "started": int(time.time()), "answers": [], "done": False, "score": 0, "correct": 0, "streak": 0}
            d["subs"][name.lower()] = sub
            store.set_json("hw:" + d["id"], d)
    return sub, None


def hw_ans_text(q):
    t = q.get("type") or "mc"
    if q.get("all_correct"):
        return "Any answer"
    if t == "text":
        return " / ".join(q["a"][:3])
    if t == "order":
        return " -> ".join(q["a"])
    if t == "match":
        return ", ".join("%s -> %s" % (p["l"], p["r"]) for p in (q.get("pairs") or []))
    if t == "slider":
        sl = q.get("slider") or {}
        u = sl.get("unit") or ""
        mg = sl.get("margin") or 0
        return ("%g%s (+/-%g)" % (sl.get("target", 0), u, mg)) if mg else ("%g%s" % (sl.get("target", 0), u))
    if t == "pin":
        pn = q.get("pin") or {}
        return "Target (%g%%, %g%%)" % (pn.get("x", 50), pn.get("y", 50))
    if t == "multi":
        cm = q.get("correct_multi") or [q.get("c", 0)]
        return " + ".join(q["a"][k] for k in cm if 0 <= k < len(q["a"]))
    if q.get("correct_multi") and len(q["correct_multi"]) > 1:
        return " OR ".join(q["a"][k] for k in q["correct_multi"] if 0 <= k < len(q["a"]))
    return q["a"][q["c"]] if 0 <= q.get("c", 0) < len(q.get("a", [])) else ""


def hw_question(doc, sub):
    i = len(sub["answers"])
    if i >= len(doc["questions"]):
        return None
    q = doc["questions"][i]
    t = q.get("type") or "mc"
    a = q.get("a", [])
    out = {"i": i, "n": len(doc["questions"]), "q": q["q"], "type": t, "qtype": t, "pts": q.get("pts"), "a": a, "img": q.get("img"), "aimg": q.get("aimg")}
    if t == "order":
        p = hw_perm(doc, sub["name"], i, len(a))
        out["a"] = [q["a"][k] for k in p]
    elif t in ("text", "slider", "pin"):
        out["a"] = []
    elif t == "match":
        pairs = q.get("pairs") or []
        p = hw_perm(doc, sub["name"], i, len(pairs))
        out["match_left"] = [x["l"] for x in pairs]
        out["match_right"] = [pairs[k]["r"] for k in p]
        out["a"] = out["match_left"]
    if t == "slider" and q.get("slider"):
        sl = q["slider"]
        out["slider_cfg"] = {"min": sl["min"], "max": sl["max"], "step": sl["step"], "unit": sl.get("unit", "")}
    return out


def _norm(x):
    return re.sub(r"[^a-z0-9]+", " ", str(x).lower()).strip()


def hw_answer(doc, sub_key, body):
    with _lock:
        d = hw_get(doc["id"])
        sub = d["subs"].get(sub_key)
        if not sub or sub.get("done"):
            return None, "No active attempt"
        if not hw_open(d):
            return None, "This assignment is closed"
        i = len(sub["answers"])
        q = d["questions"][i]
        t = q.get("type") or "mc"
        ok, credit, given = False, 0.0, ""
        if t == "text":
            given = str(body.get("text") or "").strip()[:80]
            ok = bool(_norm(given)) and (bool(q.get("all_correct")) or any(_norm(x) == _norm(given) for x in q["a"]))
            credit = 1.0 if ok else 0.0
        elif t == "order":
            seq = body.get("seq")
            n = len(q["a"])
            if not isinstance(seq, list) or sorted(seq) != list(range(n)):
                return None, "Order every item"
            p = hw_perm(d, sub["name"], i, n)
            orig = [p[j] for j in seq]
            ok = bool(q.get("all_correct")) or (orig == list(range(n)))
            credit = 1.0 if ok else 0.0
            given = " -> ".join(q["a"][k] for k in orig)
        elif t == "multi":
            raw_ch = body.get("choices")
            if raw_ch is None and body.get("choice") is not None:
                raw_ch = [body.get("choice")]
            if not isinstance(raw_ch, list) or not raw_ch:
                return None, "Select at least one option"
            try:
                picked = sorted({int(x) for x in raw_ch})
            except Exception:
                return None, "Bad selection"
            if any(x < 0 or x >= len(q["a"]) for x in picked):
                return None, "Bad selection"
            req = set(q.get("correct_multi") or [q.get("c", 0)])
            pset = set(picked)
            if q.get("all_correct") or pset == req:
                ok, credit = True, 1.0
            elif q.get("partial", True):
                credit = max(0.0, (len(pset & req) - len(pset - req)) / float(max(1, len(req))))
                ok = False
            given = " + ".join(q["a"][k] for k in picked)
        elif t == "match":
            pairs = q.get("pairs") or []
            n = len(pairs)
            rel = body.get("pairs")
            if not isinstance(rel, list) or sorted(rel) != list(range(n)):
                return None, "Match every pair"
            p = hw_perm(d, sub["name"], i, n)
            hits = 0
            parts = []
            for li in range(n):
                orig_r = p[rel[li]]
                hit = (orig_r == li) or (pairs[orig_r]["r"].strip().lower() == pairs[li]["r"].strip().lower())
                if hit:
                    hits += 1
                parts.append("%s -> %s" % (pairs[li]["l"], pairs[orig_r]["r"]))
            ok = bool(q.get("all_correct")) or (hits == n)
            credit = 1.0 if ok else (hits / float(max(1, n)))
            given = ", ".join(parts)
        elif t == "slider":
            sl = q.get("slider") or {"min": 0, "max": 100, "step": 1, "target": 50, "margin": 0, "unit": ""}
            try:
                val = float(body.get("val"))
            except Exception:
                return None, "Enter a number"
            if val < sl["min"] - 1e-6 or val > sl["max"] + 1e-6:
                return None, "Value out of range"
            diff = abs(val - sl["target"])
            mg = float(sl.get("margin") or 0)
            if q.get("all_correct") or diff <= 1e-6:
                ok, credit = True, 1.0
            elif mg > 0 and diff <= mg + 1e-6:
                ok = True
                credit = max(0.75, 1.0 - 0.25 * (diff / mg))
            given = "%g%s" % (val, sl.get("unit", ""))
        elif t == "pin":
            pn = q.get("pin") or {"x": 50.0, "y": 50.0, "r": 12.0}
            xy = body.get("pin_xy")
            if not isinstance(xy, list) or len(xy) != 2:
                return None, "Tap the image to place your pin"
            try:
                px, py = float(xy[0]), float(xy[1])
            except Exception:
                return None, "Invalid pin"
            px, py = max(0.0, min(100.0, px)), max(0.0, min(100.0, py))
            dist = ((px - pn["x"]) ** 2 + (py - pn["y"]) ** 2) ** 0.5
            r_ = max(1.0, float(pn.get("r", 12.0)))
            if q.get("all_correct") or dist <= r_:
                ok = True
                credit = max(0.6, 1.0 - 0.4 * (dist / r_))
            given = "(%.0f%%, %.0f%%)" % (px, py)
        else:
            try:
                ch = int(body.get("choice"))
            except Exception:
                return None, "Bad choice"
            if not 0 <= ch < len(q["a"]):
                return None, "Bad choice"
            valid_set = set(q.get("correct_multi") or [q["c"]])
            ok = bool(q.get("all_correct")) or (ch in valid_set)
            credit = 1.0 if ok else 0.0
            given = q["a"][ch]
        q_pts = q.get("pts")
        scale = 1.0 if q_pts is None else (float(q_pts) / 1000.0)
        if ok:
            sub["streak"] += 1
            sub["correct"] += 1
            sub["score"] += int(round((100 * credit + min(100, (sub["streak"] - 1) * 25)) * scale))
        else:
            sub["streak"] = 0
            if credit > 0:
                sub["score"] += int(round(100 * credit * scale))
        sub["answers"].append({"ok": ok, "given": given, "credit": round(credit, 2)})
        if len(sub["answers"]) >= len(d["questions"]):
            sub["done"] = True
            sub["finished"] = int(time.time())
        store.set_json("hw:" + d["id"], d)
        ans_extra = {"pin_target": q.get("pin")} if t == "pin" else {}
        ans = {"ok": ok, "partial": (not ok and credit > 0), "answer": hw_ans_text(q),
               "quote": q.get("quote", ""), "done": sub["done"], "score": sub["score"], "correct": sub["correct"], "n": len(d["questions"]), **ans_extra}
    if ans["done"]:
        try:
            topics = {}
            for qi, a in enumerate(sub["answers"]):
                tp = d["questions"][qi].get("topic")
                if tp:
                    c = topics.setdefault(tp, [0, 0])
                    c[1] += 1
                    c[0] += 1 if a["ok"] else 0
            if d.get("roster"):
                progress_update(d["owner"], sub["name"], {"ts": int(time.time()), "quiz": d["title"] + " (homework)",
                                                         "pct": round(100.0 * sub["correct"] / len(d["questions"])), "topics": topics})
        except Exception:
            pass
    return ans, None


def hw_results(doc):
    rows = []
    n = len(doc["questions"])
    for s in doc["subs"].values():
        rows.append({"name": s["name"], "done": bool(s.get("done")), "answered": len(s["answers"]), "correct": s["correct"],
                     "pct": round(100.0 * s["correct"] / n) if n else 0, "score": s["score"], "finished": s.get("finished", 0)})
    rows.sort(key=lambda r: (-int(r["done"]), -r["pct"], r["name"].lower()))
    missing = [r for r in doc.get("roster", []) if r.lower() not in doc["subs"]]
    qmiss = []
    for qi, q in enumerate(doc["questions"]):
        tot = sum(1 for s in doc["subs"].values() if len(s["answers"]) > qi)
        bad = sum(1 for s in doc["subs"].values() if len(s["answers"]) > qi and not s["answers"][qi]["ok"])
        if tot:
            qmiss.append({"q": q["q"], "pct": round(100.0 * (tot - bad) / tot), "n": tot})
    qmiss.sort(key=lambda r: r["pct"])
    return {"title": doc["title"], "deadline": doc["deadline"], "questions": n, "rows": rows, "missing": missing, "hardest": qmiss[:5]}


# ---------------------------------------------------------------- Import / AI
def fetch_sheet_csv(url):
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", str(url))
    if not m:
        return None, "That doesn't look like a Google Sheets link"
    g = re.search(r"[#&?]gid=(\d+)", str(url))
    u = "https://docs.google.com/spreadsheets/d/%s/export?format=csv" % m.group(1)
    if g:
        u += "&gid=" + g.group(1)
    try:
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 Brainrush"})
        with urllib.request.urlopen(req, timeout=12) as r:
            txt = r.read().decode("utf-8", "replace")
        if txt.lstrip().lower().startswith("<!doctype html") or "<html" in txt[:200].lower():
            return None, "Could not read the sheet. Share it as 'Anyone with the link can view'."
        return txt, None
    except Exception as e:
        return None, "Could not fetch the sheet (%s). Make sure it is shared as 'Anyone with the link can view'." % e


def heuristic_questions(text, topic, count):
    flat = re.sub(r"\s+", " ", text or "")
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", flat) if 35 <= len(s.strip()) <= 240]
    random.shuffle(sents)
    pool = list({w.lower(): w for w in re.findall(r"[A-Za-z][A-Za-z'-]{5,}", flat)}.values())
    out = []
    for s in sents:
        nums = re.findall(r"\b\d[\d,]*\b", s)
        kind, cand = None, None
        if nums:
            kind, cand = "num", nums[0].rstrip(",")
        else:
            caps = re.findall(r"(?<![.!?] )(?<!^)\b[A-Z][a-z]{3,}\b", s)
            if caps:
                kind, cand = "word", caps[0]
            else:
                ws = re.findall(r"[A-Za-z][A-Za-z'-]{5,}", s)
                if ws:
                    kind, cand = "word", max(ws, key=len)
        if not cand:
            continue
        q = s.replace(cand, "_____", 1)
        if q == s:
            continue
        dist = []
        if kind == "num":
            try:
                n = int(cand.replace(",", ""))
                step = max(1, abs(n) // 8)
                for d in (step, -step, step * 2, -step * 2, step * 3):
                    v = n + d
                    if v >= 0 and str(v) != cand and str(v) not in dist:
                        dist.append(str(v))
            except Exception:
                pass
        else:
            cap = cand[0].isupper()
            others = [w for w in pool if w.lower() != cand.lower() and (w[0].isupper() == cap)]
            random.shuffle(others)
            dist = others[:3]
        dist = [d for d in dist if d.lower() != cand.lower()][:3]
        if len(dist) < 3:
            continue
        opts = dist + [cand]
        random.shuffle(opts)
        item = {"q": "Fill in the blank: " + q, "a": opts, "c": opts.index(cand), "quote": s}
        if topic:
            item["topic"] = topic
        out.append(item)
        if len(out) >= count:
            break
    return out


def heuristic_case(text, topic):
    title = (topic or "Ethical Leadership Dilemma").strip()
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text or "") if len(p.strip()) >= 25]
    if not paras and (text or "").strip():
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text)) if s.strip()]
        mid = max(1, len(sents) // 2)
        paras = [" ".join(sents[:mid]), " ".join(sents[mid:])]
    ch1 = paras[0][:1400] if paras else ("Scenario: You are leading a team facing a high-stakes decision on %s. "
                                         "Key metrics look promising on paper, but a colleague raises an urgent concern right before a public commitment." % title)
    ch2 = "\n\n".join(paras[1:3])[:1400] if len(paras) > 1 else ("New facts emerge: delaying action carries real financial and reputational cost, "
                                                                 "while moving ahead risks harming trust and stakeholders who have no voice in the room.")
    debrief = paras[3][:1400] if len(paras) > 3 else ("Debrief: Compare what the class chose at Decision Point 1 vs. after new pressures appeared. "
                                                      "Which ethical lens dominated, and what culture did your leadership create for the next person with bad news?")
    return [
        {"type": "info", "q": "Chapter 1 — %s" % title, "body": ch1, "topic": title[:40],
         "roles": [
             {"name": "Team Lead", "text": "You own the final call and are accountable for both performance targets and team integrity."},
             {"name": "Concerned Analyst", "text": "You spotted the risk early. You want leadership to take the evidence seriously without retaliation."},
             {"name": "Operations / Finance Partner", "text": "Every week of delay has real budget and schedule consequences for the organization."},
             {"name": "Affected Stakeholder", "text": "You were not in the meeting, yet you bear the direct impact if something goes wrong."},
         ],
         "note": "Give students 60 seconds to read Chapter 1 and check their private stakeholder role card on their phones."},
        {"type": "poll", "q": "Decision Point 1: Based on what you know right now, what is your move?", "topic": "Ethical Lenses",
         "a": ["Move forward on schedule while monitoring closely", "Pause and disclose the concern before proceeding",
               "Hold a firm red line until independent verification passes", "Convene affected stakeholders to co-design a mitigation plan"],
         "lenses": ["Utilitarian (outcomes)", "Virtue & transparency", "Rights & duty (deontological)", "Stakeholder & care ethics"],
         "note": "Cold-call one defender from two different options and ask how their assigned role shaped their vote."},
        {"type": "info", "q": "Chapter 2 — Mounting Pressure", "body": ch2, "topic": title[:40],
         "note": "Ask: Did this new information change the facts, or just raise the cost of doing the right thing?"},
        {"type": "rank", "q": "Rank these leadership priorities from MOST important (#1) to LEAST (#5) in this moment:", "topic": "Competing Values",
         "a": ["Preventing harm to affected people", "Candor and transparency", "Psychological safety for truth-tellers",
               "Meeting commitments and deadlines", "Short-term financial / reputational protection"],
         "note": "Highlight where the class consensus splits between #1 and #2."},
        {"type": "open", "q": "In 1–2 sentences, write what you will say to your team or stakeholders right now — and why.",
         "topic": "Moral Courage", "note": "Have students vote on the two most persuasive statements, then read the top vote-getter aloud."},
        {"type": "info", "q": "Epilogue & Debrief", "body": debrief, "topic": "Debrief",
         "note": "Close by asking: What structural safeguard would prevent this dilemma next time?"},
    ]


def ai_generate(text, topic, count, mode="quiz"):
    is_case = (mode == "case")
    count = max(1, min(25, int(count or (8 if not is_case else 8))))
    key = os.environ.get("GEMINI_API_KEY", "")
    if key:
        model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
        if is_case:
            prompt = (
                "Create an interactive Harvard Business School-style classroom case study (focused on ethical leadership and decision-making). "
                "Topic: %s.\n%s"
                "Return ONLY a JSON array of %d slides in narrative order mixing these item formats:\n"
                "1) Chapter / Debrief slide: {\"type\":\"info\", \"q\":\"Chapter title\", \"body\":\"2 short paragraphs separated by \\n\\n\", "
                "\"roles\":[{\"name\":\"Stakeholder Role\",\"text\":\"1-2 sentence private perspective\"}] (include 4 roles on Chapter 1), \"topic\":\"short tag\", \"note\":\"host facilitation prompt\"}\n"
                "2) Decision poll (no wrong answer): {\"type\":\"poll\", \"q\":\"Decision question\", \"a\":[4 distinct action choices], "
                "\"lenses\":[\"Utilitarian lens\",\"Rights/Duty lens\",\"Virtue/Integrity lens\",\"Care/Stakeholder lens\"], \"topic\":\"short tag\", \"note\":\"host cold-call prompt\"}\n"
                "3) Priority ranking: {\"type\":\"rank\", \"q\":\"Rank these priorities...\", \"a\":[4 to 5 competing values], \"topic\":\"short tag\", \"note\":\"host prompt\"}\n"
                "4) Open response with peer voting: {\"type\":\"open\", \"q\":\"1-2 sentence justification prompt\", \"topic\":\"short tag\", \"note\":\"host prompt\"}\n"
                "5) Concept check MCQ (1-2 near the end): {\"q\":\"Question\", \"a\":[4 options], \"c\":0-3, \"quote\":\"1-sentence takeaway\", \"topic\":\"short tag\"}\n"
                "Make the dilemma realistic with genuine tension between competing responsibilities."
                % (topic or "Ethical Leadership",
                   ("Base the case study on this material:\n\"\"\"%s\"\"\"\n" % text[:7000]) if text.strip() else "",
                   max(6, min(12, count)))
            )
        else:
            prompt = ("Create %d multiple-choice quiz questions for a classroom game. Topic: %s.\n%s"
                      "Return ONLY a JSON array. Each item: {\"q\": question, \"a\": [4 answer strings], \"c\": index 0-3 of the correct answer, "
                      "\"quote\": one-sentence explanation or fun fact, \"topic\": short topic tag}. Make wrong answers plausible and vary the position of the correct answer."
                      % (count, topic or "general knowledge",
                         ("Base the questions ONLY on this text:\n\"\"\"%s\"\"\"\n" % text[:7000]) if text.strip() else ""))
        body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                           "generationConfig": {"responseMimeType": "application/json", "temperature": 0.7}}).encode()
        url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s" % (model, key)
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.loads(r.read().decode())
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            items = json.loads(raw)
            if isinstance(items, dict):
                items = items.get("questions", [])
            return items, "AI draft generated. Review and edit before saving."
        except Exception as e:
            err = "AI request failed (%s)." % e
            if is_case:
                return heuristic_case(text, topic), err + " Built a structured offline case template instead."
            if text.strip():
                return heuristic_questions(text, topic, count), err + " Used the offline generator instead."
            return None, err
    if is_case:
        return heuristic_case(text, topic), "Structured case-study draft ready (chapters, roles, ethical lenses, ranking & open prompt). Add GEMINI_API_KEY on the server for full AI-written cases."
    if not text.strip():
        return None, "Paste some text to build questions from, or ask the server admin to add a GEMINI_API_KEY for topic-only AI generation."
    qs = heuristic_questions(text, topic, count)
    if not qs:
        return None, "Could not find enough usable sentences. Paste a longer passage."
    return qs, "Offline draft (fill-in-the-blank from your text). Add a GEMINI_API_KEY on the server for full AI questions."
