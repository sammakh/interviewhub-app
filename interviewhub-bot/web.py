"""Interview Hub: Mini App uchun API va admin dashboard (aiohttp).

Bot bilan bitta jarayonda ishlaydi (bot.py -> start_web).
  /api/*    Mini App so'rovlari. Har biri Telegram initData imzosi bilan tekshiriladi.
  /admin    Admin dashboard (ADMIN_PASSWORD bilan kiriladi).
  /health   Railway tekshiruvi uchun.
"""
import asyncio
import csv
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

from aiohttp import web

log = logging.getLogger("interviewhub.web")

# Ilovadagi (index.html) PRICES bilan bir xil bo'lishi kerak. Summa serverda hisoblanadi.
PRICES = {
    "accounting": 690000, "update": 620000, "safety": 560000, "dispatch": 510000,
    "fleet": 460000, "eld": 420000, "sales": 380000, "recruiting": 330000,
}
PN = {
    "accounting": "Accounting", "update": "Updater", "safety": "Safety", "dispatch": "Dispatch (box truck)",
    "fleet": "Fleet", "eld": "ELD", "sales": "Sales", "recruiting": "Recruiting",
}
MAX_PICKS = 3
# Click/Payme merchant ulanguncha to'lov o'chiq: tasdiqlash tugmasi yo'q, ilova "to'langan" holatini olmaydi.
PAYMENTS_ON = os.getenv("PAYMENTS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
PASS_MIN = 7
HB_GAP = 90           # soniya: shundan uzoq tanaffus vaqtga qo'shilmaydi
SESSION_GAP = 30 * 60  # soniya: shundan keyin yangi sessiya hisoblanadi
INIT_MAX_AGE = 7 * 86400
TZ = timezone(timedelta(hours=5))  # Toshkent

PAY_LABEL = {
    "none": ("—", ""), "pending": ("Kutilmoqda", "am"), "paid": ("To'langan", "ok"), "rejected": ("Rad etilgan", "no"),
}
TEST_LABEL = {1: ("O'tdi", "ok"), 0: ("O'tmadi", "no")}
SCREEN_LABEL = {
    None: ("Ro'yxatdan o'tmagan", ""), "registered": ("Ko'rib chiqilmoqda", "am"),
    "passed": ("O'tdi", "ok"), "rejected": ("O'tmadi", "no"),
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_dt(s):
    if not s:
        return "—"
    try:
        return datetime.fromisoformat(s).astimezone(TZ).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return s


def fmt_dur(sec):
    sec = int(sec or 0)
    if sec < 60:
        return f"{sec} s" if sec else "—"
    h, m = divmod(sec // 60, 60)
    return f"{h} soat {m} daq" if h else f"{m} daq"


def money(n):
    return f"{int(n or 0):,}".replace(",", " ") + " so'm"


def esc(x):
    return html.escape("" if x is None else str(x))


# ---------------------------------------------------------------- baza
def init_tables(db):
    with closing(db()) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                tg_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT, lang TEXT,
                picks TEXT DEFAULT '[]', picks_at TEXT, amount INTEGER DEFAULT 0, accepted_at TEXT,
                pay_status TEXT DEFAULT 'none', pay_provider TEXT, pay_requested_at TEXT,
                paid_at TEXT, pay_by TEXT,
                total_sec INTEGER DEFAULT 0, sessions INTEGER DEFAULT 0,
                first_open TEXT, last_seen TEXT, last_hb REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS lesson_progress (
                tg_id INTEGER, position TEXT, done INTEGER, total INTEGER, updated_at TEXT,
                PRIMARY KEY (tg_id, position));
            CREATE TABLE IF NOT EXISTS test_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, position TEXT,
                score INTEGER, passed INTEGER, at TEXT);
            CREATE INDEX IF NOT EXISTS ix_attempts_user ON test_attempts (tg_id);
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY, percent INTEGER, max_uses INTEGER, expires_on TEXT,
                active INTEGER DEFAULT 1, note TEXT, created_at TEXT);
            """
        )
        cols = {r[1] for r in con.execute("PRAGMA table_info(app_state)")}
        for col, typ in (("promo_code", "TEXT"), ("promo_pct", "INTEGER DEFAULT 0"), ("base_amount", "INTEGER DEFAULT 0")):
            if col not in cols:
                con.execute(f"ALTER TABLE app_state ADD COLUMN {col} {typ}")
        con.commit()


def delete_app_data(db, tg_id):
    with closing(db()) as con:
        for t in ("app_state", "lesson_progress", "test_attempts"):
            con.execute(f"DELETE FROM {t} WHERE tg_id=?", (tg_id,))
        con.commit()


def ensure_state(con, user):
    con.execute(
        "INSERT OR IGNORE INTO app_state (tg_id, first_name, username, first_open) VALUES (?,?,?,?)",
        (user["id"], user.get("first_name"), user.get("username"), now_iso()),
    )
    con.execute(
        "UPDATE app_state SET first_name=?, username=? WHERE tg_id=?",
        (user.get("first_name"), user.get("username"), user["id"]),
    )


def clean_picks(picks):
    out = []
    for k in picks if isinstance(picks, list) else []:
        if k in PRICES and k not in out:
            out.append(k)
    return out[:MAX_PICKS]


# ---------------------------------------------------------------- Telegram initData
def verify_init_data(init_data, token):
    """Telegram WebApp imzosini tekshiradi. To'g'ri bo'lsa user dict, aks holda None."""
    if not init_data or not token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    got = pairs.pop("hash", None)
    if not got:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > INIT_MAX_AGE:
            return None
        user = json.loads(pairs.get("user", "{}"))
    except (ValueError, TypeError):
        return None
    return user if isinstance(user.get("id"), int) else None


# ---------------------------------------------------------------- Mini App API
def json_resp(data, status=200):
    return web.json_response(data, status=status)


async def read_api(request):
    """So'rovni o'qiydi va foydalanuvchini tekshiradi: (user, body) yoki web.HTTPException."""
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="bad json")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="bad json")
    request["body"] = body
    user = verify_init_data(body.get("initData", ""), request.app["token"])
    if not user:
        raise web.HTTPUnauthorized(text="bad initData")
    return user, body


async def api_hb(request):
    user, body = await read_api(request)
    db, now = request.app["db"], time.time()
    with closing(db()) as con:
        ensure_state(con, user)
        r = con.execute("SELECT last_hb FROM app_state WHERE tg_id=?", (user["id"],)).fetchone()
        gap = now - (r["last_hb"] or 0)
        add = int(gap) if 0 < gap <= HB_GAP else 0
        new_session = gap > SESSION_GAP  # birinchi kirish yoki uzoq tanaffusdan keyin
        con.execute(
            "UPDATE app_state SET total_sec=total_sec+?, sessions=sessions+?, last_hb=?, last_seen=? WHERE tg_id=?",
            (add, 1 if new_session else 0, now, now_iso(), user["id"]),
        )
        con.commit()
    return json_resp({"ok": True})


def store_picks(con, uid, picks):
    r = con.execute("SELECT pay_status, promo_pct FROM app_state WHERE tg_id=?", (uid,)).fetchone()
    if r and r["pay_status"] == "paid":
        return False  # to'lovdan keyin tanlov o'zgarmaydi
    base = sum(PRICES[k] for k in picks)
    con.execute(
        "UPDATE app_state SET picks=?, base_amount=?, amount=?, picks_at=? WHERE tg_id=?",
        (json.dumps(picks), base, apply_pct(base, (r["promo_pct"] if r else 0) or 0), now_iso(), uid),
    )
    return True


def apply_pct(base, pct):
    """Chegirmali summa. Ilovadagi net() bilan bir xil: base - round(base*pct/100)."""
    return base - int(round(base * pct / 100))


PROMO_RE = re.compile(r"^[A-Z0-9_-]{3,20}$")
_promo_tries = {}


def check_code(con, code, uid=None):
    """(row, xato) qaytaradi. xato: None | invalid | expired | limit."""
    row = con.execute("SELECT * FROM promo_codes WHERE code=?", (code,)).fetchone() if PROMO_RE.match(code) else None
    if not row or not row["active"]:
        return None, "invalid"
    if row["expires_on"] and row["expires_on"] < today_tz():
        return None, "expired"
    if row["max_uses"]:
        used = con.execute("SELECT COUNT(*) FROM app_state WHERE promo_code=? AND tg_id IS NOT ?", (code, uid)).fetchone()[0]
        if used >= row["max_uses"]:
            return None, "limit"
    return row, None


async def promo_preview(request):
    """Brauzerda sinov uchun: kodni tekshiradi, chegirmani hisoblaydi, hech narsa yozmaydi."""
    body = request["body"]
    ip = request.headers.get("X-Forwarded-For", request.remote or "").split(",")[0].strip()
    now = time.time()
    tries = [t for t in _promo_tries.get(ip, []) if now - t < 600]
    code = str(body.get("code") or "").strip().upper()[:20]
    base = sum(PRICES[k] for k in clean_picks(body.get("picks")))
    if not code:
        return json_resp({"ok": True, "percent": 0, "base": base, "amount": base, "preview": True})
    if len(tries) >= 10:
        return json_resp({"ok": False, "error": "tries"})
    with closing(request.app["db"]()) as con:
        row, err = check_code(con, code)
    if err:
        tries.append(now)
        _promo_tries[ip] = tries
        return json_resp({"ok": False, "error": err})
    return json_resp({"ok": True, "code": code, "percent": row["percent"], "base": base,
                      "amount": apply_pct(base, row["percent"]), "preview": True})


def today_tz():
    return datetime.now(TZ).date().isoformat()


async def api_promo(request):
    """Promokodni tekshiradi va foydalanuvchiga biriktiradi. Bo'sh kod = olib tashlash.
    Telegram tashqarisida (initData yo'q) faqat tekshiradi: hech narsa saqlanmaydi, limit sarflanmaydi."""
    try:
        user, body = await read_api(request)
    except web.HTTPUnauthorized:
        return await promo_preview(request)
    uid, code = user["id"], str(body.get("code") or "").strip().upper()[:20]
    now = time.time()
    tries = [t for t in _promo_tries.get(uid, []) if now - t < 600]
    with closing(request.app["db"]()) as con:
        ensure_state(con, user)
        st = con.execute("SELECT pay_status, picks FROM app_state WHERE tg_id=?", (uid,)).fetchone()
        if st["pay_status"] == "paid":
            return json_resp({"ok": False, "error": "locked"})
        picks = clean_picks(body.get("picks")) or clean_picks(json.loads(st["picks"] or "[]"))
        base = sum(PRICES[k] for k in picks)
        if not code:
            con.execute("UPDATE app_state SET promo_code=NULL, promo_pct=0, base_amount=?, amount=? WHERE tg_id=?",
                        (base, base, uid))
            con.commit()
            return json_resp({"ok": True, "percent": 0, "base": base, "amount": base})
        if len(tries) >= 10:
            return json_resp({"ok": False, "error": "tries"})
        row, err = check_code(con, code, uid)
        if err:
            tries.append(now)
            _promo_tries[uid] = tries
            return json_resp({"ok": False, "error": err})
        pct = row["percent"]
        con.execute("UPDATE app_state SET promo_code=?, promo_pct=?, picks=?, base_amount=?, amount=? WHERE tg_id=?",
                    (code, pct, json.dumps(picks), base, apply_pct(base, pct), uid))
        con.commit()
    return json_resp({"ok": True, "code": code, "percent": pct, "base": base, "amount": apply_pct(base, pct)})


async def api_picks(request):
    user, body = await read_api(request)
    picks = clean_picks(body.get("picks"))
    with closing(request.app["db"]()) as con:
        ensure_state(con, user)
        store_picks(con, user["id"], picks)
        con.commit()
    return json_resp({"ok": True})


async def api_sync(request):
    """Ilova holatining to'liq nusxasi: pozitsiyalar, darslar progressi, test natijalari."""
    user, body = await read_api(request)
    uid = user["id"]
    done = body.get("done") if isinstance(body.get("done"), dict) else {}
    totals = body.get("totals") if isinstance(body.get("totals"), dict) else {}
    tests = body.get("tests") if isinstance(body.get("tests"), dict) else {}
    with closing(request.app["db"]()) as con:
        ensure_state(con, user)
        lang = body.get("lang")
        if lang in ("uz", "ru", "en"):
            con.execute("UPDATE app_state SET lang=? WHERE tg_id=?", (lang, uid))
        if "picks" in body:
            store_picks(con, uid, clean_picks(body.get("picks")))
        for k, total in totals.items():
            if k not in PRICES or not isinstance(total, int) or not 0 < total <= 200:
                continue
            idx = done.get(k) if isinstance(done.get(k), list) else []
            n = len({i for i in idx if isinstance(i, int) and 0 <= i < total})
            con.execute(
                "INSERT INTO lesson_progress (tg_id, position, done, total, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(tg_id, position) DO UPDATE SET done=MAX(done, excluded.done), total=excluded.total, "
                "updated_at=CASE WHEN excluded.done>done THEN excluded.updated_at ELSE updated_at END",
                (uid, k, n, total, now_iso()),
            )
        # server ishga tushishidan oldingi test natijalari: urinishlar bo'lmasa, oxirgi natijani yozib qo'yamiz
        for k, tr in tests.items():
            if k not in PRICES or not isinstance(tr, dict) or not isinstance(tr.get("score"), int):
                continue
            has = con.execute("SELECT 1 FROM test_attempts WHERE tg_id=? AND position=?", (uid, k)).fetchone()
            if not has:
                sc = max(0, min(10, tr["score"]))
                con.execute(
                    "INSERT INTO test_attempts (tg_id, position, score, passed, at) VALUES (?,?,?,?,?)",
                    (uid, k, sc, int(sc >= PASS_MIN), now_iso()),
                )
        con.commit()
    return json_resp({"ok": True})


async def api_result(request):
    user, body = await read_api(request)
    k, sc = body.get("position"), body.get("score")
    if k not in PRICES or not isinstance(sc, int) or not 0 <= sc <= 10:
        raise web.HTTPBadRequest(text="bad result")
    with closing(request.app["db"]()) as con:
        ensure_state(con, user)
        con.execute(
            "INSERT INTO test_attempts (tg_id, position, score, passed, at) VALUES (?,?,?,?,?)",
            (user["id"], k, sc, int(sc >= PASS_MIN), now_iso()),
        )
        con.commit()
    return json_resp({"ok": True})


async def api_accept(request):
    user, body = await read_api(request)
    picks = clean_picks(body.get("picks"))
    with closing(request.app["db"]()) as con:
        ensure_state(con, user)
        if picks:
            store_picks(con, user["id"], picks)
        con.execute("UPDATE app_state SET accepted_at=? WHERE tg_id=?", (now_iso(), user["id"]))
        con.commit()
    return json_resp({"ok": True})


async def api_pay(request):
    """To'lov so'rovi. Click/Payme merchant ulanmaguncha havola qaytarmaydi: holat 'kutilmoqda' bo'ladi,
    admin dashboardda tasdiqlaydi."""
    user, body = await read_api(request)
    provider = str(body.get("provider") or "")[:20]
    with closing(request.app["db"]()) as con:
        ensure_state(con, user)
        picks = clean_picks(body.get("picks"))
        if picks:
            store_picks(con, user["id"], picks)
        status_sql = ("pay_status=CASE WHEN pay_status IN ('none','rejected') THEN 'pending' ELSE pay_status END"
                      if PAYMENTS_ON else "pay_status=pay_status")
        con.execute(
            f"UPDATE app_state SET pay_provider=?, pay_requested_at=?, {status_sql} WHERE tg_id=?",
            (provider, now_iso(), user["id"]),
        )
        con.commit()
    return json_resp({"ok": True})


async def api_status(request):
    user, _ = await read_api(request)
    if not PAYMENTS_ON:
        return json_resp({"paid": False})
    with closing(request.app["db"]()) as con:
        r = con.execute("SELECT pay_status FROM app_state WHERE tg_id=?", (user["id"],)).fetchone()
    return json_resp({"paid": bool(r and r["pay_status"] == "paid")})


async def api_quote(request):
    await read_api(request)
    return json_resp({})


@web.middleware
async def cors(request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    origin = request.headers.get("Origin", "")
    allowed = request.app["origins"]
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as ex:
            resp = web.json_response({"error": ex.text}, status=ex.status)
    if origin in allowed:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


# ---------------------------------------------------------------- admin: kirish
COOKIE = "ih_admin"
SESSION_TTL = 7 * 86400


def _sign(app, msg):
    return hmac.new(app["secret"], msg.encode(), hashlib.sha256).hexdigest()


def make_session(app):
    exp = str(int(time.time()) + SESSION_TTL)
    return f"{exp}.{_sign(app, 's' + exp)}"


def session_ok(request):
    raw = request.cookies.get(COOKIE, "")
    exp, _, sig = raw.partition(".")
    if not exp.isdigit() or int(exp) < time.time():
        return None
    return exp if hmac.compare_digest(sig, _sign(request.app, "s" + exp)) else None


def csrf_token(request):
    exp = session_ok(request) or ""
    return _sign(request.app, "csrf" + exp)


def require_admin(handler):
    async def wrapped(request):
        if not request.app["password"]:
            return page("Sozlanmagan", "<div class='card'>Railway'da <b>ADMIN_PASSWORD</b> o'zgaruvchisini qo'shing, "
                        "keyin dashboard ochiladi.</div>", status=503)
        if not session_ok(request):
            raise web.HTTPFound("/admin/login")
        if request.method == "POST":
            form = await request.post()
            if not hmac.compare_digest(str(form.get("csrf", "")), csrf_token(request)):
                raise web.HTTPForbidden(text="csrf")
            request["form"] = form
        return await handler(request)
    return wrapped


_login_fails = {}
MAX_FAILS = 10
LOCK_SEC = 300


async def login_get(request):
    return page("Kirish", LOGIN_HTML.format(err=""), nav=False)


async def login_post(request):
    ip = request.headers.get("X-Forwarded-For", request.remote or "").split(",")[0].strip()
    now = time.time()
    fails = [t for t in _login_fails.get(ip, []) if now - t < LOCK_SEC]
    if len(fails) >= MAX_FAILS:
        return page("Kirish", LOGIN_HTML.format(err="Ko'p urinish. 5 daqiqadan keyin qayta urinib ko'ring."), nav=False, status=429)
    form = await request.post()
    pw = request.app["password"]
    if pw and hmac.compare_digest(str(form.get("password", "")).strip().encode(), pw.encode()):
        _login_fails.pop(ip, None)
        resp = web.HTTPFound("/admin")
        resp.set_cookie(COOKIE, make_session(request.app), max_age=SESSION_TTL, httponly=True,
                        secure=request.secure or request.headers.get("X-Forwarded-Proto") == "https", samesite="Lax")
        raise resp
    fails.append(now)
    _login_fails[ip] = fails
    await asyncio.sleep(1)
    left = MAX_FAILS - len(fails)
    err = f"Parol noto'g'ri. Yana {left} ta urinish qoldi." if left else "Ko'p urinish. 5 daqiqadan keyin qayta urinib ko'ring."
    log.info("admin: noto'g'ri parol urinishi")
    return page("Kirish", LOGIN_HTML.format(err=err), nav=False, status=401)


async def logout(request):
    resp = web.HTTPFound("/admin/login")
    resp.del_cookie(COOKIE)
    raise resp


# ---------------------------------------------------------------- admin: ma'lumot yig'ish
def load_people(db):
    with closing(db()) as con:
        users = {r["tg_id"]: dict(r) for r in con.execute("SELECT * FROM users")}
        states = {r["tg_id"]: dict(r) for r in con.execute("SELECT * FROM app_state")}
        prog = {}
        for r in con.execute("SELECT * FROM lesson_progress"):
            prog.setdefault(r["tg_id"], {})[r["position"]] = (r["done"], r["total"])
        tests = {}
        for r in con.execute("SELECT * FROM test_attempts ORDER BY id"):
            tests.setdefault(r["tg_id"], []).append(dict(r))
    people = []
    for uid in set(users) | set(states):
        u, s = users.get(uid, {}), states.get(uid, {})
        picks = json.loads(s.get("picks") or "[]")
        p = prog.get(uid, {})
        keys = [k for k in picks if k in p] or list(p)
        done = sum(p[k][0] for k in keys)
        total = sum(p[k][1] for k in keys)
        best = {}
        for a in tests.get(uid, []):
            best[a["position"]] = max(best.get(a["position"], 0), a["score"])
        people.append(dict(
            uid=uid, u=u, s=s, picks=picks, prog=p, attempts=tests.get(uid, []), best=best,
            name=u.get("full_name") or s.get("first_name") or f"id {uid}",
            username=u.get("username") or s.get("username"),
            screen=u.get("status"), pay=s.get("pay_status") or "none",
            lesson_pct=round(done * 100 / total) if total else None,
            created=u.get("created_at") or s.get("first_open") or "",
        ))
    people.sort(key=lambda x: x["created"], reverse=True)
    return people


def tag(label_map, key):
    text, cls = label_map.get(key, (key or "—", ""))
    return f"<span class='tag {cls}'>{esc(text)}</span>"


# ---------------------------------------------------------------- admin: sahifalar
async def admin_home(request):
    people = load_people(request.app["db"])
    reg = sum(1 for p in people if p["screen"])
    passed = sum(1 for p in people if p["screen"] == "passed")
    chose = sum(1 for p in people if p["picks"])
    pending = sum(1 for p in people if p["pay"] == "pending")
    paid = [p for p in people if p["pay"] == "paid"]
    revenue = sum(p["s"].get("amount") or 0 for p in paid)
    kpis = [("Ro'yxatdan o'tgan", reg), ("Screeningdan o'tgan", passed), ("Pozitsiya tanlagan", chose)]
    if PAYMENTS_ON:
        kpis += [("To'lov kutilmoqda", pending), ("To'lagan", len(paid)), ("Tushum", money(revenue))]
    else:
        reached = sum(1 for p in people if p["s"].get("pay_requested_at"))
        kpis += [("To'lov bosqichiga yetgan", reached), ("To'lov", "Ulanmagan")]
    cards = "".join(f"<div class='kpi'><small>{a}</small><b>{b}</b></div>" for a, b in kpis)
    pay_opts = ("<option value='pay:pending'>To'lov kutilmoqda</option><option value='pay:paid'>To'lagan</option>"
                if PAYMENTS_ON else "")
    rows = []
    for p in people:
        tests = ", ".join(f"{PN.get(k, k)} {v}/10" for k, v in p["best"].items()) or "—"
        rows.append(
            f"<tr data-pay='{esc(p['pay'])}' data-screen='{esc(p['screen'] or 'none')}' onclick=\"location='/admin/u/{p['uid']}'\">"
            f"<td><b>{esc(p['name'])}</b><br><small>{esc('@' + p['username'] if p['username'] else p['uid'])}</small></td>"
            f"<td>{esc(p['u'].get('phone') or '—')}</td>"
            f"<td>{esc(p['u'].get('level') or '—')}</td>"
            f"<td>{tag(SCREEN_LABEL, p['screen'])}</td>"
            f"<td>{esc(', '.join(PN.get(k, k) for k in p['picks']) or '—')}</td>"
            f"<td class='num'>{money(p['s'].get('amount')) if p['picks'] else '—'}</td>"
            f"<td>{tag(PAY_LABEL, p['pay']) if PAYMENTS_ON or p['pay'] != 'none' else '—'}</td>"
            f"<td class='num'>{fmt_dur(p['s'].get('total_sec'))}</td>"
            f"<td class='num'>{'—' if p['lesson_pct'] is None else str(p['lesson_pct']) + '%'}</td>"
            f"<td>{esc(tests)}</td>"
            f"<td class='num'>{fmt_dt(p['created'])}</td></tr>"
        )
    body = f"""
<div class='kpis'>{cards}</div>
<div class='bar'>
  <input id='q' placeholder='Qidirish: ism, telefon, username' oninput='flt()'>
  <select id='f' onchange='flt()'>
    <option value=''>Hammasi</option>{pay_opts}<option value='screen:registered'>Screening kutilmoqda</option>
    <option value='screen:passed'>Screeningdan o'tgan</option><option value='screen:rejected'>O'tmagan</option>
  </select>
  <a class='btn ghost' href='/admin/export.csv'>CSV yuklab olish</a>
</div>
<div class='tbl'><table id='t'><thead><tr><th>Nomzod</th><th>Telefon</th><th>Ingliz</th><th>Screening</th>
<th>Pozitsiyalar</th><th>Summa</th><th>To'lov</th><th>Ilovada</th><th>Darslar</th><th>Testlar (eng yaxshi)</th><th>Sana</th></tr></thead>
<tbody>{''.join(rows) or "<tr><td colspan='11' class='empty'>Hali hech kim yo'q</td></tr>"}</tbody></table></div>
<script>
function flt(){{const q=document.getElementById('q').value.toLowerCase(),f=document.getElementById('f').value.split(':');
document.querySelectorAll('#t tbody tr[data-pay]').forEach(r=>{{const okq=!q||r.innerText.toLowerCase().includes(q);
const okf=!f[0]||r.dataset[f[0]]===f[1];r.style.display=okq&&okf?'':'none'}})}}
</script>"""
    return page("Nomzodlar", body)


def find_person(request):
    try:
        uid = int(request.match_info["uid"])
    except ValueError:
        raise web.HTTPNotFound()
    for p in load_people(request.app["db"]):
        if p["uid"] == uid:
            return p
    raise web.HTTPNotFound(text="Topilmadi")


async def admin_user(request):
    p = find_person(request)
    u, s, uid = p["u"], p["s"], p["uid"]
    csrf = esc(csrf_token(request))
    msg = {"paid": "To'lov tasdiqlandi, nomzodga xabar yuborildi.", "rejected": "To'lov rad etildi.",
           "reset": "To'lov holati tozalandi.", "passed": "Screening: o'tdi.", "fail": "Screening: o'tmadi.",
           "err": "Amalni bajarib bo'lmadi."}.get(request.query.get("m", ""), "")

    def form(action, value, label, cls="", confirm=""):
        onsubmit = f" onsubmit=\"return confirm('{esc(confirm)}')\"" if confirm else ""
        return (f"<form method='post' action='/admin/u/{uid}/{action}' style='display:inline'{onsubmit}>"
                f"<input type='hidden' name='csrf' value='{csrf}'><input type='hidden' name='v' value='{value}'>"
                f"<button class='btn {cls}'>{label}</button></form>")

    anketa = "".join(f"<tr><th>{a}</th><td>{esc(b)}</td></tr>" for a, b in [
        ("Telefon", u.get("phone")), ("Yosh", u.get("age")), ("Ta'lim / ish", u.get("edu")),
        ("Ingliz darajasi", u.get("level")), ("Tajriba", u.get("exp")), ("Tungi smena", u.get("shift")),
        ("Kutilayotgan oylik", f"${u['salary_usd']}" if u.get("salary_usd") else None),
        ("Reklama roziligi", "ha" if u.get("consent_marketing") else ("yo'q" if u else None)),
        ("Ro'yxatdan o'tgan", fmt_dt(u.get("created_at"))),
    ]) if u else "<tr><td class='empty'>Botda ro'yxatdan o'tmagan (faqat ilovani ochgan)</td></tr>"

    screen_btns = ""
    if p["screen"] == "registered":
        screen_btns = form("screen", "pass", "✅ O'tdi") + " " + form("screen", "fail", "❌ O'tmadi", "ghost")
    video = (f"<video controls preload='none' src='/admin/u/{uid}/video'></video>"
             "<small class='hint'>Video Telegram'dan olinadi (20 MB gacha).</small>") if u.get("video_file_id") else ""

    pay = p["pay"]
    pay_btns = ""
    if PAYMENTS_ON and pay != "paid" and p["picks"]:
        pay_btns += form("pay", "confirm", "✅ To'lovni tasdiqlash", "", "To'lov tushganini tekshirdingizmi? Nomzodga xabar yuboriladi.")
    if PAYMENTS_ON and pay == "pending":
        pay_btns += " " + form("pay", "reject", "Rad etish", "ghost")
    if pay in ("paid", "rejected") or (not PAYMENTS_ON and pay != "none"):
        pay_btns += " " + form("pay", "reset", "Holatni tozalash", "ghost", "To'lov holatini 'yo'q' ga qaytarasizmi?")

    if p["picks"]:
        rows = [("Pozitsiyalar", esc(", ".join(PN.get(k, k) for k in p["picks"]))),
                ("Summa", f"<b>{money(s.get('amount'))}</b>"),
                ("To'lov holati", tag(PAY_LABEL, pay) if PAYMENTS_ON or pay != "none"
                 else "<span class='tag'>Click/Payme hali ulanmagan</span>")]
        if s.get("promo_code"):
            rows.insert(1, ("Promokod", f"<b>{esc(s['promo_code'])}</b> −{s.get('promo_pct') or 0}% "
                                        f"<small>(chegirmasiz {money(s.get('base_amount'))})</small>"))
        if s.get("pay_provider"):
            rows.append(("Tanlagan to'lov usuli", esc(s["pay_provider"])))
        if s.get("paid_at"):
            rows.append(("To'lov tasdiqlangan", fmt_dt(s["paid_at"])))
        pay_rows = "".join(f"<tr><th>{a}</th><td>{b}</td></tr>" for a, b in rows)
    else:
        pay_rows = ("<tr><td class='empty'>Nomzod ilovada hali pozitsiya tanlamagan "
                    "(yoki ilovani yangi versiyada ochmagan).</td></tr>")
    prog_rows = "".join(
        f"<tr><td>{esc(PN.get(k, k))}</td><td class='num'>{d} / {t}</td>"
        f"<td><div class='meter'><i style='width:{round(d * 100 / t) if t else 0}%'></i></div></td>"
        f"<td class='num'>{round(d * 100 / t) if t else 0}%</td></tr>"
        for k, (d, t) in p["prog"].items()
    ) or "<tr><td class='empty' colspan='4'>Hali dars ochilmagan</td></tr>"
    test_rows = "".join(
        f"<tr><td>{fmt_dt(a['at'])}</td><td>{esc(PN.get(a['position'], a['position']))}</td>"
        f"<td class='num'>{a['score']}/10</td><td>{tag(TEST_LABEL, a['passed'])}</td></tr>"
        for a in reversed(p["attempts"])
    ) or "<tr><td class='empty' colspan='4'>Hali test topshirilmagan</td></tr>"

    body = f"""
<a class='back' href='/admin'>← Barcha nomzodlar</a>
{f"<div class='flash'>{esc(msg)}</div>" if msg else ""}
<div class='head'><div><h1>{esc(p['name'])}</h1>
<small>{esc('@' + p['username'] if p['username'] else '')} · Telegram ID {uid}</small></div>
<div>{tag(SCREEN_LABEL, p['screen'])} {tag(PAY_LABEL, pay)}</div></div>
<div class='grid'>
 <section class='card'><h2>Anketa</h2><table class='kv'>{anketa}</table>{video}
   {f"<div class='actions'>{screen_btns}</div>" if screen_btns else ""}</section>
 <section class='card'><h2>Pozitsiyalar va to'lov</h2><table class='kv'>
   {pay_rows}
 </table><div class='actions'>{pay_btns}</div></section>
 <section class='card'><h2>Ilovadagi faollik</h2><table class='kv'>
   <tr><th>Ilovada jami</th><td><b>{fmt_dur(s.get('total_sec'))}</b></td></tr>
   <tr><th>Kirishlar soni</th><td>{s.get('sessions') or 0}</td></tr>
   <tr><th>Birinchi kirish</th><td>{fmt_dt(s.get('first_open'))}</td></tr>
   <tr><th>Oxirgi faollik</th><td>{fmt_dt(s.get('last_seen'))}</td></tr>
   <tr><th>Til</th><td>{esc(s.get('lang') or '—')}</td></tr></table></section>
 <section class='card'><h2>Darslar</h2><table>{prog_rows}</table></section>
 <section class='card wide'><h2>Test urinishlari</h2><table><thead><tr><th>Sana</th><th>Pozitsiya</th><th>Ball</th><th>Natija</th></tr></thead>{test_rows}</table></section>
</div>"""
    return page(p["name"], body)


async def admin_pay(request):
    uid = int(request.match_info["uid"])
    action = request["form"].get("v")
    if not PAYMENTS_ON and action != "reset":
        raise web.HTTPForbidden(text="To'lov hali ulanmagan")
    db, now = request.app["db"], now_iso()
    with closing(db()) as con:
        con.execute("INSERT OR IGNORE INTO app_state (tg_id, first_open) VALUES (?,?)", (uid, now))
        if action == "confirm":
            con.execute("UPDATE app_state SET pay_status='paid', paid_at=?, pay_by='admin' WHERE tg_id=?", (now, uid))
        elif action == "reject":
            con.execute("UPDATE app_state SET pay_status='rejected', paid_at=NULL, pay_by=NULL WHERE tg_id=?", (uid,))
        elif action == "reset":
            con.execute("UPDATE app_state SET pay_status='none', paid_at=NULL, pay_by=NULL WHERE tg_id=?", (uid,))
        else:
            raise web.HTTPBadRequest()
        con.commit()
    if action == "confirm":
        await request.app["on_paid"](uid)
    raise web.HTTPFound(f"/admin/u/{uid}?m={'paid' if action == 'confirm' else 'rejected' if action == 'reject' else 'reset'}")


async def admin_screen(request):
    uid = int(request.match_info["uid"])
    v = request["form"].get("v")
    if v not in ("pass", "fail"):
        raise web.HTTPBadRequest()
    ok = await request.app["on_decide"](uid, "passed" if v == "pass" else "rejected")
    raise web.HTTPFound(f"/admin/u/{uid}?m={('passed' if v == 'pass' else 'fail') if ok else 'err'}")


async def admin_video(request):
    uid = int(request.match_info["uid"])
    with closing(request.app["db"]()) as con:
        r = con.execute("SELECT video_file_id FROM users WHERE tg_id=?", (uid,)).fetchone()
    if not r or not r["video_file_id"]:
        raise web.HTTPNotFound()
    bot = request.app["bot"]
    try:
        f = await bot.get_file(r["video_file_id"])
        buf = await bot.download_file(f.file_path)
    except Exception as ex:
        log.warning("video olinmadi (%s): %s", uid, ex)
        return web.Response(status=502, text="Video olinmadi (Telegram 20 MB dan katta fayllarni bermaydi).")
    return web.Response(body=buf.getvalue(), content_type="video/mp4", headers={"Cache-Control": "private, max-age=3600"})


async def admin_export(request):
    people = load_people(request.app["db"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["tg_id", "ism", "username", "telefon", "yosh", "ingliz", "tajriba", "tungi_smena", "oylik_usd",
                "screening", "pozitsiyalar", "summa_som", "promokod", "tolov", "tolov_tasdiqlangan", "ilovada_daqiqa",
                "kirishlar", "darslar_foiz", "testlar", "royxat_sanasi", "oxirgi_faollik"])
    for p in people:
        u, s = p["u"], p["s"]
        w.writerow([
            p["uid"], p["name"], p["username"] or "", u.get("phone", ""), u.get("age", ""), u.get("level", ""),
            u.get("exp", ""), u.get("shift", ""), u.get("salary_usd", ""), p["screen"] or "",
            ", ".join(PN.get(k, k) for k in p["picks"]), s.get("amount") or "",
            f"{s['promo_code']} -{s.get('promo_pct')}%" if s.get("promo_code") else "", p["pay"], fmt_dt(s.get("paid_at")),
            round((s.get("total_sec") or 0) / 60), s.get("sessions") or 0,
            "" if p["lesson_pct"] is None else p["lesson_pct"],
            "; ".join(f"{PN.get(k, k)} {v}/10" for k, v in p["best"].items()), fmt_dt(p["created"]),
            fmt_dt(s.get("last_seen")),
        ])
    return web.Response(
        body=buf.getvalue().encode("utf-8-sig"), content_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=interviewhub_nomzodlar.csv"},
    )


async def admin_promo(request):
    db = request.app["db"]
    with closing(db()) as con:
        codes = con.execute("SELECT * FROM promo_codes ORDER BY created_at DESC").fetchall()
        used = dict(con.execute("SELECT promo_code, COUNT(*) FROM app_state WHERE promo_code IS NOT NULL GROUP BY promo_code").fetchall())
        paid = dict(con.execute("SELECT promo_code, COUNT(*) FROM app_state WHERE promo_code IS NOT NULL AND pay_status='paid' GROUP BY promo_code").fetchall())
    csrf = esc(csrf_token(request))
    msg = {"ok": "Promokod yaratildi.", "dup": "Bunday kod allaqachon bor.", "bad": "Ma'lumot noto'g'ri: kod 3–20 ta lotin harfi/raqam, foiz 1–100.",
           "tg": "Holat o'zgartirildi."}.get(request.query.get("m", ""), "")
    today = today_tz()
    rows = []
    for r in codes:
        if not r["active"]:
            st = ("O'chirilgan", "")
        elif r["expires_on"] and r["expires_on"] < today:
            st = ("Muddati o'tgan", "no")
        elif r["max_uses"] and used.get(r["code"], 0) >= r["max_uses"]:
            st = ("Limit tugagan", "am")
        else:
            st = ("Faol", "ok")
        btn = "O'chirish" if r["active"] else "Yoqish"
        note = f"<br><small>{esc(r['note'])}</small>" if r["note"] else ""
        rows.append(
            f"<tr><td><b>{esc(r['code'])}</b>{note}</td>"
            f"<td class='num'>−{r['percent']}%</td>"
            f"<td class='num'>{used.get(r['code'], 0)} / {r['max_uses'] or '∞'}</td>"
            f"<td class='num'>{paid.get(r['code'], 0)}</td>"
            f"<td class='num'>{esc(r['expires_on'] or 'cheksiz')}</td>"
            f"<td><span class='tag {st[1]}'>{st[0]}</span></td>"
            f"<td><form method='post' action='/admin/promo/{esc(r['code'])}/toggle'><input type='hidden' name='csrf' value='{csrf}'>"
            f"<button class='btn ghost'>{btn}</button></form></td></tr>"
        )
    body = f"""
<h1>Promokodlar</h1>
{f"<div class='flash'>{esc(msg)}</div>" if msg else ""}
<section class='card' style='margin:14px 0'><h2>Yangi promokod</h2>
<form method='post' action='/admin/promo' class='pf'>
 <input type='hidden' name='csrf' value='{csrf}'>
 <label>Kod<input name='code' required maxlength='20' pattern='[A-Za-z0-9_\\-]{{3,20}}' placeholder='BAHOR20' style='text-transform:uppercase'></label>
 <label>Chegirma, %<input name='percent' type='number' min='1' max='100' required placeholder='20'></label>
 <label>Necha kishi ishlata oladi<input name='max_uses' type='number' min='1' placeholder='cheksiz'></label>
 <label>Amal qilish muddati (oxirgi kun)<input name='expires_on' type='date'></label>
 <label class='wide'>Izoh (ixtiyoriy)<input name='note' maxlength='80' placeholder='Masalan: Instagram aksiyasi'></label>
 <button class='btn'>Yaratish</button>
</form><small class='hint'>Kod ilova kodida ko'rinmaydi, faqat serverda tekshiriladi. To'lovdan keyin nomzodning promokodi o'zgarmaydi.</small></section>
<div class='tbl'><table><thead><tr><th>Kod</th><th>Chegirma</th><th>Ishlatgan / limit</th><th>To'lagan</th><th>Muddati</th><th>Holat</th><th></th></tr></thead>
<tbody>{''.join(rows) or "<tr><td colspan='7' class='empty'>Hali promokod yo'q</td></tr>"}</tbody></table></div>"""
    return page("Promokodlar", body)


async def admin_promo_create(request):
    f = request["form"]
    code = str(f.get("code", "")).strip().upper()
    try:
        pct = int(f.get("percent", ""))
        max_uses = int(f["max_uses"]) if str(f.get("max_uses", "")).strip() else None
    except ValueError:
        raise web.HTTPFound("/admin/promo?m=bad")
    exp = str(f.get("expires_on", "")).strip() or None
    if not PROMO_RE.match(code) or not 1 <= pct <= 100 or (max_uses is not None and max_uses < 1) \
            or (exp and not re.match(r"^\d{4}-\d{2}-\d{2}$", exp)):
        raise web.HTTPFound("/admin/promo?m=bad")
    with closing(request.app["db"]()) as con:
        try:
            con.execute("INSERT INTO promo_codes (code, percent, max_uses, expires_on, active, note, created_at) VALUES (?,?,?,?,1,?,?)",
                        (code, pct, max_uses, exp, str(f.get("note", "")).strip()[:80] or None, now_iso()))
            con.commit()
        except sqlite3.IntegrityError:
            raise web.HTTPFound("/admin/promo?m=dup")
    raise web.HTTPFound("/admin/promo?m=ok")


async def admin_promo_toggle(request):
    with closing(request.app["db"]()) as con:
        con.execute("UPDATE promo_codes SET active=1-active WHERE code=?", (request.match_info["code"],))
        con.commit()
    raise web.HTTPFound("/admin/promo?m=tg")


async def health(request):
    return web.Response(text="ok")


async def root(request):
    raise web.HTTPFound("/admin")


# ---------------------------------------------------------------- HTML
CSS = """
:root{--bg:#f6f6f8;--card:#fff;--line:#e6e6ea;--tx:#141418;--hint:#6b6b75;--brand:#CF3637;--ok:#13804f;--okbg:#e3f6ec;
--am:#9a6a00;--ambg:#fdf3dc;--no:#b3261e;--nobg:#fde8e7}
@media (prefers-color-scheme:dark){:root{--bg:#0b0b0f;--card:#16161b;--line:#2a2a31;--tx:#f2f2f4;--hint:#9c9ca5;
--okbg:#12301f;--ok:#5be3a6;--ambg:#33260b;--am:#f5b94a;--nobg:#3a1c1c;--no:#ff6b6c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.45 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
header{background:var(--brand);color:#fff;padding:12px 24px;display:flex;align-items:center;justify-content:space-between}
header .lg{display:flex;align-items:center;gap:10px;font-weight:700;font-size:17px}
header .ih{width:32px;height:32px;border-radius:50%;background:#fff;color:#0b0b0f;display:flex;align-items:center;justify-content:center;font-weight:800}
header a{color:#fff;opacity:.85;text-decoration:none;font-size:13px}header nav{display:flex;gap:16px}header a:hover{opacity:1}
main{max-width:1280px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0}h2{font-size:15px;margin:0 0 12px}small,.hint{color:var(--hint);font-size:12px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:16px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}.kpi b{display:block;font-size:22px;margin-top:2px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.bar input{flex:1;min-width:200px}
input,select{font:inherit;color:var(--tx);background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px 12px}
.btn{display:inline-block;font:inherit;font-weight:600;border:0;border-radius:10px;padding:9px 14px;background:var(--brand);color:#fff;cursor:pointer;text-decoration:none}
.btn.ghost{background:transparent;color:var(--tx);border:1px solid var(--line)}
.tbl{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
thead th{font-size:12px;color:var(--hint);font-weight:600;white-space:nowrap;position:sticky;top:0;background:var(--card)}
#t tbody tr{cursor:pointer}#t tbody tr:hover{background:color-mix(in srgb,var(--brand) 6%,transparent)}
.num{white-space:nowrap;font-variant-numeric:tabular-nums}.empty{color:var(--hint);text-align:center;padding:24px}
.tag{display:inline-block;font-size:12px;font-weight:600;padding:2px 9px;border-radius:99px;background:var(--line);white-space:nowrap}
.tag.ok{background:var(--okbg);color:var(--ok)}.tag.am{background:var(--ambg);color:var(--am)}.tag.no{background:var(--nobg);color:var(--no)}
.back{color:var(--hint);text-decoration:none;display:inline-block;margin-bottom:12px}
.head{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px}.wide{grid-column:1/-1}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.kv th{width:45%;color:var(--hint);font-weight:500}.kv td,.kv th{padding:7px 0}
.actions{margin-top:14px;display:flex;gap:8px;flex-wrap:wrap}
.meter{height:8px;background:var(--line);border-radius:99px;overflow:hidden;min-width:80px}.meter i{display:block;height:100%;background:var(--brand)}
video{width:100%;max-height:360px;border-radius:10px;margin-top:12px;background:#000}
.flash{background:var(--okbg);color:var(--ok);padding:10px 14px;border-radius:10px;margin-bottom:12px;font-weight:600}
.pf{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;align-items:end;margin-bottom:8px}.pf label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--hint)}.pf .wide{grid-column:span 2}@media (max-width:600px){.pf .wide{grid-column:auto}}
.login{max-width:360px;margin:12vh auto;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px}
.login input{width:100%;margin:14px 0}.login .btn{width:100%}.err{color:var(--no);margin-top:10px}
@media (max-width:600px){header{padding:12px 16px}.grid{grid-template-columns:1fr}}
"""

LOGIN_HTML = """<form class='login' method='post' action='/admin/login'><h1>Interview Hub</h1>
<small>Admin dashboard</small><input type='password' name='password' placeholder='Parol' autofocus required>
<button class='btn'>Kirish</button><div class='err'>{err}</div></form>"""


def page(title, body, nav=True, status=200):
    head = ("<header><div class='lg'><span class='ih'>ih</span>Interview Hub · Admin</div><nav>"
            "<a href='/admin'>Nomzodlar</a><a href='/admin/promo'>Promokodlar</a>"
            "<a href='/admin/logout'>Chiqish</a></nav></header>") if nav else ""
    doc = (f"<!doctype html><html lang='uz'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'><meta name='robots' content='noindex'>"
           f"<title>{esc(title)} · Interview Hub</title><style>{CSS}</style></head>"
           f"<body>{head}<main>{body}</main></body></html>")
    return web.Response(text=doc, content_type="text/html", status=status,
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


# ---------------------------------------------------------------- ishga tushirish
def build_app(*, bot, db, token, on_paid, on_decide):
    app = web.Application(middlewares=[cors], client_max_size=256 * 1024)
    password = os.getenv("ADMIN_PASSWORD", "").strip().strip('"').strip("'").strip()
    app["bot"], app["db"], app["token"], app["password"] = bot, db, token, password
    app["on_paid"], app["on_decide"] = on_paid, on_decide
    app["secret"] = hashlib.sha256(f"ih-admin|{token}|{password}".encode()).digest()
    app["origins"] = {o.strip() for o in os.getenv("ALLOWED_ORIGINS", "https://sammakh.github.io").split(",") if o.strip()}
    for path, h in [("/hb", api_hb), ("/picks", api_picks), ("/sync", api_sync), ("/result", api_result),
                    ("/accept", api_accept), ("/pay", api_pay), ("/status", api_status), ("/quote", api_quote),
                    ("/promo", api_promo)]:
        app.router.add_post("/api" + path, h)
        app.router.add_route("OPTIONS", "/api" + path, h)
    app.router.add_get("/", root)
    app.router.add_get("/health", health)
    app.router.add_get("/admin/login", login_get)
    app.router.add_post("/admin/login", login_post)
    app.router.add_get("/admin/logout", logout)
    app.router.add_get("/admin", require_admin(admin_home))
    app.router.add_get("/admin/export.csv", require_admin(admin_export))
    app.router.add_get(r"/admin/u/{uid:\d+}", require_admin(admin_user))
    app.router.add_get(r"/admin/u/{uid:\d+}/video", require_admin(admin_video))
    app.router.add_post(r"/admin/u/{uid:\d+}/pay", require_admin(admin_pay))
    app.router.add_post(r"/admin/u/{uid:\d+}/screen", require_admin(admin_screen))
    app.router.add_get("/admin/promo", require_admin(admin_promo))
    app.router.add_post("/admin/promo", require_admin(admin_promo_create))
    app.router.add_post(r"/admin/promo/{code:[A-Z0-9_-]{3,20}}/toggle", require_admin(admin_promo_toggle))
    return app


async def start_web(**kw):
    init_tables(kw["db"])
    app = build_app(**kw)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Web server ishga tushdi: port %s", port)
    return runner
