# Interview Hub

Telegram bot + Mini App: nomzodlarni ro'yxatdan o'tkazish, screening (o'tdi / o'tmadi) va logistika kompaniyalariga intervyu tayyorlash.

## Tuzilma

| Fayl / papka | Nima |
|---|---|
| `index.html`, `pricing.js`, `materials/` | Telegram Mini App. GitHub Pages orqali ishlaydi: https://sammakh.github.io/interviewhub-app/ |
| `interviewhub-bot/bot.py` | Telegram bot (aiogram 3): `/start` ro'yxatdan o'tish, `/status`, admin uchun `/pending`, `/export` va qaror tugmalari |
| `interviewhub-bot/banner.png`, `avatar.png` | Bot banneri (/start) va avatar |
| `interviewhub-bot/config.env.example` | Sozlamalar namunasi |

## Botni ishga tushirish

```bash
cd interviewhub-bot
cp config.env.example config.env   # BOT_TOKEN va ADMIN_IDS ni yozing
pip install -r requirements.txt
python3 bot.py
```

`config.env` GitHub'ga yuklanmaydi (`.gitignore`da). Bu repo ochiq (public) — tokenni hech qachon repoga qo'shmang.
