import asyncio
import logging
import time
import urllib.parse
from typing import Dict, Tuple, Optional, Callable, Any, Awaitable

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.types import (
    Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery,
    LabeledPrice, PreCheckoutQuery
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import httpx
import aiosqlite
from bs4 import BeautifulSoup

# ==========================================
# --- КОНФИГУРАЦИЯ ---
# ==========================================
BOT_TOKEN = "8764495369:AAGuaieVwmsHzVloDRZDgv2nP6oDijAYTC4"
TMDB_API_KEY = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiI5NTEwYjJlODAxYmFlMTcxNzFmNzM2NWU4ZGIyOTJiMSIsIm5iZiI6MTc4MTA4NzkzOS40NTIsInN1YiI6IjZhMjkzZWMzYTAwOTBhNDQ4Y2Q0ZTUwZCIsInNjb3BlcyI6WyJhcGlfcmVhZCJdLCJ2ZXJzaW9uIjoxfQ.myFjB6izWez3gXOA-8ErMPX2AH6SGKjPMFbUT7RcZrY"
TMDB_BASE_URL = "https://api.themoviedb.org/3"

# ⚠️ ЗАМЕНИТЕ НА ВАШ РЕАЛЬНЫЙ ID
ADMIN_ID = 123456789  

CHECK_INTERVAL = 7200
CACHE_TTL = 3600
MIN_CHECK_INTERVAL = 86400

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ==========================================
# --- БАЗА ДАННЫХ ---
# ==========================================
db: Optional[aiosqlite.Connection] = None

async def init_db():
    global db
    db = await aiosqlite.connect("shows.db")
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA synchronous=NORMAL;")
    
    await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('donate_enabled', '0')")
    
    await db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, total_stars INTEGER DEFAULT 0)")
    
    await db.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER, tmdb_id INTEGER, title TEXT, imdb_id TEXT,
            last_season INTEGER DEFAULT 1, last_episode INTEGER DEFAULT 0, last_checked REAL DEFAULT 0,
            PRIMARY KEY (user_id, tmdb_id)
        )
    """)
    await db.commit()

async def register_user(user_id: int):
    await db.execute("INSERT OR IGNORE INTO users (user_id, total_stars) VALUES (?, 0)", (user_id,))
    await db.commit()

async def get_bot_stats() -> dict:
    async with db.execute("SELECT COUNT(user_id) FROM users") as cur:
        total_users = (await cur.fetchone())[0]
    async with db.execute("SELECT COUNT(DISTINCT user_id) FROM subscriptions") as cur:
        active_subscribers = (await cur.fetchone())[0]
    async with db.execute("SELECT SUM(total_stars) FROM users") as cur:
        total_stars = (await cur.fetchone())[0] or 0
    return {"total_users": total_users, "active_subscribers": active_subscribers, "total_stars": total_stars}

async def get_donate_status() -> bool:
    async with db.execute("SELECT value FROM settings WHERE key='donate_enabled'") as cur:
        res = await cur.fetchone()
        return res[0] == '1' if res else False

async def toggle_donate_status() -> bool:
    current = await get_donate_status()
    new_val = '0' if current else '1'
    await db.execute("UPDATE settings SET value=? WHERE key='donate_enabled'", (new_val,))
    await db.commit()
    return new_val == '1'

async def add_stars(user_id: int, amount: int):
    await db.execute("INSERT INTO users (user_id, total_stars) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET total_stars = total_stars + ?", (user_id, amount, amount))
    await db.commit()

async def add_sub(user_id: int, tmdb_id: int, title: str, imdb_id: str):
    await db.execute("INSERT OR REPLACE INTO subscriptions (user_id, tmdb_id, title, imdb_id) VALUES (?, ?, ?, ?)", (user_id, tmdb_id, title, imdb_id))
    await db.commit()

async def get_subs(user_id: int):
    async with db.execute("SELECT tmdb_id, title, last_season, last_episode FROM subscriptions WHERE user_id=?", (user_id,)) as cur:
        return await cur.fetchall()

async def remove_sub(user_id: int, tmdb_id: int):
    await db.execute("DELETE FROM subscriptions WHERE user_id=? AND tmdb_id=?", (user_id, tmdb_id))
    await db.commit()

async def update_sub(user_id: int, tmdb_id: int, season: int, episode: int, checked: float):
    await db.execute("UPDATE subscriptions SET last_season=?, last_episode=?, last_checked=? WHERE user_id=? AND tmdb_id=?", (season, episode, checked, user_id, tmdb_id))
    await db.commit()

# ==========================================
# --- API И ГИБРИДНЫЙ ПОИСК (TMDB + MyShows) ---
# ==========================================
_cache: Dict[str, Tuple[float, dict]] = {}

async def tmdb_request(endpoint: str, params: dict = None) -> Optional[dict]:
    params = params or {}
    params["language"] = "ru-RU"
    cache_key = f"{endpoint}?{params}"
    now = time.time()
    
    if cache_key in _cache:
        ts, data = _cache[cache_key]
        if now - ts < CACHE_TTL:
            return data

    headers = {}
    if TMDB_API_KEY.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {TMDB_API_KEY}"
    else:
        params["api_key"] = TMDB_API_KEY

    async with httpx.AsyncClient(timeout=10.0) as client:
        for attempt in range(3):
            try:
                resp = await client.get(f"{TMDB_BASE_URL}{endpoint}", params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                _cache[cache_key] = (now, data)
                return data
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    await asyncio.sleep(5)
                    continue
                break
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
    return None

async def search_myshows(query: str) -> list:
    """Резервный поиск через MyShows.me, если TMDB не нашел русское название"""
    try:
        url = f"https://myshows.me/search/?q={urllib.parse.quote(query)}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        results = []
        for div in soup.find_all('div', class_='show-title'):
            a = div.find('a')
            if a and a.get('href'):
                title = a.get_text(strip=True)
                href = a['href']
                # Извлекаем ID из ссылки вида /shows/12345/show-name/
                parts = href.strip('/').split('/')
                if len(parts) >= 2 and parts[0] == 'shows':
                    show_id = parts[1]
                    results.append({'id': f"ms_{show_id}", 'name': title, 'source': 'myshows', 'href': href})
                    if len(results) >= 5:
                        break
        return results
    except Exception as e:
        logging.error(f"MyShows search error: {e}")
        return []

async def hybrid_search(query: str) -> list:
    """Сначала ищет в TMDB (RU), затем в TMDB (EN), затем в MyShows"""
    # 1. TMDB на русском
    data_ru = await tmdb_request("/search/tv", {"query": query, "include_adult": "false"})
    if data_ru and data_ru.get("results"):
        return data_ru["results"]
    
    # 2. TMDB на английском (если пользователь ввел транслит или англ. название)
    data_en = await tmdb_request("/search/tv", {"query": query, "language": "en-US", "include_adult": "false"})
    if data_en and data_en.get("results"):
        return data_en["results"]
    
    # 3. Резервный парсинг MyShows
    return await search_myshows(query)

def get_title_with_fallback(item: dict) -> str:
    if 'name' in item:
        return item.get('name') or item.get('original_name') or 'Неизвестно'
    return item.get('title') or item.get('original_title') or 'Неизвестно'

# ==========================================
# --- AIОGRAM SETUP & MIDDLEWARE ---
# ==========================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate_limit: float = 1.0):
        self.rate_limit = rate_limit
        self.user_timestamps: Dict[int, float] = {}

    async def __call__(self, handler, event, data):
        user_id = event.from_user.id
        now = time.time()
        if user_id in self.user_timestamps and (now - self.user_timestamps[user_id] < self.rate_limit):
            if isinstance(event, CallbackQuery):
                await event.answer("⏳ Не так быстро!", show_alert=True)
            return None
        self.user_timestamps[user_id] = now
        return await handler(event, data)

router.message.middleware(ThrottlingMiddleware(rate_limit=1.0))
router.callback_query.middleware(ThrottlingMiddleware(rate_limit=0.5))

# ==========================================
# --- FSM ---
# ==========================================
class AddShowStates(StatesGroup):
    waiting_for_title = State()

class DonateStates(StatesGroup):
    waiting_for_amount = State()

# ==========================================
# --- КЛАВИАТУРЫ ---
# ==========================================
async def main_kb():
    donate_enabled = await get_donate_status()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить сериал", callback_data="add_show")],
        [InlineKeyboardButton(text="📺 Мои сериалы", callback_data="my_shows")],
    ])
    if donate_enabled:
        kb.inline_keyboard.append([InlineKeyboardButton(text="💎 Поддержать проект", callback_data="donate_menu")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🔄 Обновить данные", callback_data="force_check")])
    return kb

def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]])

def cancel_donate_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_donate")]])

def search_kb(results):
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for r in results[:5]:
        title = get_title_with_fallback(r)
        year = r.get('first_air_date', '')[:4] if r.get('first_air_date') else (r.get('href', '').split('/')[-1] if 'href' in r else 'N/A')
        source_icon = "🇷🇺" if r.get('source') == 'myshows' else "🎬"
        kb.inline_keyboard.append([InlineKeyboardButton(
            text=f"{source_icon} {title} ({year})",
            callback_data=f"select_{r['id']}"
        )])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")])
    return kb

def subs_kb(shows):
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for tmdb_id, title, s, e in shows:
        kb.inline_keyboard.append([InlineKeyboardButton(
            text=f"🗑 {title} (S{s}E{e})", callback_data=f"del_{tmdb_id}"
        )])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🔙 Меню", callback_data="main_menu")])
    return kb

# ==========================================
# --- ОБРАБОТЧИКИ (ЖЕЛЕЗОБЕТОННОЕ РЕДАКТИРОВАНИЕ) ---
# ==========================================
@router.message(CommandStart())
async def cmd_start(message: Message):
    await register_user(message.from_user.id)
    await message.answer(
        "👋 Привет! Я отслеживаю выход новых серий.\n"
        "💡 Поиск работает через TMDB и MyShows (русский и английский).\n"
        "Все действия обновляют это сообщение, не засоряя чат.",
        reply_markup=await main_kb()
    )

@router.callback_query(F.data == "main_menu")
async def go_main(callback: CallbackQuery):
    await callback.message.edit_text("📺 Главное меню:", reply_markup=await main_kb())
    await callback.answer()

@router.callback_query(F.data == "add_show")
async def cmd_add(callback: CallbackQuery, state: FSMContext):
    await register_user(callback.from_user.id)
    await callback.message.edit_text(
        "🔍 Введите название сериала (на русском или английском):",
        reply_markup=cancel_kb()
    )
    await state.set_state(AddShowStates.waiting_for_title)
    await callback.answer()

@router.callback_query(F.data == "cancel")
async def cmd_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Действие отменено.", reply_markup=await main_kb())
    await callback.answer()

@router.message(AddShowStates.waiting_for_title)
async def search_show(message: Message, state: FSMContext):
    await state.clear()
    query = message.text.strip()
    
    # 1. СОЗДАЕМ сообщение, которое будем редактировать. Это наш "якорь".
    loading_msg = await message.answer("🔎 Ищу в TMDB и MyShows...")
    
    # 2. Пытаемся удалить сообщение пользователя (если бот админ, иначе игнорируем ошибку)
    try:
        await message.delete()
    except Exception:
        pass 

    if not query:
        await loading_msg.edit_text("❌ Введите название сериала.", reply_markup=await main_kb())
        return
    
    # 3. Делаем запрос
    results = await hybrid_search(query)
    
    # 4. РЕДАКТИРУЕМ loading_msg. Никаких новых message.answer!
    if not results:
        await loading_msg.edit_text(
            f"❌ По запросу '{query}' ничего не найдено.\n\n"
            "💡 Попробуйте ввести оригинальное название на английском языке.",
            reply_markup=await main_kb()
        )
    else:
        await loading_msg.edit_text(
            f"📺 Найдено совпадений: {len(results)}\n"
            f"🔍 По запросу: '{query}'\n\n"
            "Выберите сериал:",
            reply_markup=search_kb(results)
        )

@router.callback_query(F.data.startswith("select_"))
async def select_show(callback: CallbackQuery):
    item_id = callback.data.split("_", 1)[1]
    await callback.answer("⏳ Загружаю данные...")
    
    # Если это MyShows, мы не можем получить детали без API, просим выбрать из TMDB
    if item_id.startswith("ms_"):
        await callback.message.edit_text(
            "⚠️ Для надежного отслеживания дат выхода серий выберите сериал из результатов TMDB (🎬).\n"
            "MyShows используется только как резервный поиск названий.",
            reply_markup=await main_kb()
        )
        return

    tmdb_id = int(item_id)
    info = await tmdb_request(f"/tv/{tmdb_id}")
    if not info:
        await callback.message.edit_text("⚠️ Не удалось загрузить данные.", reply_markup=await main_kb())
        return
    
    title = get_title_with_fallback(info)
    imdb_id = info.get('external_ids', {}).get('imdb_id', 'N/A')
    
    await add_sub(callback.from_user.id, tmdb_id, title, imdb_id)
    await callback.message.edit_text(
        f"✅ **{title}** успешно добавлен в отслеживание!\n\n"
        f"🔔 Вы получите уведомление, когда выйдет новая серия.",
        reply_markup=await main_kb()
    )

@router.callback_query(F.data == "my_shows")
async def cmd_my(callback: CallbackQuery):
    shows = await get_subs(callback.from_user.id)
    if not shows:
        await callback.message.edit_text(
            "📭 Пока нет отслеживаемых сериалов.\n"
            "Нажмите '➕ Добавить сериал', чтобы начать!",
            reply_markup=await main_kb()
        )
    else:
        await callback.message.edit_text(
            f"📺 Ваши подписки ({len(shows)}):\n\n"
            "Нажмите на сериал, чтобы удалить его:",
            reply_markup=subs_kb(shows)
        )
    await callback.answer()

@router.callback_query(F.data.startswith("del_"))
async def cmd_del(callback: CallbackQuery):
    tmdb_id = int(callback.data.split("_", 1)[1])
    await remove_sub(callback.from_user.id, tmdb_id)
    shows = await get_subs(callback.from_user.id)
    if not shows:
        await callback.message.edit_text(
            "📭 Пока нет отслеживаемых сериалов.",
            reply_markup=await main_kb()
        )
    else:
        await callback.message.edit_text(
            f"📺 Ваши подписки ({len(shows)}):",
            reply_markup=subs_kb(shows)
        )
    await callback.answer("🗑 Удалено")

@router.callback_query(F.data == "force_check")
async def cmd_force(callback: CallbackQuery):
    await callback.answer("🔄 Проверка запущена...")
    await callback.message.edit_text("✅ Запрос на проверку новых серий отправлен.", reply_markup=await main_kb())
    asyncio.create_task(check_new_episodes(force=True))

# ==========================================
# --- ADMIN PANEL ---
# ==========================================
def is_admin(user_id: int) -> bool:
    return str(user_id) == str(ADMIN_ID)

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("🚫 Доступ запрещен.")
        return
    await show_admin_menu(message)

@router.callback_query(F.data == "admin_menu")
async def cb_admin_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("🚫 Доступ запрещен.", show_alert=True)
        return
    await show_admin_menu(callback)

async def show_admin_menu(target: Message | CallbackQuery):
    status = await get_donate_status()
    status_text = "✅ Включена" if status else "❌ Отключена"
    stats = await get_bot_stats()
    
    text = (
        f"⚙️ **Панель администратора**\n\n"
        f"👤 Ваш ID: `{target.from_user.id}`\n\n"
        f"📊 **Статистика бота:**\n"
        f"👥 Всего пользователей: `{stats['total_users']}`\n"
        f"📺 Активных подписчиков: `{stats['active_subscribers']}`\n"
        f"⭐️ Всего собрано звезд: `{stats['total_stars']}`\n\n"
        f"💎 Статус кнопки 'Поддержать проект': {status_text}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Переключить кнопку доната", callback_data="toggle_donate")],
        [InlineKeyboardButton(text="🔙 В главное меню", callback_data="main_menu")]
    ])
    
    if isinstance(target, Message):
        await target.answer(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await target.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        await target.answer()

@router.callback_query(F.data == "toggle_donate")
async def cb_toggle_donate(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    new_status = await toggle_donate_status()
    status_text = "✅ Включена" if new_status else "❌ Отключена"
    stats = await get_bot_stats()
    
    text = (
        f"⚙️ **Панель администратора**\n\n"
        f"👤 Ваш ID: `{callback.from_user.id}`\n\n"
        f"📊 **Статистика бота:**\n"
        f"👥 Всего пользователей: `{stats['total_users']}`\n"
        f"📺 Активных подписчиков: `{stats['active_subscribers']}`\n"
        f"⭐️ Всего собрано звезд: `{stats['total_stars']}`\n\n"
        f"💎 Статус кнопки: {status_text}\n\n"
        f"✅ Настройка успешно сохранена!"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Переключить кнопку доната", callback_data="toggle_donate")],
        [InlineKeyboardButton(text="🔙 В главное меню", callback_data="main_menu")]
    ])
    
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer(f"Кнопка доната теперь: {status_text}")

# ==========================================
# --- TELEGRAM STARS (DONATE) ---
# ==========================================
@router.callback_query(F.data == "donate_menu")
async def cmd_donate_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "💎 **Поддержка разработки бота**\n\n"
        "Выберите фиксированную сумму или введите свою.\n"
        "⚠️ *Минимальная сумма по правилам Telegram: 10 звезд.*",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="10 ⭐️", callback_data="donate_10"), InlineKeyboardButton(text="50 ⭐️", callback_data="donate_50")],
            [InlineKeyboardButton(text="100 ⭐️", callback_data="donate_100"), InlineKeyboardButton(text="500 ⭐️", callback_data="donate_500")],
            [InlineKeyboardButton(text="✏️ Ввести свою сумму", callback_data="donate_custom")],
            [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="main_menu")]
        ])
    )
    await callback.answer()

@router.callback_query(F.data.in_({"donate_10", "donate_50", "donate_100", "donate_500"}))
async def process_fixed_donation(callback: CallbackQuery):
    amount = int(callback.data.split("_")[1])
    await callback.answer()
    await callback.message.answer_invoice(
        title=f"💎 Поддержка бота ({amount} ⭐️)",
        description="Спасибо за вашу поддержку!",
        payload=f"donate_fixed_{amount}_stars",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Donation", amount=amount)], 
        reply_markup=await main_kb()
    )

@router.callback_query(F.data == "donate_custom")
async def cmd_donate_custom(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "✏️ Введите желаемую сумму доната в звездах (⭐️) сообщением ниже.\n\n"
        "⚠️ *Минимальная сумма: 10 звезд.*",
        reply_markup=cancel_donate_kb()
    )
    await state.set_state(DonateStates.waiting_for_amount)
    await callback.answer()

@router.callback_query(F.data == "cancel_donate")
async def cmd_cancel_donate(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Действие отменено.", reply_markup=await main_kb())
    await callback.answer()

@router.message(DonateStates.waiting_for_amount)
async def process_custom_amount(message: Message, state: FSMContext):
    await state.clear()
    user_input = message.text.strip()
    
    # 1. Создаем якорное сообщение
    response_msg = await message.answer("Обработка...")
    
    # 2. Удаляем сообщение пользователя
    try:
        await message.delete()
    except Exception:
        pass

    try:
        amount = int(user_input)
    except ValueError:
        await response_msg.edit_text("❌ Пожалуйста, введите целое число.", reply_markup=await main_kb())
        return

    if amount < 10:
        await response_msg.edit_text("❌ Минимальная сумма доната: 10 звезд ⭐️", reply_markup=await main_kb())
        return

    await response_msg.edit_text("Формирую счет...", reply_markup=await main_kb()) # Промежуточное редактирование
    await response_msg.answer_invoice( # answer_invoice всегда создает новое сообщение, это особенность Telegram API, но оно одно
        title=f"💎 Поддержка бота ({amount} ⭐️)",
        description=f"Вы указали сумму: {amount} Stars.",
        payload=f"donate_custom_{amount}_stars",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Custom Donation", amount=amount)],
        reply_markup=await main_kb()
    )

@router.pre_checkout_query()
async def on_pre_checkout_query(pre_checkout_q: PreCheckoutQuery):
    await pre_checkout_q.answer(ok=True)

@router.message(F.successful_payment)
async def on_successful_payment(message: Message):
    amount = message.successful_payment.total_amount
    user_id = message.from_user.id
    currency = message.successful_payment.currency
    await add_stars(user_id, amount)
    await message.answer(
        f"🎉 Огромное спасибо за поддержку!\n\nВы успешно задонатили **{amount} {currency}**.\nВаш вклад очень важен! ❤️",
        reply_markup=await main_kb()
    )

# ==========================================
# --- ФОНОВАЯ ПРОВЕРКА ---
# ==========================================
async def check_new_episodes(force: bool = False):
    logging.info("Запуск проверки новых серий...")
    now = time.time()
    async with db.execute("SELECT user_id, tmdb_id, title, last_season, last_episode, last_checked FROM subscriptions") as cur:
        subs = await cur.fetchall()

    for user_id, tmdb_id, title, last_s, last_e, last_checked in subs:
        if not force and (now - last_checked) < MIN_CHECK_INTERVAL:
            continue

        info = await tmdb_request(f"/tv/{tmdb_id}")
        if not info:
            continue

        if info.get("status") in ["Canceled", "Ended"]:
            await update_sub(user_id, tmdb_id, last_s, last_e, now)
            continue

        current_seasons = info.get("number_of_seasons", 1)
        season_data = await tmdb_request(f"/tv/{tmdb_id}/season/{current_seasons}")
        if not season_data:
            await update_sub(user_id, tmdb_id, last_s, last_e, now)
            continue

        current_episodes = len(season_data.get("episodes", []))
        if current_seasons > last_s or current_episodes > last_e:
            try:
                await bot.send_message(
                    user_id, 
                    f"🎉 Вышла новая серия!\n"
                    f"📺 **{title}**\n"
                    f"🔹 Сезон {current_seasons}, Серия {current_episodes}"
                )
                logging.info(f"Новая серия {title} для {user_id}")
            except Exception as e:
                logging.error(f"Ошибка отправки {user_id}: {e}")

        await update_sub(user_id, tmdb_id, current_seasons, current_episodes, now)

# ==========================================
# --- ЗАПУСК ---
# ==========================================
async def main():
    await init_db()
    asyncio.create_task(check_new_episodes())
    logging.info(f"🤖 Бот запущен! Admin ID: {ADMIN_ID}")
    try:
        await dp.start_polling(bot)
    finally:
        if db:
            await db.close()

if __name__ == "__main__":
    asyncio.run(main())
