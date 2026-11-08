import os
import re
import json
import html
import time
import logging
import asyncio
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    ChatJoinRequest,
    ChatMemberUpdated,
    LabeledPrice,
    PreCheckoutQuery,
)
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramRetryAfter,
    TelegramForbiddenError,
)

# ================= SOZLAMALAR =================
def _load_env_fallback():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k not in os.environ:
                        os.environ[k] = v

_load_env_fallback()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "8576487218,7209553265")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()]
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "@UzbekchaHentailar")
DB_FILE = "database.json"
SUB_CACHE_TTL = 300  # Obunani keshda saqlash vaqti (soniya) - 5 daqiqa
# ==============================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

from supabase_db import SupabaseDB, DBProxy, SupabaseFSMStorage

sdb = SupabaseDB()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=SupabaseFSMStorage(sdb))

# Tezkor qidiruv va keshlar
user_sub_cache: dict[int, float] = {}  # {user_id: expire_timestamp}
pending_movies: dict[int, str] = {}    # {user_id: requested_movie_code}
join_requests_sets: dict[str, set[int]] = sdb.join_requests_sets
db = DBProxy(sdb)
db_dirty = False


# ================= MA'LUMOTLAR BAZASI =================
def is_valid_chat_id(cid) -> bool:
    """chat_id haqiqiy -100... raqam yoki @username ekanini tekshiradi."""
    if isinstance(cid, int):
        return True
    s = str(cid).strip()
    if s.lstrip("-").isdigit():
        return True
    if s.startswith("@") and "/" not in s and "+" not in s and len(s) > 3:
        return True
    return False


def extract_movie_code(text: str | None) -> str | None:
    """Matndan film raqamini ajratib oladi (#1, #01, 1, 1-qism va h.k.)."""
    if not text:
        return None
    # 1. #1, #01, # 15 formatlari
    m = re.search(r"#\s*(\d+)", text)
    if m:
        return str(int(m.group(1)))
    # 2. Faqat raqam yoki raqam-qism: "1", " 25 ", "1-qism", "1 qism"
    m = re.match(r"^\s*#?(\d+)(?:-?qism|-?kino)?\s*$", text, re.IGNORECASE)
    if m:
        return str(int(m.group(1)))
    return None


async def init_app():
    """Bot va Supabase ma'lumotlarini yuklash."""
    global join_requests_sets
    if not sdb._initialized:
        await sdb.load_all()
        join_requests_sets = sdb.join_requests_sets


def is_vip_user(user_id: int) -> bool:
    """Foydalanuvchi VIP (Stars orqali Premium olgan yoki Admin) ekanligini tekshiradi."""
    return sdb.is_vip(user_id, ADMIN_IDS)


def set_vip_user(user_id: int, payment_info: dict = None):
    """Foydalanuvchiga VIP maqomini berish."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(sdb.set_vip_user(user_id, payment_info))
    except RuntimeError:
        pass
    user_sub_cache[user_id] = time.time() + 86400 * 365


async def save_db(immediate: bool = False):
    """Bazani asinxron saqlash."""
    sdb.save_local_backup()


async def db_auto_save_worker():
    """Har 10 soniyada bir marta bazani diskka zaxira yozuvchi fon jarayoni."""
    while True:
        try:
            await asyncio.sleep(10)
            sdb.save_local_backup()
        except asyncio.CancelledError:
            sdb.save_local_backup()
            break
        except Exception:
            pass


def db_dirty_flag_set():
    pass


def save_user(user: types.User, is_message: bool = False):
    """Foydalanuvchining to'liq ma'lumotlarini bazada saqlash va yangilash."""
    if not user:
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(sdb.save_user(user, is_message))
    except RuntimeError:
        pass


# ================= HOLATLAR (FSM) =================
class Form(StatesGroup):
    waiting_for_channel_id = State()
    waiting_for_invite_link = State()
    waiting_for_channel_name = State()

    waiting_for_media = State()
    waiting_for_text = State()
    waiting_for_btn_text = State()
    waiting_for_btn_link = State()


def normalize_url(raw_link: str) -> str:
    link = raw_link.strip()
    if link.startswith("@"):
        return f"https://t.me/{link[1:]}"
    if link.startswith("https://t.me+"):
        return link.replace("https://t.me+", "https://t.me/+", 1)
    if link.startswith("t.me+"):
        return link.replace("t.me+", "https://t.me/+", 1)
    if link.startswith("t.me/"):
        return f"https://{link}"
    if link.startswith(("http://", "https://", "tg://")):
        return link
    return f"https://t.me/{link}"


# ================= KLAVIATURALAR =================
def get_admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📢 Majburiy obuna sozlash"),
                KeyboardButton(text="📝 Post yaratish"),
            ],
            [
                KeyboardButton(text="🎬 Kinolar bazasi"),
                KeyboardButton(text="📊 Statistika"),
            ],
            [
                KeyboardButton(text="🔍 Obunani sinash"),
            ],
        ],
        resize_keyboard=True,
    )


def get_cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Bekor qilish")]],
        resize_keyboard=True,
    )


def get_channel_picker_keyboard() -> ReplyKeyboardMarkup:
    """Telegramning o'zidan kanalni tanlash tugmasi."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="📂 Kanalni ro'yxatdan tanlash",
                    request_chat=KeyboardButtonRequestChat(
                        request_id=1,
                        chat_is_channel=True,
                    ),
                )
            ],
            [KeyboardButton(text="❌ Bekor qilish")],
        ],
        resize_keyboard=True,
    )


def get_channels_management_keyboard() -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton(text=f"❌ {ch['name']}", callback_data=f"del_ch_{idx}")]
        for idx, ch in enumerate(db["channels"])
    ]
    inline_keyboard.append(
        [InlineKeyboardButton(text="➕ Yangi kanal qo'shish", callback_data="add_new_channel")]
    )
    inline_keyboard.append(
        [InlineKeyboardButton(text="❌ Menyuni yopish", callback_data="close_admin_menu")]
    )
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def get_link_generation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔒 Zayavkali link yaratish (Avto)", callback_data="gen_link_req")],
            [InlineKeyboardButton(text="🔓 Oddiy link yaratish (Avto)", callback_data="gen_link_std")],
        ]
    )


def get_user_sub_keyboard(channels_to_show: list) -> InlineKeyboardMarkup:
    inline_keyboard = [
        [InlineKeyboardButton(text=f"📢 {ch['name']}", url=ch["link"])]
        for ch in channels_to_show
    ]
    inline_keyboard.append(
        [InlineKeyboardButton(text="⭐ Premium Obuna (11 ⭐️ Stars)", callback_data="buy_premium")]
    )
    inline_keyboard.append(
        [InlineKeyboardButton(text="🔄 Obunani tekshirish", callback_data="check_sub")]
    )
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


# ================= ZAYAVKA VA OBUNA NAZORATI =================
@dp.chat_join_request()
async def handle_join_request(update: ChatJoinRequest):
    """Yopiq kanalga zayavka tashlagan foydalanuvchini bazaga saqlaydi."""
    chat_key = str(update.chat.id)
    user_id = update.from_user.id

    save_user(update.from_user)

    if chat_key not in join_requests_sets:
        join_requests_sets[chat_key] = set()
        db["join_requests"][chat_key] = []

    if user_id not in join_requests_sets[chat_key]:
        join_requests_sets[chat_key].add(user_id)
        db["join_requests"][chat_key].append(user_id)
        db_dirty_flag_set()
        logging.info(f"Yangi zayavka saqlandi: user={user_id}, chat={chat_key}")


@dp.chat_member()
async def handle_chat_member_update(event: ChatMemberUpdated):
    """Foydalanuvchi kanaldan chiqib ketsa, zayavkalar ro'yxatidan va keshdan o'chiradi."""
    user = event.new_chat_member.user
    if not user:
        return

    save_user(user)
    user_id = user.id
    user_sub_cache.pop(user_id, None)

    if event.new_chat_member.status in ("left", "kicked"):
        chat_key = str(event.chat.id)
        if chat_key in join_requests_sets and user_id in join_requests_sets[chat_key]:
            join_requests_sets[chat_key].discard(user_id)
            if user_id in db["join_requests"].get(chat_key, []):
                db["join_requests"][chat_key].remove(user_id)
            db_dirty_flag_set()


async def get_unsubscribed_channels(user_id: int, force_refresh: bool = False) -> list:
    """Foydalanuvchi obuna bo'lmagan kanallar ro'yxatini parallel va kesh bilan tekshiradi."""
    # VIP a'zolar hech qanday kanalga a'zo bo'lishi shart emas!
    if is_vip_user(user_id):
        return []

    now = time.time()

    if not force_refresh and user_id in user_sub_cache and user_sub_cache[user_id] > now:
        return []

    if not db["channels"]:
        return []

    async def check_single_channel(ch: dict):
        chat_id = ch["chat_id"]
        chat_key = str(chat_id)

        if user_id in join_requests_sets.get(chat_key, set()):
            return None

        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in ("member", "administrator", "creator"):
                return None
            if member.status == "restricted" and getattr(member, "is_member", False):
                return None
            if member.status == "kicked":
                return ch

            return ch
        except Exception as e:
            logging.error(f"Obuna tekshirishda xatolik (chat_id={chat_id}, user={user_id}): {e}")
            if user_id in join_requests_sets.get(chat_key, set()):
                return None
            return ch

    results = await asyncio.gather(*(check_single_channel(ch) for ch in db["channels"]))
    unsubscribed = [ch for ch in results if ch is not None]

    if not unsubscribed:
        user_sub_cache[user_id] = now + SUB_CACHE_TTL
    else:
        user_sub_cache.pop(user_id, None)

    return unsubscribed


# ================= KANAL POSTLARI VA AVTO-INDEKSLASH =================
@dp.my_chat_member()
async def my_chat_member_handler(event: types.ChatMemberUpdated):
    """Bot yangi kanalga admin qilib qo'shilganda kanal ID sini avtomatik eslab qoladi."""
    if event.chat.type in ("channel", "supergroup"):
        if event.new_chat_member.status == "administrator":
            db["movie_channel_id"] = event.chat.id
            db_dirty_flag_set()
            logging.info(f"Bot yangi kanalga admin bo'ldi: {event.chat.title} ({event.chat.id})")


@dp.channel_post()
async def channel_post_handler(message: types.Message):
    """Kanalga yangi video/post yuklanganda #raqam bo'yicha darhol bazaga oladi."""
    ch_id = message.chat.id
    caption = message.caption or message.text
    code = extract_movie_code(caption)
    if code:
        file_id = message.video.file_id if message.video else (message.document.file_id if message.document else None)
        media_type = "video" if message.video else ("document" if message.document else "text")

        db["movie_channel_id"] = ch_id
        db["movies"][code] = {
            "channel_id": ch_id,
            "message_id": message.message_id,
            "file_id": file_id,
            "caption": caption,
            "media_type": media_type,
        }
        db_dirty_flag_set()
        logging.info(f"Yangi film kanaldan avtomatik saqlandi: #{code} (msg_id={message.message_id})")


@dp.edited_channel_post()
async def edited_channel_post_handler(message: types.Message):
    """Kanaldagi post tahrirlanganda yangilanadi."""
    ch_id = message.chat.id
    caption = message.caption or message.text
    code = extract_movie_code(caption)
    if code:
        file_id = message.video.file_id if message.video else (message.document.file_id if message.document else None)
        media_type = "video" if message.video else ("document" if message.document else "text")

        db["movie_channel_id"] = ch_id
        db["movies"][code] = {
            "channel_id": ch_id,
            "message_id": message.message_id,
            "file_id": file_id,
            "caption": caption,
            "media_type": media_type,
        }
        db_dirty_flag_set()
        logging.info(f"Film posti tahrirlandi: #{code}")


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def group_movie_post_handler(message: types.Message):
    """Guruhga yangi video/post yuklanganda #raqam bo'yicha darhol bazaga oladi."""
    ch_id = message.chat.id
    caption = message.caption or message.text
    code = extract_movie_code(caption)
    if code:
        file_id = message.video.file_id if message.video else (message.document.file_id if message.document else None)
        media_type = "video" if message.video else ("document" if message.document else "text")

        db["movie_channel_id"] = ch_id
        db["movies"][code] = {
            "channel_id": ch_id,
            "message_id": message.message_id,
            "file_id": file_id,
            "caption": caption,
            "media_type": media_type,
        }
        db_dirty_flag_set()
        await save_db(immediate=True)
        logging.info(f"Guruhdan yangi film avtomatik saqlandi: #{code} (file_id={'bor' if file_id else 'yoq'})")

        # Guruhdagi postga darhol olov (🔥) reaksiyasini qo'yamiz (admin saqlanganini bilishi uchun)
        try:
            await message.react([types.ReactionTypeEmoji(emoji="🔥")])
        except Exception:
            pass



async def send_movie_to_user(chat_id: int, movie: dict):
    """Filmni foydalanuvchiga 100% toza, hech qanday yuboruvchi yoki guruh nomisiz (bot nomidan) yuboradi.
    protect_content=True orqali videoni boshqalarga forward qilish, galereyaga saqlash va skrinshot olish cheklanadi."""
    file_id = movie.get("file_id")
    caption = movie.get("caption")
    media_type = movie.get("media_type", "video")

    # send_video / send_document to'g'ridan-to'g'ri bot tomonidan yangi xabar qilib yuboriladi.
    # Natijada yuboruvchining ismi, akkaunti va rasmi UMUMAN KO'RINMAYDI!
    if file_id:
        if media_type == "video":
            await bot.send_video(
                chat_id=chat_id,
                video=file_id,
                caption=caption,
                protect_content=True,
            )
            return
        elif media_type == "document":
            await bot.send_document(
                chat_id=chat_id,
                document=file_id,
                caption=caption,
                protect_content=True,
            )
            return

    # Agar file_id saqlanmagan bo'lsa, zaxira sifatida copy_message
    await bot.copy_message(
        chat_id=chat_id,
        from_chat_id=movie["channel_id"],
        message_id=movie["message_id"],
        protect_content=True,
    )




# ================= ADMIN PANEL =================
@dp.message(Command("start"), F.from_user.id.in_(ADMIN_IDS))
async def admin_start(message: types.Message, state: FSMContext):
    await state.clear()
    save_user(message.from_user, is_message=True)

    total_users = len(db.get("users", {}))
    total_channels = len(db["channels"])
    total_requests = sum(len(reqs) for reqs in db["join_requests"].values())
    total_movies = len(db.get("movies", {}))
    vip_count = len(db.get("vip_users", {}))
    total_stars = vip_count * 11

    await message.answer(
        "👋 <b>Admin panelga xush kelibsiz!</b>\n\n"
        f"📢 Ulangan majburiy kanallar: <b>{total_channels} ta</b>\n"
        f"🎬 Bazadagi filmlar soni: <b>{total_movies} ta</b>\n"
        f"👥 Foydalanuvchilar: <b>{total_users} ta</b>\n"
        f"⭐ Stars VIP obunachilar: <b>{vip_count} ta ({total_stars} ⭐️)</b>\n"
        f"📥 Jami zayavkalar: <b>{total_requests} ta</b>",
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML",
    )


@dp.message(F.text == "🎬 Kinolar bazasi", F.from_user.id.in_(ADMIN_IDS))
async def admin_movies_database_menu(message: types.Message):
    movie_ch = db.get("movie_channel_id")
    movies = db.get("movies", {})
    movies_count = len(movies)
    ch_info = f"<code>{movie_ch}</code>" if movie_ch else "<i>Ulanmagan (quyidagi usulda ulang)</i>"

    sorted_codes = sorted(movies.keys(), key=lambda x: int(x) if x.isdigit() else 9999)
    movie_list_str = ", ".join(f"#{c}" for c in sorted_codes) if sorted_codes else "<i>Hozircha bo'sh</i>"

    msg = (
        "🎬 <b>Kinolar Bazasi Boshqaruvi</b>\n\n"
        f"📌 <b>Kino kanali/guruhi ID:</b> {ch_info}\n"
        f"📼 <b>Bazadagi filmlar soni:</b> <b>{movies_count} ta</b>\n"
        f"📋 <b>Mavjud filmlar:</b> {movie_list_str}\n\n"
        "<b>Filmlarni ulash va saqlash:</b>\n"
        "1️⃣ <b>Guruhga video tashlash:</b> Guruhga #1, #2 kabi post tashlasangiz, bot uni darhol bazaga saqlab, 🔥 olov reaksiyasini qo'yadi.\n"
        "2️⃣ <b>Eski filmlarni skanerlash:</b> <code>/scan 1 50</code> buyrug'i orqali kanaldagi postlarni bir zumda bazaga olishingiz mumkin.\n"
        "3️⃣ <b>Sinab ko'rish:</b> Istalgan film raqamini (masalan: <b>1</b> yoki <b>2</b>) botga yozsangiz, bot sizga filmni tashlab beradi!"
    )
    await message.answer(msg, parse_mode="HTML")



@dp.message(F.forward_from_chat, F.from_user.id.in_(ADMIN_IDS))
async def admin_forward_handler(message: types.Message):
    """Admin kino kanalidan xabar forward qilganda kanalni va filmni saqlab oladi."""
    ch_id = message.forward_from_chat.id
    db["movie_channel_id"] = ch_id
    caption = message.caption or message.text
    code = extract_movie_code(caption)

    original_msg_id = message.forward_from_message_id or message.message_id
    file_id = message.video.file_id if message.video else (message.document.file_id if message.document else None)
    media_type = "video" if message.video else ("document" if message.document else "text")

    if code:
        db["movies"][code] = {
            "channel_id": ch_id,
            "message_id": original_msg_id,
            "file_id": file_id,
            "caption": caption,
            "media_type": media_type,
        }
        db_dirty_flag_set()
        await message.answer(
            f"✅ <b>#{code}</b> raqamli film muvaffaqiyatli saqlandi!\n"
            f"Kanal: <b>{html.escape(message.forward_from_chat.title or '')}</b> (ID: <code>{ch_id}</code>)",
            parse_mode="HTML",
        )
    else:
        db_dirty_flag_set()
        await message.answer(
            f"🎬 Kino kanali belgilandi:\n"
            f"📌 <b>{html.escape(message.forward_from_chat.title or '')}</b> (ID: <code>{ch_id}</code>)\n\n"
            f"💡 Endi kanaldagi filmlarni <code>/scan 1 150</code> buyrug'i bilan avtomatik bazaga olishingiz yoki videolarni forward qilishingiz mumkin.",
            parse_mode="HTML",
        )


@dp.message(Command("scan"), F.from_user.id.in_(ADMIN_IDS))
async def admin_scan_channel(message: types.Message):
    """Kanaldagi mavjud postlarni avtomatik skanerlab bazaga oladi."""
    channel_id = db.get("movie_channel_id")
    if not channel_id:
        await message.answer(
            "⚠️ Avval kino kanalidan bitta videoni botga forward qiling, shunda bot kanalni taniydi!",
            parse_mode="HTML",
        )
        return

    args = message.text.split()
    start_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
    end_id = int(args[2]) if len(args) > 2 and args[2].isdigit() else (start_id + 100)

    status_msg = await message.answer(
        f"⏳ <b>Kanal skanerlanmoqda ({start_id} dan {end_id} gacha)...</b>\nIltimos, kuting...",
        parse_mode="HTML",
    )

    found_count = 0
    for msg_id in range(start_id, end_id + 1):
        try:
            fwd = await bot.forward_message(
                chat_id=message.chat.id,
                from_chat_id=channel_id,
                message_id=msg_id,
            )
            caption = fwd.caption or fwd.text
            code = extract_movie_code(caption)
            if code:
                file_id = fwd.video.file_id if fwd.video else (fwd.document.file_id if fwd.document else None)
                media_type = "video" if fwd.video else ("document" if fwd.document else "text")

                db["movies"][code] = {
                    "channel_id": channel_id,
                    "message_id": msg_id,
                    "file_id": file_id,
                    "caption": caption,
                    "media_type": media_type,
                }
                found_count += 1
                db_dirty_flag_set()

            await bot.delete_message(chat_id=message.chat.id, message_id=fwd.message_id)
            await asyncio.sleep(0.04)
        except Exception:
            pass

    await status_msg.edit_text(
        f"✅ <b>Skanerlash yakunlandi!</b>\n\n"
        f"📥 Topilgan va bazaga qo'shilgan filmlar: <b>{found_count} ta</b>\n"
        f"📼 Jami filmlar bazasi: <b>{len(db.get('movies', {}))} ta</b>",
        parse_mode="HTML",
    )


@dp.message(Command("delmovie"), F.from_user.id.in_(ADMIN_IDS))
async def admin_del_movie(message: types.Message):
    args = message.text.split()
    if len(args) > 1:
        code = extract_movie_code(args[1])
        if code and code in db.get("movies", {}):
            del db["movies"][code]
            db_dirty_flag_set()
            await message.answer(f"✅ #{code} raqamli film bazadan o'chirildi.")
            return
    await message.answer("Format: /delmovie <raqam> (Masalan: /delmovie 1)")


@dp.message(F.text == "📊 Statistika", F.from_user.id.in_(ADMIN_IDS))
async def admin_show_stats(message: types.Message):
    users = db.get("users", {})
    total_users = len(users)

    premium_users = sum(1 for u in users.values() if u.get("is_premium"))
    usernames_count = sum(1 for u in users.values() if u.get("username"))

    today_str = datetime.now().strftime("%Y-%m-%d")
    today_active = sum(1 for u in users.values() if str(u.get("last_active", "")).startswith(today_str))

    total_channels = len(db["channels"])
    total_requests = sum(len(reqs) for reqs in db["join_requests"].values())
    total_movies = len(db.get("movies", {}))
    vip_count = len(db.get("vip_users", {}))
    total_stars = vip_count * 11

    msg = (
        "📊 <b>Bot Statistikasi</b>\n\n"
        f"👥 <b>Jami foydalanuvchilar:</b> {total_users} ta\n"
        f"⚡️ <b>Bugun faol bo'lganlar:</b> {today_active} ta\n"
        f"⭐ <b>Telegram Premium egalari:</b> {premium_users} ta\n"
        f"🌟 <b>Stars VIP (11⭐️) a'zolar:</b> {vip_count} ta ({total_stars} Stars)\n"
        f"🔖 <b>Username'ga egalar:</b> {usernames_count} ta\n\n"
        f"🎬 <b>Bazadagi filmlar soni:</b> {total_movies} ta\n"
        f"📢 <b>Majburiy kanallar soni:</b> {total_channels} ta\n"
        f"📥 <b>Kanallardagi jami zayavkalar:</b> {total_requests} ta\n"
    )
    await message.answer(msg, parse_mode="HTML")


@dp.message(F.text == "❌ Bekor qilish", F.from_user.id.in_(ADMIN_IDS))
async def admin_cancel_action(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Amal bekor qilindi.", reply_markup=get_admin_keyboard())


@dp.message(F.text == "🔍 Obunani sinash", F.from_user.id.in_(ADMIN_IDS))
async def admin_test_sub(message: types.Message):
    if not db["channels"]:
        await message.answer(
            "⚠️ <b>Hozircha hech qanday majburiy kanal qo'shilmagan!</b>\n"
            "«📢 Majburiy obuna sozlash» tugmasi orqali kanal qo'shing.",
            parse_mode="HTML",
        )
        return

    unsubscribed = await get_unsubscribed_channels(message.from_user.id, force_refresh=True)
    if not unsubscribed:
        await message.answer(
            f"✅ Tekshiruv a'lo ishladi! Siz barcha ({len(db['channels'])} ta) kanalda a'zosiz."
        )
    else:
        await message.answer(
            "Bot oddiy foydalanuvchiga quyidagicha ko'rinadi:",
            reply_markup=get_user_sub_keyboard(unsubscribed),
        )


def build_channels_text() -> str:
    txt = "📢 <b>Majburiy obuna kanallarini boshqarish</b>\n\n"
    if not db["channels"]:
        txt += "Hozircha majburiy obuna kanallari qo'shilmagan."
    else:
        txt += "Joriy kanallar ro'yxati (O'chirish uchun ustiga bosing):\n"
        for idx, ch in enumerate(db["channels"], start=1):
            safe_name = html.escape(str(ch["name"]))
            safe_id = html.escape(str(ch["chat_id"]))
            txt += f"{idx}. {safe_name} (ID: <code>{safe_id}</code>)\n"
    return txt


@dp.message(F.text == "📢 Majburiy obuna sozlash", F.from_user.id.in_(ADMIN_IDS))
async def admin_channels_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        build_channels_text(),
        reply_markup=get_channels_management_keyboard(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("del_ch_"), F.from_user.id.in_(ADMIN_IDS))
async def admin_delete_channel(call: types.CallbackQuery):
    idx = int(call.data.split("_")[2])
    try:
        removed = db["channels"].pop(idx)
        user_sub_cache.clear()
        await save_db(immediate=True)
        await call.answer(f"{removed['name']} o'chirildi!", show_alert=True)
        await call.message.edit_text(
            build_channels_text(),
            reply_markup=get_channels_management_keyboard(),
            parse_mode="HTML",
        )
    except IndexError:
        await call.answer("Kanal topilmadi.", show_alert=True)


@dp.callback_query(F.data == "close_admin_menu", F.from_user.id.in_(ADMIN_IDS))
async def close_admin_menu(call: types.CallbackQuery):
    await call.message.delete()
    await call.message.answer("Menyu yopildi.", reply_markup=get_admin_keyboard())


# --- Yangi kanal qo'shish ---
@dp.callback_query(F.data == "add_new_channel", F.from_user.id.in_(ADMIN_IDS))
async def admin_add_channel_start(call: types.CallbackQuery, state: FSMContext):
    await call.message.delete()
    await state.set_state(Form.waiting_for_channel_id)
    await call.message.answer(
        "<b>1-qadam: Kanalni aniqlash</b>\n\n"
        "Pastdagi <b>«📂 Kanalni ro'yxatdan tanlash»</b> tugmasini bosing (eng oson yo'li)\n"
        "yoki kanalning <code>-100...</code> ID raqamini / <code>@username</code> ini yozing:\n\n"
        "<i>❗️ Diqqat: Bu yerga https://t.me/+... taklif havolasini yubormang! Havola keyingi qadamda so'raladi.</i>",
        reply_markup=get_channel_picker_keyboard(),
        parse_mode="HTML",
    )


@dp.message(Form.waiting_for_channel_id, F.from_user.id.in_(ADMIN_IDS))
async def admin_get_channel_id(message: types.Message, state: FSMContext):
    target_id = None

    if message.chat_shared:
        target_id = message.chat_shared.chat_id
    elif message.forward_from_chat:
        target_id = message.forward_from_chat.id
    elif message.text:
        raw = message.text.strip()
        if "+" in raw or "joinchat" in raw.lower():
            await message.answer(
                "❌ <b>Xato! Bu yopiq taklif havolasi.</b>\n"
                "Telegram taklif havolasidan kanal ID raqamini aniqlay olmaydi.\n\n"
                "👉 Pastdagi <b>«📂 Kanalni ro'yxatdan tanlash»</b> tugmasini bosing — bot ID ni o'zi oladi!",
                reply_markup=get_channel_picker_keyboard(),
                parse_mode="HTML",
            )
            return

        m = re.match(r"^(?:https?://)?t\.me/([a-zA-Z0-9_]{4,})/?$", raw)
        if m:
            target_id = f"@{m.group(1)}"
        elif raw.lstrip("-").isdigit():
            target_id = int(raw)
        elif raw.startswith("@"):
            target_id = raw
        else:
            await message.answer(
                "❌ Noto'g'ri format! Pastdagi <b>«📂 Kanalni ro'yxatdan tanlash»</b> tugmasini bosing "
                "yoki <code>-100...</code> ID / <code>@username</code> yuboring:",
                parse_mode="HTML",
            )
            return
    else:
        await message.answer("Pastdagi «📂 Kanalni ro'yxatdan tanlash» tugmasidan foydalaning:")
        return

    try:
        chat_info = await bot.get_chat(target_id)
        bot_member = await bot.get_chat_member(chat_info.id, bot.id)
        if bot_member.status != "administrator":
            await message.answer(
                f"❌ Bot <b>{html.escape(chat_info.title or '')}</b> kanalida Admin emas!\n"
                "Avval botni kanalga Admin qiling va qayta tanlang:",
                parse_mode="HTML",
            )
            return
    except Exception as e:
        await message.answer(
            "❌ Bot bu kanalni ko'ra olmadi (katta ehtimol bilan bot u yerda <b>Admin emas</b>).\n"
            "Botni kanalga Admin qilib, qaytadan tanlang!",
            parse_mode="HTML",
        )
        logging.error(f"get_chat xatosi ({target_id}): {e}")
        return

    await state.update_data(chat_id=chat_info.id)
    await state.set_state(Form.waiting_for_invite_link)

    await message.answer(
        f"✅ Kanal aniqlandi: <b>{html.escape(chat_info.title or '')}</b>\n"
        f"🆔 Haqiqiy ID: <code>{chat_info.id}</code>\n\n"
        "<b>2-qadam:</b> Endi taklif havolasini (<code>https://t.me/+...</code>) yuboring:",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML",
    )
    await message.answer(
        "Yoki havolani botning o'zi avtomatik yaratib bersinmi?",
        reply_markup=get_link_generation_keyboard(),
    )


@dp.callback_query(
    Form.waiting_for_invite_link,
    F.data.in_({"gen_link_req", "gen_link_std"}),
    F.from_user.id.in_(ADMIN_IDS),
)
async def admin_auto_generate_link(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    chat_id = data["chat_id"]
    is_request = call.data == "gen_link_req"

    try:
        invite = await bot.create_chat_invite_link(
            chat_id=chat_id,
            creates_join_request=is_request,
        )
    except TelegramBadRequest as e:
        await call.answer(
            "Havola yaratib bo'lmadi! Kanalda botga «Invite Users via Link» huquqini bering yoki havolani o'zingiz yozib yuboring.",
            show_alert=True,
        )
        logging.error(f"Link yaratishda xatolik: {e}")
        return

    await state.update_data(link=invite.invite_link)
    await state.set_state(Form.waiting_for_channel_name)
    await call.message.edit_text(
        f"🔗 Havola yaratildi: {invite.invite_link}\n\n"
        "<b>3-qadam:</b> Tugmada ko'rinadigan kanal nomini yozing (Masalan: 1-Kanal):",
        parse_mode="HTML",
    )
    await call.answer()


@dp.message(Form.waiting_for_invite_link, F.from_user.id.in_(ADMIN_IDS))
async def admin_get_invite_link(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Havolani matn ko'rinishida yuboring:")
        return

    link = normalize_url(message.text)
    await state.update_data(link=link)
    await state.set_state(Form.waiting_for_channel_name)
    await message.answer(
        "<b>3-qadam:</b> Tugmada ko'rinadigan kanal nomini kiriting (Masalan: 1-Kanal):",
        parse_mode="HTML",
    )


@dp.message(Form.waiting_for_channel_name, F.from_user.id.in_(ADMIN_IDS))
async def admin_add_channel_finish(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Kanal nomini matn ko'rinishida kiriting:")
        return

    data = await state.get_data()
    chat_id = int(data["chat_id"])

    db["channels"] = [ch for ch in db["channels"] if str(ch["chat_id"]) != str(chat_id)]
    db["channels"].append(
        {
            "chat_id": chat_id,
            "link": data["link"],
            "name": message.text.strip(),
        }
    )
    user_sub_cache.clear()
    await save_db(immediate=True)
    await state.clear()
    await message.answer(
        f"✅ <b>{html.escape(message.text.strip())}</b> (ID: <code>{chat_id}</code>) muvaffaqiyatli saqlandi!",
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML",
    )


# --- Post yaratish ---
@dp.message(F.text == "📝 Post yaratish", F.from_user.id.in_(ADMIN_IDS))
async def post_create_start(message: types.Message, state: FSMContext):
    await state.set_state(Form.waiting_for_media)
    await message.answer(
        "Post uchun <b>rasm</b> yoki <b>video</b> yuboring:",
        reply_markup=get_cancel_keyboard(),
        parse_mode="HTML",
    )


@dp.message(Form.waiting_for_media, F.photo | F.video, F.from_user.id.in_(ADMIN_IDS))
async def post_get_media(message: types.Message, state: FSMContext):
    if message.photo:
        await state.update_data(media_type="photo", media_id=message.photo[-1].file_id)
    else:
        await state.update_data(media_type="video", media_id=message.video.file_id)

    await state.set_state(Form.waiting_for_text)
    await message.answer("Post matnini (caption) kiriting:")


@dp.message(Form.waiting_for_media, F.from_user.id.in_(ADMIN_IDS))
async def post_media_invalid(message: types.Message):
    await message.answer("Faqat rasm yoki video yuboring (yoki «❌ Bekor qilish» tugmasini bosing):")


@dp.message(Form.waiting_for_text, F.from_user.id.in_(ADMIN_IDS))
async def post_get_text(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Post matnini yuboring:")
        return
    if len(message.text) > 1024:
        await message.answer("Matn 1024 ta belgidan oshmasligi kerak. Qisqaroq matn yuboring:")
        return

    await state.update_data(text=message.html_text)
    await state.set_state(Form.waiting_for_btn_text)
    await message.answer("Inline tugma uchun matn kiriting (Masalan: 🎬 Tomosha qilish):")


@dp.message(Form.waiting_for_btn_text, F.from_user.id.in_(ADMIN_IDS))
async def post_get_btn_text(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Tugma matnini kiriting:")
        return

    await state.update_data(btn_text=message.text.strip())
    await state.set_state(Form.waiting_for_btn_link)
    await message.answer("Inline tugma uchun havola yoki @username kiriting:")


@dp.message(Form.waiting_for_btn_link, F.from_user.id.in_(ADMIN_IDS))
async def post_get_btn_link(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Havolani matn ko'rinishida kiriting:")
        return

    link = normalize_url(message.text)
    data = await state.get_data()
    await state.clear()

    inline_markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=data["btn_text"], url=link)]]
    )

    try:
        if data["media_type"] == "photo":
            await bot.send_photo(
                chat_id=TARGET_CHANNEL,
                photo=data["media_id"],
                caption=data["text"],
                reply_markup=inline_markup,
                parse_mode="HTML",
            )
        else:
            await bot.send_video(
                chat_id=TARGET_CHANNEL,
                video=data["media_id"],
                caption=data["text"],
                reply_markup=inline_markup,
                parse_mode="HTML",
            )

        await message.answer(
            "Post muvaffaqiyatli yaratildi va kanalga yuborildi! ✅",
            reply_markup=get_admin_keyboard(),
        )
    except TelegramBadRequest as telegram_error:
        await message.answer(
            f"❌ Telegram xatosi: {telegram_error.message}\n"
            "(Bot maqsadli kanalda admin ekanligini va havola to'g'riligini tekshiring).",
            reply_markup=get_admin_keyboard(),
        )
    except Exception as e:
        await message.answer(
            f"❌ Kutilmagan xatolik: {e}",
            reply_markup=get_admin_keyboard(),
        )


# ================= FOYDALANUVCHILAR UCHUN (FILMLAR VA OBUNA) =================
@dp.callback_query(F.data == "check_sub")
async def callback_check_sub(call: types.CallbackQuery):
    save_user(call.from_user)
    user_id = call.from_user.id
    unsubscribed = await get_unsubscribed_channels(user_id, force_refresh=True)

    if not unsubscribed:
        try:
            await call.message.delete()
        except TelegramBadRequest:
            pass

        # Agar foydalanuvchi obunadan oldin biror film so'ragan bo'lsa, o'shani darhol yuboramiz
        req_code = pending_movies.pop(user_id, None)
        if req_code and req_code in db.get("movies", {}):
            movie = db["movies"][req_code]
            try:
                # send_movie_to_user - video file_id orqali yuboradi, yuboruvchining ismi/rasmi 100% yashirin bo'ladi
                await send_movie_to_user(call.message.chat.id, movie)
                return
            except Exception as e:
                logging.error(f"Zayavkadan keyin film yuborishda xato: {e}")

        await call.message.answer(
            "✅ <b>Obunangiz tasdiqlandi!</b>\n\n"
            "🎬 Endi ko'rmoqchi bo'lgan filmingiz raqamini yuboring (Masalan: <b>1</b>):",
            parse_mode="HTML",
        )
    else:
        try:
            await call.message.edit_reply_markup(
                reply_markup=get_user_sub_keyboard(unsubscribed)
            )
        except TelegramBadRequest:
            pass
        await call.answer(
            f"Siz hali {len(unsubscribed)} ta kanalga obuna bo'lmadingiz (yoki zayavka yubormadingiz)! ❌",
            show_alert=True,
        )


# ================= TELEGRAM STARS (PREMIUM VIP OBUNA) =================
async def send_premium_invoice(chat_id: int):
    """11 Telegram Stars uchun to'lov invoice yuboradi."""
    try:
        await bot.send_invoice(
            chat_id=chat_id,
            title="⭐ Premium VIP Obuna",
            description="Kanallarga a'zo bo'lmasdan botdan to'liq erkin foydalanish, VIP filmlar katalogi va tezkor yuklash!",
            payload=f"vip_sub_11_stars_{chat_id}",
            currency="XTR",  # Telegram Stars valyutasi
            prices=[LabeledPrice(label="VIP Obuna (11 Stars)", amount=11)],
            provider_token="",  # Telegram Stars uchun bo'sh qoldiriladi
        )
    except Exception as e:
        logging.error(f"Stars invoice yuborishda xatolik: {e}")
        await bot.send_message(
            chat_id=chat_id,
            text="❌ Telegram Stars to'lovini yaratishda xatolik yuz berdi. Iltimos, keyinroq urinib ko'ring.",
        )


@dp.callback_query(F.data == "buy_premium")
async def callback_buy_premium(call: types.CallbackQuery):
    user_id = call.from_user.id
    if is_vip_user(user_id):
        await call.answer("Siz allaqachon Premium VIP foydalanuvchisiz! 🌟", show_alert=True)
        return

    await call.answer()
    await send_premium_invoice(call.message.chat.id)


@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    sp = message.successful_payment
    user = message.from_user
    user_id = user.id

    payment_info = {
        "currency": sp.currency,
        "total_amount": sp.total_amount,
        "invoice_payload": sp.invoice_payload,
        "telegram_payment_charge_id": sp.telegram_payment_charge_id,
        "provider_payment_charge_id": sp.provider_payment_charge_id,
    }
    set_vip_user(user_id, payment_info)
    await save_db(immediate=True)

    congrats_text = (
        "🎉 <b>TABRIKLAYMIZ! SIZ ENDI PREMIUM VIP FOYDALANUVCHISIZ!</b> 🌟\n\n"
        "✅ <b>11 Telegram Stars</b> to'lovingiz muvaffaqiyatli qabul qilindi.\n\n"
        "🚀 <b>Siz uchun ochilgan maxsus imkoniyatlar:</b>\n"
        "• 🔓 <b>Cheklovlarsiz kirish</b> — kanallarga majburiy a'zo bo'lish talab qilinmaydi!\n"
        "• 📋 <b>To'liq Filmlar Katalogi</b> — barcha filmlar ro'yxatini ko'rish va bir bosishda tomosha qilish (/katalog)\n"
        "• ⚡️ <b>Tezkor yuklash</b> — videolarni navbatsiz olish\n"
        "• 👑 <b>VIP Profil</b> — maxsus status nishoni\n\n"
        "Quyidagi tugmalar orqali botdan to'liq foydalanishingiz mumkin:"
    )

    vip_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Filmlar katalogi", callback_data="show_catalog")],
            [InlineKeyboardButton(text="👤 VIP Profilim", callback_data="show_profile")],
        ]
    )

    await message.answer(congrats_text, reply_markup=vip_kb, parse_mode="HTML")

    # Adminlarga darhol bildirishnoma yuborish
    admin_alert = (
        f"💰 <b>YANGI STARS TO'LOVI! (11 ⭐️)</b>\n\n"
        f"👤 Foydalanuvchi: <a href='tg://user?id={user.id}'>{html.escape(user.full_name)}</a>\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"⭐ Miqdor: <b>{sp.total_amount} Stars</b>\n"
        f"💳 Charge ID: <code>{sp.telegram_payment_charge_id}</code>"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=admin_alert, parse_mode="HTML")
        except Exception:
            pass


@dp.message(Command("premium"))
@dp.message(Command("vip"))
async def cmd_premium_vip(message: types.Message):
    save_user(message.from_user, is_message=True)
    if is_vip_user(message.from_user.id):
        vip_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📋 Filmlar katalogi", callback_data="show_catalog")],
                [InlineKeyboardButton(text="👤 VIP Profilim", callback_data="show_profile")],
            ]
        )
        await message.answer(
            "🌟 <b>Siz allaqachon Premium VIP foydalanuvchisiz!</b>\n\n"
            "Sizda barcha imkoniyatlar ochiq:\n"
            "• Kanallarga a'zo bo'lish shart emas ✅\n"
            "• Filmlar katalogi ochiq (/katalog) ✅\n"
            "• VIP status faol ✅",
            reply_markup=vip_kb,
            parse_mode="HTML",
        )
        return
    await send_premium_invoice(message.chat.id)


def build_catalog_keyboard() -> InlineKeyboardMarkup:
    movies = db.get("movies", {})
    sorted_codes = sorted(movies.keys(), key=lambda x: int(x) if x.isdigit() else 9999)
    keyboard = []
    row = []
    for code in sorted_codes:
        row.append(InlineKeyboardButton(text=f"🎬 #{code}", callback_data=f"play_movie_{code}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="👤 VIP Profilim", callback_data="show_profile")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


@dp.callback_query(F.data.startswith("play_movie_"))
async def callback_play_movie(call: types.CallbackQuery):
    code = call.data.replace("play_movie_", "")
    movie = db.get("movies", {}).get(code)
    if movie:
        await call.answer("Film yuborilmoqda... ⏳")
        await send_movie_to_user(call.message.chat.id, movie)
    else:
        await call.answer(f"#{code} raqamli film topilmadi!", show_alert=True)


@dp.callback_query(F.data == "show_catalog")
@dp.message(Command("katalog"))
@dp.message(Command("catalog"))
async def cmd_or_call_catalog(event: types.Message | types.CallbackQuery):
    is_call = isinstance(event, types.CallbackQuery)
    user = event.from_user
    chat_id = event.message.chat.id if is_call else event.chat.id
    save_user(user)

    if not is_vip_user(user.id):
        upsell_text = (
            "🔒 <b>Filmlar Katalogi — faqat Premium VIP foydalanuvchilar uchun!</b>\n\n"
            "⭐ <b>11 Telegram Stars</b> evaziga Premium obunani xarid qiling va:\n"
            "• Barcha filmlarni bitta qulay katalogda ko'ring\n"
            "• Kanallarga majburiy a'zo bo'lmasdan tomosha qiling\n"
            "• Bir bosishda xohlagan filmingizni oching!"
        )
        upsell_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⭐ 11 Starsga Premium olish", callback_data="buy_premium")]
            ]
        )
        if is_call:
            await event.message.answer(upsell_text, reply_markup=upsell_kb, parse_mode="HTML")
            await event.answer()
        else:
            await event.answer(upsell_text, reply_markup=upsell_kb, parse_mode="HTML")
        return

    movies = db.get("movies", {})
    if not movies:
        msg = "📋 Hozircha bazada filmlar mavjud emas."
        if is_call:
            await event.message.answer(msg)
            await event.answer()
        else:
            await event.answer(msg)
        return

    text = (
        f"🎬 <b>VIP FILMLAR KATALOGI</b>\n"
        f"Jami mavjud filmlar: <b>{len(movies)} ta</b>\n\n"
        f"Tomosha qilmoqchi bo'lgan filmingiz tugmasini bosing:"
    )
    if is_call:
        await event.message.answer(text, reply_markup=build_catalog_keyboard(), parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=build_catalog_keyboard(), parse_mode="HTML")


@dp.callback_query(F.data == "show_profile")
@dp.message(Command("profil"))
@dp.message(Command("profile"))
async def cmd_or_call_profile(event: types.Message | types.CallbackQuery):
    is_call = isinstance(event, types.CallbackQuery)
    user = event.from_user
    chat_id = event.message.chat.id if is_call else event.chat.id
    save_user(user)

    vip = is_vip_user(user.id)
    u_data = db.get("users", {}).get(str(user.id), {})
    first_seen = u_data.get("first_seen", "Noma'lum")

    if vip:
        status_str = "🌟 <b>PREMIUM VIP FOYDALANUVCHI</b>"
        perks_str = (
            "✅ Kanallarga majburiy obuna talab qilinmaydi\n"
            "✅ To'liq filmlar katalogi ochiq (/katalog)\n"
            "✅ VIP tezkor yuklash faol"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📋 Filmlar katalogi", callback_data="show_catalog")]
            ]
        )
    else:
        status_str = "👤 <b>Oddiy foydalanuvchi</b>"
        perks_str = (
            "❌ Kanallarga obuna talab qilinadi\n"
            "❌ Filmlar katalogi yopiq"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⭐ 11 Starsga Premium olish", callback_data="buy_premium")]
            ]
        )

    profile_text = (
        f"👤 <b>SHAXSIY PROFILINGIZ</b>\n\n"
        f"👤 <b>Ism:</b> {html.escape(user.full_name)}\n"
        f"🆔 <b>ID:</b> <code>{user.id}</code>\n"
        f"👑 <b>Status:</b> {status_str}\n"
        f"📅 <b>Botga qo'shilgan:</b> {first_seen}\n\n"
        f"✨ <b>Imkoniyatlar:</b>\n{perks_str}"
    )

    if is_call:
        await event.message.answer(profile_text, reply_markup=kb, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(profile_text, reply_markup=kb, parse_mode="HTML")


@dp.message(F.chat.type == "private", F.from_user.id.in_(ADMIN_IDS))
async def admin_movie_test_handler(message: types.Message):
    """Admin botga 1, 2, #1 kabi raqam yozganda filmni darhol chiqarib beradi."""
    if message.text:
        code = extract_movie_code(message.text)
        if code:
            movie = db.get("movies", {}).get(code)
            if movie:
                await send_movie_to_user(message.chat.id, movie)
                return
            else:
                sorted_codes = sorted(db.get("movies", {}).keys(), key=lambda x: int(x) if x.isdigit() else 9999)
                m_list = ", ".join(f"#{c}" for c in sorted_codes) or "Mavjud emas"
                await message.answer(
                    f"❌ <b>#{code}</b> raqamli film bazada topilmadi!\n\n"
                    f"📋 <b>Bazada mavjud filmlar:</b> {m_list}",
                    parse_mode="HTML",
                )
                return

    await message.answer(
        "👋 <b>Admin panel</b>\nQuyidagi menyulardan birini tanlang yoki film raqamini yuboring (Masalan: <b>1</b>):",
        reply_markup=get_admin_keyboard(),
        parse_mode="HTML",
    )


@dp.message(F.chat.type == "private", ~F.from_user.id.in_(ADMIN_IDS))
async def global_user_checker(message: types.Message):
    save_user(message.from_user, is_message=True)
    user_id = message.from_user.id

    # 1. Foydalanuvchi film kodini yuborganini tekshiramiz
    req_code = None
    if message.text:
        if message.text.startswith("/start"):
            parts = message.text.split(maxsplit=1)
            if len(parts) > 1:
                req_code = extract_movie_code(parts[1])
        else:
            req_code = extract_movie_code(message.text)

    # 2. Obunani tekshiramiz
    unsubscribed = await get_unsubscribed_channels(user_id, force_refresh=False)

    if unsubscribed:
        if req_code:
            pending_movies[user_id] = req_code
        await message.answer(
            "⚠️ <b>Botdan foydalanish va filmni yuklab olish uchun quyidagi kanallarga a'zo bo'ling:</b>",
            reply_markup=get_user_sub_keyboard(unsubscribed),
            parse_mode="HTML",
        )
        return

    # 3. Agar obuna bo'lgan bo'lsa va film kodini yuborgan bo'lsa
    if req_code:
        movie = db.get("movies", {}).get(req_code)
        if movie:
            try:
                # send_movie_to_user orqali video to'g'ridan-to'g'ri bot nomidan yuboriladi (akkaunt 100% yashirin)
                await send_movie_to_user(message.chat.id, movie)
                return
            except Exception as e:
                logging.error(f"Film yuborishda xatolik (#{req_code}): {e}")
                await message.answer("❌ Filmni yuklashda xatolik yuz berdi. Iltimos, keyinroq urinib ko'ring.")
                return
        else:
            await message.answer(
                f"❌ <b>#{req_code}</b> raqamli film topilmadi!\n"
                "Iltimos, kodni to'g'ri kiritganingizni tekshiring.",
                parse_mode="HTML",
            )
            return

    # 4. Agar shunchaki /start yoki boshqa matn yozgan bo'lsa
    if message.text and message.text.startswith("/start"):
        if is_vip_user(user_id):
            vip_start_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Filmlar katalogi", callback_data="show_catalog")],
                    [InlineKeyboardButton(text="👤 VIP Profilim", callback_data="show_profile")],
                ]
            )
            await message.answer(
                "🌟 <b>Assalomu alaykum, Hurmatli PREMIUM Foydalanuvchi!</b>\n\n"
                "Sizda barcha imkoniyatlar ochilgan, hech qanday kanallarga a'zo bo'lish shart emas!\n\n"
                "🎬 Tomosha qilmoqchi bo'lgan filmingiz raqamini yuboring (Masalan: <b>1</b>) yoki quyidagi katalogdan tanlang:",
                reply_markup=vip_start_kb,
                parse_mode="HTML",
            )
            return
        else:
            std_start_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⭐ Premium VIP (11 ⭐️ Stars)", callback_data="buy_premium")],
                    [InlineKeyboardButton(text="👤 Profilim", callback_data="show_profile")],
                ]
            )
            await message.answer(
                "👋 <b>Assalomu alaykum!</b>\n\n"
                "🎬 Tomosha qilmoqchi bo'lgan filmingiz raqamini yuboring (Masalan: <b>1</b> yoki <b>#1</b>):\n\n"
                "💡 <i>Eslatma: Kanallarga majburiy a'zo bo'lmasdan tomosha qilish uchun <b>Premium</b> obunadan foydalanishingiz mumkin.</i>",
                reply_markup=std_start_kb,
                parse_mode="HTML",
            )
            return
    else:
        if is_vip_user(user_id):
            vip_prompt_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Filmlar katalogi", callback_data="show_catalog")]
                ]
            )
            await message.answer(
                "🎬 Film ko'rish uchun uning raqamini yuboring (Masalan: <b>1</b> yoki <b>#1</b>) yoki katalogdan tanlang:",
                reply_markup=vip_prompt_kb,
                parse_mode="HTML",
            )
        else:
            await message.answer(
                "🎬 Film ko'rish uchun uning raqamini yuboring (Masalan: <b>1</b> yoki <b>#1</b>):",
                parse_mode="HTML",
            )


# ================= ISHGA TUSHIRISH =================
async def notify_users_on_startup():
    """Bot har safar qayta ishga tushganda barcha userlarga tartib bilan xabar yuboradi."""
    # Polling boshlanishi va ulanishlar o'rnatilishi uchun 3 soniya kutamiz
    await asyncio.sleep(3)

    user_ids = list(db.get("users", {}).keys())
    if not user_ids:
        return

    msg_text = (
        "Bot Faol ishlamoqda /start✅\n"
        "Bot Faol ishlamoqda /start✅\n"
        "Bot Faol ishlamoqda /start✅"
    )

    logging.info(f"Bot qayta ishga tushdi: {len(user_ids)} ta userga xabarnoma tarqatilmoqda...")
    sent = 0
    blocked = 0
    failed = 0

    for uid_str in user_ids:
        try:
            uid = int(uid_str)
            await bot.send_message(chat_id=uid, text=msg_text)
            sent += 1
            # Telegram limiti: sekundiga maksimal 30 ta. 0.05s kutish = sekundiga ~20 ta (xavfsiz va barqaror)
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            logging.warning(f"Telegram flood limit: {e.retry_after} soniya kutilmoqda...")
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(chat_id=uid, text=msg_text)
                sent += 1
            except Exception:
                failed += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            # User botni bloklagan, bot chatdan o'chirilgan yoki akkaunt mavjud emas
            blocked += 1
        except asyncio.CancelledError:
            logging.info("Xabarnoma tarqatish to'xtatildi (Bot o'chirildi).")
            return
        except Exception as e:
            failed += 1
            logging.debug(f"User {uid_str} ga xabar yuborishda xatolik: {e}")
            await asyncio.sleep(0.05)

    logging.info(f"Xabarnoma yakunlandi: {sent} ta yetkazildi, {blocked} ta bloklagan/o'chgan, {failed} ta xato.")

    # Adminlarga natija haqida hisobot yuboramiz
    admin_report = (
        f"📢 <b>Qayta ishga tushish xabarnomasi yakunlandi:</b>\n\n"
        f"✅ Yetkazildi: <b>{sent}</b> ta userga\n"
        f"🚫 Botni bloklagan/o'chgan: <b>{blocked}</b> ta\n"
        f"👥 Bazadagi jami userlar: <b>{len(user_ids)}</b> ta"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=admin_report, parse_mode="HTML")
        except Exception:
            pass


async def main():
    await init_app()
    save_task = asyncio.create_task(db_auto_save_worker())
    notify_task = asyncio.create_task(notify_users_on_startup())
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(
            bot,
            allowed_updates=[
                "message",
                "callback_query",
                "chat_join_request",
                "chat_member",
                "my_chat_member",
                "channel_post",
                "edited_channel_post",
            ],
        )
    finally:
        save_task.cancel()
        notify_task.cancel()
        await save_db(immediate=True)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot to'xtatildi.")
