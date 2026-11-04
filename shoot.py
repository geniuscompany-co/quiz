import os, subprocess
D = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(D, "shots"); os.makedirs(OUT, exist_ok=True)
Q = dict(index=1, total=5, question="Which planet is known as the Red Planet?", answers=["Venus", "Jupiter", "Mars", "Mercury"], qtime=20, left=14.2, n=3, answered=2)
BOARD = [{"name": "Ann", "score": 1969}, {"name": "Cy", "score": 1917}, {"name": "Bob", "score": 0}]
host = {
 "lobby": dict(phase="lobby", n=3, names=["Ann", "Bob", "Cy"]),
 "question": dict(phase="question", **Q),
 "results": dict(phase="results", correct=2, counts=[0, 1, 2, 0], board=BOARD, **Q),
 "final": dict(phase="final", board=BOARD, n=3, total=5),
}
me = dict(name="Ann", score=1969, answered=True, choice=2, gained=969, correct=True, rank=1)
play = {
 "lobby": dict(phase="lobby", me=dict(me, name="Ann")),
 "question": dict(phase="question", me=dict(me, answered=False), **Q),
 "answered": dict(phase="question", me=me, **Q),
 "correct": dict(phase="results", me=me, correct=2, counts=[0,1,2,0], board=BOARD, **Q),
 "wrong": dict(phase="results", me=dict(me, correct=False, gained=0, rank=2), correct=2, counts=[0,1,2,0], board=BOARD, **Q),
 "final": dict(phase="final", me=me, board=BOARD, n=3, total=5),
}
import json
def build(src, state, extra):
    html = open(os.path.join(D, src)).read()
    inj = extra + "S=%s;render();" % json.dumps(state)
    i = html.rindex("</script>")
    return html[:i] + inj + html[i:]
jobs = []
for k, s in host.items():
    p = os.path.join(OUT, "host_%s.html" % k); open(p, "w").write(build("host.html", s, "pin='123456';")); jobs.append((p, "1280,720"))
for k, s in play.items():
    p = os.path.join(OUT, "play_%s.html" % k); open(p, "w").write(build("play.html", s, "")); jobs.append((p, "500,800"))
chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
for p, size in jobs:
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=" + size,
                    "--screenshot=" + p.replace(".html", ".png"), "file://" + p], capture_output=True, timeout=60)
print(sorted(f for f in os.listdir(OUT) if f.endswith(".png")))
