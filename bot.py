import os
import re
import sqlite3
import requests
from PIL import Image, ImageEnhance, ImageFilter
import pytesseract
from io import BytesIO

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
CENSUS_API_KEY = os.environ["CENSUS_API_KEY"]

US_MEDIAN_INCOME = 74000
US_MEDIAN_HOME = 320000
US_POPULATION_AVG_ZIP = 10000


# -------------------------
# DATABASE
# -------------------------
conn = sqlite3.connect("history.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    search_number INTEGER,
    message TEXT
)
""")
conn.commit()


def get_next_search_number(user_id: int) -> int:
    cursor.execute(
        "SELECT MAX(search_number) FROM history WHERE user_id=?",
        (user_id,)
    )
    result = cursor.fetchone()[0]
    return (result or 0) + 1


def save_history(user_id: int, search_number: int, message: str):
    cursor.execute(
        "INSERT INTO history (user_id, search_number, message) VALUES (?, ?, ?)",
        (user_id, search_number, message)
    )
    conn.commit()


def get_history(user_id: int):
    cursor.execute("""
        SELECT message
        FROM history
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 10
    """, (user_id,))

    rows = cursor.fetchall()
    rows.reverse()
    return [row[0] for row in rows]


# -------------------------
# ZIP HELPERS
# -------------------------
def get_city_state(zip_code: str):
    try:
        r = requests.get(f"https://api.zippopotam.us/us/{zip_code}", timeout=20)
        if r.status_code != 200:
            return {"city": "Unknown", "state": "Unknown"}

        data = r.json()
        place = data["places"][0]

        return {
            "city": place.get("place name", "Unknown"),
            "state": place.get("state abbreviation", "Unknown"),
        }

    except Exception:
        return {"city": "Unknown", "state": "Unknown"}


def get_zip_stats(zip_code: str):
    variables = ",".join([
        "B19013_001E",
        "B25077_001E",
        "B01003_001E",
        "B17001_002E",
        "B23025_005E",
    ])

    url = (
        "https://api.census.gov/data/2024/acs/acs5"
        f"?get={variables}"
        f"&for=zip%20code%20tabulation%20area:{zip_code}"
        f"&key={CENSUS_API_KEY}"
    )

    try:
        r = requests.get(url, timeout=20)
        if r.status_code != 200:
            return None

        data = r.json()

    except Exception:
        return None

    if not data or len(data) < 2:
        return None

    row = data[1]

    def safe(x):
        try:
            return int(x)
        except:
            return 0

    income = safe(row[0])
    home_value = safe(row[1])
    population = safe(row[2])
    poverty_count = safe(row[3])
    unemployed = safe(row[4])

    poverty_rate = round((poverty_count / population) * 100, 1) if population else 0

    wealth_score = max(0, min(100, round(
        (income / US_MEDIAN_INCOME) * 50 +
        (home_value / US_MEDIAN_HOME) * 30 +
        (population / US_POPULATION_AVG_ZIP) * 20
    )))

    estimated_rent = round(home_value * 0.005) if home_value else 0

    geo = get_city_state(zip_code)

    return {
        "income": income,
        "home_value": home_value,
        "population": population,
        "poverty_rate": poverty_rate,
        "unemployed": unemployed,
        "wealth_score": wealth_score,
        "estimated_rent": estimated_rent,
        "city": geo["city"],
        "state": geo["state"],
    }


def extract_zips(text: str):
    return re.findall(r"\b\d{5}\b", text)


def preprocess_image(image: Image.Image):
    image = image.convert("L")
    image = image.filter(ImageFilter.SHARPEN)
    image = ImageEnhance.Contrast(image).enhance(2.0)
    return image


# -------------------------
# OCR
# -------------------------
async def extract_zips_from_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.message.from_user.id

        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()

        image = Image.open(BytesIO(image_bytes))
        image = preprocess_image(image)

        text = pytesseract.image_to_string(image)
        zips = sorted(set(extract_zips(text)))

        if not zips:
            await update.message.reply_text("No ZIP codes found.")
            return

        results = []

        for z in zips:
            search_number = get_next_search_number(user_id)

            stats = get_zip_stats(z)
            if not stats:
                continue

            msg = (
                f"🔢 Search #{search_number}\n"
                f"📍 ZIP: {z} ({stats['city']}, {stats['state']})\n"
                f"💰 Income: ${stats['income']:,}\n"
                f"🏠 Home: ${stats['home_value']:,}\n"
                f"👥 Pop: {stats['population']:,}\n"
                f"📉 Poverty: {stats['poverty_rate']}%\n"
                f"🚫 Unemp: {stats['unemployed']}\n"
                f"📊 Score: {stats['wealth_score']}/100\n"
                f"🏘 Rent: ${stats['estimated_rent']}/mo"
            )

            results.append(msg)
            save_history(user_id, search_number, msg)

        await update.message.reply_text("\n\n".join(results)[:4000])

    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")


# -------------------------
# TEXT HANDLER
# -------------------------
async def lookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    zip_codes = extract_zips(update.message.text)

    if not zip_codes:
        await update.message.reply_text("Send at least one ZIP code.")
        return

    results = []

    for z in zip_codes:
        search_number = get_next_search_number(user_id)

        stats = get_zip_stats(z)
        if not stats:
            continue

        msg = (
            f"🔢 Search #{search_number}\n"
            f"📍 ZIP: {z} ({stats['city']}, {stats['state']})\n\n"
            f"💰 Income: ${stats['income']:,}\n"
            f"🏠 Home Value: ${stats['home_value']:,}\n"
            f"👥 Population: ${stats['population']:,}\n"
            f"📉 Poverty Rate: {stats['poverty_rate']}%\n"
            f"🚫 Unemployment: {stats['unemployed']:,}\n\n"
            f"📊 Wealth Score: {stats['wealth_score']}/100\n"
            f"🏘 Est. Rent: ${stats['estimated_rent']:,}/mo"
        )

        results.append(msg)
        save_history(user_id, search_number, msg)

    await update.message.reply_text("\n\n".join(results)[:4000])


# -------------------------
# HISTORY
# -------------------------
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    rows = get_history(user_id)

    if not rows:
        await update.message.reply_text("No history yet.")
        return

    await update.message.reply_text("📜 Your recent searches:\n\n" + "\n\n".join(rows))


# -------------------------
# BIN LOOKUP + VBV STATUS
# -------------------------
def get_bin_info(bin_number: str):
    try:
        r = requests.get(
            f"https://lookup.binlist.net/{bin_number}",
            headers={"Accept-Version": "3"},
            timeout=15
        )

        if r.status_code != 200:
            return None

        return r.json()

    except Exception:
        return None


def get_vbv_status(data: dict) -> str:
    """
    Inferred VBV/3DS status (NOT guaranteed).
    """
    country = data.get("country", {})
    bank = data.get("bank", {})

    # binlist sometimes includes "number" / "prepaid" / brand hints only
    prepaid = data.get("prepaid", False)

    scheme = (data.get("scheme") or "").lower()
    type_ = (data.get("type") or "").lower()

    # heuristic-based inference
    if prepaid:
        return "LIKELY NO VBV (Prepaid)"
    if scheme in ["visa", "mastercard"] and type_ in ["debit", "credit"]:
        return "LIKELY VBV ENABLED (3DS supported)"
    if country.get("alpha2") in ["US", "GB", "CA"]:
        return "POSSIBLE VBV (region supported)"
    return "UNKNOWN"


async def bin_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage:\n/bin 45717360")
        return

    bin_number = context.args[0].strip()

    if not bin_number.isdigit() or len(bin_number) < 6:
        await update.message.reply_text("BIN must contain at least 6 digits.")
        return

    data = get_bin_info(bin_number[:8])

    if not data:
        await update.message.reply_text("BIN not found.")
        return

    bank = data.get("bank", {})
    country = data.get("country", {})

    vbv_status = get_vbv_status(data)

    msg = (
        f"💳 BIN: {bin_number[:8]}\n\n"
        f"🌐 Scheme: {data.get('scheme', 'N/A')}\n"
        f"🏷 Brand: {data.get('brand', 'N/A')}\n"
        f"📂 Type: {data.get('type', 'N/A')}\n"
        f"💰 Prepaid: {data.get('prepaid', 'N/A')}\n\n"
        f"🔐 VBV Status: {vbv_status}\n\n"
        f"🏦 Bank: {bank.get('name', 'N/A')}\n"
        f"🌍 Country: {country.get('name', 'N/A')}\n"
        f"💱 Currency: {country.get('currency', 'N/A')}"
    )

    await update.message.reply_text(msg)


# -------------------------
# START
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send ZIP codes or screenshots.\n\n"
        "/history - view past searches\n"
        "/bin <BIN> - lookup BIN + VBV status"
    )


# -------------------------
# MAIN
# -------------------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("bin", bin_lookup))

    app.add_handler(MessageHandler(filters.PHOTO, extract_zips_from_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lookup))

    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
