#!/usr/bin/env python3
"""Kahoot-style live quiz server. Standard library only. Run: python3 server.py [port]"""
import json, random, sys, time, os, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
LOCK = threading.Lock()
GAMES = {}
DEFAULT_TIME = 20  # seconds; override per question with "t" in quiz.json


def qtime(g, i=None):
    return g["quiz"][g["q"] if i is None else i].get("t", DEFAULT_TIME)

QUIZ = [
    {"q": "What is the capital of France?", "a": ["Berlin", "Paris", "Madrid", "Rome"], "c": 1},
    {"q": "Which planet is known as the Red Planet?", "a": ["Venus", "Jupiter", "Mars", "Mercury"], "c": 2},
    {"q": "What is 7 x 8?", "a": ["54", "56", "64", "48"], "c": 1},
    {"q": "Which ocean is the largest?", "a": ["Atlantic", "Indian", "Arctic", "Pacific"], "c": 3},
    {"q": "Who painted the Mona Lisa?", "a": ["Van Gogh", "Picasso", "Da Vinci", "Monet"], "c": 2},
]


def load_quiz():
    p = os.path.join(ROOT, "quiz.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return QUIZ


def new_game():
    while True:
        pin = str(random.randint(100000, 999999))
        if pin not in GAMES:
            break
    GAMES[pin] = {
        "pin": pin, "host": "h%08x" % random.getrandbits(32), "quiz": load_quiz(),
        "phase": "lobby", "q": -1, "start": 0, "players": {},
    }
    return GAMES[pin]


def tick(g):
    """Move question -> results when time is up or everyone answered."""
    if g["phase"] == "question":
        ps = g["players"].values()
        done = ps and all(g["q"] in p["answers"] for p in ps)
        if time.time() - g["start"] >= qtime(g) or done:
            g["phase"] = "results"


def leaderboard(g):
    return sorted(({"name": p["name"], "score": p["score"]} for p in g["players"].values()),
                  key=lambda x: -x["score"])


def view(g, pid=None, host=False):
    tick(g)
    ph = g["phase"]
    out = {"phase": ph, "pin": g["pin"], "n": len(g["players"]), "total": len(g["quiz"]),
           "names": [p["name"] for p in g["players"].values()] if ph == "lobby" else None}
    if g["q"] >= 0 and ph in ("question", "results"):
        q = g["quiz"][g["q"]]
        out.update(index=g["q"], question=q["q"], answers=q["a"], qtime=qtime(g),
                   left=max(0, qtime(g) - (time.time() - g["start"])))
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
            out["board"] = leaderboard(g)[:5]
    if ph == "final":
        out["board"] = leaderboard(g)[:10]
    if pid and pid in g["players"]:
        p = g["players"][pid]
        a = p["answers"].get(g["q"])
        out["me"] = {"name": p["name"], "score": p["score"], "answered": a is not None,
                     "choice": a["choice"] if a else None,
                     "gained": a["gained"] if a else 0,
                     "correct": a["gained"] > 0 if a else False,
                     "rank": [x["name"] for x in leaderboard(g)].index(p["name"]) + 1}
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
        if path == "/api/state":
            q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
            with LOCK:
                g = GAMES.get(q.get("pin"))
                if not g:
                    return self.send({"error": "Game not found"}, 404)
                return self.send(view(g, q.get("pid")))
        files = {"/": "host.html", "/host": "host.html", "/play": "play.html"}
        if path in files:
            with open(os.path.join(ROOT, files[path]), "rb") as f:
                return self.send(f.read(), ctype="text/html; charset=utf-8")
        self.send({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        path = self.path
        with LOCK:
            if path == "/api/create":
                g = new_game()
                return self.send({"pin": g["pin"], "host": g["host"]})
            g = GAMES.get(body.get("pin"))
            if not g:
                return self.send({"error": "Game not found"}, 404)
            if path == "/api/join":
                name = (body.get("name") or "").strip()[:16]
                if g["phase"] != "lobby":
                    return self.send({"error": "Game already started"}, 400)
                if not name or any(p["name"].lower() == name.lower() for p in g["players"].values()):
                    return self.send({"error": "Pick a unique nickname"}, 400)
                pid = "p%08x" % random.getrandbits(32)
                g["players"][pid] = {"name": name, "score": 0, "answers": {}}
                return self.send({"pid": pid})
            if path == "/api/answer":
                tick(g)
                p = g["players"].get(body.get("pid"))
                if not p or g["phase"] != "question" or g["q"] in p["answers"]:
                    return self.send({"error": "Cannot answer"}, 400)
                choice = int(body["choice"])
                q = g["quiz"][g["q"]]
                t = time.time() - g["start"]
                gained = round(1000 * (1 - t / qtime(g) / 2)) if choice == q["c"] else 0
                p["score"] += gained
                p["answers"][g["q"]] = {"choice": choice, "gained": gained}
                return self.send({"ok": True})
            if path == "/api/next":
                if body.get("host") != g["host"]:
                    return self.send({"error": "Forbidden"}, 403)
                tick(g)
                if g["phase"] == "question":
                    g["phase"] = "results"  # skip
                elif g["q"] + 1 >= len(g["quiz"]) and g["phase"] == "results":
                    g["phase"] = "final"
                elif g["phase"] in ("lobby", "results"):
                    g["q"] += 1
                    g["phase"] = "question"
                    g["start"] = time.time()
                return self.send({"ok": True})
        self.send({"error": "not found"}, 404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8000))
    print("Host:    http://localhost:%d/host\nPlayers: http://<your-ip>:%d/play" % (port, port))
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
