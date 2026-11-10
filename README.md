# 🌐 Telegram HTML Web Hosting Bot

Telegram bot jisme user `.html` file bhejta hai, site ka naam deta hai, aur turant uski site live ho jaati hai.

**Stack:** Python, python-telegram-bot, Flask, Upstash Redis, Render

---

## ✨ Features
- 📤 HTML file upload + custom site naam
- 🔗 Instant link: `https://yourapp.onrender.com/sitename`
- 📂 My Sites list (Open / Update / Delete)
- ♻️ Same site mein nayi file se update
- 🛡 Admin Panel: stats, broadcast, ban/unban, delete any site
- 👥 Users list: kitne users hain aur kisne kitni sites banayi (user par tap karke uski sites, ban/unban)
- 📡 Force-Join Channels: admin panel se channel add/remove, users ko pehle join karna padega
- 📏 File size limit aur per-user site limit
- 🔒 Naam validation (`a-z 0-9 -`), duplicate naam block

---

## 📁 Files
| File | Kaam |
|---|---|
| `main.py` | Poora bot + web server |
| `requirements.txt` | Python libraries |
| `.env.example` | ENV variables ka sample |
| `.gitignore` | `.env` ko GitHub se bachata hai |
| `.python-version` | Render ko Python 3.12.8 use karne ke liye batata hai |

---

## 🔑 Environment Variables

| Key | Value |
|---|---|
| `BOT_TOKEN` | @BotFather se mila token |
| `UPSTASH_REDIS_REST_URL` | Upstash REST URL |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash REST Token |
| `BASE_URL` | Render ka link (bina `/` ke), jaise `https://yourapp.onrender.com` |
| `ADMIN_ID` | Tumhara Telegram ID (kai ho to comma se: `123,456`) |
| `DEV_NAME` | Developer ka naam |
| `DEV_USERNAME` | Telegram username (bina `@` ke) |
| `PYTHON_VERSION` | `3.12.8` (zaroori, Render ka default Python 3.14 hai) |
| `MAX_SITES` | Per user max sites (default `10`) |
| `MAX_FILE_MB` | Max file size MB mein (default `1`) |

---

## 🚀 Setup (Step by Step)

### 1. Bot token
1. Telegram par **@BotFather** kholo
2. `/newbot` bhejo, naam aur username do
3. Token copy karo
4. Apna Telegram ID **@userinfobot** se lo

### 2. Upstash Redis
1. https://upstash.com par login karo
2. **Create Database** dabao, region chuno
3. **REST API** section se `REST URL` aur `REST TOKEN` copy karo

### 3. GitHub
1. Naya repo banao
2. Is folder ki saari files upload karo
3. `.env` kabhi upload mat karna

### 4. Render par deploy
1. **New → Web Service** → GitHub repo connect karo
2. **Build Command:** `pip install -r requirements.txt`
3. **Start Command:** `python main.py`
4. **Plan:** Free
5. Upar wale saare ENV variables daalo
6. Deploy ke baad Render ka link milega, usse `BASE_URL` mein daalo aur **Manual Deploy** karo

### 5. 24/7 chalane ke liye
Uptime monitor mein ye URL add karo (har 5 min ping):
```
https://yourapp.onrender.com/health
```

---

## 🤖 Bot Commands

**Users:** `/start` (baaki sab buttons se)

**Admin only:**
```
/ban <user_id>
/unban <user_id>
/delsite <name>
```
Broadcast aur stats **🛡 Admin Panel** button se.

---

## 🗄 Redis Data Structure
```
site:<name>       -> HTML content
owner:<name>      -> owner user id
user:<id>:sites   -> user ki sites ka set
users             -> saare users ka set
uinfo:<id>        -> user ka naam aur username
channels          -> force-join channel ids ka set
chan:<id>         -> channel ka title aur link
banned            -> banned users ka set
```

---

## 💻 Local par chalana
```bash
pip install -r requirements.txt
cp .env.example .env     # values bharo
export $(grep -v '^#' .env | xargs)
python main.py
```

---

## 📡 Channel Add Kaise Karein
1. Bot ko channel/group mein **Admin** banao
2. Bot mein **🛡 Admin Panel → 📡 Channels → ➕ Channel Add Karo**
3. Channel ka `@username` ya ID bhejo, ya channel ka koi message forward karo
4. Ab har user ko bot use karne se pehle wo channel join karna hoga (admin par ye rule nahi lagta)

---

## ⚠️ Dhyan Rakho
- Saari sites ek hi domain par chalti hain. Malicious/phishing HTML ho sakti hai, isliye `/delsite` aur `/ban` use karte raho.
- Render Free par service 15 min inactive rehne par sleep ho jaati hai, uptime monitor zaroor lagao.
- Bot token aur Redis token kisi ke saath share mat karo.

---

Developed by **Aditya**
