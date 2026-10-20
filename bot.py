"""
Трекер маркета подарков Telegram.
pip install aiogram==3.*

ENV: BOT_TOKEN, GROUP_ID (id супергруппы с включёнными топиками, вида -100...), ADMIN_ID
     API_ID, API_HASH (с https://my.telegram.org -> API development tools)
Разделы 1,2,3,4 бот создаёт сам командой /setup (написать её в группе от имени админа).
Лоты берутся с официального маркета подарков Telegram через пользовательский аккаунт (Telethon).
pip install "aiogram>=3,<4" -U telethon
"""
import asyncio
import logging
import os
import random
import sqlite3
import time
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

# Получаем параметры из переменных окружения
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
GROUP_ID = int(os.environ["GROUP_ID"])
session_string = os.environ["SESSION_STRING"]

# Считываем токен и параметры из переменных окружения
TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "1.0"))
BACKFILL = int(os.environ.get("BACKFILL", "1"))

# Инициализация клиента через StringSession (для личного аккаунта)
tg = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    # Дальше идет ваш основной код (например, запуск aiogram / циклов)
db = sqlite3.connect("tracker.db", isolation_level=None)
db.execute("""CREATE TABLE IF NOT EXISTS listings(
    id TEXT PRIMARY KEY, name TEXT, link TEXT, owner TEXT,
    price REAL, currency TEXT, section INT, msg_id INT,
    status TEXT DEFAULT 'open', taken_by INT, taken_at TEXT)""")
db.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY)")


def get_topics() -> dict[int, int]:
    return dict(db.execute("SELECT section, thread_id FROM topics").fetchall())

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


def section_of(price: float, currency: str) -> int | None:
    """Строго по цене:
    1: до 1000 зв включительно; 2: от 1000 до 5000 зв; 3: от 5000 зв и выше;
    4: лоты за TON до 10 TON включительно. Лоты за TON дороже 10 TON не публикуются."""
    if currency == "ton":
        return 4 if price <= 10 else None
    if price <= 1000:
        return 1
    if price < 5000:
        return 2
    return 3


def fmt_price(price: float, currency: str) -> str:
    return f"{price:g} TON" if currency == "ton" else f"{price:,.0f} ⭐".replace(",", " ")


async def publish(l: dict):
    """l = {id, name, link, owner, price, currency}"""
    section = section_of(l["price"], l["currency"])
    topics = get_topics()
    if section is None or section not in topics:
        return False
    if db.execute("SELECT 1 FROM listings WHERE id=?", (l["id"],)).fetchone():
        return False  # уже публиковали
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔥 Забрать лог", callback_data=f"take:{l['id']}")
    ]])
    text = (
        f"🎁 <b>{escape(l['name'])}</b>\n"
        f"💰 Цена: <b>{fmt_price(l['price'], l['currency'])}</b>\n"
        f"📂 Раздел {section}\n"
        f"👤 Владелец: {escape(l['owner'])}\n"
        f"🔗 <a href=\"{l['link']}\">Ссылка на подарок</a>"
    )
    while True:
        try:
            m = await bot.send_message(GROUP_ID, text, reply_markup=kb,
                                       message_thread_id=topics[section],
                                       disable_web_page_preview=True)
            break
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
    db.execute("INSERT INTO listings(id,name,link,owner,price,currency,section,msg_id) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (l["id"], l["name"], l["link"], l["owner"], l["price"], l["currency"], section, m.message_id))
    return True


@dp.callback_query(F.data.startswith("take:"))
async def take(cb: CallbackQuery):
    lid = cb.data.split(":", 1)[1]
    uid = cb.from_user.id

    # атомарно: только один человек сможет перевести open -> taken
    cur = db.execute(
        "UPDATE listings SET status='taken', taken_by=?, taken_at=datetime('now') "
        "WHERE id=? AND status='open'", (uid, lid))
    if cur.rowcount == 0:
        return await cb.answer("Этот лог уже забрали", show_alert=True)

    name, link, owner, price, currency, msg_id = db.execute(
        "SELECT name,link,owner,price,currency,msg_id FROM listings WHERE id=?", (lid,)).fetchone()

    # уведомление в личку бота
    try:
        await bot.send_message(
            uid,
            f"✅ <b>Ты взял лог</b>\n\n"
            f"🎁 {escape(name)}\n"
            f"💰 Цена: <b>{fmt_price(price, currency)}</b>\n"
            f"👤 Владелец: {escape(owner)}\n"
            f"🔗 <a href=\"{link}\">Открыть подарок</a>",
            disable_web_page_preview=True)
    except TelegramForbiddenError:
        # пользователь не запускал бота — откатываем, чтобы лог не пропал
        db.execute("UPDATE listings SET status='open', taken_by=NULL, taken_at=NULL WHERE id=?", (lid,))
        me = await bot.get_me()
        return await cb.answer(f"Сначала запусти бота: @{me.username} (/start), потом жми снова",
                               show_alert=True)

    # закрываем в группе: убираем кнопку и помечаем как забранный
    who = f"@{cb.from_user.username}" if cb.from_user.username else escape(cb.from_user.full_name)
    try:
        await cb.message.edit_text(
            f"❌ <s>{escape(name)} — {fmt_price(price, currency)}</s>\n🔒 Лог забрал {who}",
            reply_markup=None)
        # хочешь, чтобы сообщение пропадало совсем — замени строку выше на:
        # await cb.message.delete()
    except TelegramBadRequest:
        pass
    await cb.answer("Лог твой! Детали в личке с ботом")


@dp.message(Command("setup"), F.from_user.id == ADMIN_ID)
async def setup(m: Message):
    """Создаёт в группе разделы 1, 2, 3, 4 (один раз)."""
    if m.chat.id != GROUP_ID:
        return await m.answer("Напиши /setup в самой группе")
    if get_topics():
        return await m.answer("Разделы уже созданы")
    for section in (1, 2, 3, 4):
        t = await bot.create_forum_topic(GROUP_ID, name=str(section))
        db.execute("INSERT INTO topics(section, thread_id) VALUES(?,?)", (section, t.message_thread_id))
    await m.answer("Разделы 1–4 созданы ✅")


@dp.message(Command("topics"), F.from_user.id == ADMIN_ID)
async def topics_info(m: Message):
    t = get_topics()
    await m.answer("Разделы в базе: " + (", ".join(f"{k}→{v}" for k, v in sorted(t.items())) or "нет, сделай /setup"))


@dp.message(CommandStart())
async def start(m: Message):
    db.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (m.from_user.id,))
    await m.answer("Готово ✅ Сюда будут приходить только уведомления о взятых логах.")


@dp.message(Command("id"))
async def get_id(m: Message):
    await m.answer(f"id чата: {m.chat.id}\nтвой id: {m.from_user.id}")


@dp.message(Command("fake"), F.from_user.id == ADMIN_ID)
async def fake(m: Message):
    """Тест без источника данных: /fake публикует случайный лог."""
    price, cur = random.choice([(300, "stars"), (2500, "stars"), (7000, "stars"), (8, "ton")])
    n = random.randint(1000, 99999)
    await publish(dict(id=f"test{n}", name=f"Plush Pepe #{n}", link=f"https://t.me/nft/PlushPepe-{n}",
                       owner="@someone", price=price, currency=cur))


_gift_ids: list[int] = []
_gift_ids_at = 0.0
_baseline_done = False


_raw_logged = 0


def price_of(g):
    """Цена лота: для лотов только за TON берём TON, иначе приоритет звёзды."""
    global _raw_logged
    stars = ton = None
    for a in (getattr(g, "resell_amount", None) or []):
        name = type(a).__name__
        if name == "StarsAmount":
            stars = a.amount
        elif name == "StarsTonAmount":
            ton = a.amount / 1e9  # нано-TON -> TON
    if _raw_logged < 5:  # для отладки: как выглядит цена в сыром виде
        _raw_logged += 1
        logging.info("цена сырая: %s | ton_only=%s", getattr(g, "resell_amount", None),
                     getattr(g, "resale_ton_only", None))
    if getattr(g, "resale_ton_only", False) and ton is not None:
        return float(ton), "ton"
    if stars is not None:
        return float(stars), "stars"
    if ton is not None:
        return float(ton), "ton"
    return None


def owner_of(g, users: dict) -> str:
    uid = getattr(getattr(g, "owner_id", None), "user_id", None)
    if uid and uid in users:
        u = users[uid]
        return f"@{u.username}" if u.username else (u.first_name or f"id{uid}")
    if uid:
        return f"id{uid}"
    return getattr(g, "owner_name", None) or getattr(g, "owner_address", None) or "скрыт"


async def refresh_gift_ids():
    """Список коллекций, у которых есть лоты на перепродаже (обновляем раз в 10 минут)."""
    global _gift_ids, _gift_ids_at
    if _gift_ids and time.time() - _gift_ids_at < 600:
        return
    res = await tg(functions.payments.GetStarGiftsRequest(hash=0))
    all_gifts = getattr(res, "gifts", [])
    _gift_ids = [g.id for g in all_gifts if getattr(g, "availability_resale", 0)]
    if not _gift_ids:  # если поле не пришло, проверяем все коллекции
        _gift_ids = [g.id for g in all_gifts]
    _gift_ids_at = time.time()
    logging.info("коллекций с лотами: %d", len(_gift_ids))


async def fetch_collection(gid: int) -> list[dict]:
    """Лоты одной коллекции, самые свежие первыми."""
    res = await tg(functions.payments.GetResaleStarGiftsRequest(gift_id=gid, offset="", limit=30))
    users = {u.id: u for u in res.users}
    out = []
    for g in res.gifts:
        p = price_of(g)
        slug = getattr(g, "slug", None)
        if not p or not slug:
            continue
        price, cur = p
        out.append(dict(id=f"{slug}:{cur}:{price:g}", name=f"{g.title} #{g.num}",
                        link=f"https://t.me/nft/{slug}", owner=owner_of(g, users),
                        price=price, currency=cur, rank=len(out)))
    return out


async def poller():
    """Бесконечно обходит коллекции и публикует лоты сразу, не дожидаясь конца круга."""
    first_round = True
    while True:
        try:
            await refresh_gift_ids()
            ids = list(_gift_ids)
            total = posted = 0
            for n, gid in enumerate(ids, 1):
                try:
                    items = await fetch_collection(gid)
                except FloodWaitError as e:
                    logging.info("Telegram просит подождать %s сек", e.seconds)
                    await asyncio.sleep(e.seconds + 1)
                    continue
                except Exception:
                    logging.exception("ошибка коллекции %s", gid)
                    continue
                total += len(items)
                for l in items:
                    if first_round and l["rank"] >= BACKFILL:
                        # при первом круге старые лоты только запоминаем
                        db.execute("INSERT OR IGNORE INTO listings(id,name,link,owner,price,currency,section,status) "
                                   "VALUES(?,?,?,?,?,?,?,'seen')",
                                   (l["id"], l["name"], l["link"], l["owner"], l["price"], l["currency"],
                                    section_of(l["price"], l["currency"]) or 0))
                        continue
                    try:
                        if await publish(l):
                            posted += 1
                            logging.info("опубликован: %s — %s → раздел %s", l["name"],
                                         fmt_price(l["price"], l["currency"]),
                                         section_of(l["price"], l["currency"]))
                    except Exception:
                        logging.exception("не удалось опубликовать %s", l["name"])
                if n % 20 == 0:
                    logging.info("проверено коллекций: %d/%d", n, len(ids))
                await asyncio.sleep(REQUEST_DELAY)
            logging.info("круг завершён: лотов в выдаче %d, опубликовано %d", total, posted)
            first_round = False
        except Exception:
            logging.exception("poller error")
            await asyncio.sleep(5)


async def main():
    logging.basicConfig(level=logging.INFO)
    
    # Безопасно подключаем клиент через StringSession без интерактивного ввода
    await tg.start()
    
    # Проверяем, удалось ли авторизоваться
    if not await tg.is_user_authorized():
        print("=" * 50)
        print("ОШИБКА АВТОРИЗАЦИИ: Строковая сессия недействительна!")
        print(f"Текущее значение SESSION_STRING: '{os.environ.get('SESSION_STRING')}'")
        print("=" * 50)
        return

    print("Успешная авторизация в Telegram по строковой сессии!")

    # Запускаем фоновые задачи
    asyncio.create_task(poller())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
