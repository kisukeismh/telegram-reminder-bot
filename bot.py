import os
import asyncio
import re
import aiosqlite
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# --- НАСТРОЙКИ ---
TOKEN = os.environ.get("BOT_TOKEN", "8619291995:AAHKm8AVF5CWhnCfc8YDs4VkwDeyMBZwZ0I")
DB_NAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders.db")
MOSCOW_OFFSET = timezone(timedelta(hours=3))

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def moscow_to_utc(time_str: str) -> str:
    dt = datetime.strptime(time_str, '%d.%m.%Y %H:%M').replace(tzinfo=MOSCOW_OFFSET)
    return dt.astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M')

def utc_to_moscow(time_str: str) -> str:
    dt = datetime.strptime(time_str, '%d.%m.%Y %H:%M').replace(tzinfo=timezone.utc)
    return dt.astimezone(MOSCOW_OFFSET).strftime('%d.%m.%Y %H:%M')

def get_preset_utc_time(preset: str) -> str:
    now_utc = datetime.now(timezone.utc)
    if preset == '15m':
        target = now_utc + timedelta(minutes=15)
    elif preset == '1h':
        target = now_utc + timedelta(hours=1)
    elif preset == 'tom_9':
        tomorrow = (now_utc + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
        target = tomorrow
    elif preset == 'tom_18':
        tomorrow = (now_utc + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
        target = tomorrow
    else:
        target = now_utc + timedelta(minutes=5)
    return target.strftime('%d.%m.%Y %H:%M')

def parse_user_time(text: str, now_msk: datetime) -> datetime:
    text = text.strip()
    if ':' in text and '.' not in text:
        t = datetime.strptime(text, '%H:%M')
        target = now_msk.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if target <= now_msk:
            target += timedelta(days=1)
        return target
    else:
        return datetime.strptime(text, '%d.%m.%Y %H:%M').replace(tzinfo=MOSCOW_OFFSET)

def parse_advance_input(text: str) -> int:
    """
    Парсит ввод интервала предварительного напоминания.
    Возвращает количество минут (int).
    Поддерживает:
      - '0' или 'off' — отключить (возвращает 0)
      - '5', '30' — минуты
      - '5m', '30m', '5м' — минуты
      - '1h', '2h', '1ч', '2ч' — часы
      - '1.5h' — полтора часа (90 минут)
    """
    text = text.strip().lower().replace(' ', '')
    
    if text in ('0', 'off', 'нет', 'no', 'выкл'):
        return 0
    
    # Часы: 1h, 2ч, 1.5h
    m = re.match(r'^(\d+(?:[.,]\d+)?)\s*(h|ч|час|часа|часов)$', text)
    if m:
        value = float(m.group(1).replace(',', '.'))
        return max(1, int(round(value * 60)))
    
    # Минуты: 5m, 30м, 5мин, 30минут
    m = re.match(r'^(\d+(?:[.,]\d+)?)\s*(m|м|min|мин|минут|минуты)?$', text)
    if m:
        value = float(m.group(1).replace(',', '.'))
        return max(1, int(round(value)))
    
    raise ValueError("Неверный формат")

# --- СОСТОЯНИЯ (FSM) ---
class AddEvent(StatesGroup):
    text = State()
    time = State()
    time_select = State()
    advance = State()
    advance_manual = State()   # ⭐ НОВОЕ: ручной ввод интервала
    repeat = State()

class EditEvent(StatesGroup):
    time = State()
    event_id = State()

# --- ИНИЦИАЛИЗАЦИЯ БД ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                text TEXT,
                event_time TEXT,
                repeat_type TEXT,
                notified_5min INTEGER DEFAULT 0,
                advance_minutes INTEGER DEFAULT 5
            )
        ''')
        cursor = await db.execute("PRAGMA table_info(events)")
        columns = [row[1] for row in await cursor.fetchall()]
        if 'advance_minutes' not in columns:
            await db.execute("ALTER TABLE events ADD COLUMN advance_minutes INTEGER DEFAULT 5")
        await db.commit()

# --- ФОНОВАЯ ПРОВЕРКА НАПОМИНАНИЙ ---
async def reminder_loop(bot: Bot):
    while True:
        await asyncio.sleep(5)
        now = datetime.now(timezone.utc)
        now_str = now.strftime('%d.%m.%Y %H:%M')
        
        async with aiosqlite.connect(DB_NAME) as db:
            cursor = await db.execute(
                "SELECT id, user_id, text, event_time, advance_minutes FROM events "
                "WHERE notified_5min = 0 AND advance_minutes > 0"
            )
            rows = await cursor.fetchall()
            for e_id, user_id, text, e_time, adv_min in rows:
                event_dt = datetime.strptime(e_time, '%d.%m.%Y %H:%M').replace(tzinfo=timezone.utc)
                remind_dt = event_dt - timedelta(minutes=adv_min)
                if remind_dt <= now < event_dt:
                    moscow_time = utc_to_moscow(e_time)
                    try:
                        await bot.send_message(
                            user_id,
                            f"⏳ <b>Через {adv_min} мин. до события:</b>\n\n{text}\n<i>Время: {moscow_time}</i>",
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        print(f"Ошибка отправки предварительного уведомления: {e}")
                    await db.execute("UPDATE events SET notified_5min = 1 WHERE id = ?", (e_id,))

            cursor = await db.execute(
                "SELECT id, user_id, text, event_time, repeat_type FROM events WHERE event_time <= ?",
                (now_str,)
            )
            for e_id, user_id, text, e_time, repeat in await cursor.fetchall():
                try:
                    await bot.send_message(
                        user_id,
                        f"⏰ <b>Напоминание:</b>\n\n{text}",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    print(f"Ошибка отправки напоминания: {e}")

                current_dt = datetime.strptime(e_time, '%d.%m.%Y %H:%M').replace(tzinfo=timezone.utc)
                if repeat == 'daily':
                    new_dt = current_dt + timedelta(days=1)
                elif repeat == 'weekly':
                    new_dt = current_dt + timedelta(weeks=1)
                else:
                    await db.execute("DELETE FROM events WHERE id = ?", (e_id,))
                    continue
                
                await db.execute(
                    "UPDATE events SET event_time = ?, notified_5min = 0 WHERE id = ?", 
                    (new_dt.strftime('%d.%m.%Y %H:%M'), e_id)
                )
            await db.commit()

# --- РОУТЕР ---
router = Router()

# --- КЛАВИАТУРЫ ---
def get_main_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить событие", callback_data="menu_add"),
         InlineKeyboardButton(text="📋 Мои события", callback_data="menu_list")],
        [InlineKeyboardButton(text="❌ Отменить действие", callback_data="menu_cancel"),
         InlineKeyboardButton(text="❓ Помощь", callback_data="menu_help")]
    ])

def get_advance_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="5 минут", callback_data="adv_5"),
         InlineKeyboardButton(text="15 минут", callback_data="adv_15")],
        [InlineKeyboardButton(text="30 минут", callback_data="adv_30"),
         InlineKeyboardButton(text="1 час", callback_data="adv_60")],
        [InlineKeyboardButton(text="🔕 Без предв. напоминания", callback_data="adv_0")],
        [InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="adv_manual")],  # ⭐ НОВОЕ
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
    ])

def get_time_select_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱ Через 15 мин", callback_data="time_15m"),
         InlineKeyboardButton(text="⏱ Через 1 час", callback_data="time_1h")],
        [InlineKeyboardButton(text="📅 Завтра 9:00", callback_data="time_tom_9"),
         InlineKeyboardButton(text="📅 Завтра 18:00", callback_data="time_tom_18")],
        [InlineKeyboardButton(text="📝 Ввести вручную", callback_data="time_manual")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_text")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
    ])

def get_repeat_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Не повторять", callback_data="repeat_none"),
         InlineKeyboardButton(text="Ежедневно", callback_data="repeat_daily")],
        [InlineKeyboardButton(text="Еженедельно", callback_data="repeat_weekly")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_advance")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
    ])

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 <b>Привет! Я бот-напоминалка</b>\n\n"
        "Все время указывается <b>по Москве</b>.\n\n"
        "Выберите действие:",
        parse_mode="HTML",
        reply_markup=get_main_menu_keyboard()
    )

@router.message(Command("menu"))
async def cmd_menu(message: types.Message):
    await message.answer(
        "📋 <b>Главное меню</b>\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=get_main_menu_keyboard()
    )

# --- ОБРАБОТЧИКИ МЕНЮ ---
@router.callback_query(F.data == "menu_add")
async def menu_add(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AddEvent.text)
    await callback.message.edit_text(
        "Введите описание события:", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
        ])
    )
    await callback.answer()

@router.callback_query(F.data == "menu_list")
async def menu_list(callback: types.CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT id, text, event_time, repeat_type, advance_minutes FROM events WHERE user_id = ? ORDER BY event_time ASC", 
            (callback.from_user.id,)
        )
        events = await cursor.fetchall()
    
    if not events:
        await callback.message.edit_text(
            "📋 У вас нет запланированных событий.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
            ])
        )
        await callback.answer()
        return
    
    text = "📋 <b>Ваши события:</b>\n\n"
    kb = []
    for e_id, txt, tm, rep, adv_min in events:
        icon = {"none": "🔹", "daily": "🔁", "weekly": "🔂"}.get(rep, "")
        adv_info = f" ⏳({adv_min} мин)" if adv_min > 0 else ""
        text += f"{icon} <b>{utc_to_moscow(tm)}</b>{adv_info}\n{txt}\n\n"
        kb.append([
            InlineKeyboardButton(text="⏳ Изм. время", callback_data=f"edit_time_{e_id}"), 
            InlineKeyboardButton(text="❌ Удалить", callback_data=f"del_{e_id}")
        ])
    
    kb.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "menu_cancel")
async def menu_cancel(callback: types.CallbackQuery, state: FSMContext):
    if await state.get_state():
        await state.clear()
        await callback.message.edit_text(
            "❌ Действие отменено.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
            ])
        )
    else:
        await callback.message.edit_text(
            "ℹ️ Нечего отменять.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
            ])
        )
    await callback.answer()

@router.callback_query(F.data == "menu_help")
async def menu_help(callback: types.CallbackQuery):
    await callback.message.edit_text(get_help_text(), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
        ])
    )
    await callback.answer()

@router.callback_query(F.data == "menu_main")
async def menu_main(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "📋 <b>Главное меню</b>\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=get_main_menu_keyboard()
    )
    await callback.answer()

# --- КОМАНДЫ ---
@router.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    if await state.get_state():
        await state.clear()
        await message.answer("❌ Действие отменено.", reply_markup=get_main_menu_keyboard())
    else:
        await message.answer("Нечего отменять.", reply_markup=get_main_menu_keyboard())

@router.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(get_help_text(), parse_mode="HTML", reply_markup=get_main_menu_keyboard())

def get_help_text() -> str:
    return (
        "❓ <b>Помощь</b>\n\n"
        "📌 <b>Как добавить событие:</b>\n"
        "1. Нажмите ➕ Добавить событие\n"
        "2. Введите описание\n"
        "3. Выберите время (кнопки или вручную)\n"
        "4. Выберите, за сколько минут предупредить\n"
        "5. Выберите повторение\n\n"
        "📌 <b>Форматы времени (ввод вручную):</b>\n"
        "• <b>ДД.ММ.ГГГГ ЧЧ:ММ</b> — полная дата и время\n"
        "• <b>ЧЧ:ММ</b> — только время (сегодня, или завтра, если время уже прошло)\n\n"
        "📌 <b>Предварительное напоминание:</b>\n"
        "Можно выбрать готовый вариант (5/15/30 мин, 1 час, отключить) "
        "или ввести вручную:\n"
        "• <b>5</b> или <b>5m</b> — 5 минут\n"
        "• <b>30м</b> — 30 минут\n"
        "• <b>2h</b> или <b>2ч</b> — 2 часа\n"
        "• <b>1.5h</b> — полтора часа\n"
        "• <b>0</b> — отключить\n\n"
        "📌 <b>Как просмотреть события:</b>\n"
        "Нажмите 📋 Мои события\n\n"
        "📌 <b>Как изменить время:</b>\n"
        "В списке событий нажмите ⏳ Изм. время\n\n"
        "📌 <b>Время:</b>\n"
        "Все время указывается <b>по Москве</b>.\n"
        "Прошедшие даты ввести нельзя.\n\n"
        "📌 <b>Команды:</b>\n"
        "/start — главное меню\n"
        "/menu — повторить меню\n"
        "/add — добавить событие\n"
        "/list — мои события\n"
        "/cancel — отменить действие\n"
        "/help — эта помощь"
    )

# --- ДОБАВЛЕНИЕ СОБЫТИЯ ---
@router.message(Command("add"))
async def cmd_add(message: types.Message, state: FSMContext):
    await state.set_state(AddEvent.text)
    await message.answer(
        "Введите описание события:", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
        ])
    )

@router.callback_query(F.data == "cancel_add")
async def cancel_add(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Отменено.", reply_markup=get_main_menu_keyboard())
    await callback.answer()

@router.message(AddEvent.text)
async def process_text(message: types.Message, state: FSMContext):
    await state.update_data(text=message.text)
    await state.set_state(AddEvent.time_select)
    await message.answer("Когда напомнить?", reply_markup=get_time_select_keyboard())

@router.callback_query(F.data == "back_to_text", AddEvent.time_select)
async def back_to_text(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AddEvent.text)
    await callback.message.edit_text(
        "Введите описание события:", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
        ])
    )
    await callback.answer()

@router.callback_query(AddEvent.time_select, F.data.startswith("time_"))
async def handle_preset_time(callback: types.CallbackQuery, state: FSMContext):
    preset = callback.data.split("_", 1)[1]
    
    if preset == 'manual':
        await state.set_state(AddEvent.time)
        await callback.message.edit_text(
            "Введите время в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b> или просто <b>ЧЧ:ММ</b> (по Москве)\n\n"
            "Если ввести только время — напоминание будет на сегодня "
            "(или на завтра, если это время уже прошло).",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_time_select")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
            ])
        )
        await callback.answer()
        return

    utc_time = get_preset_utc_time(preset)
    await state.update_data(time=utc_time)
    await state.set_state(AddEvent.advance)
    
    await callback.message.edit_text(
        "За сколько минут предупредить о событии?",
        reply_markup=get_advance_keyboard()
    )
    await callback.answer()

@router.callback_query(F.data == "back_to_time_select")
async def back_to_time_select(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AddEvent.time_select)
    await callback.message.edit_text("Когда напомнить?", reply_markup=get_time_select_keyboard())
    await callback.answer()

@router.callback_query(F.data == "back_to_advance")
async def back_to_advance(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AddEvent.advance)
    await callback.message.edit_text(
        "За сколько минут предупредить о событии?",
        reply_markup=get_advance_keyboard()
    )
    await callback.answer()

@router.message(AddEvent.time)
async def process_time_manual(message: types.Message, state: FSMContext):
    now_msk = datetime.now(MOSCOW_OFFSET)
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
    ])
    
    try:
        target = parse_user_time(message.text, now_msk)
        
        text = message.text.strip()
        if ':' in text and '.' in text:
            if target <= now_msk:
                await message.answer(
                    "⚠️ <b>Это время уже прошло.</b>\n"
                    "Введите дату и время в будущем, или введите только время (ЧЧ:ММ) — "
                    "тогда напоминание будет на сегодня/завтра.",
                    parse_mode="HTML",
                    reply_markup=cancel_kb
                )
                return
        
        time_str = target.strftime('%d.%m.%Y %H:%M')
        await state.update_data(time=moscow_to_utc(time_str))
        await state.set_state(AddEvent.advance)
        
        await message.answer(
            "За сколько минут предупредить о событии?",
            reply_markup=get_advance_keyboard()
        )
    except ValueError:
        await message.answer(
            "Неверный формат. Используйте <b>ДД.ММ.ГГГГ ЧЧ:ММ</b> или <b>ЧЧ:ММ</b>", 
            parse_mode="HTML",
            reply_markup=cancel_kb
        )

# ⭐ НОВОЕ: ручной ввод интервала предварительного напоминания
@router.callback_query(AddEvent.advance, F.data == "adv_manual")
async def advance_manual_prompt(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AddEvent.advance_manual)
    await callback.message.edit_text(
        "✏️ <b>Введите интервал</b> для предварительного напоминания.\n\n"
        "Примеры:\n"
        "• <code>5</code> или <code>5m</code> — 5 минут\n"
        "• <code>30м</code> — 30 минут\n"
        "• <code>2h</code> или <code>2ч</code> — 2 часа\n"
        "• <code>1.5h</code> — полтора часа (90 минут)\n"
        "• <code>0</code> — отключить предварительное напоминание",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад к пресетам", callback_data="back_to_advance_from_manual")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
        ])
    )
    await callback.answer()

@router.callback_query(F.data == "back_to_advance_from_manual")
async def back_to_advance_from_manual(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AddEvent.advance)
    await callback.message.edit_text(
        "За сколько минут предупредить о событии?",
        reply_markup=get_advance_keyboard()
    )
    await callback.answer()

@router.message(AddEvent.advance_manual)
async def process_advance_manual(message: types.Message, state: FSMContext):
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
    ])
    
    try:
        minutes = parse_advance_input(message.text)
        
        # Ограничение: не больше года (525600 минут)
        if minutes > 525600:
            await message.answer(
                "⚠️ Слишком большое значение. Максимум — 525600 минут (1 год).",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Попробовать снова", callback_data="back_to_advance_from_manual")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
                ])
            )
            return
        
        await state.update_data(advance_minutes=minutes)
        await state.set_state(AddEvent.repeat)
        
        await message.answer("Нужно ли повторять событие?", reply_markup=get_repeat_keyboard())
    except ValueError:
        await message.answer(
            "❌ Не удалось распознать. Примеры:\n"
            "• <code>5</code> — 5 минут\n"
            "• <code>30m</code> — 30 минут\n"
            "• <code>2h</code> или <code>2ч</code> — 2 часа\n"
            "• <code>0</code> — отключить",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К пресетам", callback_data="back_to_advance_from_manual")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
            ])
        )

@router.callback_query(AddEvent.advance, F.data.startswith("adv_"))
async def process_advance(callback: types.CallbackQuery, state: FSMContext):
    adv = int(callback.data.split("_")[1])
    await state.update_data(advance_minutes=adv)
    await state.set_state(AddEvent.repeat)
    await callback.message.edit_text("Нужно ли повторять событие?", reply_markup=get_repeat_keyboard())
    await callback.answer()

@router.callback_query(AddEvent.repeat, F.data.startswith("repeat_"))
async def process_repeat(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    repeat = callback.data.split("_")[1]
    advance_minutes = data.get('advance_minutes', 5)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO events (user_id, text, event_time, repeat_type, notified_5min, advance_minutes) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (callback.from_user.id, data['text'], data['time'], repeat, advance_minutes)
        )
        await db.commit()
    await state.clear()
    await callback.message.edit_text("✅ Событие добавлено!", reply_markup=get_main_menu_keyboard())
    await callback.answer()

# --- ПРОСМОТР ---
@router.message(Command("list"))
async def cmd_list(message: types.Message):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT id, text, event_time, repeat_type, advance_minutes FROM events WHERE user_id = ? ORDER BY event_time ASC", 
            (message.from_user.id,)
        )
        events = await cursor.fetchall()
    if not events:
        await message.answer("Нет событий.", reply_markup=get_main_menu_keyboard())
        return
    text = "📋 <b>Ваши события:</b>\n\n"
    kb = []
    for e_id, txt, tm, rep, adv_min in events:
        icon = {"none": "🔹", "daily": "🔁", "weekly": "🔂"}.get(rep, "")
        adv_info = f" ⏳({adv_min} мин)" if adv_min > 0 else ""
        text += f"{icon} <b>{utc_to_moscow(tm)}</b>{adv_info}\n{txt}\n\n"
        kb.append([
            InlineKeyboardButton(text="⏳ Изм. время", callback_data=f"edit_time_{e_id}"), 
            InlineKeyboardButton(text="❌ Удалить", callback_data=f"del_{e_id}")
        ])
    kb.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

# --- РЕДАКТИРОВАНИЕ ---
@router.callback_query(F.data.startswith("edit_time_"))
async def process_edit_time(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(edit_event_id=int(callback.data.split("_")[-1]))
    await state.set_state(EditEvent.time)
    await callback.message.edit_text(
        "Введите новое время:\n"
        "• <b>ДД.ММ.ГГГГ ЧЧ:ММ</b> — полная дата\n"
        "• <b>ЧЧ:ММ</b> — только время (сегодня или завтра)",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
        ])
    )
    await callback.answer()

@router.callback_query(F.data == "cancel_edit")
async def cancel_edit(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Отменено.", reply_markup=get_main_menu_keyboard())
    await callback.answer()

@router.message(EditEvent.time)
async def process_new_time(message: types.Message, state: FSMContext):
    now_msk = datetime.now(MOSCOW_OFFSET)
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
    ])
    
    try:
        target = parse_user_time(message.text, now_msk)
        
        text = message.text.strip()
        if ':' in text and '.' in text:
            if target <= now_msk:
                await message.answer(
                    "⚠️ <b>Это время уже прошло.</b>\nВведите время в будущем.",
                    parse_mode="HTML",
                    reply_markup=cancel_kb
                )
                return
        
        new_msk = target.strftime('%d.%m.%Y %H:%M')
        new_utc = moscow_to_utc(new_msk)
        eid = (await state.get_data())['edit_event_id']
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE events SET event_time = ?, notified_5min = 0 WHERE id = ?", (new_utc, eid))
            await db.commit()
        await state.clear()
        await message.answer(
            f"✅ Время изменено на <b>{new_msk}</b> (МСК)",
            parse_mode="HTML",
            reply_markup=get_main_menu_keyboard()
        )
    except ValueError:
        await message.answer(
            "Неверный формат. Используйте <b>ДД.ММ.ГГГГ ЧЧ:ММ</b> или <b>ЧЧ:ММ</b>", 
            parse_mode="HTML",
            reply_markup=cancel_kb
        )

@router.callback_query(F.data.startswith("del_"))
async def process_delete(callback: types.CallbackQuery):
    eid = int(callback.data.split("_")[-1])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM events WHERE id = ?", (eid,))
        await db.commit()
    await callback.message.edit_text("❌ Удалено.", reply_markup=get_main_menu_keyboard())
    await callback.answer()

# --- ЗАПУСК ---
async def main():
    await init_db()
    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(reminder_loop(bot))
    print("Бот запущен...")
    await dp.start_polling(bot)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
