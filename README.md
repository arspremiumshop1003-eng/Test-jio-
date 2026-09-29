# Premium Hub — Unified Platform

**Telegram Bot + Web App · একই Backend · একই Database (`ars_bot.db`)**

```
Telegram User ──► Bot (polling)
                      │
                      ▼
                 Shared Core + SQLite
                      ▲
                      │
Website User ──► FastAPI ──► Frontend (SPA)
```

## Features preserved

| Feature | Telegram | Website |
|---------|----------|---------|
| User / Wallet | ✅ | ✅ |
| Products (Gemini + OWN + suppliers) | ✅ | ✅ |
| Balance purchase | ✅ | ✅ |
| Direct PayHub invoice | ✅ | ✅ |
| Auto delivery (OWN + suppliers) | ✅ | ✅ (OWN instant; supplier via bot) |
| Top-up / Add Funds | ✅ | ✅ |
| Admin panel | ✅ (bot) | ✅ (web) |
| CSV export | ✅ | ✅ |
| Payment webhook | ✅ shared | ✅ |

## Project structure

```
premium-hub/
├── backend/
│   ├── app/
│   │   ├── main.py          # FastAPI (API + webhook + static)
│   │   ├── core/            # config, db, users, helpers
│   │   └── auth/            # JWT + Telegram Login
│   ├── bot/
│   │   └── bot_original.py  # Your existing bot (unchanged logic)
│   ├── run_all.py           # Unified entry (API + Bot)
│   ├── run_api.py
│   └── requirements.txt
├── frontend/
│   ├── index.html
│   └── assets/              # app.js + style.css
├── .env.example
├── Procfile
└── README.md
```

## Quick start (local)

```bash
cd backend
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp ../.env.example ../.env
# Edit .env — BOT_TOKEN, PAYMENT_*, ADMIN_IDS, JWT_SECRET, TELEGRAM_BOT_USERNAME

# Use your existing ars_bot.db (copy next to process or set DB_FILE)
export DB_FILE=/path/to/ars_bot.db

python run_all.py
```

- Web: http://localhost:8080  
- API docs: http://localhost:8080/docs  
- Bot: Telegram long-polling (same process)

## Railway deploy

1. New Railway project → Deploy from GitHub (or CLI).
2. **Volume** mount (e.g. `/data`) so SQLite persists.
3. Variables (from `.env.example`):

```
BOT_TOKEN=
PAYMENT_API_KEY=
PAYMENT_WEBHOOK_SECRET=
PAYMENT_BASE_URL=
ADMIN_IDS=
DB_FILE=ars_bot.db
RAILWAY_VOLUME_MOUNT_PATH=/data
JWT_SECRET=<long-random>
TELEGRAM_BOT_USERNAME=<bot_username_without_@>
RUN_MODE=unified
PORT=8080
```

4. Start command:

```
cd backend && pip install -r requirements.txt && python run_all.py
```

5. PayHub webhook URL:

```
https://YOUR-RAILWAY-DOMAIN/api/v1/payments/webhook
```

Header: `X-Webhook-Secret: <PAYMENT_WEBHOOK_SECRET>`

6. Telegram Login Widget: BotFather → set domain to your Railway domain.

## Web auth

1. **Telegram Login Widget** (production) — set `TELEGRAM_BOT_USERNAME`.
2. **Dev login** — set `DEV_LOGIN_SECRET` and use the modal “Telegram ID” field.

Same `telegram_id` → same balance, orders, products as the bot.

## API overview

| Method | Path | Auth |
|--------|------|------|
| POST | `/api/auth/telegram` | — |
| GET | `/api/user/me` | JWT |
| GET | `/api/products` | optional |
| POST | `/api/orders` | JWT |
| POST | `/api/topup` | JWT |
| POST | `/api/topup/verify` | JWT |
| POST | `/api/orders/verify` | JWT |
| GET | `/api/admin/stats` | Admin JWT |
| POST | `/api/admin/balance/add` | Admin |
| GET | `/api/admin/export/users` | Admin |

Full list: `/docs`

## Important notes

1. **Database**: Never delete `ars_bot.db`. Migrations only ADD columns.
2. **Supplier delivery** (AIVerse / Elite / ShopAPI): Web creates `PAID` orders; the **bot process** continues to run the full `deliver_order` state machine. OWN stock delivers immediately from the API.
3. **Webhook**: Only FastAPI listens on `PORT`. Bot’s internal Flask webhook is disabled in unified mode.
4. **Admin IDs**: Same `ADMIN_IDS` env for bot + web.

## Support

Telegram: [@lostdopay](https://t.me/lostdopay)
