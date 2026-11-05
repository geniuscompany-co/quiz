#!/usr/bin/env python3
"""Kahoot-style live quiz server. Standard library only. Run: python3 server.py [port]"""
import json, random, sys, time, os, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
LOCK = threading.Lock()
GAMES = {}
DEFAULT_TIME = 20  # seconds; override per question with "t" in quiz.json
PREVIEW_TIME = 5.0  # seconds to read question alone before answers appear and timer starts


def qtime(g, i=None):
    return g["quiz"][g["q"] if i is None else i].get("t", DEFAULT_TIME)


def qmult(g, i=None):
    idx = g["q"] if i is None else i
    q = g["quiz"][idx]
    if "double" in q:
        return 2 if q["double"] else 1
    return 2 if idx == len(g["quiz"]) - 1 and len(g["quiz"]) > 1 else 1


QUIZ = [
    {"q": "What is the capital of France?", "a": ["Berlin", "Paris", "Madrid", "Rome"], "c": 1},
    {"q": "Which planet is known as the Red Planet?", "a": ["Venus", "Jupiter", "Mars", "Mercury"], "c": 2},
    {"q": "What is 7 x 8?", "a": ["54", "56", "64", "48"], "c": 1},
    {"q": "Which ocean is the largest?", "a": ["Atlantic", "Indian", "Arctic", "Pacific"], "c": 3},
    {"q": "Who painted the Mona Lisa?", "a": ["Van Gogh", "Picasso", "Da Vinci", "Monet"], "c": 2},
]


def list_quizzes():
    items = []
    labels = {
        "quiz.json": "Current Default Quiz",
        "quiz_chapter8.json": "Chapter 8: Overcoming Challenges & Regulations",
        "quiz_fun_facts.json": "Fun Facts Trivia",
    }
    files = sorted(f for f in os.listdir(ROOT) if f == "quiz.json" or (f.startswith("quiz_") and f.endswith(".json")))
    for f in files:
        try:
            with open(os.path.join(ROOT, f)) as fp:
                q = json.load(fp)
            if isinstance(q, list) and q:
                title = labels.get(f) or f[5:-5].replace("_", " ").title()
                items.append({"file": f, "title": "%s (%d Qs)" % (title, len(q)), "count": len(q)})
        except Exception:
            pass
    return items


def load_quiz(fname="quiz.json"):
    safe = os.path.basename(fname or "quiz.json")
    p = os.path.join(ROOT, safe)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return QUIZ


def new_game(quiz_file="quiz.json", count=None, shuffle=False):
    while True:
        pin = str(random.randint(100000, 999999))
        if pin not in GAMES:
            break
    bank = list(load_quiz(quiz_file))
    if count is not None:
        try:
            k = max(1, min(len(bank), int(count)))
        except Exception:
            k = len(bank)
    else:
        k = len(bank)
    if k < len(bank) or shuffle:
        quiz = random.sample(bank, k)
    else:
        quiz = bank
    GAMES[pin] = {
        "pin": pin, "host": "h%08x" % random.getrandbits(32), "quiz": quiz,
        "phase": "lobby", "q": -1, "start": 0, "preview": PREVIEW_TIME, "players": {},
        "reactions": [], "rseq": 0,
    }
    return GAMES[pin]


def calc_streak(p, up_to_q):
    s = 0
    for qi in range(up_to_q + 1):
        a = p["answers"].get(qi)
        if a and a.get("correct"):
            s += 1
        else:
            s = 0
    return s


def calc_max_streak(p, total_q):
    best = cur = 0
    for qi in range(total_q):
        a = p["answers"].get(qi)
        if a and a.get("correct"):
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 0
    return best


def ptag(p):
    return ((p.get("avatar") + " ") if p.get("avatar") else "") + p["name"]


def tick(g):
    """Move question -> results when time is up or everyone answered."""
    if g["phase"] == "question":
        ps = g["players"].values()
        done = ps and all(g["q"] in p["answers"] for p in ps)
        elapsed = time.time() - g["start"] - g.get("preview", PREVIEW_TIME)
        if elapsed >= qtime(g) or done:
            g["phase"] = "results"


def leaderboard(g):
    q = g["q"]
    rows = []
    for p in g["players"].values():
        a = p["answers"].get(q, {}) if q >= 0 else {}
        gained = a.get("gained", 0)
        streak = calc_streak(p, q) if q >= 0 else 0
        rows.append({
            "name": p["name"], "avatar": p.get("avatar", ""),
            "score": p["score"], "prev": p["score"] - gained,
            "gained": gained, "streak": streak,
        })
    prev_order = [x["name"] for x in sorted(rows, key=lambda x: -x["prev"])]
    rows.sort(key=lambda x: -x["score"])
    for i, r in enumerate(rows):
        r["prev_rank"] = prev_order.index(r["name"]) + 1
        r["rank"] = i + 1
    return rows


def round_shoutouts(g, rows):
    q = g["q"]
    shouts = []
    # Fastest correct answer on this question
    fastest = None
    for p in g["players"].values():
        a = p["answers"].get(q)
        if a and a.get("correct") and "time" in a:
            if fastest is None or a["time"] < fastest[0]:
                fastest = (a["time"], p)
    if fastest:
        shouts.append("⚡ Fastest Finger: %s (%.1fs)" % (ptag(fastest[1]), fastest[0]))
    # Biggest rank climber
    if q > 0:
        climber = None
        for r in rows:
            diff = r["prev_rank"] - r["rank"]
            if diff > 0 and (climber is None or diff > climber[0]):
                climber = (diff, r)
        if climber:
            c = climber[1]
            label = ((c["avatar"] + " ") if c.get("avatar") else "") + c["name"]
            shouts.append("🚀 Biggest Climber: %s (+%d %s)" % (label, climber[0], "spot" if climber[0] == 1 else "spots"))
    # Highest active streak
    top_streak = max(rows, key=lambda r: r["streak"], default=None)
    if top_streak and top_streak["streak"] >= 2:
        label = ((top_streak["avatar"] + " ") if top_streak.get("avatar") else "") + top_streak["name"]
        shouts.append("🔥 On Fire: %s (%d in a row!)" % (label, top_streak["streak"]))
    return shouts[:3]


def final_stats(g):
    total_q = len(g["quiz"])
    total_ans = 0
    total_cor = 0
    speed_best = None
    streak_best = None
    for p in g["players"].values():
        cor_times = []
        for qi in range(total_q):
            a = p["answers"].get(qi)
            if a:
                total_ans += 1
                if a.get("correct"):
                    total_cor += 1
                    if "time" in a:
                        cor_times.append(a["time"])
        if cor_times:
            avg_t = sum(cor_times) / len(cor_times)
            if speed_best is None or avg_t < speed_best[0]:
                speed_best = (avg_t, p)
        ms = calc_max_streak(p, total_q)
        if ms >= 2 and (streak_best is None or ms > streak_best[0]):
            streak_best = (ms, p)
    denom = max(1, len(g["players"]) * total_q)
    accuracy = round(100.0 * total_cor / denom)
    awards = []
    if speed_best:
        awards.append({"title": "⚡ Speed Demon", "winner": ptag(speed_best[1]), "detail": "%.1fs avg" % speed_best[0]})
    if streak_best:
        awards.append({"title": "🔥 Longest Streak", "winner": ptag(streak_best[1]), "detail": "%d in a row" % streak_best[0]})
    return {"accuracy": accuracy, "correct": total_cor, "possible": denom, "awards": awards}


def view(g, pid=None, host=False):
    tick(g)
    ph = g["phase"]
    now = time.time()
    g["reactions"] = [r for r in g.get("reactions", []) if now - r["t"] < 8]
    out = {
        "phase": ph, "pin": g["pin"], "n": len(g["players"]), "total": len(g["quiz"]),
        "names": [ptag(p) for p in g["players"].values()] if ph == "lobby" else None,
        "reactions": g["reactions"][-25:],
    }
    if g["q"] >= 0 and ph in ("question", "results"):
        q = g["quiz"][g["q"]]
        prev_t = g.get("preview", PREVIEW_TIME)
        elapsed = now - g["start"]
        preview_left = max(0.0, prev_t - elapsed) if ph == "question" else 0.0
        q_elapsed = max(0.0, elapsed - prev_t)
        out.update(
            index=g["q"], question=q["q"], answers=q["a"], qtime=qtime(g),
            left=max(0.0, qtime(g) - q_elapsed),
            preview_left=round(preview_left, 2),
            preview_total=prev_t,
            double=(qmult(g) == 2),
        )
        out["answered"] = sum(1 for p in g["players"].values() if g["q"] in p["answers"])
        if ph == "results":
            out["correct"] = q["c"]
            out["quote"] = q.get("quote")
            out["source"] = q.get("src")
            counts = [0] * len(q["a"])
            for p in g["players"].values():
                a = p["answers"].get(g["q"])
                if a:
                    counts[a["choice"]] += 1
            out["counts"] = counts
    if ph == "board":
        rows = leaderboard(g)
        out["index"] = g["q"]
        out["board"] = rows[:10]
        out["shoutouts"] = round_shoutouts(g, rows)
    if ph == "final":
        out["board"] = leaderboard(g)[:10]
        out["stats"] = final_stats(g)
    if pid and pid in g["players"]:
        p = g["players"][pid]
        a = p["answers"].get(g["q"])
        streak = calc_streak(p, g["q"]) if g["q"] >= 0 else 0
        pu = p.get("powerups", {})
        out["me"] = {
            "name": p["name"], "avatar": p.get("avatar", ""), "score": p["score"],
            "answered": a is not None,
            "choice": a["choice"] if a else None,
            "gained": a["gained"] if a else 0,
            "bonus": a.get("bonus", 0) if a else 0,
            "correct": a.get("correct", False) if a else False,
            "streak": streak,
            "rank": [x["name"] for x in leaderboard(g)].index(p["name"]) + 1,
            "pu_5050": not pu.get("5050_used", False),
            "pu_2x": not pu.get("2x_used", False),
            "active_2x": pu.get("2x_q") == g["q"],
            "hide": pu.get("hide", []) if pu.get("5050_q") == g["q"] else [],
        }
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj, code=200, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if path == "/api/quizzes":
            return self.send({"quizzes": list_quizzes()})
        if path == "/api/state":
            q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
            with LOCK:
                g = GAMES.get(q.get("pin"))
                if not g:
                    return self.send({"error": "Game not found"}, 404)
                return self.send(view(g, q.get("pid")))
        files = {"/": "host.html", "/host": "host.html", "/play": "play.html", "/logo.jpg": "logo.jpg"}
        if path in files:
            ctype = "image/jpeg" if path.endswith(".jpg") else "text/html; charset=utf-8"
            with open(os.path.join(ROOT, files[path]), "rb") as f:
                return self.send(f.read(), ctype=ctype)
        self.send({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        path = self.path
        with LOCK:
            if path == "/api/create":
                g = new_game(body.get("quiz") or "quiz.json", body.get("count"), bool(body.get("shuffle")))
                if "preview" in body:
                    g["preview"] = float(body["preview"])
                return self.send({"pin": g["pin"], "host": g["host"]})
            g = GAMES.get(body.get("pin"))
            if not g:
                return self.send({"error": "Game not found"}, 404)
            if path == "/api/join":
                name = (body.get("name") or "").strip()[:16]
                avatar = (body.get("avatar") or "").strip()[:4]
                if g["phase"] != "lobby":
                    return self.send({"error": "Game already started"}, 400)
                if not name or any(p["name"].lower() == name.lower() for p in g["players"].values()):
                    return self.send({"error": "Pick a unique nickname"}, 400)
                pid = "p%08x" % random.getrandbits(32)
                g["players"][pid] = {"name": name, "avatar": avatar, "score": 0, "answers": {}, "powerups": {}}
                return self.send({"pid": pid})
            if path == "/api/powerup":
                p = g["players"].get(body.get("pid"))
                kind = body.get("kind")
                if not p or g["phase"] != "question" or g["q"] in p["answers"]:
                    return self.send({"error": "Cannot use powerup"}, 400)
                pu = p.setdefault("powerups", {})
                if kind == "5050" and not pu.get("5050_used"):
                    q = g["quiz"][g["q"]]
                    wrongs = [i for i in range(len(q["a"])) if i != q["c"]]
                    random.shuffle(wrongs)
                    pu["5050_used"] = True
                    pu["5050_q"] = g["q"]
                    pu["hide"] = wrongs[:2]
                    return self.send({"ok": True, "hide": pu["hide"]})
                if kind == "2x" and not pu.get("2x_used"):
                    pu["2x_used"] = True
                    pu["2x_q"] = g["q"]
                    return self.send({"ok": True})
                return self.send({"error": "Already used"}, 400)
            if path == "/api/react":
                p = g["players"].get(body.get("pid"))
                emo = (body.get("emoji") or "").strip()[:4]
                if p and emo in ("🔥", "😂", "😮", "🎉", "👏", "❤️"):
                    g["rseq"] = g.get("rseq", 0) + 1
                    g["reactions"].append({
                        "id": g["rseq"], "emoji": emo, "name": p["name"], "t": time.time()
                    })
                return self.send({"ok": True})
            if path == "/api/answer":
                tick(g)
                p = g["players"].get(body.get("pid"))
                if not p or g["phase"] != "question" or g["q"] in p["answers"]:
                    return self.send({"error": "Cannot answer"}, 400)
                choice = int(body["choice"])
                q = g["quiz"][g["q"]]
                prev_t = g.get("preview", PREVIEW_TIME)
                t = max(0.0, time.time() - g["start"] - prev_t)
                is_correct = (choice == q["c"])
                pu = p.get("powerups", {})
                p_mult = 2 if pu.get("2x_q") == g["q"] else 1
                if is_correct:
                    prev_streak = calc_streak(p, g["q"] - 1) if g["q"] > 0 else 0
                    new_streak = prev_streak + 1
                    bonus = min(300, (new_streak - 1) * 100) if new_streak >= 2 else 0
                    base = round(1000 * (1 - min(1.0, t / qtime(g)) / 2)) * qmult(g) * p_mult
                    gained = base + bonus
                else:
                    bonus = 0
                    gained = 0
                p["score"] += gained
                p["answers"][g["q"]] = {
                    "choice": choice, "gained": gained, "bonus": bonus,
                    "correct": is_correct, "time": round(t, 2),
                }
                return self.send({"ok": True})
            if path == "/api/next":
                if body.get("host") != g["host"]:
                    return self.send({"error": "Forbidden"}, 403)
                tick(g)
                if g["phase"] == "question":
                    g["phase"] = "results"  # skip
                elif g["phase"] == "results":
                    g["phase"] = "board"  # top players page
                elif g["phase"] == "board" and g["q"] + 1 >= len(g["quiz"]):
                    g["phase"] = "final"
                elif g["phase"] in ("lobby", "board"):
                    g["q"] += 1
                    g["phase"] = "question"
                    g["start"] = time.time()
                return self.send({"ok": True})
        self.send({"error": "not found"}, 404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8000))
    print("Host:    http://localhost:%d/host\nPlayers: http://<your-ip>:%d/play" % (port, port))
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
