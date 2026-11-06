#!/usr/bin/env python3
"""Kahoot-style live quiz server. Standard library only. Run: python3 server.py [port]"""
import csv, hmac, html, io, json, os, random, re, sys, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote

ROOT = os.path.dirname(os.path.abspath(__file__))
LOCK = threading.Lock()
GAMES = {}
DEFAULT_TIME = 15  # seconds; override per question with "t" in quiz.json
PREVIEW_TIME = 5.0  # seconds to read question alone before answers appear and timer starts

TEAMS = [
    {"id": "red", "name": "Red Dragons", "emoji": "🔴", "color": "#e21b3c"},
    {"id": "blue", "name": "Blue Sharks", "emoji": "🔵", "color": "#1368ce"},
    {"id": "gold", "name": "Gold Lions", "emoji": "🟡", "color": "#d89e00"},
    {"id": "green", "name": "Green Vipers", "emoji": "🟢", "color": "#26890c"},
]
TEAM_MAP = {t["id"]: t for t in TEAMS}

HOST_PW = os.environ.get("HOST_PASSWORD", "")
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(ROOT, ".data")
TEAM_BONUS = 300
HOT_BONUS = 400
PRED_BONUS = 200
INT_KEY_G = ("wheel_mods", "robin_stolen", "shuf", "hot", "team_results")
INT_KEY_P = ("answers", "wagers", "confident", "plan")


def pw_ok(val):
    return (not HOST_PW) or hmac.compare_digest(str(val or ""), HOST_PW)


def data_path(name):
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, name)


LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})
BAD_SUB = ["fuck", "shit", "bitch", "cunt", "nigg", "fagg", "whore", "slut", "penis", "vagina", "porn",
           "nazi", "hitler", "jizz", "dildo", "pussy", "retard", "blowjob", "handjob", "asshole"]
BAD_WORD = {"ass", "arse", "cum", "dick", "tit", "tits", "boob", "boobs", "anal", "sex", "rape", "kkk", "cock", "fag"}


def is_profane(text):
    t = str(text or "").lower().translate(LEET)
    squeezed = re.sub(r"[^a-z]", "", t)
    if any(w in squeezed for w in BAD_SUB):
        return True
    return any(tok in BAD_WORD for tok in re.split(r"[^a-z]+", t) if tok)


def qtype_of(q):
    return q.get("type") or "mc"


def norm_text(x):
    return re.sub(r"[^a-z0-9]+", " ", str(x).lower()).strip()


def text_correct(q, txt):
    n = norm_text(txt)
    return bool(n) and any(norm_text(a) == n for a in q["a"])


def ans_text(q):
    t = qtype_of(q)
    if t == "order":
        return " \u2192 ".join(q["a"])
    if t == "text":
        return " / ".join(q["a"][:3])
    return q["a"][q["c"]] if 0 <= q["c"] < len(q["a"]) else q["a"][0]


def bot_choice(q, skill):
    if random.random() < skill:
        return q["c"]
    if qtype_of(q) in ("text", "order"):
        return -1
    wrongs = [i for i in range(len(q["a"])) if i != q["c"]]
    return random.choice(wrongs) if wrongs else -1


def bot_given(q, ch):
    if ch != q["c"]:
        return "(wrong)"
    return ans_text(q) if qtype_of(q) == "order" else q["a"][0]

WHEEL_EVENTS = [
    {"id": "speed", "label": "⚡ SPEED DEMON: 1.35x Points for Fast Answers!"},
    {"id": "robin", "label": "🏹 ROBIN HOOD: Correct Answers Steal +150 pts from 1st Place!"},
    {"id": "crate", "label": "🎁 MYSTERY CRATE: Random +150 to +400 pt Bonus on Correct!"},
    {"id": "streak2x", "label": "🔥 DOUBLE STREAK: Streak Bonuses are 2x This Round!"},
]


def qtime(g, i=None):
    if g.get("qtime"):
        return g["qtime"]
    return g["quiz"][g["q"] if i is None else i].get("t", DEFAULT_TIME)


def qmult(g, i=None):
    idx = g["q"] if i is None else i
    q = g["quiz"][idx]
    if "double" in q:
        return 2 if q["double"] else 1
    if is_boss_q(g, idx):
        return 1
    return 2 if idx == len(g["quiz"]) - 1 and len(g["quiz"]) > 1 else 1


def is_boss_q(g, i=None):
    idx = g["q"] if i is None else i
    q = g["quiz"][idx]
    if q.get("redemption") or q.get("tiebreak"):
        return False
    return bool(g.get("wager") and idx == len(g["quiz"]) - 1)


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
        "quiz_ultimate_silly_trivia.json": "Ultimate Silly Trivia",
    }
    files = sorted(f for f in os.listdir(ROOT) if f == "quiz.json" or (f.startswith("quiz_") and f.endswith(".json")))
    for f in files:
        try:
            with open(os.path.join(ROOT, f)) as fp:
                data = json.load(fp)
            if isinstance(data, dict):
                q = data.get("questions", [])
                title = data.get("title") or f[5:-5].replace("_", " ").title()
            else:
                q = data
                title = labels.get(f) or f[5:-5].replace("_", " ").title()
            if isinstance(q, list) and q:
                items.append({"file": f, "title": "%s (%d Qs)" % (title, len(q)), "count": len(q)})
        except Exception:
            pass
    return items


def load_quiz(fname="quiz.json"):
    safe = os.path.basename(fname or "quiz.json")
    p = os.path.join(ROOT, safe)
    if os.path.exists(p):
        with open(p) as f:
            data = json.load(f)
            return data.get("questions", []) if isinstance(data, dict) else data
    return QUIZ


def clean_url(u):
    u = str(u or "").strip()
    return u if re.match(r"^(https?://|/)", u) else ""


def clean_questions(questions):
    cleaned = []
    for q in questions or []:
        if not isinstance(q, dict) or not q.get("q") or not isinstance(q.get("a"), list):
            continue
        qtype = str(q.get("type") or "mc").lower()
        if qtype not in ("mc", "tf", "text", "order"):
            qtype = "mc"
        raw = [str(x).strip() for x in q["a"] if str(x).strip()]
        try:
            c = int(q.get("c", 0))
        except Exception:
            c = 0
        if qtype == "tf":
            ans, c = ["True", "False"], max(0, min(1, c))
        elif qtype == "text":
            if not raw:
                continue
            ans, c = raw[:6], 0
        elif qtype == "order":
            if len(raw) < 2:
                continue
            ans, c = raw[:4], 0
        else:
            if len(raw) < 2:
                continue
            ans = raw[:4]
            while len(ans) < 4:
                ans.append("Option %d" % (len(ans) + 1))
            c = max(0, min(3, c))
        item = {"q": str(q["q"]).strip(), "a": ans, "c": c}
        if qtype != "mc":
            item["type"] = qtype
        if qtype == "mc" and isinstance(q.get("aimg"), list):
            ai = [clean_url(x) for x in q["aimg"][:len(ans)]]
            ai += [""] * (len(ans) - len(ai))
            if any(ai):
                item["aimg"] = ai
        if q.get("quote"):
            item["quote"] = str(q["quote"]).strip()
        if q.get("src"):
            item["src"] = str(q["src"]).strip()
        if q.get("img"):
            item["img"] = str(q["img"]).strip()
        cleaned.append(item)
    return cleaned


def save_custom_quiz(title, questions):
    slug = re.sub(r"[^a-z0-9]+", "_", (title or "custom").lower()).strip("_") or "custom"
    fname = "quiz_%s.json" % slug
    cleaned = clean_questions(questions)
    if not cleaned:
        raise ValueError("No valid questions provided")
    with open(os.path.join(ROOT, fname), "w") as f:
        json.dump(cleaned, f, indent=2)
    return fname, len(cleaned), cleaned


def new_game(quiz_file="quiz.json", count=None, shuffle=False, custom_questions=None):
    while True:
        pin = str(random.randint(100000, 999999))
        if pin not in GAMES:
            break
    cleaned_custom = clean_questions(custom_questions) if custom_questions else []
    bank = cleaned_custom if cleaned_custom else [dict(x) for x in load_quiz(quiz_file)]
    if count is not None:
        try:
            k = max(1, min(len(bank), int(count)))
        except Exception:
            k = len(bank)
    else:
        k = len(bank)
    if k < len(bank) or shuffle:
        quiz = [dict(x) for x in random.sample(bank, k)]
    else:
        quiz = bank
    GAMES[pin] = {
        "pin": pin, "host": "h%08x" % random.getrandbits(32), "quiz": quiz,
        "phase": "lobby", "q": -1, "start": 0, "preview": PREVIEW_TIME, "players": {},
        "reactions": [], "rseq": 0,
        "teams": False, "wager": True, "redemption": False, "redemption_added": False,
        "sabotage": False, "icebreaker": False, "icebreaker_prompt": "In one word, how are you feeling about today's quiz?",
        "words": [],
        "wheel": True, "wheel_mods": {}, "robin_stolen": {},
        "hotseat": True, "team_battle": True, "tiebreak": True,
        "roster": [], "banned": [], "logo": "", "bank": bank, "quiz_file": quiz_file,
        "created": time.time(), "shuf": {}, "hot": {}, "team_results": {},
        "auto": False, "phase_entered": time.time(),
        "paused": False, "pause_rem": 0.0, "pause_prev": 0.0, "extra_time": 0.0,
    }
    return GAMES[pin]


def replay_game(g, quiz_file="quiz.json", count=None, shuffle=True, custom_questions=None):
    cleaned_custom = clean_questions(custom_questions) if custom_questions else []
    bank = cleaned_custom if cleaned_custom else [dict(x) for x in load_quiz(quiz_file)]
    if count is not None:
        try:
            k = max(1, min(len(bank), int(count)))
        except Exception:
            k = len(bank)
    else:
        k = len(bank)
    if k < len(bank) or shuffle:
        g["quiz"] = [dict(x) for x in random.sample(bank, k)]
    else:
        g["quiz"] = bank
    g["phase"] = "lobby"
    g["q"] = -1
    g["start"] = 0
    g["words"] = []
    g["redemption_added"] = False
    g["wheel_mods"] = {}
    g["robin_stolen"] = {}
    g["shuf"] = {}
    g["hot"] = {}
    g["team_results"] = {}
    g["settled_q"] = -1
    g["tb_done"] = False
    g["tb_pids"] = []
    g["hist_saved"] = False
    g["bank"] = bank
    g["quiz_file"] = quiz_file
    g["paused"] = False
    g["extra_time"] = 0.0
    g["phase_entered"] = time.time()
    for p in g["players"].values():
        p["score"] = 0
        p["answers"] = {}
        p["powerups"] = {}
        p["wagers"] = {}
        p["confident"] = {}
        p["plan"] = {}
        p["frozen_next"] = False
        p["frozen_q"] = -1
        p["word_sent"] = False
        p["ice_sent_q"] = -1


def calc_streak(p, up_to_q):
    s = 0
    for qi in range(up_to_q + 1):
        a = p["answers"].get(qi)
        if a and a.get("correct"):
            s += 1
        elif a and a.get("shielded"):
            pass
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
        elif a and a.get("shielded"):
            pass
        else:
            cur = 0
    return best


def ptag(p):
    t_emo = ""
    if p.get("team") and p["team"] in TEAM_MAP:
        t_emo = TEAM_MAP[p["team"]]["emoji"] + " "
    av = (p.get("avatar") + " ") if p.get("avatar") else ""
    return t_emo + av + p["name"]


BOT_NAMES = [
    ("🦊", "FoxBot"), ("🐼", "Panda"), ("🦁", "Leo"), ("🐯", "Tigger"),
    ("🐸", "Hopper"), ("🐙", "Octo"), ("🦄", "Sparkle"), ("🐲", "Draco"),
    ("🤠", "Tex"), ("👾", "Pixel"), ("🚀", "Nova"), ("⚡", "Flash"),
    ("🦉", "Athena"), ("🦈", "Finn"), ("🐝", "Buzz"), ("🦖", "Rex"),
    ("🐧", "Pip"), ("🦋", "Skye"), ("🐢", "Turbo"), ("🐺", "Luna"),
]

BOT_ICE_WORDS = [
    "Ready!", "Pumped", "Excited", "Curious", "Unstoppable", "Focused",
    "Hyped", "Let'sGo", "Brainy", "Confident", "Caffeine", "Winning",
]


def pick_balanced_team(g, preferred=None):
    if not g.get("teams"):
        return ""
    if preferred in TEAM_MAP:
        return preferred
    counts = {t["id"]: 0 for t in TEAMS}
    for p in g["players"].values():
        if p.get("team") in counts:
            counts[p["team"]] += 1
    min_c = min(counts.values())
    candidates = [tid for tid, c in counts.items() if c == min_c]
    return random.choice(candidates)


def add_bots(g, count=5):
    existing = {p["name"].lower() for p in g["players"].values()}
    pool = [(av, nm) for av, nm in BOT_NAMES if nm.lower() not in existing]
    random.shuffle(pool)
    added = 0
    for i in range(max(1, min(30, int(count or 5)))):
        if pool:
            av, nm = pool.pop()
        else:
            av = random.choice(["🤖", "👾", "🦊", "🐼", "🦁", "🚀"])
            nm = "Bot %d" % (len(g["players"]) + 1)
            while nm.lower() in existing:
                nm = "Bot %d" % random.randint(10, 999)
        existing.add(nm.lower())
        pid = "b%08x" % random.getrandbits(32)
        g["players"][pid] = {
            "name": nm, "avatar": av, "team": pick_balanced_team(g),
            "score": 0, "answers": {}, "powerups": {}, "wagers": {},
            "bot": True, "skill": round(random.uniform(0.45, 0.88), 2),
            "plan": {},
        }
        added += 1
    return added


def is_team_battle(g, qi):
    if not g.get("teams") or not g.get("team_battle", True):
        return False
    if qi < 4 or (qi + 1) % 5 != 0:
        return False
    q = g["quiz"][qi]
    if q.get("redemption") or q.get("tiebreak") or is_boss_q(g, qi):
        return False
    return qi not in g.get("wheel_mods", {})


def prev_time(g, qi=None):
    idx = g["q"] if qi is None else qi
    base = g.get("preview", PREVIEW_TIME)
    if idx is not None and 0 <= idx < len(g["quiz"]) and is_team_battle(g, idx):
        return max(base, 12.0)
    return base


def cur_timer_state(g):
    """Return (preview_left, q_left, q_elapsed, total_qtime)."""
    qt = qtime(g) + g.get("extra_time", 0.0)
    prev_t = prev_time(g)
    if g.get("paused"):
        prev_left = g.get("pause_prev", 0.0)
        q_left = g.get("pause_rem", qt)
        q_elapsed = max(0.0, qt - q_left)
        return prev_left, q_left, q_elapsed, qt
    elapsed = time.time() - g["start"]
    prev_left = max(0.0, prev_t - elapsed) if g["phase"] == "question" else 0.0
    q_elapsed = max(0.0, elapsed - prev_t)
    q_left = max(0.0, qt - q_elapsed)
    return prev_left, q_left, q_elapsed, qt


def is_underdog(g, p, qi):
    if qi < 3 or len(g["players"]) < 2:
        return False
    scores = sorted([x["score"] for x in g["players"].values()], reverse=True)
    if scores[0] <= 0:
        return False
    median = scores[len(scores) // 2]
    return p["score"] < median


def audience_pcts(g, qi):
    q = g["quiz"][qi]
    n_opt = len(q["a"])
    counts = [0.0] * n_opt
    real_votes = 0
    for p in g["players"].values():
        a = p["answers"].get(qi)
        if a and 0 <= a.get("choice", -1) < n_opt:
            counts[a["choice"]] += 1.0
            real_votes += 1
    if real_votes < 4:
        for i in range(n_opt):
            counts[i] += 2.6 if i == q["c"] else 0.6
    tot = max(1.0, sum(counts))
    pcts = [round(100.0 * c / tot) for c in counts]
    diff = 100 - sum(pcts)
    pcts[q["c"]] = max(0, pcts[q["c"]] + diff)
    return pcts


def record_answer(g, p, choice, t, given=None):
    qi = g["q"]
    q = g["quiz"][qi]
    qt = max(1.0, qtime(g) + g.get("extra_time", 0.0))
    is_correct = (choice == q["c"])
    if q.get("tiebreak"):
        p["answers"][qi] = {
            "choice": choice, "gained": 0, "bonus": 0, "wager": 0, "correct": is_correct,
            "time": round(t, 2), "shielded": False, "underdog": False, "given": given,
        }
        return
    pu = p.get("powerups", {})
    p_mult = 2 if pu.get("2x_q") == qi else 1
    shielded = (pu.get("shield_q") == qi)
    underdog = is_underdog(g, p, qi)
    u_mult = 1.5 if underdog else 1.0
    wmod = g.get("wheel_mods", {}).get(qi)
    confident = bool(p.get("confident", {}).get(qi))
    wager_pct = p.get("wagers", {}).get(qi)
    wager_amt = 0
    if wager_pct is not None and wager_pct > 0:
        stake = max(500, p["score"])
        wager_amt = round(stake * (wager_pct / 100.0))
    wheel_bonus = 0
    conf_delta = 0
    if is_correct:
        prev_streak = calc_streak(p, qi - 1) if qi > 0 else 0
        new_streak = prev_streak + 1
        s_mult = 2 if (wmod and wmod["id"] == "streak2x") else 1
        bonus = (min(300, (new_streak - 1) * 100) if new_streak >= 2 else 0) * s_mult
        spd_mult = 1.35 if (wmod and wmod["id"] == "speed" and t <= qt * 0.45) else 1.0
        base = round(1000 * (1 - min(1.0, t / qt) / 2) * qmult(g) * p_mult * u_mult * spd_mult)
        if wmod and wmod["id"] == "crate":
            wheel_bonus = random.choice([150, 200, 250, 300, 400])
        elif wmod and wmod["id"] == "robin" and len(g["players"]) > 1:
            leader = max(g["players"].values(), key=lambda x: x["score"])
            if leader["name"] != p["name"] and leader["score"] > 0:
                wheel_bonus = 150
                stolen_so_far = g.setdefault("robin_stolen", {}).get(qi, 0)
                if stolen_so_far < 300:
                    ded = min(150, 300 - stolen_so_far, leader["score"])
                    leader["score"] = max(0, leader["score"] - ded)
                    g["robin_stolen"][qi] = stolen_so_far + ded
        if confident:
            conf_delta = 250
        gained = base + bonus + wager_amt + wheel_bonus + conf_delta
    else:
        bonus = 0
        wager_loss = wager_amt if (wager_amt > 0 and not shielded) else 0
        if confident and not shielded:
            conf_delta = -150
        gained = -wager_loss + conf_delta
    p["score"] = max(0, p["score"] + gained)
    p["answers"][qi] = {
        "choice": choice, "gained": gained, "bonus": bonus,
        "wager": wager_amt, "correct": is_correct, "time": round(t, 2),
        "shielded": shielded, "underdog": underdog and is_correct,
        "confident": confident, "conf_delta": conf_delta, "wheel_bonus": wheel_bonus,
        "given": given,
    }


def add_bonus(p, qi, amt, key):
    a = p["answers"].get(qi)
    if a is None:
        a = {"choice": -1, "gained": 0, "bonus": 0, "wager": 0, "correct": False, "time": 99,
             "shielded": False, "underdog": False, "blank": True}
        p["answers"][qi] = a
    a["gained"] = a.get("gained", 0) + amt
    a[key] = a.get(key, 0) + amt
    p["score"] = max(0, p["score"] + amt)


def eligible(g):
    q = g["quiz"][g["q"]] if 0 <= g["q"] < len(g["quiz"]) else {}
    if q.get("tiebreak"):
        return [p for pid, p in g["players"].items() if pid in g.get("tb_pids", [])]
    return list(g["players"].values())


def settle_question(g):
    qi = g["q"]
    if qi < 0 or g.get("settled_q") == qi:
        return
    g["settled_q"] = qi
    q = g["quiz"][qi]
    P = g["players"]
    if q.get("tiebreak"):
        best = None
        for pid in g.get("tb_pids", []):
            p = P.get(pid)
            a = p["answers"].get(qi) if p else None
            if a and a.get("correct") and (best is None or a["time"] < best[0]):
                best = (a["time"], p)
        if best:
            best[1]["score"] += 1
            best[1]["answers"][qi]["gained"] = 1
            g["tb_winner"] = best[1]["name"]
        return
    qt = qtype_of(q)
    if is_team_battle(g, qi):
        by_team = {}
        for p in P.values():
            if p.get("team") in TEAM_MAP:
                by_team.setdefault(p["team"], []).append(p)
        res = []
        for tid, members in by_team.items():
            answered = [m for m in members if qi in m["answers"] and not m["answers"][qi].get("blank")]
            if not answered:
                continue
            if qt in ("mc", "tf"):
                votes = {}
                for m in answered:
                    a = m["answers"][qi]
                    votes.setdefault(a["choice"], []).append(a["time"])
                top = max(votes.items(), key=lambda kv: (len(kv[1]), -min(kv[1])))
                ok = (top[0] == q["c"])
            else:
                ok = sum(1 for m in answered if m["answers"][qi]["correct"]) * 2 > len(answered)
            if ok:
                for m in answered:
                    add_bonus(m, qi, TEAM_BONUS, "team_bonus")
            t = TEAM_MAP[tid]
            res.append({"name": t["name"], "emoji": t["emoji"], "color": t["color"], "correct": ok, "members": len(answered)})
        g.setdefault("team_results", {})[qi] = res
    hs = g.get("hot", {}).get(qi)
    if hs:
        hp = P.get(hs["pid"])
        hot_ok = bool(hp and hp["answers"].get(qi, {}).get("correct"))
        if hp and hot_ok:
            add_bonus(hp, qi, HOT_BONUS, "hot_bonus")
        right = 0
        for pid, guess in hs.get("preds", {}).items():
            pp = P.get(pid)
            if pp and bool(guess) == hot_ok:
                add_bonus(pp, qi, PRED_BONUS, "pred_bonus")
                right += 1
        hs["ok"] = hot_ok
        hs["right"] = right


def start_question(g, qi):
    g["q"] = qi
    g["phase"] = "question"
    g["paused"] = False
    g["extra_time"] = 0.0
    if g.get("wheel", True) and qi > 0 and (qi + 1) % 4 == 0 and not is_boss_q(g, qi) and not g["quiz"][qi].get("redemption"):
        g.setdefault("wheel_mods", {})[qi] = random.choice(WHEEL_EVENTS)
    q = g["quiz"][qi]
    if qtype_of(q) == "order":
        n = len(q["a"])
        perm = list(range(n))
        for _ in range(12):
            random.shuffle(perm)
            if perm != list(range(n)):
                break
        g.setdefault("shuf", {})[qi] = perm
    if (g.get("hotseat", True) and qi >= 5 and (qi + 1) % 6 == 0 and len(g["players"]) >= 3
            and qi not in g.get("wheel_mods", {}) and not is_team_battle(g, qi)
            and not is_boss_q(g, qi) and not q.get("redemption") and not q.get("tiebreak")):
        humans = [pid for pid, p in g["players"].items() if not p.get("bot")]
        pool = humans or list(g["players"].keys())
        hot_pid = random.choice(pool)
        preds = {}
        for pid, p in g["players"].items():
            if p.get("bot") and pid != hot_pid:
                preds[pid] = random.random() < 0.6
        g.setdefault("hot", {})[qi] = {"pid": hot_pid, "preds": preds}
    for p in g["players"].values():
        if p.get("frozen_next"):
            p["frozen_q"] = qi
            p["frozen_by_q"] = p.get("frozen_by", "Someone")
            p["frozen_next"] = False
    if qi in g.get("wheel_mods", {}) or q.get("redemption"):
        g["phase"] = "intro"
        g["intro_start"] = time.time()
        g["phase_entered"] = time.time()
    else:
        begin_question(g)


def intro_len(g):
    base = g.get("preview", PREVIEW_TIME)
    return base + 2.0 if g["quiz"][g["q"]].get("redemption") else base


def begin_question(g):
    g["phase"] = "question"
    g["start"] = time.time()
    g["phase_entered"] = time.time()
    g["paused"] = False


def find_most_missed(g):
    """Return index of most missed non-redemption question so far, or None."""
    if not g["players"]:
        return None
    best = None
    for qi, q in enumerate(g["quiz"]):
        if q.get("redemption"):
            continue
        ans_count = 0
        wrong_count = 0
        for p in g["players"].values():
            if qi in p["answers"]:
                ans_count += 1
                if not p["answers"][qi].get("correct"):
                    wrong_count += 1
            else:
                wrong_count += 1
        if ans_count > 0 and wrong_count > 0:
            rate = wrong_count / float(len(g["players"]))
            if best is None or rate > best[0]:
                best = (rate, qi)
    return best[1] if best else None


def tie_leaders(g):
    ps = list(g["players"].items())
    if len(ps) < 2:
        return []
    best = max(p["score"] for _, p in ps)
    if best <= 0:
        return []
    tied = [pid for pid, p in ps if p["score"] == best]
    return tied if len(tied) >= 2 else []


def pending_extra(g):
    if g.get("redemption") and not g.get("redemption_added") and find_most_missed(g) is not None:
        return "redemption"
    if g.get("tiebreak", True) and not g.get("tb_done") and tie_leaders(g):
        return "tiebreak"
    return None


def make_tiebreak_q(g):
    used = {x["q"] for x in g["quiz"]}
    pool = [x for x in g.get("bank", []) if x.get("q") not in used and qtype_of(x) in ("mc", "tf")]
    if not pool:
        pool = [x for x in g["quiz"] if qtype_of(x) in ("mc", "tf") and not x.get("tiebreak")] or list(g["quiz"])
    tq = dict(random.choice(pool))
    tq["tiebreak"] = True
    tq["double"] = False
    tq.pop("redemption", None)
    return tq


def quiz_title(g):
    f = g.get("quiz_file") or "quiz.json"
    for item in list_quizzes():
        if item["file"] == f:
            return re.sub(r"\s*\(\d+ Qs\)$", "", item["title"])
    return f[5:-5].replace("_", " ").title() if f.startswith("quiz_") else f


def load_history():
    try:
        with open(data_path("history.json")) as fp:
            return json.load(fp)
    except Exception:
        return []


def save_history(h):
    try:
        tmp = data_path("history.json.tmp")
        with open(tmp, "w") as fp:
            json.dump(h, fp)
        os.replace(tmp, data_path("history.json"))
    except Exception:
        pass


def finalize_game(g):
    if g.get("hist_saved"):
        return
    g["hist_saved"] = True
    try:
        st = final_stats(g)
        lb = leaderboard(g)
        n_p = max(1, len(g["players"]))
        qs = []
        for qi, q in enumerate(g["quiz"]):
            if q.get("tiebreak"):
                continue
            cor = sum(1 for p in g["players"].values() if p["answers"].get(qi, {}).get("correct"))
            qs.append({"q": q["q"], "pct": round(100.0 * cor / n_p), "ans": ans_text(q)})
        entry = {
            "id": "%s-%d" % (g["pin"], int(time.time())), "ts": int(time.time()), "pin": g["pin"],
            "quiz": quiz_title(g), "players": len(g["players"]),
            "humans": sum(1 for p in g["players"].values() if not p.get("bot")),
            "questions": len(qs), "accuracy": st["accuracy"],
            "top": [{"name": r["name"], "score": r["score"]} for r in lb[:3]], "qs": qs,
        }
        h = load_history()
        h.append(entry)
        save_history(h[-200:])
    except Exception:
        pass


def go_final(g):
    g["phase"] = "final"
    g["phase_entered"] = time.time()
    finalize_game(g)


def will_finish_after_results(g):
    if g["q"] + 1 < len(g["quiz"]):
        return False
    return pending_extra(g) is None


def advance_game(g):
    if g["phase"] == "question":
        qi = g["q"]
        for p in eligible(g):
            if p.get("bot") and qi not in p["answers"]:
                plan = p.get("plan", {})
                if qi in plan:
                    delay, ch = plan[qi]
                else:
                    delay = round(random.uniform(2.0, qtime(g) * 0.7), 2)
                    ch = bot_choice(g["quiz"][qi], p.get("skill", 0.65))
                if is_boss_q(g, qi) and qi not in p.setdefault("wagers", {}):
                    p["wagers"][qi] = random.choice([0, 50, 50, 100])
                record_answer(g, p, ch, delay, bot_given(g["quiz"][qi], ch))
        g["phase"] = "results"
        g["phase_entered"] = time.time()
        g["paused"] = False
        settle_question(g)
    elif g["phase"] == "results":
        if g["q"] + 1 >= len(g["quiz"]):
            kind = pending_extra(g)
            if kind == "redemption":
                missed_qi = find_most_missed(g)
                g["redemption_added"] = True
                rq = dict(g["quiz"][missed_qi])
                rq["redemption"] = True
                rq["double"] = True
                g["quiz"].append(rq)
                g["phase"] = "board"
                g["phase_entered"] = time.time()
                return
            if kind == "tiebreak":
                g["tb_done"] = True
                g["tb_pids"] = tie_leaders(g)
                g["quiz"].append(make_tiebreak_q(g))
                g["phase"] = "board"
                g["phase_entered"] = time.time()
                return
            g["redemption_added"] = True
            go_final(g)
        else:
            g["phase"] = "board"
            g["phase_entered"] = time.time()
    elif g["phase"] == "board":
        if g["q"] + 1 >= len(g["quiz"]):
            go_final(g)
        else:
            start_question(g, g["q"] + 1)
    elif g["phase"] == "intro":
        begin_question(g)
    elif g["phase"] == "icebreaker":
        start_question(g, 0)
    elif g["phase"] == "lobby":
        if g.get("icebreaker"):
            g["phase"] = "icebreaker"
            g["phase_entered"] = time.time()
            for p in g["players"].values():
                if p.get("bot"):
                    g["words"].append({"text": random.choice(BOT_ICE_WORDS), "name": p["name"]})
        else:
            start_question(g, 0)


def tick(g):
    """Move question -> results when time is up or everyone answered, run bots, and handle auto-advance."""
    now = time.time()
    if g["phase"] == "question":
        prev_left, q_left, q_elapsed, qt = cur_timer_state(g)
        qi = g["q"]
        q = g["quiz"][qi]
        if not g.get("paused") and prev_left <= 0:
            for p in eligible(g):
                if not p.get("bot") or qi in p["answers"]:
                    continue
                plan = p.setdefault("plan", {})
                if qi not in plan:
                    delay = round(random.uniform(1.4, max(2.5, qt * 0.75)), 2)
                    ch = bot_choice(q, p.get("skill", 0.65))
                    plan[qi] = (delay, ch)
                    pu = p.setdefault("powerups", {})
                    if not pu.get("2x_used") and random.random() < 0.2:
                        pu["2x_used"] = True
                        pu["2x_q"] = qi
                    elif not pu.get("shield_used") and calc_streak(p, qi - 1) >= 2 and random.random() < 0.25:
                        pu["shield_used"] = True
                        pu["shield_q"] = qi
                    if is_boss_q(g, qi):
                        p.setdefault("wagers", {})[qi] = random.choice([0, 50, 50, 100])
                    elif p.get("skill", 0.65) >= 0.72 and random.random() < 0.25:
                        p.setdefault("confident", {})[qi] = True
                delay, ch = plan[qi]
                if q_elapsed >= delay:
                    record_answer(g, p, ch, delay, bot_given(q, ch))
                    if random.random() < 0.22:
                        g["rseq"] = g.get("rseq", 0) + 1
                        g["reactions"].append({
                            "id": g["rseq"],
                            "emoji": random.choice(("🔥", "😂", "😮", "🎉", "👏", "❤️")),
                            "name": p["name"], "t": now,
                        })
        ps = eligible(g)
        done = ps and all(qi in p["answers"] for p in ps)
        if (not g.get("paused") and q_left <= 0) or done:
            g["phase"] = "results"
            g["phase_entered"] = now
            g["paused"] = False
            settle_question(g)
    elif g["phase"] == "intro":
        if now - g.get("intro_start", now) >= intro_len(g):
            begin_question(g)
    elif g["phase"] in ("results", "board") and g.get("auto"):
        wait_s = 7.0 if g["phase"] == "results" else 6.0
        if now - g.get("phase_entered", now) >= wait_s:
            advance_game(g)


def leaderboard(g):
    q = g["q"]
    rows = []
    for p in g["players"].values():
        a = p["answers"].get(q, {}) if q >= 0 else {}
        gained = a.get("gained", 0)
        streak = calc_streak(p, q) if q >= 0 else 0
        t_info = TEAM_MAP.get(p.get("team", ""), {})
        rows.append({
            "name": p["name"], "avatar": p.get("avatar", ""),
            "team": p.get("team", ""), "team_emoji": t_info.get("emoji", ""),
            "score": p["score"], "prev": p["score"] - gained,
            "gained": gained, "streak": streak,
            "frozen_next": bool(p.get("frozen_next")),
            "_ans": a,
        })
    prev_order = [x["name"] for x in sorted(rows, key=lambda x: -x["prev"])]
    rows.sort(key=lambda x: -x["score"])
    for i, r in enumerate(rows):
        r["prev_rank"] = prev_order.index(r["name"]) + 1
        r["rank"] = i + 1
        a = r.pop("_ans", {})
        diff = r["prev_rank"] - r["rank"] if q > 0 else 0
        title = ""
        if r["streak"] >= 4:
            title = "🔥 Unstoppable"
        elif a.get("correct") and a.get("time", 99) <= 2.5:
            title = "⚡ Speedster"
        elif diff >= 3:
            title = "🚀 Climber"
        elif a.get("correct") and (a.get("wager", 0) > 0 or a.get("confident")):
            title = "🎰 High Roller"
        elif a.get("shielded") and not a.get("correct"):
            title = "🛡️ Tank"
        r["title"] = title
    return rows


def team_leaderboard(g):
    if not g.get("teams"):
        return []
    buckets = {t["id"]: [] for t in TEAMS}
    for p in g["players"].values():
        tid = p.get("team")
        if tid in buckets:
            buckets[tid].append(p["score"])
    out = []
    for t in TEAMS:
        scores = buckets[t["id"]]
        if scores:
            out.append({
                "id": t["id"], "name": t["name"], "emoji": t["emoji"], "color": t["color"],
                "avg": round(sum(scores) / len(scores)), "total": sum(scores), "members": len(scores),
            })
    out.sort(key=lambda x: -x["avg"])
    return out


def round_shoutouts(g, rows):
    q = g["q"]
    shouts = []
    fastest = None
    for p in g["players"].values():
        a = p["answers"].get(q)
        if a and a.get("correct") and "time" in a:
            if fastest is None or a["time"] < fastest[0]:
                fastest = (a["time"], p)
    if fastest:
        shouts.append("⚡ Fastest Finger: %s (%.1fs)" % (ptag(fastest[1]), fastest[0]))
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
    top_streak = max(rows, key=lambda r: r["streak"], default=None)
    if top_streak and top_streak["streak"] >= 2:
        label = ((top_streak["avatar"] + " ") if top_streak.get("avatar") else "") + top_streak["name"]
        shouts.append("🔥 On Fire: %s (%d in a row!)" % (label, top_streak["streak"]))
    return shouts[:3]


def final_stats(g):
    total_q = len(g["quiz"])
    total_cor = 0
    speed_best = None
    streak_best = None
    for p in g["players"].values():
        cor_times = []
        for qi in range(total_q):
            a = p["answers"].get(qi)
            if a and a.get("correct"):
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
    q_stats = []
    n_p = max(1, len(g["players"]))
    for qi, q in enumerate(g["quiz"]):
        if q.get("tiebreak"):
            continue
        cor = sum(1 for p in g["players"].values() if p["answers"].get(qi, {}).get("correct"))
        pct = round(100.0 * cor / n_p)
        if pct < 100:
            q_stats.append({
                "index": qi + 1, "q": q["q"], "ans": ans_text(q),
                "pct": pct, "cor": cor, "total": len(g["players"]),
                "redemption": bool(q.get("redemption")),
            })
    q_stats.sort(key=lambda x: x["pct"])
    return {
        "accuracy": accuracy, "correct": total_cor, "possible": denom,
        "awards": awards, "tough": q_stats[:3],
    }


def player_report(g, p):
    cor = 0
    times = []
    missed = []
    total_q = 0
    for qi, q in enumerate(g["quiz"]):
        if q.get("tiebreak"):
            continue
        total_q += 1
        a = p["answers"].get(qi)
        if a and a.get("correct"):
            cor += 1
            if "time" in a:
                times.append(a["time"])
        else:
            if a and a.get("given"):
                your_ans = a["given"]
            elif a and a.get("choice") is not None and 0 <= a["choice"] < len(q["a"]) and not a.get("blank"):
                your_ans = q["a"][a["choice"]]
            else:
                your_ans = "No answer"
            missed.append({
                "num": qi + 1, "q": q["q"], "correct_ans": ans_text(q), "your_ans": your_ans,
            })
    return {
        "correct": cor, "total": total_q,
        "accuracy": round(100.0 * cor / max(1, total_q)),
        "fastest": round(min(times), 1) if times else None,
        "max_streak": calc_max_streak(p, len(g["quiz"])),
        "missed": missed,
    }


def export_csv(g):
    buf = io.StringIO()
    w = csv.writer(buf)
    total_q = len(g["quiz"])
    headers = ["Rank", "Nickname", "Team", "Score", "Correct", "Accuracy %", "Max Streak", "Fastest (s)"]
    for qi in range(total_q):
        headers.append("Q%d" % (qi + 1))
    w.writerow(headers)
    rows = leaderboard(g)
    by_name = {p["name"]: p for p in g["players"].values()}
    for r in rows:
        p = by_name.get(r["name"], {})
        rep = player_report(g, p) if p else {}
        t_name = TEAM_MAP.get(p.get("team", ""), {}).get("name", "")
        row = [
            r["rank"], r["name"], t_name, r["score"],
            "%d/%d" % (rep.get("correct", 0), rep.get("total", total_q)),
            "%d%%" % rep.get("accuracy", 0),
            rep.get("max_streak", 0),
            rep.get("fastest") if rep.get("fastest") is not None else "-",
        ]
        for qi, q in enumerate(g["quiz"]):
            a = p.get("answers", {}).get(qi)
            if not a:
                row.append("No answer")
            else:
                mark = "✓" if a.get("correct") else "✗"
                ch_txt = a.get("given") or (q["a"][a["choice"]] if 0 <= a["choice"] < len(q["a"]) else "?")
                row.append("%s %s (%.1fs)" % (mark, ch_txt, a.get("time", 0)))
        w.writerow(row)
    return buf.getvalue().encode("utf-8")


def study_html(g):
    n_p = max(1, len(g["players"]))
    rows = []
    for qi, q in enumerate(g["quiz"]):
        if q.get("tiebreak"):
            continue
        cor = sum(1 for p in g["players"].values() if p["answers"].get(qi, {}).get("correct"))
        pct = round(100.0 * cor / n_p)
        if pct < 100:
            rows.append((pct, qi, q))
    rows.sort(key=lambda r: (r[0], r[1]))
    e = html.escape
    body = []
    for pct, qi, q in rows:
        extra = ""
        if q.get("quote"):
            extra = "<div class='fact'>%s</div>" % e(q["quote"])
        body.append("<div class='item'><div class='pct'>%d%% got this right</div><h3>%s</h3><div class='ans'>Answer: <b>%s</b></div>%s</div>"
                    % (pct, e(q["q"]), e(ans_text(q)), extra))
    if not body:
        body = ["<p><b>Perfect game!</b> Every question was answered correctly by everyone.</p>"]
    return ("<!doctype html><html><head><meta charset='utf-8'><title>Study Guide</title><style>"
            "body{font-family:system-ui,sans-serif;max-width:760px;margin:24px auto;padding:0 16px;color:#222}"
            "h1{color:#46178f}.item{border:1px solid #ddd;border-left:6px solid #46178f;border-radius:8px;padding:10px 16px;margin:12px 0;page-break-inside:avoid}"
            ".pct{font-size:.8em;color:#c2185b;font-weight:700}h3{margin:.3em 0}.ans{color:#1b5e20}.fact{font-style:italic;color:#555;margin-top:6px}"
            "button{padding:10px 20px;font-size:1em;border-radius:8px;border:0;background:#26890c;color:#fff;font-weight:700;cursor:pointer}"
            "@media print{button{display:none}}</style></head><body>"
            "<h1>\U0001F4DA Study Guide \u2014 %s</h1><p>Questions the class missed most (lowest score first). "
            "%d players.</p><button onclick='window.print()'>\U0001F5A8\ufe0f Print / Save as PDF</button>%s</body></html>"
            % (e(quiz_title(g)), len(g["players"]), "".join(body))).encode("utf-8")


def spectator_view(g):
    tick(g)
    ph = g["phase"]
    rows = []
    if ph != "lobby":
        lb = leaderboard(g)
        if ph == "question":
            lb = sorted(lb, key=lambda r: -r["prev"])
            for i, r in enumerate(lb):
                r["score"] = r["prev"]
                r["rank"] = i + 1
        for r in lb[:10]:
            rows.append({"name": r["name"], "avatar": r.get("avatar", ""), "team_emoji": r.get("team_emoji", ""),
                         "score": r["score"], "streak": r["streak"], "title": r.get("title", ""), "rank": r["rank"]})
    counts = {}
    for w in g.get("words", []):
        counts[w["text"]] = counts.get(w["text"], 0) + 1
    return {
        "phase": ph, "pin": g["pin"], "n": len(g["players"]), "index": g["q"], "total": len(g["quiz"]),
        "board": rows, "prompt": g.get("icebreaker_prompt"), "word_total": len(g.get("words", [])),
        "words": [{"text": k, "count": v} for k, v in counts.items()],
        "names": [ptag(p) for p in g["players"].values()] if ph == "lobby" else None,
        "team_board": team_leaderboard(g) if g.get("teams") else [],
    }


def view(g, pid=None, host=False):
    tick(g)
    ph = g["phase"]
    now = time.time()
    g["reactions"] = [r for r in g.get("reactions", []) if now - r["t"] < 8]
    out = {
        "phase": ph, "pin": g["pin"], "n": len(g["players"]), "total": len(g["quiz"]),
        "names": [ptag(p) for p in g["players"].values()] if ph == "lobby" else None,
        "lobby": [{"name": p["name"], "tag": ptag(p), "bot": bool(p.get("bot"))} for p in g["players"].values()] if ph == "lobby" else None,
        "reactions": g["reactions"][-25:],
        "teams_enabled": bool(g.get("teams")),
        "sabotage_enabled": bool(g.get("sabotage", True)),
        "team_board": team_leaderboard(g) if g.get("teams") else [],
        "auto": bool(g.get("auto")),
        "paused": bool(g.get("paused")),
    }
    if ph == "icebreaker":
        out["prompt"] = g.get("icebreaker_prompt") or "In one word, how are you feeling about today's quiz?"
        counts = {}
        for w in g.get("words", []):
            txt = w["text"]
            counts[txt] = counts.get(txt, 0) + 1
        out["words"] = [{"text": k, "count": v} for k, v in counts.items()]
        out["word_total"] = len(g.get("words", []))
    if ph in ("results", "board") and g.get("auto"):
        wait_s = 7.0 if ph == "results" else 6.0
        out["auto_left"] = max(0, round(wait_s - (now - g.get("phase_entered", now))))
    if g["q"] >= 0 and ph in ("question", "results"):
        q = g["quiz"][g["q"]]
        prev_left, q_left, q_elapsed, qt = cur_timer_state(g)
        wmod = g.get("wheel_mods", {}).get(g["q"])
        qtp = qtype_of(q)
        disp = q["a"]
        if qtp == "order":
            perm = g.get("shuf", {}).get(g["q"])
            if perm:
                disp = [q["a"][k] for k in perm]
        elif qtp == "text":
            disp = []
        out.update(
            qtype=qtp, aimg=q.get("aimg"), tiebreak=bool(q.get("tiebreak")),
            team_battle=is_team_battle(g, g["q"]),
            index=g["q"], question=q["q"], answers=disp, img=q.get("img"), qtime=round(qt),
            left=round(q_left, 2),
            preview_left=round(prev_left, 2),
            preview_total=prev_time(g),
            double=(qmult(g) == 2),
            boss=is_boss_q(g),
            redemption=bool(q.get("redemption")),
            wheel_mod=wmod["label"] if wmod else None,
            wheel_id=wmod["id"] if wmod else None,
        )
        el = eligible(g)
        out["answered"] = sum(1 for p in el if g["q"] in p["answers"] and not p["answers"][g["q"]].get("blank"))
        out["n_active"] = len(el)
        if q.get("tiebreak"):
            out["tb_names"] = [ptag(g["players"][x]) for x in g.get("tb_pids", []) if x in g["players"]]
            out["tb_winner"] = g.get("tb_winner") if ph == "results" else None
        hs = g.get("hot", {}).get(g["q"])
        if hs and hs["pid"] in g["players"]:
            out["hot"] = {
                "name": ptag(g["players"][hs["pid"]]),
                "yes": sum(1 for v in hs["preds"].values() if v), "no": sum(1 for v in hs["preds"].values() if not v),
                "ok": hs.get("ok") if ph == "results" else None, "right": hs.get("right") if ph == "results" else None,
            }
        if ph == "results":
            out["team_results"] = g.get("team_results", {}).get(g["q"], [])
            out["answers_correct"] = q["a"]
            out["n_correct"] = sum(1 for p in g["players"].values() if p["answers"].get(g["q"], {}).get("correct"))
            out["correct"] = q["c"]
            out["quote"] = q.get("quote")
            out["source"] = q.get("src")
            out["is_last"] = will_finish_after_results(g)
            counts = [0] * len(q["a"])
            for p in g["players"].values():
                a = p["answers"].get(g["q"])
                if a and 0 <= a["choice"] < len(counts):
                    counts[a["choice"]] += 1
            out["counts"] = counts
    if ph == "intro":
        iq = g["quiz"][g["q"]]
        iw = g.get("wheel_mods", {}).get(g["q"])
        il = intro_len(g)
        out.update(
            index=g["q"], redemption=bool(iq.get("redemption")),
            wheel_mod=iw["label"] if iw else None, wheel_id=iw["id"] if iw else None,
            intro_total=il, intro_left=round(max(0.0, il - (now - g.get("intro_start", now))), 2),
        )
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
        wagers = p.get("wagers", {})
        t_info = TEAM_MAP.get(p.get("team", ""), {})
        lb = leaderboard(g)
        my_idx = [x["name"] for x in lb].index(p["name"])
        my_rank = my_idx + 1
        my_row = lb[my_idx]
        leader_name = lb[0]["name"] if lb else ""
        rival = None
        if my_idx > 0:
            ah = lb[my_idx - 1]
            rival = {"type": "ahead", "name": ah["name"], "avatar": ah.get("avatar", ""), "gap": max(1, ah["score"] - p["score"])}
        elif len(lb) > 1:
            bh = lb[1]
            rival = {"type": "behind", "name": bh["name"], "avatar": bh.get("avatar", ""), "gap": max(0, p["score"] - bh["score"])}
        overtook = None
        if g["q"] > 0 and my_row["prev_rank"] > my_rank:
            passed = [x for x in lb if x["rank"] > my_rank and x["prev_rank"] < my_row["prev_rank"]]
            if passed:
                ov = passed[0]
                overtook = ((ov["avatar"] + " ") if ov.get("avatar") else "") + ov["name"]
        act_aud = (pu.get("audience_q") == g["q"])
        me = {
            "name": p["name"], "avatar": p.get("avatar", ""),
            "team": p.get("team", ""), "team_name": t_info.get("name", ""), "team_emoji": t_info.get("emoji", ""),
            "title": my_row.get("title", ""),
            "score": p["score"],
            "answered": a is not None and not a.get("blank"),
            "choice": a["choice"] if a else None,
            "gained": a["gained"] if a else 0,
            "bonus": a.get("bonus", 0) if a else 0,
            "wager_amt": a.get("wager", 0) if a else 0,
            "wager_pct": wagers.get(g["q"]),
            "confident": bool(p.get("confident", {}).get(g["q"])),
            "conf_delta": a.get("conf_delta", 0) if a else 0,
            "wheel_bonus": a.get("wheel_bonus", 0) if a else 0,
            "correct": a.get("correct", False) if a else False,
            "shielded": a.get("shielded", False) if a else False,
            "underdog": (is_underdog(g, p, g["q"]) if ph == "question" else (a.get("underdog", False) if a else False)),
            "streak": streak,
            "rank": my_rank,
            "leader": leader_name,
            "rival": rival,
            "overtook": overtook,
            "pu_5050": not pu.get("5050_used", False),
            "pu_2x": not pu.get("2x_used", False),
            "pu_shield": not pu.get("shield_used", False),
            "pu_audience": not pu.get("audience_used", False),
            "pu_freeze": not pu.get("freeze_used", False),
            "active_2x": pu.get("2x_q") == g["q"],
            "active_shield": pu.get("shield_q") == g["q"],
            "active_audience": act_aud,
            "audience_pcts": audience_pcts(g, g["q"]) if (act_aud and g["q"] >= 0) else None,
            "frozen": p.get("frozen_q") == g["q"],
            "frozen_by": p.get("frozen_by_q", "Someone"),
            "ice_sent": bool(p.get("ice_sent_q") == g["q"]),
            "word_sent": bool(p.get("word_sent")),
            "hide": pu.get("hide", []) if pu.get("5050_q") == g["q"] else [],
        }
        hs = g.get("hot", {}).get(g["q"]) if g["q"] >= 0 else None
        if hs and ph in ("question", "results"):
            hp = g["players"].get(hs["pid"])
            me["hot"] = {
                "name": hp["name"] if hp else "?", "avatar": hp.get("avatar", "") if hp else "",
                "you": hs["pid"] == pid, "pred": hs["preds"].get(pid), "ok": hs.get("ok"),
                "pred_bonus": a.get("pred_bonus", 0) if a else 0, "hot_bonus": a.get("hot_bonus", 0) if a else 0,
            }
        me["team_bonus"] = a.get("team_bonus", 0) if a else 0
        cq = g["quiz"][g["q"]] if 0 <= g["q"] < len(g["quiz"]) else {}
        if cq.get("tiebreak") and pid not in g.get("tb_pids", []):
            me["spectating"] = True
        if ph == "final":
            me["report"] = player_report(g, p)
        out["me"] = me
    elif pid:
        out["kicked"] = True
    return out


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj, code=200, ctype="application/json", extra_headers=None):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if path == "/api/active":
            q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
            with LOCK:
                if not GAMES:
                    return self.send({"pin": "", "teams": False})
                g = GAMES.get(q.get("pin")) if q.get("pin") else list(GAMES.values())[-1]
                if not g or g.get("phase") == "final":
                    return self.send({"pin": "", "teams": False})
                taken = {p["name"].lower() for p in g["players"].values()}
                roster = [r for r in g.get("roster", []) if r.lower() not in taken]
                return self.send({"pin": g["pin"], "teams": bool(g.get("teams")), "roster": roster,
                                  "has_roster": bool(g.get("roster")), "logo": g.get("logo", "")})
        if path == "/api/config":
            return self.send({"pw": bool(HOST_PW)})
        if path == "/api/history":
            q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
            if not pw_ok(unquote(q.get("pw", ""))):
                return self.send({"error": "Host password required", "need_pw": True}, 403)
            hist = load_history()
            agg = {}
            for e_ in hist:
                for qq in e_.get("qs", []):
                    d = agg.setdefault(qq["q"], {"q": qq["q"], "ans": qq.get("ans", ""), "sum": 0, "n": 0})
                    d["sum"] += qq["pct"]
                    d["n"] += 1
            reteach = sorted(({"q": d["q"], "ans": d["ans"], "avg": round(d["sum"] / d["n"]), "games": d["n"]}
                              for d in agg.values()), key=lambda r: r["avg"])
            return self.send({"games": list(reversed(hist))[:50], "reteach": [r for r in reteach if r["avg"] < 85][:12]})
        if path == "/api/study":
            q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
            with LOCK:
                g = GAMES.get(q.get("pin"))
                if not g:
                    return self.send({"error": "Game not found"}, 404)
                return self.send(study_html(g), ctype="text/html; charset=utf-8")
        if path == "/api/spectator":
            q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
            with LOCK:
                g = GAMES.get(q.get("pin")) if q.get("pin") else (list(GAMES.values())[-1] if GAMES else None)
                if not g:
                    return self.send({"error": "Game not found"}, 404)
                return self.send(spectator_view(g))
        if path == "/api/quizzes":
            return self.send({"quizzes": list_quizzes()})
        if path == "/api/quiz_data":
            q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
            safe = os.path.basename(q.get("file") or "quiz.json")
            return self.send({"file": safe, "questions": load_quiz(safe)})
        if path == "/api/export":
            q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
            with LOCK:
                g = GAMES.get(q.get("pin"))
                if not g:
                    return self.send({"error": "Game not found"}, 404)
                csv_bytes = export_csv(g)
                return self.send(
                    csv_bytes, code=200, ctype="text/csv; charset=utf-8",
                    extra_headers={"Content-Disposition": 'attachment; filename="brainrush_%s_results.csv"' % g["pin"]},
                )
        if path == "/api/state":
            q = dict(x.split("=", 1) for x in qs.split("&") if "=" in x)
            with LOCK:
                g = GAMES.get(q.get("pin"))
                if not g:
                    return self.send({"error": "Game not found"}, 404)
                return self.send(view(g, q.get("pid")))
        files = {
            "/": "host.html", "/host": "host.html",
            "/play": "play.html", "/remote": "remote.html",
            "/practice": "practice.html", "/spectator": "spectator.html",
            "/logo.jpg": "logo.jpg",
        }
        if path in files:
            ctype = "image/jpeg" if path.endswith(".jpg") else "text/html; charset=utf-8"
            with open(os.path.join(ROOT, files[path]), "rb") as f:
                return self.send(f.read(), ctype=ctype)
        self.send({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        path = self.path
        with LOCK:
            if path in ("/api/upload_quiz", "/api/delete_quiz", "/api/create", "/api/history_clear") and not pw_ok(body.get("password")):
                return self.send({"error": "Host password required", "need_pw": True}, 403)
            if path == "/api/history_clear":
                save_history([])
                return self.send({"ok": True})
            if path == "/api/upload_quiz":
                try:
                    fname, cnt, cleaned = save_custom_quiz(body.get("title"), body.get("questions") or [])
                    return self.send({"ok": True, "file": fname, "count": cnt, "questions": cleaned, "quizzes": list_quizzes()})
                except Exception as e:
                    return self.send({"error": str(e)}, 400)
            if path == "/api/delete_quiz":
                safe = os.path.basename(body.get("file") or "")
                protected = {"quiz.json", "quiz_chapter8.json", "quiz_fun_facts.json", "quiz_ultimate_silly_trivia.json"}
                if safe and safe not in protected and safe.startswith("quiz_") and safe.endswith(".json"):
                    p = os.path.join(ROOT, safe)
                    if os.path.exists(p):
                        os.remove(p)
                return self.send({"ok": True, "quizzes": list_quizzes()})
            if path == "/api/create":
                g = new_game(
                    body.get("quiz") or "quiz.json",
                    body.get("count"),
                    bool(body.get("shuffle")),
                    custom_questions=body.get("custom_questions"),
                )
                if "preview" in body:
                    g["preview"] = float(body["preview"])
                if body.get("qtime"):
                    try:
                        g["qtime"] = max(5, min(120, int(body["qtime"])))
                    except Exception:
                        pass
                if "teams" in body:
                    g["teams"] = bool(body["teams"])
                if "wager" in body:
                    g["wager"] = bool(body["wager"])
                if "redemption" in body:
                    g["redemption"] = bool(body["redemption"])
                if "sabotage" in body:
                    g["sabotage"] = bool(body["sabotage"])
                if "wheel" in body:
                    g["wheel"] = bool(body["wheel"])
                for k_ in ("hotseat", "team_battle", "tiebreak"):
                    if k_ in body:
                        g[k_] = bool(body[k_])
                if isinstance(body.get("roster"), list):
                    seen_ = set()
                    for r_ in body["roster"][:120]:
                        r_ = re.sub(r"\s+", " ", str(r_)).strip()[:24]
                        if r_ and r_.lower() not in seen_:
                            seen_.add(r_.lower())
                            g["roster"].append(r_)
                if body.get("logo"):
                    g["logo"] = clean_url(body["logo"])
                if "icebreaker" in body:
                    g["icebreaker"] = bool(body["icebreaker"])
                if body.get("icebreaker_prompt"):
                    g["icebreaker_prompt"] = str(body["icebreaker_prompt"]).strip()[:120]
                if "auto" in body:
                    g["auto"] = bool(body["auto"])
                if body.get("bots"):
                    add_bots(g, body.get("bots"))
                return self.send({"pin": g["pin"], "host": g["host"]})
            g = GAMES.get(body.get("pin"))
            if not g:
                return self.send({"error": "Game not found"}, 404)
            if path == "/api/kick":
                if body.get("host") != g["host"]:
                    return self.send({"error": "Forbidden"}, 403)
                nm = str(body.get("name") or "").strip().lower()
                gone = [pid for pid, p in g["players"].items() if p["name"].lower() == nm]
                for pid in gone:
                    del g["players"][pid]
                if gone and not body.get("allow_rejoin"):
                    g.setdefault("banned", []).append(nm)
                return self.send({"ok": True, "removed": len(gone)})
            if path == "/api/predict":
                p = g["players"].get(body.get("pid"))
                hs = g.get("hot", {}).get(g["q"]) if g["q"] >= 0 else None
                if not p or not hs or g["phase"] != "question" or hs["pid"] == body.get("pid"):
                    return self.send({"error": "Cannot predict"}, 400)
                hs["preds"][body["pid"]] = bool(body.get("guess"))
                return self.send({"ok": True})
            if path == "/api/replay":
                if body.get("host") != g["host"]:
                    return self.send({"error": "Forbidden"}, 403)
                replay_game(
                    g,
                    body.get("quiz") or "quiz.json",
                    body.get("count"),
                    bool(body.get("shuffle", True)),
                    custom_questions=body.get("custom_questions"),
                )
                return self.send({"ok": True, "pin": g["pin"]})
            if path == "/api/bots":
                if body.get("host") != g["host"]:
                    return self.send({"error": "Forbidden"}, 403)
                n = add_bots(g, body.get("count", 5))
                return self.send({"ok": True, "added": n})
            if path == "/api/host_ctl":
                if body.get("host") != g["host"]:
                    return self.send({"error": "Forbidden"}, 403)
                act = body.get("action")
                if act == "toggle_auto":
                    g["auto"] = not g.get("auto", False)
                    g["phase_entered"] = time.time()
                elif act == "pause" and g["phase"] == "question":
                    if not g.get("paused"):
                        prev_l, q_l, _, _ = cur_timer_state(g)
                        g["paused"] = True
                        g["pause_prev"] = prev_l
                        g["pause_rem"] = q_l
                    else:
                        prev_t = prev_time(g)
                        qt = qtime(g) + g.get("extra_time", 0.0)
                        if g.get("pause_prev", 0) > 0:
                            g["start"] = time.time() - (prev_t - g["pause_prev"])
                        else:
                            g["start"] = time.time() - prev_t - (qt - g.get("pause_rem", qt))
                        g["paused"] = False
                elif act == "add_time" and g["phase"] == "question":
                    add_s = float(body.get("sec", 10))
                    g["extra_time"] = g.get("extra_time", 0.0) + add_s
                    if g.get("paused"):
                        g["pause_rem"] = g.get("pause_rem", 0.0) + add_s
                elif act == "set_prompt":
                    pr = str(body.get("prompt") or "").strip()[:120]
                    if pr:
                        g["icebreaker_prompt"] = pr
                        g["words"] = []
                        for p in g["players"].values():
                            p["word_sent"] = False
                            p.get("plan", {}).pop("ice_t", None)
                return self.send({"ok": True, "auto": g["auto"], "paused": g["paused"]})
            if path == "/api/join":
                name = (body.get("name") or "").strip()[:24]
                avatar = (body.get("avatar") or "").strip()[:4]
                team_req = (body.get("team") or "").strip()
                if g["phase"] == "final":
                    return self.send({"error": "Game already finished"}, 400)
                if g.get("roster"):
                    match = next((r for r in g["roster"] if r.lower() == name.lower()), None)
                    if not match:
                        return self.send({"error": "Pick your name from the class list"}, 400)
                    name = match
                    for old_pid, op in g["players"].items():
                        if op["name"].lower() == name.lower() and not op.get("bot"):
                            return self.send({"pid": old_pid, "team": op.get("team", "")})
                else:
                    name = name[:16]
                    if is_profane(name):
                        return self.send({"error": "That nickname isn't allowed. Pick another!"}, 400)
                if name.lower() in g.get("banned", []):
                    return self.send({"error": "You were removed from this game"}, 400)
                if not name or any(p["name"].lower() == name.lower() for p in g["players"].values()):
                    return self.send({"error": "Pick a unique nickname"}, 400)
                pid = "p%08x" % random.getrandbits(32)
                team_id = pick_balanced_team(g, team_req) if g.get("teams") else ""
                g["players"][pid] = {
                    "name": name, "avatar": avatar, "team": team_id,
                    "score": 0, "answers": {}, "powerups": {}, "wagers": {}, "confident": {},
                }
                return self.send({"pid": pid, "team": team_id})
            if path == "/api/confident":
                p = g["players"].get(body.get("pid"))
                if not p or g["phase"] != "question" or g["q"] in p["answers"]:
                    return self.send({"error": "Cannot toggle confidence"}, 400)
                cur = bool(p.setdefault("confident", {}).get(g["q"]))
                p["confident"][g["q"]] = not cur
                return self.send({"ok": True, "confident": p["confident"][g["q"]]})
            if path == "/api/word":
                p = g["players"].get(body.get("pid"))
                word = (body.get("word") or "").strip()[:28]
                if not p or g["phase"] != "icebreaker" or not word:
                    return self.send({"error": "Cannot submit word"}, 400)
                if is_profane(word):
                    p["word_sent"] = True
                    return self.send({"ok": True})
                g.setdefault("words", []).append({"text": word, "name": p["name"]})
                p["word_sent"] = True
                return self.send({"ok": True})
            if path == "/api/wager":
                p = g["players"].get(body.get("pid"))
                if not p or g["phase"] != "question":
                    return self.send({"error": "Cannot wager now"}, 400)
                pct = max(0, min(100, int(body.get("pct", 0))))
                p.setdefault("wagers", {})[g["q"]] = pct
                return self.send({"ok": True, "pct": pct})
            if path == "/api/sabotage":
                p = g["players"].get(body.get("pid"))
                if not p or not g.get("sabotage", True):
                    return self.send({"error": "Cannot sabotage"}, 400)
                pu = p.setdefault("powerups", {})
                if pu.get("freeze_used") or p["score"] < 200:
                    return self.send({"error": "Need 200 pts (once per game)"}, 400)
                lb = leaderboard(g)
                if not lb:
                    return self.send({"error": "No target"}, 400)
                target_name = lb[0]["name"]
                if target_name == p["name"] and len(lb) > 1:
                    target_name = lb[1]["name"]
                for tp in g["players"].values():
                    if tp["name"] == target_name:
                        tp["frozen_next"] = True
                        tp["frozen_by"] = p["name"]
                        break
                p["score"] = max(0, p["score"] - 200)
                pu["freeze_used"] = True
                p["ice_sent_q"] = g["q"]
                g["rseq"] = g.get("rseq", 0) + 1
                g["reactions"].append({
                    "id": g["rseq"], "emoji": "⛄", "name": "%s → %s" % (p["name"], target_name), "t": time.time(),
                })
                return self.send({"ok": True, "target": target_name})
            if path == "/api/powerup":
                p = g["players"].get(body.get("pid"))
                kind = body.get("kind")
                if not p or g["phase"] != "question" or g["q"] in p["answers"]:
                    return self.send({"error": "Cannot use powerup"}, 400)
                pu = p.setdefault("powerups", {})
                if kind in ("5050", "audience") and qtype_of(g["quiz"][g["q"]]) in ("text", "order"):
                    return self.send({"error": "Not available for this question type"}, 400)
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
                if kind == "shield" and not pu.get("shield_used"):
                    pu["shield_used"] = True
                    pu["shield_q"] = g["q"]
                    return self.send({"ok": True})
                if kind == "audience" and not pu.get("audience_used"):
                    pu["audience_used"] = True
                    pu["audience_q"] = g["q"]
                    return self.send({"ok": True, "pcts": audience_pcts(g, g["q"])})
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
                if p not in eligible(g):
                    return self.send({"error": "Only the tied players can answer this one"}, 400)
                q = g["quiz"][g["q"]]
                qtp = qtype_of(q)
                given = None
                if qtp == "text":
                    given = str(body.get("text") or "").strip()[:80]
                    if not given:
                        return self.send({"error": "Type an answer"}, 400)
                    choice = q["c"] if text_correct(q, given) else -1
                elif qtp == "order":
                    seq = body.get("seq")
                    n_ = len(q["a"])
                    perm = g.get("shuf", {}).get(g["q"]) or list(range(n_))
                    if not isinstance(seq, list) or sorted(seq) != list(range(n_)):
                        return self.send({"error": "Order all the items"}, 400)
                    orig = [perm[j] for j in seq]
                    choice = q["c"] if orig == list(range(n_)) else -1
                    given = " \u2192 ".join(q["a"][k] for k in orig)
                else:
                    choice = int(body["choice"])
                    if not 0 <= choice < len(q["a"]):
                        return self.send({"error": "Bad choice"}, 400)
                _, _, q_elapsed, _ = cur_timer_state(g)
                record_answer(g, p, choice, q_elapsed, given)
                return self.send({"ok": True})
            if path == "/api/next":
                if body.get("host") != g["host"]:
                    return self.send({"error": "Forbidden"}, 403)
                tick(g)
                advance_game(g)
                return self.send({"ok": True})
        self.send({"error": "not found"}, 404)


def _ik(d):
    out = {}
    for k, v in d.items():
        out[int(k) if str(k).lstrip("-").isdigit() else k] = v
    return out


def restore_games():
    try:
        with open(data_path("games.json")) as fp:
            raw = json.load(fp)
    except Exception:
        return 0
    n = 0
    now = time.time()
    for pin, g in raw.items():
        try:
            if now - g.get("created", now) > 12 * 3600:
                continue
            for k in INT_KEY_G:
                if isinstance(g.get(k), dict):
                    g[k] = _ik(g[k])
            for p in g["players"].values():
                for k in INT_KEY_P:
                    if isinstance(p.get(k), dict):
                        p[k] = _ik(p[k])
            g["phase_entered"] = now
            if g.get("phase") == "intro":
                g["intro_start"] = now
            if g.get("phase") == "question":
                g["start"] = now - 0.1
                g["paused"] = True
                g["pause_prev"] = prev_time(g)
                g["pause_rem"] = qtime(g) + g.get("extra_time", 0.0)
            GAMES[pin] = g
            n += 1
        except Exception:
            continue
    return n


def snapshot_loop():
    last = ""
    while True:
        time.sleep(4)
        try:
            with LOCK:
                snap = json.dumps(GAMES)
            if snap != last:
                tmp = data_path("games.json.tmp")
                with open(tmp, "w") as fp:
                    fp.write(snap)
                os.replace(tmp, data_path("games.json"))
                last = snap
        except Exception:
            pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8000))
    restored = restore_games()
    threading.Thread(target=snapshot_loop, daemon=True).start()
    if restored:
        print("Restored %d game(s) from disk (timers paused until host resumes)." % restored)
    print("Host:    http://localhost:%d/host\nPlayers: http://<your-ip>:%d/play" % (port, port))
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
