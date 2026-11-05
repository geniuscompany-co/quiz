#!/usr/bin/env python3
"""Kahoot-style live quiz server. Standard library only. Run: python3 server.py [port]"""
import csv, io, json, os, random, re, sys, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
LOCK = threading.Lock()
GAMES = {}
DEFAULT_TIME = 20  # seconds; override per question with "t" in quiz.json
PREVIEW_TIME = 5.0  # seconds to read question alone before answers appear and timer starts

TEAMS = [
    {"id": "red", "name": "Red Dragons", "emoji": "🔴", "color": "#e21b3c"},
    {"id": "blue", "name": "Blue Sharks", "emoji": "🔵", "color": "#1368ce"},
    {"id": "gold", "name": "Gold Lions", "emoji": "🟡", "color": "#d89e00"},
    {"id": "green", "name": "Green Vipers", "emoji": "🟢", "color": "#26890c"},
]
TEAM_MAP = {t["id"]: t for t in TEAMS}


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
    if q.get("redemption"):
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


def save_custom_quiz(title, questions):
    slug = re.sub(r"[^a-z0-9]+", "_", (title or "custom").lower()).strip("_") or "custom"
    fname = "quiz_%s.json" % slug
    cleaned = []
    for q in questions:
        if not isinstance(q, dict) or not q.get("q") or not isinstance(q.get("a"), list) or len(q["a"]) < 2:
            continue
        ans = [str(x).strip() for x in q["a"][:4]]
        while len(ans) < 4:
            ans.append("Option %d" % (len(ans) + 1))
        item = {"q": str(q["q"]).strip(), "a": ans, "c": max(0, min(3, int(q.get("c", 0))))}
        if q.get("quote"):
            item["quote"] = str(q["quote"]).strip()
        if q.get("src"):
            item["src"] = str(q["src"]).strip()
        if q.get("img"):
            item["img"] = str(q["img"]).strip()
        cleaned.append(item)
    if not cleaned:
        raise ValueError("No valid questions provided")
    with open(os.path.join(ROOT, fname), "w") as f:
        json.dump(cleaned, f, indent=2)
    return fname, len(cleaned)


def new_game(quiz_file="quiz.json", count=None, shuffle=False):
    while True:
        pin = str(random.randint(100000, 999999))
        if pin not in GAMES:
            break
    bank = [dict(x) for x in load_quiz(quiz_file)]
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
        "sabotage": True, "icebreaker": False, "icebreaker_prompt": "In one word, how are you feeling right now?",
        "words": [],
        "auto": False, "phase_entered": time.time(),
        "paused": False, "pause_rem": 0.0, "pause_prev": 0.0, "extra_time": 0.0,
    }
    return GAMES[pin]


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


def cur_timer_state(g):
    """Return (preview_left, q_left, q_elapsed, total_qtime)."""
    qt = qtime(g) + g.get("extra_time", 0.0)
    prev_t = g.get("preview", PREVIEW_TIME)
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


def record_answer(g, p, choice, t):
    qi = g["q"]
    q = g["quiz"][qi]
    qt = max(1.0, qtime(g) + g.get("extra_time", 0.0))
    is_correct = (choice == q["c"])
    pu = p.get("powerups", {})
    p_mult = 2 if pu.get("2x_q") == qi else 1
    shielded = (pu.get("shield_q") == qi)
    underdog = is_underdog(g, p, qi)
    u_mult = 1.5 if underdog else 1.0
    wager_pct = p.get("wagers", {}).get(qi)
    wager_amt = 0
    if wager_pct is not None and wager_pct > 0:
        stake = max(500, p["score"])
        wager_amt = round(stake * (wager_pct / 100.0))
    if is_correct:
        prev_streak = calc_streak(p, qi - 1) if qi > 0 else 0
        new_streak = prev_streak + 1
        bonus = min(300, (new_streak - 1) * 100) if new_streak >= 2 else 0
        base = round(1000 * (1 - min(1.0, t / qt) / 2) * qmult(g) * p_mult * u_mult)
        gained = base + bonus + wager_amt
    else:
        bonus = 0
        gained = -wager_amt if (wager_amt > 0 and not shielded) else 0
    p["score"] = max(0, p["score"] + gained)
    p["answers"][qi] = {
        "choice": choice, "gained": gained, "bonus": bonus,
        "wager": wager_amt, "correct": is_correct, "time": round(t, 2),
        "shielded": shielded, "underdog": underdog and is_correct,
    }


def start_question(g, qi):
    g["q"] = qi
    g["phase"] = "question"
    g["start"] = time.time()
    g["phase_entered"] = time.time()
    g["paused"] = False
    g["extra_time"] = 0.0
    for p in g["players"].values():
        if p.get("frozen_next"):
            p["frozen_q"] = qi
            p["frozen_by_q"] = p.get("frozen_by", "Someone")
            p["frozen_next"] = False


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


def will_finish_after_results(g):
    if g["q"] + 1 < len(g["quiz"]):
        return False
    if g.get("redemption") and not g.get("redemption_added"):
        return find_most_missed(g) is None
    return True


def advance_game(g):
    if g["phase"] == "question":
        qi = g["q"]
        for p in g["players"].values():
            if p.get("bot") and qi not in p["answers"]:
                plan = p.get("plan", {})
                if qi in plan:
                    delay, ch = plan[qi]
                else:
                    delay = round(random.uniform(2.0, qtime(g) * 0.7), 2)
                    ch = g["quiz"][qi]["c"] if random.random() < p.get("skill", 0.65) else random.randint(0, len(g["quiz"][qi]["a"]) - 1)
                if is_boss_q(g, qi) and qi not in p.setdefault("wagers", {}):
                    p["wagers"][qi] = random.choice([0, 50, 50, 100])
                record_answer(g, p, ch, delay)
        g["phase"] = "results"
        g["phase_entered"] = time.time()
        g["paused"] = False
    elif g["phase"] == "results":
        if g["q"] + 1 >= len(g["quiz"]):
            if g.get("redemption") and not g.get("redemption_added"):
                missed_qi = find_most_missed(g)
                g["redemption_added"] = True
                if missed_qi is not None:
                    rq = dict(g["quiz"][missed_qi])
                    rq["redemption"] = True
                    rq["double"] = True
                    g["quiz"].append(rq)
                    g["phase"] = "board"
                    g["phase_entered"] = time.time()
                    return
            g["phase"] = "final"
            g["phase_entered"] = time.time()
        else:
            g["phase"] = "board"
            g["phase_entered"] = time.time()
    elif g["phase"] == "board":
        if g["q"] + 1 >= len(g["quiz"]):
            g["phase"] = "final"
            g["phase_entered"] = time.time()
        else:
            start_question(g, g["q"] + 1)
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
            for p in g["players"].values():
                if not p.get("bot") or qi in p["answers"]:
                    continue
                plan = p.setdefault("plan", {})
                if qi not in plan:
                    delay = round(random.uniform(1.4, max(2.5, qt * 0.75)), 2)
                    if random.random() < p.get("skill", 0.65):
                        ch = q["c"]
                    else:
                        wrongs = [i for i in range(len(q["a"])) if i != q["c"]]
                        ch = random.choice(wrongs) if wrongs else 0
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
                delay, ch = plan[qi]
                if q_elapsed >= delay:
                    record_answer(g, p, ch, delay)
                    if random.random() < 0.22:
                        g["rseq"] = g.get("rseq", 0) + 1
                        g["reactions"].append({
                            "id": g["rseq"],
                            "emoji": random.choice(("🔥", "😂", "😮", "🎉", "👏", "❤️")),
                            "name": p["name"], "t": now,
                        })
        ps = g["players"].values()
        done = ps and all(qi in p["answers"] for p in ps)
        if (not g.get("paused") and q_left <= 0) or done:
            g["phase"] = "results"
            g["phase_entered"] = now
            g["paused"] = False
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
        })
    prev_order = [x["name"] for x in sorted(rows, key=lambda x: -x["prev"])]
    rows.sort(key=lambda x: -x["score"])
    for i, r in enumerate(rows):
        r["prev_rank"] = prev_order.index(r["name"]) + 1
        r["rank"] = i + 1
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
        cor = sum(1 for p in g["players"].values() if p["answers"].get(qi, {}).get("correct"))
        pct = round(100.0 * cor / n_p)
        if pct < 100:
            q_stats.append({
                "index": qi + 1, "q": q["q"], "ans": q["a"][q["c"]],
                "pct": pct, "cor": cor, "total": len(g["players"]),
                "redemption": bool(q.get("redemption")),
            })
    q_stats.sort(key=lambda x: x["pct"])
    return {
        "accuracy": accuracy, "correct": total_cor, "possible": denom,
        "awards": awards, "tough": q_stats[:3],
    }


def player_report(g, p):
    total_q = len(g["quiz"])
    cor = 0
    times = []
    missed = []
    for qi, q in enumerate(g["quiz"]):
        a = p["answers"].get(qi)
        if a and a.get("correct"):
            cor += 1
            if "time" in a:
                times.append(a["time"])
        else:
            your_ans = q["a"][a["choice"]] if (a and a.get("choice") is not None and 0 <= a["choice"] < len(q["a"])) else "No answer"
            missed.append({
                "num": qi + 1, "q": q["q"], "correct_ans": q["a"][q["c"]], "your_ans": your_ans,
            })
    return {
        "correct": cor, "total": total_q,
        "accuracy": round(100.0 * cor / max(1, total_q)),
        "fastest": round(min(times), 1) if times else None,
        "max_streak": calc_max_streak(p, total_q),
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
            "%d/%d" % (rep.get("correct", 0), total_q),
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
                ch_txt = q["a"][a["choice"]] if 0 <= a["choice"] < len(q["a"]) else "?"
                row.append("%s %s (%.1fs)" % (mark, ch_txt, a.get("time", 0)))
        w.writerow(row)
    return buf.getvalue().encode("utf-8")


def view(g, pid=None, host=False):
    tick(g)
    ph = g["phase"]
    now = time.time()
    g["reactions"] = [r for r in g.get("reactions", []) if now - r["t"] < 8]
    out = {
        "phase": ph, "pin": g["pin"], "n": len(g["players"]), "total": len(g["quiz"]),
        "names": [ptag(p) for p in g["players"].values()] if ph == "lobby" else None,
        "reactions": g["reactions"][-25:],
        "teams_enabled": bool(g.get("teams")),
        "sabotage_enabled": bool(g.get("sabotage", True)),
        "team_board": team_leaderboard(g) if g.get("teams") else [],
        "auto": bool(g.get("auto")),
        "paused": bool(g.get("paused")),
    }
    if ph == "icebreaker":
        out["prompt"] = g.get("icebreaker_prompt") or "In one word, how are you feeling right now?"
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
        out.update(
            index=g["q"], question=q["q"], answers=q["a"], img=q.get("img"), qtime=round(qt),
            left=round(q_left, 2),
            preview_left=round(prev_left, 2),
            preview_total=g.get("preview", PREVIEW_TIME),
            double=(qmult(g) == 2),
            boss=is_boss_q(g),
            redemption=bool(q.get("redemption")),
        )
        out["answered"] = sum(1 for p in g["players"].values() if g["q"] in p["answers"])
        if ph == "results":
            out["correct"] = q["c"]
            out["quote"] = q.get("quote")
            out["source"] = q.get("src")
            out["is_last"] = will_finish_after_results(g)
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
        wagers = p.get("wagers", {})
        t_info = TEAM_MAP.get(p.get("team", ""), {})
        lb = leaderboard(g)
        my_rank = [x["name"] for x in lb].index(p["name"]) + 1
        leader_name = lb[0]["name"] if lb else ""
        me = {
            "name": p["name"], "avatar": p.get("avatar", ""),
            "team": p.get("team", ""), "team_name": t_info.get("name", ""), "team_emoji": t_info.get("emoji", ""),
            "score": p["score"],
            "answered": a is not None,
            "choice": a["choice"] if a else None,
            "gained": a["gained"] if a else 0,
            "bonus": a.get("bonus", 0) if a else 0,
            "wager_amt": a.get("wager", 0) if a else 0,
            "wager_pct": wagers.get(g["q"]),
            "correct": a.get("correct", False) if a else False,
            "shielded": a.get("shielded", False) if a else False,
            "underdog": (is_underdog(g, p, g["q"]) if ph == "question" else (a.get("underdog", False) if a else False)),
            "streak": streak,
            "rank": my_rank,
            "leader": leader_name,
            "pu_5050": not pu.get("5050_used", False),
            "pu_2x": not pu.get("2x_used", False),
            "pu_shield": not pu.get("shield_used", False),
            "pu_freeze": not pu.get("freeze_used", False),
            "active_2x": pu.get("2x_q") == g["q"],
            "active_shield": pu.get("shield_q") == g["q"],
            "frozen": p.get("frozen_q") == g["q"],
            "frozen_by": p.get("frozen_by_q", "Someone"),
            "ice_sent": bool(p.get("ice_sent_q") == g["q"]),
            "word_sent": bool(p.get("word_sent")),
            "hide": pu.get("hide", []) if pu.get("5050_q") == g["q"] else [],
        }
        if ph == "final":
            me["report"] = player_report(g, p)
        out["me"] = me
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
                return self.send({"pin": g["pin"], "teams": bool(g.get("teams"))})
        if path == "/api/quizzes":
            return self.send({"quizzes": list_quizzes()})
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
            if path == "/api/upload_quiz":
                try:
                    fname, cnt = save_custom_quiz(body.get("title"), body.get("questions") or [])
                    return self.send({"ok": True, "file": fname, "count": cnt, "quizzes": list_quizzes()})
                except Exception as e:
                    return self.send({"error": str(e)}, 400)
            if path == "/api/create":
                g = new_game(body.get("quiz") or "quiz.json", body.get("count"), bool(body.get("shuffle")))
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
                        prev_t = g.get("preview", PREVIEW_TIME)
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
                return self.send({"ok": True, "auto": g["auto"], "paused": g["paused"]})
            if path == "/api/join":
                name = (body.get("name") or "").strip()[:16]
                avatar = (body.get("avatar") or "").strip()[:4]
                team_req = (body.get("team") or "").strip()
                if g["phase"] not in ("lobby", "icebreaker"):
                    return self.send({"error": "Game already started"}, 400)
                if not name or any(p["name"].lower() == name.lower() for p in g["players"].values()):
                    return self.send({"error": "Pick a unique nickname"}, 400)
                pid = "p%08x" % random.getrandbits(32)
                team_id = pick_balanced_team(g, team_req) if g.get("teams") else ""
                g["players"][pid] = {
                    "name": name, "avatar": avatar, "team": team_id,
                    "score": 0, "answers": {}, "powerups": {}, "wagers": {},
                }
                return self.send({"pid": pid, "team": team_id})
            if path == "/api/word":
                p = g["players"].get(body.get("pid"))
                word = (body.get("word") or "").strip()[:28]
                if not p or g["phase"] != "icebreaker" or not word:
                    return self.send({"error": "Cannot submit word"}, 400)
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
                    "id": g["rseq"], "emoji": "❄️", "name": "%s → %s" % (p["name"], target_name), "t": time.time(),
                })
                return self.send({"ok": True, "target": target_name})
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
                if kind == "shield" and not pu.get("shield_used"):
                    pu["shield_used"] = True
                    pu["shield_q"] = g["q"]
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
                _, _, q_elapsed, _ = cur_timer_state(g)
                record_answer(g, p, choice, q_elapsed)
                return self.send({"ok": True})
            if path == "/api/next":
                if body.get("host") != g["host"]:
                    return self.send({"error": "Forbidden"}, 403)
                tick(g)
                advance_game(g)
                return self.send({"ok": True})
        self.send({"error": "not found"}, 404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8000))
    print("Host:    http://localhost:%d/host\nPlayers: http://<your-ip>:%d/play" % (port, port))
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
