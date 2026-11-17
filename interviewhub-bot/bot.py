"""Interview Hub: ro'yxatdan o'tkazuvchi Telegram bot (aiogram 3).

Oqim: /start -> ism -> telefon (kontakt tugmasi) -> yosh -> ta'lim -> ingliz darajasi
      -> tajriba -> smena -> oylik -> 1 daqiqalik video -> rozilik -> Mini App tugmasi.
Sozlamalar config.env faylidan o'qiladi.
"""
import asyncio
import csv
import html
import io
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand, BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, MenuButtonDefault, MenuButtonWebApp,
    Message, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo,
)

import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("interviewhub")


# ---------------------------------------------------------------- sozlamalar
def load_env(path="config.env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://sammakh.github.io/interviewhub-app/")
DB_PATH = os.getenv("DB_PATH", "data.db")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()]
BANNER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "banner.png")
MIN_VIDEO_SEC = 20
MAX_VIDEO_SEC = 180

CONSENTS = [
    "Ishga kirishga kafolat berilmasligini tushundim.",
    "Xizmat 3 ta intervyu tashkil etish ekanini tushundim.",
    "Ma'lumotlarim va intervyu natijam kompaniyalarga berilishiga roziman.",
    "Natijam reklama maqsadida (video/fikr) ishlatilishiga roziman. (ixtiyoriy)",
]
REQUIRED_CONSENTS = 3  # birinchi 3 tasi majburiy, 4-chisi ixtiyoriy

INTRO = (
    "<b>Interview Hub</b>ga xush kelibsiz!\n\n"
    "Biz AQSh logistika kompaniyalarida intervyu tashkil etishga yordam beramiz.\n\n"
    "• Hozir <b>pilot</b> bosqichi\n"
    "• Ishga kirishni <b>kafolatlamaymiz</b>: xizmat 3 ta intervyu tashkil etish\n"
    "• Ilovadan foydalanish uchun avval ro'yxatdan o'tasiz (3-4 daqiqa)\n"
    "• Ma'lumotlaringiz (ism, telefon, video) faqat nomzodlarni saralash va, "
    "roziligingiz bilan, kompaniyalarga taqdim etish uchun ishlatiladi\n\n"
    "Boshlaymizmi?"
)


# ---------------------------------------------------------------- baza
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with closing(db()) as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, phone TEXT,
                age INTEGER, edu TEXT, level TEXT, exp TEXT, shift TEXT, salary_usd INTEGER,
                video_file_id TEXT, video_kind TEXT, consent_marketing INTEGER,
                consent_at TEXT, status TEXT, created_at TEXT)"""
        )
        con.commit()


def get_user(tg_id):
    with closing(db()) as con:
        return con.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()


def save_user(rec):
    cols = ",".join(rec)
    marks = ",".join("?" * len(rec))
    with closing(db()) as con:
        con.execute(f"INSERT OR REPLACE INTO users ({cols}) VALUES ({marks})", list(rec.values()))
        con.commit()


def delete_user(tg_id):
    with closing(db()) as con:
        con.execute("DELETE FROM users WHERE tg_id=?", (tg_id,))
        con.commit()
    web.delete_app_data(db, tg_id)


# ---------------------------------------------------------------- klaviaturalar
def app_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Ilovani ochish", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )


def ikb(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def consent_kb(flags):
    nums = [(("✅ " if f else "") + str(i + 1), f"c:{i}") for i, f in enumerate(flags)]
    return ikb([nums, [("Tasdiqlash", "c:done")]])


def consent_text(flags):
    lines = [f"{'✅' if f else '▫️'} {i + 1}. {t}" for i, (f, t) in enumerate(zip(flags, CONSENTS))]
    return "<b>Shartlar va rozilik</b>\nHar bir bandni raqami orqali belgilang:\n\n" + "\n".join(lines)


def decision_kb(uid):
    return ikb([[("✅ O'tdi", f"dec:pass:{uid}"), ("❌ O'tmadi", f"dec:fail:{uid}")]])


def summary_text(r):
    e = html.escape
    uname = f"@{r['username']}" if r["username"] else f"id {r['tg_id']}"
    return (
        "<b>Yangi nomzod</b>\n"
        f"Ism: {e(r['full_name'])}\nTelefon: {r['phone']}\nYosh: {r['age']}\n"
        f"Ta'lim/ish: {e(r['edu'])}\nIngliz: {r['level']}\nTajriba: {e(r['exp'])}\n"
        f"Tungi smena: {e(r['shift'])}\nOylik: ${r['salary_usd']}\n"
        f"Telegram: {e(uname)}\nReklama roziligi: {'ha' if r['consent_marketing'] else 'yoq'}"
    )


STATUS_TEXT = {
    "registered": "Arizangiz qabul qilindi. Jamoa videongizni ko'rib chiqmoqda, natija shu botga yuboriladi.",
    "passed": "Siz screeningdan o'tdingiz. Keyingi qadam: ilovada pozitsiyalar va shartlar bilan tanishish.",
    "rejected": "Hozircha screeningdan o'tmadingiz. Ingliz tilingizni mashq qilib, keyinroq qayta urinib ko'rishingiz mumkin.",
}


# ---------------------------------------------------------------- holatlar
class Reg(StatesGroup):
    name = State()
    phone = State()
    age = State()
    edu = State()
    level = State()
    exp = State()
    shift = State()
    salary = State()
    video = State()
    consent = State()


router = Router()


def is_admin(uid):
    return uid in ADMIN_IDS


# ---------------------------------------------------------------- umumiy buyruqlar
@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    u = get_user(m.from_user.id)
    if u and u["status"] == "rejected":
        await m.answer(STATUS_TEXT["rejected"])
        return
    if u and u["status"]:
        await m.answer(
            f"Siz ro'yxatdan o'tgansiz, <b>{html.escape(u['full_name'])}</b>.\n"
            "Ilovani ochish uchun tugmani bosing.",
            reply_markup=app_kb(),
        )
        return
    kb = ikb([[("Boshlash", "reg:start")]])
    if os.path.exists(BANNER_PATH):
        await m.answer_photo(FSInputFile(BANNER_PATH), caption=INTRO, reply_markup=kb)
    else:
        await m.answer(INTRO, reply_markup=kb)


@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi. Qayta boshlash uchun /start yuboring.", reply_markup=ReplyKeyboardRemove())


@router.message(Command("myid"))
async def cmd_myid(m: Message):
    await m.answer(f"Sizning Telegram ID: <code>{m.from_user.id}</code>")


@router.message(Command("delete_me"))
async def cmd_delete_me(m: Message, state: FSMContext):
    await state.clear()
    delete_user(m.from_user.id)
    try:
        await m.bot.set_chat_menu_button(chat_id=m.chat.id, menu_button=MenuButtonDefault())
    except Exception:
        pass
    await m.answer("Ma'lumotlaringiz o'chirildi. Qayta ro'yxatdan o'tish uchun /start yuboring.")


@router.message(Command("stats"))
async def cmd_stats(m: Message):
    if not is_admin(m.from_user.id):
        return
    with closing(db()) as con:
        total = con.execute("SELECT COUNT(*) FROM users WHERE status IS NOT NULL").fetchone()[0]
        by_level = con.execute("SELECT level, COUNT(*) c FROM users GROUP BY level ORDER BY level").fetchall()
    lv = ", ".join(f"{r['level']}: {r['c']}" for r in by_level) or "-"
    await m.answer(f"Ro'yxatdan o'tganlar: <b>{total}</b>\nIngliz darajasi: {lv}")


@router.message(Command("export"))
async def cmd_export(m: Message):
    if not is_admin(m.from_user.id):
        return
    with closing(db()) as con:
        rows = con.execute("SELECT * FROM users ORDER BY created_at").fetchall()
    if not rows:
        await m.answer("Hali ro'yxatdan o'tganlar yo'q.")
        return
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(rows[0].keys())
    for r in rows:
        w.writerow(list(r))
    data = buf.getvalue().encode("utf-8-sig")
    await m.answer_document(BufferedInputFile(data, filename="nomzodlar.csv"), caption=f"{len(rows)} ta nomzod")


@router.message(Command("status"))
async def cmd_status(m: Message):
    u = get_user(m.from_user.id)
    if not u or not u["status"]:
        await m.answer("Siz hali ro'yxatdan o'tmagansiz. /start yuboring.")
        return
    await m.answer(STATUS_TEXT.get(u["status"], u["status"]))


@router.message(Command("pending"))
async def cmd_pending(m: Message):
    if not is_admin(m.from_user.id):
        return
    with closing(db()) as con:
        rows = con.execute("SELECT * FROM users WHERE status='registered' ORDER BY created_at LIMIT 10").fetchall()
    if not rows:
        await m.answer("Kutayotgan nomzod yo'q.")
        return
    for r in rows:
        await m.answer(summary_text(r), reply_markup=decision_kb(r["tg_id"]))


@router.callback_query(F.data.regexp(r"^dec:(pass|fail):\d+$"))
async def decide(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Ruxsat yo'q", show_alert=True)
        return
    _, act, uid = cb.data.split(":")
    uid = int(uid)
    u = get_user(uid)
    if not u:
        await cb.answer("Nomzod topilmadi (ma'lumotini o'chirgan bo'lishi mumkin).", show_alert=True)
        return
    if u["status"] != "registered":
        await cb.answer(f"Allaqachon hal qilingan: {u['status']}", show_alert=True)
        return
    new = "passed" if act == "pass" else "rejected"
    await apply_decision(cb.bot, uid, new)
    await cb.answer("Saqlandi")
    mark = "✅ O'tdi" if new == "passed" else "❌ O'tmadi"
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer(f"{mark}: {html.escape(u['full_name'])} (qaror: {html.escape(cb.from_user.full_name)})")
    except Exception as ex:
        log.warning("admin xabari yangilanmadi: %s", ex)


async def apply_decision(bot, uid, new):
    """Skrining qarorini saqlaydi va nomzodga xabar beradi (bot tugmasi va dashboard uchun umumiy)."""
    with closing(db()) as con:
        cur = con.execute("UPDATE users SET status=? WHERE tg_id=? AND status='registered'", (new, uid))
        con.commit()
        if cur.rowcount == 0:
            return False
    try:
        if new == "passed":
            await bot.send_message(
                uid,
                "Tabriklaymiz! Siz screeningdan o'tdingiz.\n"
                "Keyingi qadam: ilovada pozitsiyalarni tanlang va shartlar bilan tanishing.",
                reply_markup=app_kb(),
            )
        else:
            await bot.send_message(uid, STATUS_TEXT["rejected"])
            await bot.set_chat_menu_button(chat_id=uid, menu_button=MenuButtonDefault())
    except Exception as ex:
        log.warning("nomzodga natija yuborilmadi (%s): %s", uid, ex)
    return True


async def notify_paid(bot, uid):
    try:
        await bot.send_message(
            uid,
            "✅ To'lovingiz tasdiqlandi. Rahmat!\n"
            "Ilovada «Men» bo'limi ochildi: intervyularga tayyorgarlikni boshlang.",
            reply_markup=app_kb(),
        )
    except Exception as ex:
        log.warning("to'lov xabari yuborilmadi (%s): %s", uid, ex)


# ---------------------------------------------------------------- ro'yxatdan o'tish
@router.callback_query(F.data == "reg:start")
async def reg_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.set_state(Reg.name)
    await cb.message.answer("1/9. Ism va familiyangizni yozing:")


@router.message(Reg.name, F.text)
async def reg_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if not (3 <= len(name) <= 80) or name.startswith("/"):
        await m.answer("Ism va familiyani to'liq yozing (kamida 3 harf).")
        return
    await state.update_data(full_name=name)
    await state.set_state(Reg.phone)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Raqamimni yuborish", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await m.answer("2/9. Telefon raqamingizni pastdagi tugma orqali yuboring:", reply_markup=kb)


@router.message(Reg.phone, F.contact)
async def reg_phone(m: Message, state: FSMContext):
    c = m.contact
    if c.user_id != m.from_user.id:
        await m.answer("Iltimos, o'zingizning raqamingizni yuboring (pastdagi tugma orqali).")
        return
    digits = "".join(ch for ch in c.phone_number if ch.isdigit())
    await state.update_data(phone="+" + digits)
    await state.set_state(Reg.age)
    await m.answer("3/9. Yoshingiz (raqam bilan):", reply_markup=ReplyKeyboardRemove())


@router.message(Reg.phone)
async def reg_phone_wrong(m: Message):
    await m.answer("Raqamni faqat pastdagi <b>Raqamimni yuborish</b> tugmasi orqali yuboring.")


@router.message(Reg.age, F.text)
async def reg_age(m: Message, state: FSMContext):
    t = m.text.strip()
    if not t.isdigit() or not (16 <= int(t) <= 60):
        await m.answer("Yoshni raqam bilan yozing (16 dan 60 gacha).")
        return
    await state.update_data(age=int(t))
    await state.set_state(Reg.edu)
    await m.answer("4/9. Hozir qayerda o'qiysiz yoki o'qigansiz? (universitet, kurs yoki ish joyi)")


@router.message(Reg.edu, F.text)
async def reg_edu(m: Message, state: FSMContext):
    await state.update_data(edu=m.text.strip()[:200])
    await state.set_state(Reg.level)
    await m.answer(
        "5/9. Ingliz tili darajangiz (o'zingiz baholang):",
        reply_markup=ikb([[("B1", "lvl:B1"), ("B2", "lvl:B2"), ("C1", "lvl:C1"), ("C2", "lvl:C2")]]),
    )


@router.callback_query(Reg.level, F.data.startswith("lvl:"))
async def reg_level(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    lvl = cb.data.split(":")[1]
    await state.update_data(level=lvl)
    await cb.message.edit_text(f"5/9. Ingliz tili darajangiz: <b>{lvl}</b>")
    await state.set_state(Reg.exp)
    await cb.message.answer(
        "6/9. Logistika bo'yicha tajriba yoki kursingiz bormi? Qisqa yozing.",
        reply_markup=ikb([[("Yo'q", "exp:none")]]),
    )


@router.callback_query(Reg.exp, F.data == "exp:none")
async def reg_exp_none(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.update_data(exp="Yo'q")
    await ask_shift(cb.message, state)


@router.message(Reg.exp, F.text)
async def reg_exp(m: Message, state: FSMContext):
    await state.update_data(exp=m.text.strip()[:500])
    await ask_shift(m, state)


async def ask_shift(msg: Message, state: FSMContext):
    await state.set_state(Reg.shift)
    await msg.answer(
        "7/9. Tungi smenaga (AQSh vaqti bo'yicha) tayyormisiz?",
        reply_markup=ikb([[("Ha", "sh:Ha"), ("Yo'q", "sh:Yo'q"), ("Kelishsa bo'ladi", "sh:Kelishsa bo'ladi")]]),
    )


@router.callback_query(Reg.shift, F.data.startswith("sh:"))
async def reg_shift(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.update_data(shift=cb.data.split(":", 1)[1])
    await state.set_state(Reg.salary)
    await cb.message.answer("8/9. Kutilayotgan oylik maosh (USD, raqam bilan). Masalan: 800")


@router.message(Reg.salary, F.text)
async def reg_salary(m: Message, state: FSMContext):
    t = m.text.strip().replace(" ", "")
    if not t.isdigit() or int(t) > 100000:
        await m.answer("Oylikni faqat raqam bilan yozing (USD). Masalan: 800")
        return
    await state.update_data(salary_usd=int(t))
    await state.set_state(Reg.video)
    await m.answer(
        "9/9. <b>Inglizcha video tanishtiruv</b>\n"
        f"{MIN_VIDEO_SEC} soniyadan 3 daqiqagacha video (yoki dumaloq video) yuboring. Ayting:\n"
        "• ismingiz va qayerda o'qiysiz/ishlaysiz\n"
        "• nega logistika sohasida ishlamoqchisiz\n"
        "• tungi smena va bosimga qanday qaraysiz\n\n"
        "Kameraga qarab, erkin gapiring."
    )


@router.message(Reg.video, F.video | F.video_note)
async def reg_video(m: Message, state: FSMContext):
    media = m.video or m.video_note
    kind = "video" if m.video else "video_note"
    if media.duration < MIN_VIDEO_SEC:
        await m.answer(f"Video juda qisqa. Kamida {MIN_VIDEO_SEC} soniya bo'lsin, qayta yuboring.")
        return
    if media.duration > MAX_VIDEO_SEC:
        await m.answer("Video juda uzun. 3 daqiqadan oshmasin, qayta yuboring.")
        return
    await state.update_data(
        video_file_id=media.file_id, video_kind=kind, video_msg_id=m.message_id, consent=[False] * len(CONSENTS)
    )
    await state.set_state(Reg.consent)
    await m.answer(consent_text([False] * len(CONSENTS)), reply_markup=consent_kb([False] * len(CONSENTS)))


@router.message(Reg.video)
async def reg_video_wrong(m: Message):
    await m.answer("Iltimos, video yoki dumaloq video yuboring (matn qabul qilinmaydi).")


@router.callback_query(Reg.consent, F.data.regexp(r"^c:\d$"))
async def reg_consent_toggle(cb: CallbackQuery, state: FSMContext):
    i = int(cb.data.split(":")[1])
    flags = (await state.get_data())["consent"]
    if i >= len(flags):
        await cb.answer()
        return
    flags[i] = not flags[i]
    await state.update_data(consent=flags)
    await cb.answer()
    await cb.message.edit_text(consent_text(flags), reply_markup=consent_kb(flags))


@router.callback_query(Reg.consent, F.data == "c:done")
async def reg_consent_done(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    flags = data["consent"]
    if not all(flags[:REQUIRED_CONSENTS]):
        await cb.answer("Dastlabki 3 ta bandni belgilash majburiy.", show_alert=True)
        return
    await cb.answer()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    u = cb.from_user
    rec = dict(
        tg_id=u.id, username=u.username, full_name=data["full_name"], phone=data["phone"],
        age=data["age"], edu=data["edu"], level=data["level"], exp=data["exp"], shift=data["shift"],
        salary_usd=data["salary_usd"], video_file_id=data["video_file_id"], video_kind=data["video_kind"],
        consent_marketing=int(flags[3]), consent_at=now, status="registered", created_at=now,
    )
    save_user(rec)
    await state.clear()
    await cb.message.edit_reply_markup(reply_markup=None)

    # shu foydalanuvchi uchun menyu tugmasini Mini Appga almashtiramiz
    try:
        await cb.bot.set_chat_menu_button(
            chat_id=u.id, menu_button=MenuButtonWebApp(text="Open", web_app=WebAppInfo(url=WEBAPP_URL))
        )
    except Exception as e:
        log.warning("menu button o'rnatilmadi: %s", e)

    await cb.bot.send_message(
        u.id,
        "Ro'yxatdan o'tdingiz. Rahmat!\n\n"
        "Jamoamiz videongizni ko'rib chiqadi, natija shu botga yuboriladi. "
        "Ilovani hozirdan ochishingiz mumkin.",
        reply_markup=app_kb(),
    )

    # adminlarga xabar
    summary = summary_text(rec)
    for aid in ADMIN_IDS:
        try:
            await cb.bot.forward_message(aid, from_chat_id=u.id, message_id=data["video_msg_id"])
            await cb.bot.send_message(aid, summary, reply_markup=decision_kb(u.id))
        except Exception as ex:
            log.warning("adminga yuborilmadi (%s): %s", aid, ex)


@router.message(StateFilter(Reg))
async def reg_fallback(m: Message):
    await m.answer("Iltimos, savolga javob bering yoki /cancel bilan bekor qiling.")


@router.message()
async def anything(m: Message):
    u = get_user(m.from_user.id)
    if u and u["status"] in ("registered", "passed"):
        await m.answer(STATUS_TEXT[u["status"]], reply_markup=app_kb())
    elif u and u["status"] == "rejected":
        await m.answer(STATUS_TEXT["rejected"])
    else:
        await m.answer("Boshlash uchun /start yuboring.")


# ---------------------------------------------------------------- ishga tushirish
async def main():
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_TOKEN_HERE":
        raise SystemExit("BOT_TOKEN topilmadi. config.env faylini oching va tokenni yozing.")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS bo'sh: yangi nomzodlar haqida xabar kelmaydi. Botga /myid yuboring va config.env ga yozing.")
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    # ro'yxatdan o'tmaganlarda Mini App tugmasi ko'rinmasligi uchun umumiy menyuni standartga qaytaramiz
    await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    await bot.set_my_commands([
        BotCommand(command="start", description="Boshlash / ro'yxatdan o'tish"),
        BotCommand(command="status", description="Arizam holati"),
        BotCommand(command="cancel", description="Bekor qilish"),
        BotCommand(command="delete_me", description="Ma'lumotlarimni o'chirish"),
    ])
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await web.start_web(
        bot=bot, db=db, token=BOT_TOKEN,
        on_paid=lambda uid: notify_paid(bot, uid),
        on_decide=lambda uid, new: apply_decision(bot, uid, new),
    )
    log.info("Bot ishga tushdi")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
