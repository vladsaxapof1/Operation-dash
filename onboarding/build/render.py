"""Кадры видео-онбординга 1920×1080: слайды (HTML) и снимки демо-стенда с подсветкой нужного места.

Нужен запущенный стенд (см. seed_demo.py) и Playwright с Google Chrome:
    python3 onboarding/build/render.py [id сцены …]      — без id: все кадры
"""

from __future__ import annotations

import base64
import html
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from storyboard import SCENES  # noqa: E402

BASE = "http://127.0.0.1:8790"
FRAMES = HERE / "frames"
APP_DIR = HERE.parent.parent
VIEW = {"width": 1600, "height": 900}  # ×1.2 = 1920×1080
SCALE = 1.2
# в кадр не должны попасть пути и почта того, кто собирает видео
REPLACE = [(str(HERE / ".demo-home"), "~/PULSE"), (str(APP_DIR), "~/PULSE"), (str(Path.home()), "~")]
EMAIL_RE = r"[\w.+-]+@(?!example\.ru|company\.ru)[\w-]+\.[\w.]+"

SANITIZE = """([reps, emailRe]) => {
  const re = new RegExp(emailRe, "g");
  const fix = (t) => { for (const [a, b] of reps) t = t.split(a).join(b); return t.replace(re, "anna.smirnova@company.ru"); };
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n; while ((n = walk.nextNode())) { const t = fix(n.nodeValue); if (t !== n.nodeValue) n.nodeValue = t; }
  document.querySelectorAll("input, textarea").forEach((i) => { i.value = fix(i.value); i.placeholder = fix(i.placeholder || ""); });
}"""

SPOT = """([x, y, w, h]) => {
  const o = document.createElement("div");
  o.style.cssText = `position:fixed;left:${x - 10}px;top:${y - 10}px;width:${w + 20}px;height:${h + 20}px;z-index:99999;
    pointer-events:none;border:3px solid #3b5bdb;border-radius:16px;
    box-shadow:0 0 0 9999px rgba(17,24,39,.42),0 0 0 8px rgba(59,91,219,.22)`;
  document.body.appendChild(o);
}"""

KAITEN_SPACES = {"ok": True, "spaces": [{"id": 1, "title": "Операционка"}, {"id": 2, "title": "Продажи"},
                                         {"id": 3, "title": "HR и найм"}, {"id": 4, "title": "Маркетинг"}]}


# --- снимки стенда ---------------------------------------------------------------------------

def meeting_url(page) -> str:
    page.goto(BASE + "/meetings")
    return next(a.get_attribute("href") for a in page.query_selector_all("a[href^='/meetings/']")
                if "planerka" in (a.get_attribute("href") or ""))


def shot(ctx, spec: dict, out: Path, meeting: str) -> None:
    ctx.clear_cookies()
    cookies = [] if spec.get("toast") else [{"name": "voice_tip", "value": "me", "url": BASE}]
    if spec.get("folded"):
        cookies.append({"name": "kb_folded", "value": spec["folded"], "url": BASE})
    ctx.add_cookies(cookies)
    page = ctx.new_page()
    if spec.get("kaiten_spaces"):
        page.route("**/api/kaiten/spaces", lambda route: route.fulfill(json=KAITEN_SPACES))
    page.goto(BASE + spec["url"].replace("{meeting}", meeting))
    page.wait_for_timeout(500)
    for action in spec.get("do", []):
        kind, sel = action[0], action[1]
        loc = page.locator(sel).first
        if kind == "click":
            loc.click()
        elif kind == "hover":
            loc.scroll_into_view_if_needed()
            loc.hover()
        elif kind == "fill":
            loc.fill(action[2])
        elif kind == "check":
            loc.check()
        page.wait_for_timeout(900)
    page.evaluate(SANITIZE, [REPLACE, EMAIL_RE])
    spots = spec.get("spot") or []
    spots = [spots] if isinstance(spots, str) else spots
    target = spec.get("scroll") or (spots[0] if spots else None)
    if target:
        def doc_box(sel):
            b = page.locator(sel).first.bounding_box()
            y = page.evaluate("window.scrollY")
            return b["x"], b["y"] + y, b["width"], b["height"]

        boxes = [doc_box(s) for s in spots] if spots and not spec.get("scroll") else [doc_box(target)]
        top = min(b[1] for b in boxes)
        bottom = max(b[1] + b[3] for b in boxes)
        h = VIEW["height"]
        scroll_y = top - 70 if bottom - top > h - 140 or spec.get("scroll") else (top + bottom) / 2 - h / 2
        page.evaluate("y => window.scrollTo(0, Math.max(0, y))", scroll_y)
        page.wait_for_timeout(200)
    if spots:
        boxes = [page.locator(s).first.bounding_box() for s in spots]
        x0 = min(b["x"] for b in boxes)
        y0 = min(b["y"] for b in boxes)
        x1 = max(b["x"] + b["width"] for b in boxes)
        y1 = max(b["y"] + b["height"] for b in boxes)
        page.evaluate(SPOT, [x0, max(y0, 6), x1 - x0, min(y1, VIEW["height"] - 6) - max(y0, 6)])
    page.screenshot(path=str(out))
    page.close()


# --- слайды -----------------------------------------------------------------------------------

CSS = """
:root { --accent:#3b5bdb; --accent-soft:#edf1ff; --text:#1c2230; --muted:#687284; --line:#e3e6ec; --bg:#f4f5f7; }
* { box-sizing:border-box; margin:0; padding:0; }
body { width:1600px; height:900px; overflow:hidden; position:relative; background:var(--bg); color:var(--text);
  font-family:-apple-system, "SF Pro Display", system-ui, "Helvetica Neue", Arial, sans-serif; -webkit-font-smoothing:antialiased; }
body.dark { background:radial-gradient(1100px 700px at 78% 18%, #2a3a78 0%, #131a33 55%, #0d1224 100%); color:#fff; }
.brand { position:absolute; top:40px; left:60px; display:flex; align-items:center; gap:12px; font-weight:800; font-size:22px; letter-spacing:.06em; }
.mark { width:38px; height:38px; border-radius:11px; background:var(--accent); color:#fff; display:grid; place-items:center; font-size:21px; font-weight:800; letter-spacing:0; }
.label { position:absolute; top:44px; right:60px; font-size:16px; font-weight:650; color:var(--accent); background:var(--accent-soft); padding:6px 16px; border-radius:999px; }
h1 { font-size:50px; font-weight:760; letter-spacing:-.015em; line-height:1.15; }
.wrap { position:absolute; left:60px; right:60px; top:118px; bottom:56px; display:flex; gap:44px; }
.col { flex:1; display:flex; flex-direction:column; gap:18px; min-width:0; }
.body { flex:1; display:flex; flex-direction:column; justify-content:center; gap:22px; padding-bottom:30px; }
.steps { display:flex; flex-direction:column; gap:16px; }
.step { display:flex; gap:18px; background:#fff; border:1px solid var(--line); border-radius:16px; padding:20px 26px; align-items:flex-start; box-shadow:0 2px 10px rgba(20,30,50,.05); }
.num { flex:none; width:38px; height:38px; border-radius:50%; background:var(--accent); color:#fff; display:grid; place-items:center; font-weight:750; font-size:19px; }
.num.dot { background:var(--accent-soft); color:var(--accent); }
.step b { display:block; font-size:28px; font-weight:680; line-height:1.25; }
.step span { display:block; font-size:21px; color:var(--muted); line-height:1.4; margin-top:3px; }
.note { font-size:21px; color:var(--muted); display:flex; gap:10px; align-items:center; }
.note::before { content:"i"; width:26px; height:26px; flex:none; border-radius:50%; background:var(--accent-soft); color:var(--accent); display:grid; place-items:center; font-weight:800; font-family:Georgia,serif; }
.side { flex:0 0 640px; display:flex; flex-direction:column; justify-content:center; }
.term { background:#141a26; color:#d7dde8; border-radius:16px; padding:22px 26px; font:17px/1.6 "SF Mono", Menlo, monospace; box-shadow:0 18px 50px rgba(15,20,35,.28); }
.term .bar { display:flex; gap:8px; margin-bottom:14px; } .term .bar i { width:12px; height:12px; border-radius:50%; background:#ff5f57; display:block; } .term .bar i:nth-child(2){background:#febc2e} .term .bar i:nth-child(3){background:#28c840}
.term .ok { color:#57d38c; } .term .step-h { color:#7b9cff; font-weight:700; }
.path { background:#fff; border:1px solid var(--line); border-radius:16px; padding:26px 30px; font:22px/1.7 "SF Mono", Menlo, monospace; box-shadow:0 10px 30px rgba(15,20,35,.08); }
.chat { background:#e6ebf2; border-radius:18px; overflow:hidden; box-shadow:0 18px 50px rgba(15,20,35,.18); }
.chat .top { background:#fff; padding:16px 22px; font-weight:700; font-size:20px; display:flex; gap:12px; align-items:center; border-bottom:1px solid var(--line); }
.chat .ava { width:36px; height:36px; border-radius:50%; background:var(--accent); color:#fff; display:grid; place-items:center; font-size:17px; }
.chat .msgs { padding:18px; display:flex; flex-direction:column; gap:10px; }
.msg { max-width:82%; padding:10px 15px; border-radius:16px; font-size:18px; line-height:1.4; white-space:pre-line; background:#fff; align-self:flex-start; border-bottom-left-radius:5px; }
.msg.me { background:#dcf5c8; align-self:flex-end; border-bottom-left-radius:16px; border-bottom-right-radius:5px; }
.browser { background:#fff; border-radius:14px; overflow:hidden; border:1px solid var(--line); box-shadow:0 18px 50px rgba(15,20,35,.2); }
.browser .bar { height:40px; background:#eef0f4; display:flex; align-items:center; gap:8px; padding:0 14px; }
.browser .bar i { width:11px; height:11px; border-radius:50%; background:#ff5f57; display:block; } .browser .bar i:nth-child(2){background:#febc2e} .browser .bar i:nth-child(3){background:#28c840}
.browser .url { margin-left:12px; flex:1; background:#fff; border-radius:7px; padding:4px 12px; font-size:15px; color:var(--muted); }
.browser img { width:100%; display:block; }
.cover { position:absolute; inset:0; display:flex; flex-direction:column; justify-content:center; padding:0 130px; gap:26px; }
.cover .logo { display:flex; align-items:center; gap:26px; }
.cover .logo .mark { width:110px; height:110px; border-radius:30px; font-size:64px; box-shadow:0 18px 60px rgba(59,91,219,.55); }
.cover h1 { font-size:120px; font-weight:820; letter-spacing:.04em; }
.cover .sub { font-size:44px; font-weight:600; color:#c9d3ff; }
.cover .note2 { font-size:26px; color:#9aa6cc; margin-top:20px; }
.chap { position:absolute; inset:0; display:flex; align-items:center; gap:90px; padding:0 130px; }
.chap .big { font-size:260px; font-weight:820; color:#7b93ff; line-height:1; }
.chap h1 { font-size:76px; font-weight:780; margin-bottom:34px; }
.chap .items { display:flex; flex-wrap:wrap; gap:12px; max-width:880px; }
.chap .items span { font-size:24px; padding:10px 20px; border-radius:999px; background:rgba(255,255,255,.08); border:1px solid rgba(255,255,255,.16); }
.chap .note2 { margin-top:34px; font-size:23px; color:#9aa6cc; }
table { width:100%; border-collapse:separate; border-spacing:0 14px; }
td { background:#fff; padding:24px 28px; font-size:26px; line-height:1.4; border-top:1px solid var(--line); border-bottom:1px solid var(--line); }
td:first-child { width:300px; font-weight:700; color:var(--accent); border-left:1px solid var(--line); border-radius:14px 0 0 14px; }
td:last-child { border-right:1px solid var(--line); border-radius:0 14px 14px 0; }
.flow { display:flex; align-items:stretch; gap:22px; }
.flow .box { flex:1; background:#fff; border:1px solid var(--line); border-radius:18px; padding:26px 28px; box-shadow:0 2px 10px rgba(20,30,50,.05); }
.flow .box.mid { border:2px solid var(--accent); }
.flow .box h3 { font-size:30px; margin-bottom:16px; color:var(--accent); }
.flow .box li { font-size:23px; line-height:1.5; list-style:none; padding-left:22px; position:relative; margin-bottom:6px; }
.flow .box li::before { content:""; position:absolute; left:2px; top:12px; width:8px; height:8px; border-radius:50%; background:var(--accent); }
.flow .arrow { align-self:center; font-size:46px; color:var(--accent); }
.badges { display:flex; gap:16px; }
.badges span { flex:1; text-align:center; font-size:22px; font-weight:650; padding:16px; border-radius:14px; background:var(--accent-soft); color:var(--accent); }
.tl { display:flex; flex-direction:column; gap:0; }
.tl .row { display:flex; gap:28px; align-items:flex-start; position:relative; padding-bottom:22px; }
.tl .t { flex:0 0 220px; text-align:right; font-size:27px; font-weight:750; color:var(--accent); padding-top:6px; }
.tl .dot { flex:none; width:22px; height:22px; margin-top:12px; border-radius:50%; background:var(--accent); box-shadow:0 0 0 6px var(--accent-soft); position:relative; z-index:1; }
.tl .row:not(:last-child)::after { content:""; position:absolute; left:258px; top:34px; bottom:0; width:2px; background:var(--line); }
.tl .d { flex:1; font-size:27px; background:#fff; border:1px solid var(--line); border-radius:14px; padding:14px 20px; }
"""

E = html.escape


def brand(dark: bool = False) -> str:
    return '<div class="brand"><span class="mark">P</span>PULSE</div>'


def side_html(side: dict) -> str:
    kind = side["type"]
    if kind == "terminal":
        rows = []
        for line in side["lines"]:
            cls = "step-h" if line.startswith("==>") else "ok" if "✓" in line else ""
            rows.append(f'<div class="{cls}">{E(line) or "&nbsp;"}</div>')
        return f'<div class="term"><div class="bar"><i></i><i></i><i></i></div>{"".join(rows)}</div>'
    if kind == "path":
        return f'<div class="path">{"<br>".join(E(x).replace(" ", "&nbsp;") for x in side["lines"])}</div>'
    if kind == "chat":
        msgs = "".join(f'<div class="msg{" me" if who == "me" else ""}">{E(t)}</div>' for who, t in side["messages"])
        return (f'<div class="chat"><div class="top"><span class="ava">{E(side["title"][0])}</span>{E(side["title"])}</div>'
                f'<div class="msgs">{msgs}</div></div>')
    if kind == "browser":
        data = base64.b64encode((FRAMES / f'{side["image"]}.png').read_bytes()).decode()
        return (f'<div class="browser"><div class="bar"><i></i><i></i><i></i><span class="url">{E(side["url"])}</span></div>'
                f'<img src="data:image/png;base64,{data}"></div>')
    raise ValueError(kind)


def slide_html(s: dict) -> str:
    kind = s["kind"]
    if kind == "cover":
        body = (f'<div class="cover"><div class="logo"><span class="mark">P</span><h1>{E(s["title"])}</h1></div>'
                f'<div class="sub">{E(s["subtitle"])}</div><div class="note2">{E(s.get("note", ""))}</div></div>')
        return f'<body class="dark">{body}</body>'
    if kind == "chapter":
        items = "".join(f"<span>{E(i)}</span>" for i in s["items"])
        note = f'<div class="note2">{E(s["note"])}</div>' if s.get("note") else ""
        big = f'<div class="big">{E(s["num"])}</div>' if s.get("num") else ""
        return f'<body class="dark">{brand(True)}<div class="chap">{big}<div><h1>{E(s["title"])}</h1><div class="items">{items}</div>{note}</div></div></body>'
    label = f'<div class="label">{E(s["label"])}</div>' if s.get("label") else ""
    note = f'<div class="note">{E(s["note"])}</div>' if s.get("note") else ""
    if kind == "steps":
        steps, n = [], 0
        for head, text in s["steps"]:
            n += bool(head)
            num = f'<span class="num">{n}</span>' if head else '<span class="num dot">›</span>'
            steps.append(f'<div class="step">{num}<div>{f"<b>{E(head)}</b>" if head else ""}<span>{E(text)}</span></div></div>')
        side = f'<div class="side">{side_html(s["side"])}</div>' if s.get("side") else ""
        return (f'<body>{brand()}{label}<div class="wrap"><div class="col"><h1>{E(s["title"])}</h1>'
                f'<div class="body"><div class="steps">{"".join(steps)}</div>{note}</div></div>{side}</div></body>')
    if kind == "table":
        rows = "".join(f"<tr><td>{E(a)}</td><td>{E(b)}</td></tr>" for a, b in s["rows"])
        return f'<body>{brand()}{label}<div class="wrap"><div class="col"><h1>{E(s["title"])}</h1><div class="body"><table>{rows}</table>{note}</div></div></div></body>'
    if kind == "flow":
        cols = []
        for i, (head, items) in enumerate(s["columns"]):
            if i:
                cols.append('<div class="arrow">→</div>' if s.get("badges") else "")
            lis = "".join(f"<li>{E(x)}</li>" for x in items)
            cols.append(f'<div class="box{" mid" if i == 1 and s.get("badges") else ""}"><h3>{E(head)}</h3><ul>{lis}</ul></div>')
        badges = "".join(f"<span>{E(b)}</span>" for b in s.get("badges", []))
        return (f'<body>{brand()}<div class="wrap"><div class="col"><h1>{E(s["title"])}</h1><div class="body"><div class="flow">{"".join(cols)}</div>'
                f'{f"<div class=badges>{badges}</div>" if badges else ""}</div></div></div></body>')
    if kind == "timeline":
        rows = "".join(f'<div class="row"><div class="t">{E(t)}</div><div class="dot"></div><div class="d">{E(d)}</div></div>'
                       for t, d in s["items"])
        return f'<body>{brand()}<div class="wrap"><div class="col"><h1>{E(s["title"])}</h1><div class="body"><div class="tl">{rows}</div></div></div></div></body>'
    raise ValueError(kind)


def main(only: set[str]) -> None:
    FRAMES.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        ctx = browser.new_context(viewport=VIEW, device_scale_factor=SCALE, color_scheme="light", locale="ru-RU")
        meeting = meeting_url(ctx.new_page())
        if not only or "mac4" in only:  # для слайда «Готово: PULSE открылся в браузере»
            shot(ctx, {"url": "/"}, FRAMES / "home-plain.png", meeting)
        for s in [s for s in SCENES if "shot" in s] + [s for s in SCENES if "slide" in s]:
            if only and s["id"] not in only:
                continue
            out = FRAMES / f'{s["id"]}.png'
            if "shot" in s:
                shot(ctx, s["shot"], out, meeting)
            else:
                page = ctx.new_page()
                page.set_content(f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><style>{CSS}</style></head>'
                                 f'{slide_html(s["slide"])}</html>')
                page.wait_for_timeout(150)
                page.screenshot(path=str(out))
                page.close()
            print("кадр:", out.name)
        browser.close()


if __name__ == "__main__":
    main(set(sys.argv[1:]))
