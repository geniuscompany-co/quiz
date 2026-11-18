# BrainRush — Project Guide & Agent Rules

BrainRush is a zero-dependency classroom quiz, interactive case study, async homework, and Canvas LMS teaching studio built with pure Python 3 and vanilla HTML/CSS/JS.

## Live Deployment & Git Workflow

- **GitHub Remote:** `https://github.com/geniuscompany-co/quiz.git` (`main` branch)
- **Live Host Studio:** `https://quiz-saxb.onrender.com/host`
- **Live Student Join (`/` redirects to `/play`):** `https://quiz-saxb.onrender.com/play`
- **Deploying Changes:**
  1. Verify syntax and tests locally (`python3 -m py_compile server.py kstore.py`).
  2. Stage and commit changes to `main`.
  3. Run `./deploy.sh` from the repository root (`git push` + triggers Render deploy via `.render_hook`).
- **Secrets Hygiene:** Never print, log, or commit `.render_hook` (git-ignored) or any Canvas Personal Access Tokens.

## Local Development

- **Start Local Server:** `python3 -u server.py 8080` (default port `8080`, or pass any port such as `python3 -u server.py 8000`).
- **Local Routes:**
  - `/host` — Teacher Studio (`host.html`)
  - `/` — Redirects (`302`) to `/play`
  - `/play` — Student mobile controller (`play.html`)
  - `/present` — Clean 2nd-screen projector audience view (`host.html` with `.is-audience`)
  - `/remote` — Paired mobile host remote (`remote.html`, labeled `📱 Remote`)
  - `/practice` — Solo student self-study (`practice.html`, accessed via `🖥️ Modes`)
  - `/spectator` — Live leaderboard & word cloud (`spectator.html`, accessed via `🖥️ Modes`)
  - `/hw` — Self-paced homework (`homework.html`)

## Codebase Architecture

- `server.py`: `ThreadingHTTPServer` backend handling live Quiz & Case Study game state, scoring modes (`classic`, `accuracy`, `percent`, `survey`), game-show modifiers, optional live chat / Top-30 standings & head-to-head profiles / host play-along, QR SVG generation, `/host` lock & password/account endpoints (`/api/config`, `/api/host_unlock`, `/api/host_password`), per-course deck endpoints (`/api/deck_courses`), and Canvas proxy routes (`/api/canvas/*`).
- `kstore.py`: Storage & auth layer supporting local `.data/` JSON persistence and optional Upstash Redis REST cloud storage (`UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN`), PBKDF2-SHA256 teacher accounts & in-app host passwords, custom quizzes (`uq:<owner>`) & course subset mappings (`deckcourses:<owner>`), async homework, longitudinal student progress, and Canvas LMS REST API + Gradebook CSV parsing (with SSRF protection).
- `host.html`: 3-column Teacher Studio:
  - **Left Sidebar (`#studioRail`):** Branding, `#railActiveCourseBox` (active course card above `🎮 Quizzes`), clean Main `🎮 Quizzes` & `📖 Case Studies` tabs (`openMainLibrary` always shows all items including unassigned ones), and `CLASSROOM` (`🎓 Courses` with collapsible `#railCoursesToggle` / `#railCoursesSub` Default Courses submenu, `➕ Deck Builder`, `📚 Homework`, `👥 Progress`, `📈 History`, `📱 Remote`, `📽️ Dual Screen`, `🖥️ Modes`, `📖 Feature Guide`, and `#wsAcctBtn` `🔒 Host Security & Account`).
  - **Center Workspace:** Main Quiz / Case Study Library, course subset banner (`#courseLibBar`), and Visual + Text Deck/Case Builder (`#quizModal`).
  - **Right Sidebar (`#studioDrawer` & `#liveHostWing`):** Scrollable `⚙️ Session Inspector` (`max-height: 100vh`, `min-height: 0`, `flex-shrink: 0` on `details.acc-box` / `details.sub-acc`) and collapsible in-game Private Host Wing.
- `play.html`: Student game client supporting MC, T/F, Multi-Select, Tap-to-Pair Matching, Up-to-8-Item Ordering, Short Text, Estimation Slider, Pin-on-Image, Zoom-In Image, Likert Scale, Brainstorm Sticky Notes, and Case Study Narrative/Roles/Exhibits/Polls/Ranking/Open Responses.

## Strict UI, UX & Security Rules

1. **Zero Static HTML Pre-Checks:** Never add `checked` on `<input>` or `selected` on `<option>` in static HTML across `host.html`, `play.html`, `remote.html`, `practice.html`, or `homework.html`. Initialize defaults in JavaScript so optional host features remain opt-in.
2. **Course Subsets vs. Main Libraries:** Main `🎮 Quizzes` and `📖 Case Studies` must always show all quizzes/cases (including items not assigned to any course). Default Courses appear once in the collapsible submenu under `🎓 Courses` (`#railCoursesSub`) and act as subsets of Quizzes and Case Studies.
3. **Audience Screen Privacy:** Never show teacher notes, answer keys, `#liveHostWing`, `#hostLockGate`, or `#canvasModal` on `/present` (`.is-audience` / `.aud-hide`).
4. **Host Access Control:** Keep `/` redirecting to `/play` and keep `/host` gated behind `#hostLockGate` whenever `HOST_PASSWORD` (env), an in-app host password, or a Teacher Account is active.
