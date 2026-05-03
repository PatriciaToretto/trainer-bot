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
            "water_today": 0.0,
            "water_date": "",
            "last_weekly": None,
            "registered": datetime.now().isoformat()
        }
    return data[key]

SYSTEM_PROMPT = """Ти — персональний тренер і дієтолог Макс. Суворий але мотивуючий.
Відповідай КОРОТКО — максимум 5-6 речень. Українською мовою.
При аналізі їжі: назви страву, калорії (~), БЖВ одним рядком, оцінка для дефіциту, одна порада.
При тренуванні: оцінка, одна похвала, одна порада.
Завжди закінчуй коротким мотивуючим гаслом! 💪"""

async def ask_claude(messages, image_b64=None, image_type="image/jpeg"):
    content = []
    if image_b64:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": image_type, "data": image_b64}
        })
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
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 400,
                "system": SYSTEM_PROMPT,
                "messages": api_messages
            }
        )
        result = resp.json()
        return result["content"][0]["text"]

def build_history(user, extra_text):
    hist = user["history"][-8:] if len(user["history"]) > 8 else user["history"]
    messages = list(hist)
    messages.append({"role": "user", "content": extra_text})
    return messages

def needs_weekly_check(user):
    if not user["last_weekly"]:
        return True
    last = datetime.fromisoformat(user["last_weekly"])
    return datetime.now() - last >= timedelta(days=7)

def get_today():
    return datetime.now().strftime("%d.%m.%Y")

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🥗 Фото їжі", callback_data="report_food"),
         InlineKeyboardButton("💪 Тренування", callback_data="report_workout")],
        [InlineKeyboardButton("💧 Вода", callback_data="report_water"),
         InlineKeyboardButton("📏 Виміри", callback_data="report_measurements")],
        [InlineKeyboardButton("📊 Прогрес", callback_data="show_progress"),
         InlineKeyboardButton("🔥 Мотивація", callback_data="motivate")]
    ])

def water_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("0.5 л", callback_data="water_0.5"),
         InlineKeyboardButton("1 л", callback_data="water_1.0"),
         InlineKeyboardButton("1.5 л", callback_data="water_1.5")],
        [InlineKeyboardButton("2 л", callback_data="water_2.0"),
         InlineKeyboardButton("2.5 л", callback_data="water_2.5"),
         InlineKeyboardButton("3 л", callback_data="water_3.0")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")]
    ])

def measurements_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚖️ Вага", callback_data="meas_weight"),
         InlineKeyboardButton("📐 Талія", callback_data="meas_waist")],
        [InlineKeyboardButton("🫃 Живіт", callback_data="meas_belly"),
         InlineKeyboardButton("🦵 Стегна", callback_data="meas_hips")],
        [InlineKeyboardButton("💪 Біцепс", callback_data="meas_bicep"),
         InlineKeyboardButton("📋 Зберегти все", callback_data="meas_save")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")]
    ])

MEAS_LABELS = {
    "weight": ("⚖️ Вага", "кг"),
    "waist": ("📐 Талія", "см"),
    "belly": ("🫃 Живіт", "см"),
    "hips": ("🦵 Стегна", "см"),
    "bicep": ("💪 Біцепс", "см"),
}

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user = get_user(data, update.effective_user.id)
    user["name"] = update.effective_user.first_name
    save_data(data)
    text = (
        f"Привіт, {user['name']}! 👊 Я Макс — твій персональний тренер.\n\n"
        "Надсилай фото їжі чи тренувань — одразу аналізую!\n"
        "Або обери дію 👇"
    )
    await update.message.reply_text(text, reply_markup=main_keyboard())

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = load_data()
    user = get_user(data, query.from_user.id)
    action = query.data

    if action == "back_main":
        ctx.user_data.clear()
        await query.message.reply_text("Головне меню 👇", reply_markup=main_keyboard())

    elif action == "report_food":
        ctx.user_data["awaiting"] = "food"
        await query.message.reply_text("📸 Надішли фото їжі — одразу оціню!")

    elif action == "report_workout":
        ctx.user_data["awaiting"] = "workout"
        await query.message.reply_text("💪 Надішли фото тренування або опиши що робила!")

    elif action == "motivate":
        messages = [{"role": "user", "content": "Дай коротку потужну мотивацію! 2-3 речення."}]
        reply = await ask_claude(messages)
        await query.message.reply_text(reply, reply_markup=main_keyboard())

    elif action == "show_progress":
        await show_progress(query, user)

    elif action == "report_water":
        today = get_today()
        if user.get("water_date") != today:
            user["water_today"] = 0.0
            user["water_date"] = today
            save_data(data)
        current = user.get("water_today", 0.0)
        await query.message.reply_text(
            f"💧 Сьогодні випито: *{current} л*\nДодай скільки зараз випила:",
            parse_mode="Markdown",
            reply_markup=water_keyboard()
        )

    elif action.startswith("water_"):
        amount = float(action.split("_")[1])
        today = get_today()
        if user.get("water_date") != today:
            user["water_today"] = 0.0
            user["water_date"] = today
        user["water_today"] = round(user.get("water_today", 0.0) + amount, 1)
        total = user["water_today"]
        save_data(data)
        if total < 1.5:
            status = "Мало! Пий більше 🚨"
        elif total < 2.0:
            status = "Непогано, але ще є куди рости 👍"
        elif total < 2.5:
            status = "Добре! Так тримати ✅"
        else:
            status = "Відмінно! Норму виконано 🏆"
        await query.message.reply_text(
            f"💧 Додано *{amount} л*\nСьогодні всього: *{total} л*\n\n{status}",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

    elif action == "report_measurements":
        if "pending_meas" not in ctx.user_data:
            ctx.user_data["pending_meas"] = {}
        pending = ctx.user_data["pending_meas"]
        text = "📏 *Виміри тіла*\nОбери параметр і введи значення:\n\n"
        for key, (label, unit) in MEAS_LABELS.items():
            val = pending.get(key)
            text += f"{label}: *{val} {unit}*\n" if val else f"{label}: —\n"
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=measurements_keyboard())

    elif action.startswith("meas_") and action != "meas_save":
        key = action.replace("meas_", "")
        if key in MEAS_LABELS:
            label, unit = MEAS_LABELS[key]
            ctx.user_data["awaiting"] = f"meas_{key}"
            await query.message.reply_text(
                f"Введи {label} в {unit} (тільки цифру, наприклад: 65.5):"
            )

    elif action == "meas_save":
        pending = ctx.user_data.get("pending_meas", {})
        if not pending:
            await query.message.reply_text("Ще нічого не введено! Обери параметр вище.")
            return
        pending["date"] = get_today()
        user["measurements"].append(pending)
        user["last_weekly"] = datetime.now().isoformat()
        save_data(data)
        ctx.user_data.pop("pending_meas", None)
        prev_str = ""
        if len(user["measurements"]) > 1:
            prev = user["measurements"][-2]
            prev_str = f" Попередні: {json.dumps(prev, ensure_ascii=False)}"
        prompt = f"Нові виміри: {json.dumps(pending, ensure_ascii=False)}.{prev_str} Коротко: динаміка і мотивація!"
        messages = build_history(user, prompt)
        reply = await ask_claude(messages)
        await query.message.reply_text(reply, reply_markup=main_keyboard())

async def show_progress(query_or_msg, user):
    measurements = user.get("measurements", [])
    today = get_today()
    water = user.get("water_today", 0.0) if user.get("water_date") == today else 0.0
    if not measurements:
        text = "📊 Поки що немає замірів.\n\n"
    else:
        last = measurements[-1]
        text = f"📊 *Виміри* ({last.get('date','')}):\n"
        for key, (label, unit) in MEAS_LABELS.items():
            if key in last:
                val = last[key]
                if len(measurements) > 1:
                    prev = measurements[-2].get(key)
                    if prev:
                        diff = round(val - prev, 1)
                        arrow = "📉" if diff < 0 else ("📈" if diff > 0 else "➡️")
                        sign = "+" if diff > 0 else ""
                        text += f"{arrow} {label}: {val} {unit} ({sign}{diff})\n"
                    else:
                        text += f"📌 {label}: {val} {unit}\n"
                else:
                    text += f"📌 {label}: {val} {unit}\n"
        text += f"\nВсього записів: {len(measurements)}\n\n"
    text += f"💧 Вода сьогодні: *{water} л*"
    keyboard = main_keyboard()
    if hasattr(query_or_msg, 'message'):
        await query_or_msg.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await query_or_msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user = get_user(data, update.effective_user.id)
    awaiting = ctx.user_data.get("awaiting", "food")
    caption = update.message.caption or ""
    await update.message.reply_text("🔍 Секунду...")
    photo = update.message.photo[-1]
    file = await ctx.bot.get_file(photo.file_id)
    img_bytes = await file.download_as_bytearray()
    img_b64 = base64.b64encode(img_bytes).decode()
    if awaiting == "workout":
        default = "Оціни вправу або активність. Коротко."
        prompt = f"Фото тренування. {caption if caption else default}"
    else:
        default = "Назви страву, калорії, БЖВ одним рядком, оцінка для дефіциту, одна порада."
        prompt = f"Фото їжі. {caption if caption else default}"
    messages = build_history(user, prompt)
    reply = await ask_claude(messages, img_b64)
    user["history"].append({"role": "user", "content": prompt})
    user["history"].append({"role": "assistant", "content": reply})
    save_data(data)
    ctx.user_data.pop("awaiting", None)
    if needs_weekly_check(user):
        reply += "\n\n⏰ Час тижневих замірів! Тисни 📏 Виміри"
    await update.message.reply_text(reply, reply_markup=main_keyboard())

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user = get_user(data, update.effective_user.id)
    text = update.message.text.strip()
    awaiting = ctx.user_data.get("awaiting", None)
    if awaiting and awaiting.startswith("meas_"):
        key = awaiting.replace("meas_", "")
        try:
            value = float(text.replace(",", "."))
            if "pending_meas" not in ctx.user_data:
                ctx.user_data["pending_meas"] = {}
            ctx.user_data["pending_meas"][key] = value
            ctx.user_data.pop("awaiting", None)
            label, unit = MEAS_LABELS[key]
            pending = ctx.user_data["pending_meas"]
            status_text = "📏 *Виміри тіла*\nОбери параметр і введи значення:\n\n"
            for k, (lbl, u) in MEAS_LABELS.items():
                val = pending.get(k)
                status_text += f"{lbl}: *{val} {u}*\n" if val else f"{lbl}: —\n"
            await update.message.reply_text(
                f"✅ {label}: {value} {unit} збережено!\n\n{status_text}",
                parse_mode="Markdown",
                reply_markup=measurements_keyboard()
            )
        except ValueError:
            await update.message.reply_text("Введи тільки цифру, наприклад: 65.5")
        return
    await update.message.chat.send_action("typing")
    messages = build_history(user, text)
    reply = await ask_claude(messages)
    user["history"].append({"role": "user", "content": text})
    user["history"].append({"role": "assistant", "content": reply})
    save_data(data)
    if needs_weekly_check(user):
        reply += "\n\n⏰ Час тижневих замірів! Тисни 📏 Виміри"
    await update.message.reply_text(reply, reply_markup=main_keyboard())

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
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("🤖 Бот запущено!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
