import json, sys, urllib.request, urllib.error

B = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"


def call(path, body=None):
    req = urllib.request.Request(B + path, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


s, g = call("/api/create", {})
pin, host = g["pin"], g["host"]
check(s == 200 and len(pin) == 6, "create game")
ids = {}
for n in ["Ann", "Bob", "Cy"]:
    s, r = call("/api/join", {"pin": pin, "name": n})
    ids[n] = r["pid"]
check(call("/api/join", {"pin": pin, "name": "ann"})[0] == 400, "duplicate nickname rejected")
check(call("/api/join", {"pin": "000000", "name": "X"})[0] == 404, "bad pin rejected")
check(call("/api/next", {"pin": pin, "host": "bad"})[0] == 403, "non-host cannot advance")
check(call("/api/state?pin=" + pin)[1]["n"] == 3, "3 players in lobby")
call("/api/next", {"pin": pin, "host": host})
check(call("/api/state?pin=" + pin)[1]["phase"] == "question", "question 1 started")
quiz = json.load(open(__file__.replace("test_game.py", "quiz.json")))
for qi in range(len(quiz)):
    st = call("/api/state?pin=%s&pid=%s" % (pin, ids["Ann"]))[1]
    check(st["phase"] == "question" and st["index"] == qi, "question %d showing" % (qi + 1))
    c = quiz[qi]["c"]
    call("/api/answer", {"pin": pin, "pid": ids["Ann"], "choice": c})
    check(call("/api/answer", {"pin": pin, "pid": ids["Ann"], "choice": c})[0] == 400, "double answer rejected")
    call("/api/answer", {"pin": pin, "pid": ids["Bob"], "choice": (c + 1) % 4})
    st = call("/api/state?pin=%s&pid=%s" % (pin, ids["Cy"]))[1]
    check(st["phase"] == "question", "waits for last player")
    call("/api/answer", {"pin": pin, "pid": ids["Cy"], "choice": c})
    st = call("/api/state?pin=%s&pid=%s" % (pin, ids["Ann"]))[1]
    check(st["phase"] == "results" and st["correct"] == c and st["me"]["correct"], "results after all answered")
    check(sum(st["counts"]) == 3, "answer counts add up")
    call("/api/next", {"pin": pin, "host": host})
st = call("/api/state?pin=%s&pid=%s" % (pin, ids["Bob"]))[1]
check(st["phase"] == "final" and st["me"]["score"] == 0 and st["me"]["rank"] == 3, "final: Bob scored 0, rank 3")
check(st["board"][0]["score"] > 4000, "winner score is high")
print("ALL PASSED", st["board"])
