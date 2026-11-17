# Interview Hub

Telegram bot + Mini App: nomzodlarni ro'yxatdan o'tkazish, screening (o'tdi / o'tmadi) va logistika kompaniyalariga intervyu tayyorlash.

## Tuzilma

| Fayl / papka | Nima |
|---|---|
| `index.html`, `pricing.js`, `materials/` | Telegram Mini App. GitHub Pages orqali ishlaydi: https://sammakh.github.io/interviewhub-app/ |
| `interviewhub-bot/bot.py` | Telegram bot (aiogram 3): `/start` ro'yxatdan o'tish, `/status`, admin uchun `/pending`, `/export` va qaror tugmalari |
| `interviewhub-bot/banner.png`, `avatar.png` | Bot banneri (/start) va avatar |
| `interviewhub-bot/config.env.example` | Sozlamalar namunasi |

## Admin dashboard

https://interviewhub-bot-production.up.railway.app/admin — parol Railway'dagi `ADMIN_PASSWORD` o'zgaruvchisida.

Ko'rinadi: anketa va video, skrining qarori, tanlangan pozitsiyalar va summa, to'lov holati (tasdiqlash tugmasi),
ilovada o'tkazilgan vaqt, darslar progressi (%), test urinishlari, CSV eksport.
Ilova ma'lumotlarni `interviewhub-bot/web.py` dagi `/api/*` ga yuboradi (Telegram initData imzosi bilan tekshiriladi).
Narxlar `index.html` (`PRICES`) va `web.py` (`PRICES`) da bir xil bo'lishi kerak.

## Botni ishga tushirish

```bash
cd interviewhub-bot
cp config.env.example config.env   # BOT_TOKEN va ADMIN_IDS ni yozing
pip install -r requirements.txt
python3 bot.py
```

## Railway'da ishga tushirish

1. railway.com → **New Project → Deploy from GitHub repo** → `sammakh/interviewhub-app`.
2. Servis → **Settings**:
   - **Root Directory**: `/interviewhub-bot`
   - **Config-as-code → Railway Config File**: `/interviewhub-bot/railway.json` (yoki **Custom Start Command**: `python bot.py`)
3. Servis → o'ng tugma → **Attach Volume**, mount path: `/data` (nomzodlar bazasi saqlanib qolishi uchun).
4. **Variables**:
   - `BOT_TOKEN` — @BotFather bergan token
   - `ADMIN_IDS` — admin Telegram ID lari, vergul bilan
   - `DB_PATH` = `/data/data.db`
5. Deploy. Logda `Bot ishga tushdi` chiqsa — tayyor. GitHub'ga har push qilinganda Railway o'zi qayta deploy qiladi.

Faqat **1 ta** nusxa ishlashi kerak: bot boshqa joyda (masalan, Mac'da) ham ishlab tursa, Telegram `Conflict` xatosini beradi.

`config.env` GitHub'ga yuklanmaydi (`.gitignore`da). Bu repo ochiq (public) — tokenni hech qachon repoga qo'shmang.
