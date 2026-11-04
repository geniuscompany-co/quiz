import json, sys, time, urllib.request, urllib.error
B = sys.argv[1]
def call(path, body=None):
    req = urllib.request.Request(B + path, data=json.dumps(body).encode() if body is not None else None, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req) as r: return r.status, json.load(r)
    except urllib.error.HTTPError as e: return e.code, json.load(e)
g = call("/api/create", {})[1]; pin, host = g["pin"], g["host"]
a = call("/api/join", {"pin": pin, "name": "Ann"})[1]["pid"]
b = call("/api/join", {"pin": pin, "name": "Bob"})[1]["pid"]
call("/api/next", {"pin": pin, "host": host})
st = call("/api/state?pin=%s&pid=%s" % (pin, a))[1]
print("qtime Q1 =", st["qtime"], "(expect 3)")
call("/api/answer", {"pin": pin, "pid": a, "choice": 1})   # Bob never answers
print("phase right after Ann answers:", call("/api/state?pin=" + pin)[1]["phase"], "(expect question)")
time.sleep(3.5)
st = call("/api/state?pin=%s&pid=%s" % (pin, b))[1]
print("phase after timeout:", st["phase"], "(expect results); Bob:", st["me"])
print("late answer rejected:", call("/api/answer", {"pin": pin, "pid": b, "choice": 1})[0], "(expect 400)")
call("/api/next", {"pin": pin, "host": host})
st = call("/api/state?pin=" + pin)[1]
print("Q2 qtime =", st["qtime"], "(expect 20 default)", "index", st["index"])
print("PIN", pin)
