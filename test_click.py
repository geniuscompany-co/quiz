import json, os, subprocess
D = os.path.dirname(os.path.abspath(__file__))
html = open(os.path.join(D, "host.html")).read()
S = dict(phase="question", index=0, total=3, question="Q?", answers=["a", "b", "c", "d"], qtime=20, left=14.2, n=3, answered=1)
test = """pin='1';S=%s;render();
const b1=document.querySelector('button');
S.left=13.5;render();S.left=12.4;render();
const same=document.querySelector('button')===b1;
S.answered=2;render();
const changed=document.querySelector('button')!==b1;
document.body.insertAdjacentHTML('beforeend','<pre id=res>buttonKeptAcrossTicks='+same+' timer='+document.getElementById('t').textContent+' rerenderOnRealChange='+changed+'</pre>');
""" % json.dumps(S)
i = html.rindex("</script>")
p = os.path.join(D, "shots", "click_test.html")
open(p, "w").write(html[:i] + test + html[i:])
chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
out = subprocess.run([chrome, "--headless=new", "--disable-gpu", "--dump-dom", "file://" + p], capture_output=True, text=True, timeout=60).stdout
print([l for l in out.splitlines() if "buttonKept" in l])
