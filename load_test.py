"""Load test: N simulated players polling every 0.7s, answering at random times.
Usage: python3 load_test.py https://quiz-saxb.onrender.com 30"""
import json, random, sys, threading, time, urllib.request, urllib.error

B = sys.argv[1].rstrip("/")
N = int(sys.argv[2]) if len(sys.argv) > 2 else 30
lat, errs, lock = [], [], threading.Lock()


def call(path, body=None):
    t = time.time()
    req = urllib.request.Request(B + path, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
            code = r.status
    except urllib.error.HTTPError as e:
        d, code = json.load(e), e.code
    except Exception as e:
        with lock:
            errs.append(repr(e))
        return 0, {}
    with lock:
        lat.append(time.time() - t)
    return code, d


print("waking server...")
t0 = time.time()
call("/api/create", {})
print("ready in %.1fs" % (time.time() - t0))
lat.clear()

g = call("/api/create", {})[1]
pin, host = g["pin"], g["host"]
done = threading.Event()
final = {}


def player(i):
    code, r = call("/api/join", {"pin": pin, "name": "P%02d" % i})
    if code != 200:
        errs.append("join %s" % code)
        return
    pid, answered = r["pid"], -1
    plan = {}
    while not done.is_set():
        c, s = call("/api/state?pin=%s&pid=%s" % (pin, pid))
        if s.get("phase") == "question" and s["index"] != answered:
            plan.setdefault(s["index"], time.time() + random.uniform(0.5, 4))
            if time.time() >= plan[s["index"]]:
                call("/api/answer", {"pin": pin, "pid": pid, "choice": random.randint(0, 3)})
                answered = s["index"]
        if s.get("phase") == "final":
            final[i] = s["me"]["rank"]
            return
        time.sleep(0.7)


ths = [threading.Thread(target=player, args=(i,)) for i in range(N)]
for t in ths:
    t.start()
    time.sleep(0.05)
time.sleep(2)
st = call("/api/state?pin=" + pin)[1]
print("players in lobby: %d/%d" % (st["n"], N))
start = time.time()
call("/api/next", {"pin": pin, "host": host})
while True:
    time.sleep(1)
    s = call("/api/state?pin=" + pin)[1]
    if s.get("phase") == "final":
        break
    if s.get("phase") in ("results", "board") or (s.get("phase") == "question" and s["answered"] == s["n"]):
        call("/api/next", {"pin": pin, "host": host})
    if time.time() - start > 300:
        print("timeout"); break
done.set()
for t in ths:
    t.join()
lat.sort()
q = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))] * 1000
print("game took %.0fs; players reaching final: %d/%d" % (time.time() - start, len(final), N))
print("requests: %d  errors: %d" % (len(lat) + len(errs), len(errs)))
print("latency ms  p50 %.0f  p95 %.0f  p99 %.0f  max %.0f" % (q(.5), q(.95), q(.99), lat[-1] * 1000))
print("unique ranks:", len(set(final.values())), errs[:3])
