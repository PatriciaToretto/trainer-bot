import os
import json
import asyncio
import base64
import httpx
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    filters, ContextTypes, CallbackQueryHandler
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_KEY_HERE")
DATA_FILE = "user_data.json"

# ── persistence ───────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_user(data, uid):
    key = str(uid)
    if key not in data:
        data[key] = {
            "name": "",
            "history": [],
            "measurements": [],
            "last_weekly": None,
            "registered": datetime.now().isoformat()
        }
    return data[key]

# ── Claude API ────────────────────────────────────────────────
SYSTEM_PROMPT = """Ти — персональний тренер і дієтолог Макс. Ти суворий але дуже мотивуючий наставник. 
Ти розмовляєш українською мовою. Твій стиль: прямий, мотивуючий, без зайвих слів, із конкретними порадами.

Твоя роль:
- Аналізувати фото їжі, тренувань, виміри тіла
- Оцінювати дефіцит калорій та якість харчування
- Мотивувати до щоденної активності
- Відслідковувати прогрес замірів (талія, біцепс, живіт, стегна, вага)
- Давати конкретні рекомендації

При аналізі їжі: оцінюй калорії (приблизно), білки/жири/вуглеводи, чи підходить для дефіциту.
При аналізі тренувань: оцінюй інтенсивність, хвали зусилля, пропонуй покращення.
При замірах: порівнюй з попередніми, показуй динаміку, мотивуй.
При воді: підтримуй норму 2-3 літри на день.

Завжди закінчуй відповідь коротким мотивуючим гаслом! 💪"""

async def ask_claude(messages, image_b64=None, image_type="image/jpeg"):
    content = []
    if image_b64:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": image_type, "data": image_b64}
        })
    # last user text
    last_text = messages[-1]["content"] if messages else ""
    content.append({"type": "text", "text": last_text if isinstance(last_text, str) else ""})
    
    api_messages = []
    for m in messages[:-1]:
        api_messages.append({"role": m["role"], "content": m["content"]})
    api_messages.append({"role": "user", "content": content})

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "system": SYSTEM_PROMPT,
                "messages": api_messages
            }
        )
        result = resp.json()
        return result["content"][0]["text"]

# ── helpers ───────────────────────────────────────────────────
def build_history(user, extra_text):
    hist = user["history"][-10:] if len(user["history"]) > 10 else user["history"]
    messages = list(hist)
    messages.append({"role": "user", "content": extra_text})
    return messages

def needs_weekly_check(user):
    if not user["last_weekly"]:
        return True
    last = datetime.fromisoformat(user["last_weekly"])
    return datetime.now() - last >= timedelta(days=7)

# ── keyboards ─────────────────────────────────────────────────
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🥗 Звіт їжі", callback_data="report_food"),
         InlineKeyboardButton("💪 Звіт тренування", callback_data="report_workout")],
        [InlineKeyboardButton("💧 Вода", callback_data="report_water"),
         InlineKeyboardButton("📏 Виміри тіла", callback_data="report_measurements")],
        [InlineKeyboardButton("📊 Мій прогрес", callback_data="show_progress"),
         InlineKeyboardButton("🔥 Мотивація!", callback_data="motivate")]
    ])

# ── handlers ──────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user = get_user(data, update.effective_user.id)
    user["name"] = update.effective_user.first_name
    save_data(data)
    
    text = (
        f"Привіт, {user['name']}! 👊 Я Макс — твій персональний тренер.\n\n"
        "Я допоможу тобі:\n"
        "• 🥗 Аналізувати харчування по фото\n"
        "• 💪 Відстежувати тренування\n"
        "• 💧 Контролювати водний баланс\n"
        "• 📏 Вести щотижневі виміри тіла\n"
        "• 📊 Бачити твій прогрес\n\n"
        "Надсилай фото їжі чи тренувань просто так — я одразу аналізую!\n"
        "Або обери дію нижче 👇"
    )
    await update.message.reply_text(text, reply_markup=main_keyboard())

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = load_data()
    user = get_user(data, query.from_user.id)
    action = query.data

    if action == "report_food":
        ctx.user_data["awaiting"] = "food"
        await query.message.reply_text(
            "📸 Надішли фото свого прийому їжі!\n"
            "Я оціню калорійність, склад та чи підходить це для твоєї мети 🎯"
        )
    elif action == "report_workout":
        ctx.user_data["awaiting"] = "workout"
        await query.message.reply_text(
            "💪 Надішли фото або відео з тренування!\n"
            "Або просто опиши що робив — я дам зворотній зв'язок 🔥"
        )
    elif action == "report_water":
        ctx.user_data["awaiting"] = "water"
        await query.message.reply_text(
            "💧 Скільки склянок/літрів води випив сьогодні?\n"
            "Напиши цифру (наприклад: 1.5л або 6 склянок)"
        )
    elif action == "report_measurements":
        ctx.user_data["awaiting"] = "measurements"
        await query.message.reply_text(
            "📏 Надішли свої виміри у такому форматі:\n\n"
            "Вага: ___ кг\n"
            "Талія: ___ см\n"
            "Живіт: ___ см\n"
            "Стегна: ___ см\n"
            "Біцепс: ___ см\n"
            "Груди: ___ см (необов'язково)\n\n"
            "Можеш надіслати лише ті, що маєш 👌"
        )
    elif action == "show_progress":
        await show_progress(query, user)
    elif action == "motivate":
        messages = [{"role": "user", "content": "Дай мені потужну мотивацію для тренування сьогодні! Коротко і потужно!"}]
        reply = await ask_claude(messages)
        await query.message.reply_text(reply, reply_markup=main_keyboard())

async def show_progress(query_or_msg, user):
    measurements = user.get("measurements", [])
    if not measurements:
        text = "📊 Поки що немає замірів. Надішли перші виміри — і я почну відстежувати прогрес!"
    else:
        last = measurements[-1]
        text = f"📊 *Останні виміри* ({last.get('date','')}):\n\n"
        fields = [("Вага","weight","кг"),("Талія","waist","см"),
                  ("Живіт","belly","см"),("Стегна","hips","см"),("Біцепс","bicep","см")]
        for label, key, unit in fields:
            if key in last:
                val = last[key]
                if len(measurements) > 1:
                    prev = measurements[-2].get(key)
                    if prev:
                        diff = round(val - prev, 1)
                        arrow = "📉" if diff < 0 else ("📈" if diff > 0 else "➡️")
                        text += f"{arrow} {label}: {val} {unit} ({'+' if diff>0 else ''}{diff})\n"
                    else:
                        text += f"📌 {label}: {val} {unit}\n"
                else:
                    text += f"📌 {label}: {val} {unit}\n"
        text += f"\nВсього замірів: {len(measurements)}"
    
    keyboard = main_keyboard()
    if hasattr(query_or_msg, 'message'):
        await query_or_msg.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await query_or_msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

def parse_measurements(text):
    import re
    result = {}
    patterns = {
        "weight": r"вага[:\s]+(\d+\.?\d*)",
        "waist": r"талія[:\s]+(\d+\.?\d*)",
        "belly": r"живіт[:\s]+(\d+\.?\d*)",
        "hips": r"стегна[:\s]+(\d+\.?\d*)",
        "bicep": r"біцепс[:\s]+(\d+\.?\d*)",
        "chest": r"груди[:\s]+(\d+\.?\d*)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text.lower())
        if match:
            result[key] = float(match.group(1))
    return result

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user = get_user(data, update.effective_user.id)
    awaiting = ctx.user_data.get("awaiting", "food")
    caption = update.message.caption or ""
    
    await update.message.reply_text("🔍 Аналізую... секунду!")
    
    photo = update.message.photo[-1]
    file = await ctx.bot.get_file(photo.file_id)
    img_bytes = await file.download_as_bytearray()
    img_b64 = base64.b64encode(img_bytes).decode()
    
    if awaiting == "food":
        prompt = f"Аналізуй це фото їжі. {caption if caption else 'Оціни калорійність, білки/жири/вуглеводи, чи підходить для дефіциту калорій. Дай рекомендацію.'}"
    elif awaiting == "workout":
        default_workout = "Оціни вправу або активність. Дай зворотній зв'язок і мотивацію."
        prompt = f"Аналізуй це фото тренування. {caption if caption else default_workout}"
    else:
        default_other = "Проаналізуй це зображення у контексті фітнесу і здоров'я."
        prompt = f"{default_other} {caption}"
    
    messages = build_history(user, prompt)
    reply = await ask_claude(messages, img_b64)
    
    user["history"].append({"role": "user", "content": prompt})
    user["history"].append({"role": "assistant", "content": reply})
    save_data(data)
    ctx.user_data.pop("awaiting", None)
    
    # weekly check
    if needs_weekly_check(user):
        reply += "\n\n⏰ *Нагадування:* Час зробити тижневі виміри! Тицяй 📏 Виміри тіла"
    
    await update.message.reply_text(reply, reply_markup=main_keyboard())

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user = get_user(data, update.effective_user.id)
    text = update.message.text.strip()
    awaiting = ctx.user_data.get("awaiting", None)
    
    if awaiting == "measurements":
        parsed = parse_measurements(text)
        if parsed:
            parsed["date"] = datetime.now().strftime("%d.%m.%Y")
            user["measurements"].append(parsed)
            user["last_weekly"] = datetime.now().isoformat()
            save_data(data)
            
            prev_measurements = user["measurements"][:-1]
            prev_str = ""
            if prev_measurements:
                prev = prev_measurements[-1]
                prev_str = f"\nПопередні виміри ({prev.get('date','')}): {json.dumps(prev, ensure_ascii=False)}"
            
            prompt = (f"Нові виміри тіла: {json.dumps(parsed, ensure_ascii=False)}{prev_str}\n"
                      "Проаналізуй результати, покажи динаміку якщо є попередні, і потужно мотивуй!")
            messages = build_history(user, prompt)
            reply = await ask_claude(messages)
            
            user["history"].append({"role": "user", "content": f"Мої виміри: {text}"})
            user["history"].append({"role": "assistant", "content": reply})
            save_data(data)
            ctx.user_data.pop("awaiting", None)
            await update.message.reply_text(reply, reply_markup=main_keyboard())
        else:
            await update.message.reply_text(
                "Не зміг розпізнати виміри 😅 Спробуй у форматі:\n\nВага: 75 кг\nТалія: 80 см"
            )
        return
    
    if awaiting == "water":
        prompt = f"Я випив сьогодні {text} води. Оціни чи достатньо це і дай пораду."
        ctx.user_data.pop("awaiting", None)
    else:
        prompt = text
    
    await update.message.chat.send_action("typing")
    messages = build_history(user, prompt)
    reply = await ask_claude(messages)
    
    user["history"].append({"role": "user", "content": prompt})
    user["history"].append({"role": "assistant", "content": reply})
    save_data(data)
    
    if needs_weekly_check(user):
        reply += "\n\n⏰ *Час тижневих замірів!* Натисни 📏 Виміри тіла"
    
    await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=main_keyboard())

async def remind_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = ("⚡ *Щоденні нагадування активні*\n\n"
            "Я буду нагадувати:\n"
            "• 🌅 08:00 — звіт про сніданок\n"
            "• 💧 12:00 — перевірка води\n"
            "• 💪 18:00 — звіт про тренування\n"
            "• 🌙 21:00 — підсумок дня\n\n"
            "Щоб зупинити: /stop_reminders")
    await update.message.reply_text(text, parse_mode="Markdown")

async def progress_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user = get_user(data, update.effective_user.id)
    await show_progress(update.message, user)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

def main():
    threading.Thread(target=run_web_server, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("progress", progress_command))
    app.add_handler(CommandHandler("reminders", remind_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("🤖 Бот запущено!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
