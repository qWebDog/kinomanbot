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
OMDB_API_KEY = "a3c0826c"  
TMDB_BASE_URL = "https://api.themoviedb.org/3"
OMDB_BASE_URL = "https://www.omdbapi.com"

ADMIN_ID = 673594120 

CHECK_INTERVAL = 7200
CACHE_TTL = 3600
MIN_CHECK_INTERVAL = 86400

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

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
# --- API: TMDB + OMDb + MyShows ---
# ==========================================
_cache: Dict[str, Tuple[float, dict]] = {}

async def tmdb_request(endpoint: str, params: dict = None) -> Optional[dict]:
    params = params or {}
    cache_key = f"tmdb_{endpoint}?{params}"
    now = time.time()
    
    if cache_key in _cache:
        ts, data = _cache[cache_key]
        if now - ts < CACHE_TTL:
            return data

    headers = {"accept": "application/json"}
    if TMDB_API_KEY.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {TMDB_API_KEY}"
    else:
        params["api_key"] = TMDB_API_KEY

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            url = f"{TMDB_BASE_URL}{endpoint}"
            logger.info(f"TMDB Request: {url}")
            resp = await client.get(url, params=params, headers=headers)
            logger.info(f"TMDB Status: {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
            _cache[cache_key] = (now, data)
            return data
        except Exception as e:
            logger.error(f"TMDB Error: {e}")
            return None

async def omdb_search(query: str) -> list:
    """Поиск через OMDb API (работает даже когда TMDB заблокирован)"""
    if not OMDB_API_KEY or OMDB_API_KEY == "ВАШ_OMDB_API_КЛЮЧ":
        logger.warning("OMDb API ключ не настроен")
        return []
    
    cache_key = f"omdb_search_{query}"
    now = time.time()
    if cache_key in _cache:
        ts, data = _cache[cache_key]
        if now - ts < CACHE_TTL:
            return data
    
    try:
        params = {
            "apikey": OMDB_API_KEY,
            "s": query,
            "type": "series"
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            logger.info(f"OMDb Request: {OMDB_BASE_URL} | Query: {query}")
            resp = await client.get(OMDB_BASE_URL, params=params)
            logger.info(f"OMDb Status: {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
            
            if data.get("Response") == "True" and data.get("Search"):
                results = []
                for item in data["Search"]:
                    results.append({
                        "id": f"omdb_{item['imdbID']}",
                        "name": item.get("Title", "Неизвестно"),
                        "year": item.get("Year", "N/A"),
                        "imdb_id": item.get("imdbID", ""),
                        "source": "omdb"
                    })
                _cache[cache_key] = (now, results)
                logger.info(f"OMDb found {len(results)} results")
                return results
            else:
                logger.info(f"OMDb: {data.get('Error', 'No results')}")
                return []
    except Exception as e:
        logger.error(f"OMDb Error: {e}")
        return []

async def omdb_get_details(imdb_id: str) -> Optional[dict]:
    """Получение деталей сериала по IMDb ID через OMDb"""
    if not OMDB_API_KEY or OMDB_API_KEY == "ВАШ_OMDB_API_КЛЮЧ":
        return None
    
    cache_key = f"omdb_detail_{imdb_id}"
    now = time.time()
    if cache_key in _cache:
        ts, data = _cache[cache_key]
        if now - ts < CACHE_TTL:
            return data
    
    try:
        params = {
            "apikey": OMDB_API_KEY,
            "i": imdb_id,
            "plot": "short"
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(OMDB_BASE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            if data.get("Response") == "True":
                _cache[cache_key] = (now, data)
                return data
            return None
    except Exception as e:
        logger.error(f"OMDb Detail Error: {e}")
        return None

async def search_myshows(query: str) -> list:
    """Резервный поиск через MyShows.me"""
    try:
        url = f"https://myshows.me/search/?q={urllib.parse.quote(query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers, follow_redirects=True)
            if resp.status_code == 403:
                logger.warning("MyShows: Cloudflare 403")
                return []
            resp.raise_for_status()
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        results = []
        
        # Пробуем разные селекторы (MyShows может менять структуру)
        for selector in ['div.show-title a', 'h2 a', '.search-results a', 'a[href*="/shows/"]']:
            for a in soup.select(selector):
                if a.get('href') and '/shows/' in a.get('href', ''):
                    title = a.get_text(strip=True)
                    href = a['href']
                    if title and len(title) > 2:
                        parts = href.strip('/').split('/')
                        if len(parts) >= 2:
                            show_id = parts[1] if parts[0] == 'shows' else parts[0]
                            results.append({
                                'id': f"ms_{show_id}",
                                'name': title,
                                'source': 'myshows',
                                'href': href
                            })
                            if len(results) >= 5:
                                break
            if results:
                break
        
        logger.info(f"MyShows found {len(results)} results")
        return results
    except Exception as e:
        logger.error(f"MyShows Error: {e}")
        return []

async def hybrid_search(query: str) -> list:
    logger.info(f"--- ПОИСК: '{query}' ---")
    
    # 1. TMDB (RU)
    data_ru = await tmdb_request("/search/tv", {"query": query, "language": "ru-RU", "include_adult": "false"})
    if data_ru and data_ru.get("results") and len(data_ru["results"]) > 0:
        logger.info(f"TMDB (RU): {len(data_ru['results'])} результатов")
        return data_ru["results"]
    
    # 2. TMDB (EN)
    data_en = await tmdb_request("/search/tv", {"query": query, "language": "en-US", "include_adult": "false"})
    if data_en and data_en.get("results") and len(data_en["results"]) > 0:
        logger.info(f"TMDB (EN): {len(data_en['results'])} результатов")
        return data_en["results"]
    
    # 3. OMDb (запасной вариант, работает всегда)
    logger.info("TMDB недоступен, пробуем OMDb...")
    omdb_results = await omdb_search(query)
    if omdb_results:
        return omdb_results
    
    # 4. MyShows (последний резерв)
    logger.info("OMDb не дал результатов, пробуем MyShows...")
    ms_results = await search_myshows(query)
    if ms_results:
        return ms_results
    
    logger.info("--- НИЧЕГО НЕ НАЙДЕНО ---")
    return []

def get_title_with_fallback(item: dict) -> str:
    if 'name' in item:
        return item.get('name') or item.get('original_name') or 'Неизвестно'
    return item.get('title') or item.get('original_title') or item.get('Title') or 'Неизвестно'

# ==========================================
# --- AIОGRAM SETUP ---
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
        year = r.get('first_air_date', '')[:4] if r.get('first_air_date') else r.get('year', 'N/A')
        source_icon = "🇷🇺" if r.get('source') == 'myshows' else ("🎬" if r.get('source') == 'omdb' else "🎥")
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
# --- ОБРАБОТЧИКИ ---
# ==========================================
@router.message(CommandStart())
async def cmd_start(message: Message):
    await register_user(message.from_user.id)
    await message.answer(
        "👋 Привет! Я отслеживаю выход новых серий.\n"
        "💡 Поиск: TMDB → OMDb → MyShows (работает всегда!)\n"
        "Все действия обновляют одно сообщение.",
        reply_markup=await main_kb()
    )

@router.message(Command("test_search"))
async def cmd_test_search(message: Message):
    if str(message.from_user.id) != str(ADMIN_ID):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: `/test_search <название>`", parse_mode="Markdown")
        return
    query = parts[1].strip()
    loading_msg = await message.answer(f"🔍 Тестирую: '{query}'...")
    results = await hybrid_search(query)
    if not results:
        await loading_msg.edit_text(f"❌ Ничего не найдено для '{query}'.\nПроверьте логи.")
    else:
        titles = [get_title_with_fallback(r) for r in results]
        await loading_msg.edit_text(f"✅ Найдено {len(results)}:\n\n" + "\n".join(f"• {t}" for t in titles))

@router.callback_query(F.data == "main_menu")
async def go_main(callback: CallbackQuery):
    await callback.message.edit_text("📺 Главное меню:", reply_markup=await main_kb())
    await callback.answer()

@router.callback_query(F.data == "add_show")
async def cmd_add(callback: CallbackQuery, state: FSMContext):
    await register_user(callback.from_user.id)
    await callback.message.edit_text(
        "🔍 Введите название сериала (RU/EN):",
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
    loading_msg = await message.answer("🔎 Ищу...")
    try:
        await message.delete()
    except Exception:
        pass

    if not query:
        await loading_msg.edit_text("❌ Введите название.", reply_markup=await main_kb())
        return
    
    results = await hybrid_search(query)
    
    if not results:
        await loading_msg.edit_text(
            f"❌ По запросу '{query}' ничего не найдено.\n\n"
            "💡 Попробуйте оригинальное название на английском.",
            reply_markup=await main_kb()
        )
    else:
        await loading_msg.edit_text(
            f"📺 Найдено: {len(results)}\n🔍 Запрос: '{query}'\n\nВыберите сериал:",
            reply_markup=search_kb(results)
        )

@router.callback_query(F.data.startswith("select_"))
async def select_show(callback: CallbackQuery):
    item_id = callback.data.split("_", 1)[1]
    await callback.answer("⏳ Загружаю...")
    
    # OMDb результат
    if item_id.startswith("omdb_"):
        imdb_id = item_id.replace("omdb_", "")
        details = await omdb_get_details(imdb_id)
        if details:
            title = details.get("Title", "Неизвестно")
            await add_sub(callback.from_user.id, 0, title, imdb_id)
            await callback.message.edit_text(
                f"✅ **{title}** добавлен!\n\n🔔 Уведомления будут приходить при выходе новых серий.",
                reply_markup=await main_kb()
            )
        else:
            await callback.message.edit_text("⚠️ Не удалось загрузить данные.", reply_markup=await main_kb())
        return
    
    # MyShows результат
    if item_id.startswith("ms_"):
        await callback.message.edit_text(
            "⚠️ Для отслеживания выберите сериал из TMDB (🎥) или OMDb (🎬).",
            reply_markup=await main_kb()
        )
        return

    # TMDB результат
    tmdb_id = int(item_id)
    info = await tmdb_request(f"/tv/{tmdb_id}")
    if not info:
        await callback.message.edit_text("⚠️ Не удалось загрузить данные.", reply_markup=await main_kb())
        return
    
    title = get_title_with_fallback(info)
    imdb_id = info.get('external_ids', {}).get('imdb_id', 'N/A')
    
    await add_sub(callback.from_user.id, tmdb_id, title, imdb_id)
    await callback.message.edit_text(
        f"✅ **{title}** добавлен!\n\n🔔 Уведомления будут приходить при выходе новых серий.",
        reply_markup=await main_kb()
    )

@router.callback_query(F.data == "my_shows")
async def cmd_my(callback: CallbackQuery):
    shows = await get_subs(callback.from_user.id)
    if not shows:
        await callback.message.edit_text(
            "📭 Пока нет сериалов.\nНажмите '➕ Добавить сериал'.",
            reply_markup=await main_kb()
        )
    else:
        await callback.message.edit_text(
            f"📺 Ваши подписки ({len(shows)}):",
            reply_markup=subs_kb(shows)
        )
    await callback.answer()

@router.callback_query(F.data.startswith("del_"))
async def cmd_del(callback: CallbackQuery):
    tmdb_id = int(callback.data.split("_", 1)[1])
    await remove_sub(callback.from_user.id, tmdb_id)
    shows = await get_subs(callback.from_user.id)
    if not shows:
        await callback.message.edit_text("📭 Пока нет сериалов.", reply_markup=await main_kb())
    else:
        await callback.message.edit_text(f"📺 Ваши подписки ({len(shows)}):", reply_markup=subs_kb(shows))
    await callback.answer("🗑 Удалено")

@router.callback_query(F.data == "force_check")
async def cmd_force(callback: CallbackQuery):
    await callback.answer("🔄 Проверка запущена...")
    await callback.message.edit_text("✅ Запрос отправлен.", reply_markup=await main_kb())
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

async def show_admin_menu(target: Message | CallbackQuery):
    status = await get_donate_status()
    status_text = "✅ Включена" if status else "❌ Отключена"
    stats = await get_bot_stats()
    
    text = (
        f"⚙️ **Панель администратора**\n\n"
        f"👤 Ваш ID: `{target.from_user.id}`\n\n"
        f"📊 **Статистика:**\n"
        f"👥 Пользователей: `{stats['total_users']}`\n"
        f"📺 Активных: `{stats['active_subscribers']}`\n"
        f"⭐️ Звезд: `{stats['total_stars']}`\n\n"
        f"💎 Донаты: {status_text}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Переключить донаты", callback_data="toggle_donate")],
        [InlineKeyboardButton(text="🔙 Меню", callback_data="main_menu")]
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
        f"📊 Пользователей: `{stats['total_users']}`\n"
        f"📺 Активных: `{stats['active_subscribers']}`\n"
        f"⭐️ Звезд: `{stats['total_stars']}`\n\n"
        f"💎 Донаты: {status_text}\n\n✅ Сохранено!"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Переключить донаты", callback_data="toggle_donate")],
        [InlineKeyboardButton(text="🔙 Меню", callback_data="main_menu")]
    ])
    
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer(f"Донаты: {status_text}")

# ==========================================
# --- DONATE ---
# ==========================================
@router.callback_query(F.data == "donate_menu")
async def cmd_donate_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "💎 **Поддержка проекта**\n\nВыберите сумму:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="10 ⭐️", callback_data="donate_10"), InlineKeyboardButton(text="50 ⭐️", callback_data="donate_50")],
            [InlineKeyboardButton(text="100 ⭐️", callback_data="donate_100"), InlineKeyboardButton(text="500 ⭐️", callback_data="donate_500")],
            [InlineKeyboardButton(text="✏️ Своя сумма", callback_data="donate_custom")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]
        ])
    )
    await callback.answer()

@router.callback_query(F.data.in_({"donate_10", "donate_50", "donate_100", "donate_500"}))
async def process_fixed_donation(callback: CallbackQuery):
    amount = int(callback.data.split("_")[1])
    await callback.answer()
    await callback.message.answer_invoice(
        title=f"💎 Поддержка ({amount} ⭐️)",
        description="Спасибо за поддержку!",
        payload=f"donate_{amount}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Donation", amount=amount)], 
        reply_markup=await main_kb()
    )

@router.callback_query(F.data == "donate_custom")
async def cmd_donate_custom(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "✏️ Введите сумму (мин. 10 ⭐️):",
        reply_markup=cancel_donate_kb()
    )
    await state.set_state(DonateStates.waiting_for_amount)
    await callback.answer()

@router.callback_query(F.data == "cancel_donate")
async def cmd_cancel_donate(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Отменено.", reply_markup=await main_kb())
    await callback.answer()

@router.message(DonateStates.waiting_for_amount)
async def process_custom_amount(message: Message, state: FSMContext):
    await state.clear()
    response_msg = await message.answer("Обработка...")
    try:
        await message.delete()
    except Exception:
        pass

    try:
        amount = int(message.text.strip())
    except ValueError:
        await response_msg.edit_text("❌ Введите число.", reply_markup=await main_kb())
        return

    if amount < 10:
        await response_msg.edit_text("❌ Минимум: 10 ⭐️", reply_markup=await main_kb())
        return

    await response_msg.edit_text("Формирую счет...", reply_markup=await main_kb())
    await response_msg.answer_invoice(
        title=f"💎 Поддержка ({amount} ⭐️)",
        description=f"Сумма: {amount} Stars.",
        payload=f"donate_{amount}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Donation", amount=amount)],
        reply_markup=await main_kb()
    )

@router.pre_checkout_query()
async def on_pre_checkout_query(pre_checkout_q: PreCheckoutQuery):
    await pre_checkout_q.answer(ok=True)

@router.message(F.successful_payment)
async def on_successful_payment(message: Message):
    amount = message.successful_payment.total_amount
    await add_stars(message.from_user.id, amount)
    await message.answer(
        f"🎉 Спасибо! Вы задонатили **{amount} ⭐️**.\nВаш вклад очень важен! ❤️",
        reply_markup=await main_kb()
    )

# ==========================================
# --- ФОНОВАЯ ПРОВЕРКА ---
# ==========================================
async def check_new_episodes(force: bool = False):
    logger.info("Проверка новых серий...")
    now = time.time()
    async with db.execute("SELECT user_id, tmdb_id, title, imdb_id, last_season, last_episode, last_checked FROM subscriptions") as cur:
        subs = await cur.fetchall()

    for user_id, tmdb_id, title, imdb_id, last_s, last_e, last_checked in subs:
        if not force and (now - last_checked) < MIN_CHECK_INTERVAL:
            continue

        current_seasons = last_s
        current_episodes = last_e
        
        # Если есть TMDB ID, используем TMDB
        if tmdb_id > 0:
            info = await tmdb_request(f"/tv/{tmdb_id}")
            if info:
                if info.get("status") in ["Canceled", "Ended"]:
                    await update_sub(user_id, tmdb_id, last_s, last_e, now)
                    continue
                current_seasons = info.get("number_of_seasons", last_s)
                season_data = await tmdb_request(f"/tv/{tmdb_id}/season/{current_seasons}")
                if season_data:
                    current_episodes = len(season_data.get("episodes", []))
        # Если есть IMDb ID, используем OMDb
        elif imdb_id and imdb_id != 'N/A':
            details = await omdb_get_details(imdb_id)
            if details:
                total_seasons = details.get("totalSeasons")
                if total_seasons and total_seasons != "N/A":
                    try:
                        current_seasons = int(total_seasons)
                    except:
                        pass

        if current_seasons > last_s or current_episodes > last_e:
            try:
                await bot.send_message(
                    user_id, 
                    f"🎉 Вышла новая серия!\n📺 **{title}**\n🔹 Сезон {current_seasons}, Серия {current_episodes}"
                )
                logger.info(f"Новая серия {title} для {user_id}")
            except Exception as e:
                logger.error(f"Ошибка отправки {user_id}: {e}")

        await update_sub(user_id, tmdb_id, current_seasons, current_episodes, now)

# ==========================================
# --- ЗАПУСК ---
# ==========================================
async def main():
    await init_db()
    asyncio.create_task(check_new_episodes())
    logger.info(f"🤖 Бот запущен! Admin ID: {ADMIN_ID}")
    try:
        await dp.start_polling(bot)
    finally:
        if db:
            await db.close()

if __name__ == "__main__":
    asyncio.run(main())
