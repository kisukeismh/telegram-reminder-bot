import os
import asyncio
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
    """Конвертирует московское время в UTC для хранения в БД"""
    dt = datetime.strptime(time_str, '%d.%m.%Y %H:%M').replace(tzinfo=MOSCOW_OFFSET)
    return dt.astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M')

def utc_to_moscow(time_str: str) -> str:
    """Конвертирует UTC из БД в московское время для отображения"""
    dt = datetime.strptime(time_str, '%d.%m.%Y %H:%M').replace(tzinfo=timezone.utc)
    return dt.astimezone(MOSCOW_OFFSET).strftime('%d.%m.%Y %H:%M')

def get_preset_utc_time(preset: str) -> str:
    """Возвращает время UTC для пресетов кнопок"""
    now_utc = datetime.now(timezone.utc)
    if preset == '15m':
        target = now_utc + timedelta(minutes=15)
    elif preset == '1h':
        target = now_utc + timedelta(hours=1)
    elif preset == 'tom_9':
        # Завтра 9:00 МСК = завтра 6:00 UTC
        tomorrow = (now_utc + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
        target = tomorrow
    elif preset == 'tom_18':
        # Завтра 18:00 МСК = завтра 15:00 UTC
        tomorrow = (now_utc + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
        target = tomorrow
    else:
        target = now_utc + timedelta(minutes=5)
    return target.strftime('%d.%m.%Y %H:%M')

# --- СОСТОЯНИЯ (FSM) ---
class AddEvent(StatesGroup):
    text = State()
    time = State()
    time_select = State()
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
                notified_5min INTEGER DEFAULT 0
            )
        ''')
        await db.commit()

# --- ФОНОВАЯ ПРОВЕРКА НАПОМИНАНИЙ (КАЖДЫЕ 5 СЕКУНД) ---
async def reminder_loop(bot: Bot):
    while True:
        await asyncio.sleep(5)
        now = datetime.now(timezone.utc)
        now_str = now.strftime('%d.%m.%Y %H:%M')
        in_5min_str = (now + timedelta(minutes=5)).strftime('%d.%m.%Y %H:%M')
        
        async with aiosqlite.connect(DB_NAME) as db:
            # 1. Уведомления "за 5 минут"
            cursor = await db.execute(
                "SELECT id, user_id, text, event_time FROM events "
                "WHERE event_time <= ? AND event_time > ? AND notified_5min = 0",
                (in_5min_str, now_str)
            )
            for e_id, user_id, text, e_time in await cursor.fetchall():
                moscow_time = utc_to_moscow(e_time)
                try:
                    await bot.send_message(
                        user_id,
                        f"⏳ <b>5 минут до события:</b>\n\n{text}\n<i>Время: {moscow_time}</i>",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    print(f"Ошибка отправки 5-min уведомления: {e}")
                await db.execute("UPDATE events SET notified_5min = 1 WHERE id = ?", (e_id,))

            # 2. Основные уведомления в момент события
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

                # Обработка повторяемости
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

# --- ГЛАВНОЕ МЕНЮ ---
def get_main_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить событие", callback_data="menu_add"),
         InlineKeyboardButton(text="📋 Мои события", callback_data="menu_list")],
        [InlineKeyboardButton(text="❌ Отменить действие", callback_data="menu_cancel"),
         InlineKeyboardButton(text="❓ Помощь", callback_data="menu_help")]
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

# --- ОБРАБОТЧИКИ КНОПОК МЕНЮ ---
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
            "SELECT id, text, event_time, repeat_type FROM events WHERE user_id = ? ORDER BY event_time ASC", 
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
    for e_id, txt, tm, rep in events:
        icon = {"none": "🔹", "daily": "🔁", "weekly": "🔂"}.get(rep, "")
        text += f"{icon} <b>{utc_to_moscow(tm)}</b>\n{txt}\n\n"
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
    await callback.message.edit_text(
        "❓ <b>Помощь</b>\n\n"
        "📌 <b>Как добавить событие:</b>\n"
        "1. Нажмите ➕ Добавить событие\n"
        "2. Введите описание\n"
        "3. Выберите время (кнопки или вручную)\n"
        "4. Выберите повторение\n\n"
        "📌 <b>Как просмотреть события:</b>\n"
        "Нажмите 📋 Мои события\n\n"
        "📌 <b>Как изменить время:</b>\n"
        "В списке событий нажмите ⏳ Изм. время\n\n"
        "📌 <b>Время:</b>\n"
        "Все время указывается <b>по Москве</b>.\n"
        "Напоминания приходят за 5 минут и в момент события.\n\n"
        "📌 <b>Команды:</b>\n"
        "/start — главное меню\n"
        "/menu — повторить меню\n"
        "/add — добавить событие\n"
        "/list — мои события\n"
        "/cancel — отменить действие\n"
        "/help — эта помощь",
        parse_mode="HTML",
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
    await message.answer(
        "❓ <b>Помощь</b>\n\n"
        "📌 <b>Как добавить событие:</b>\n"
        "1. Нажмите ➕ Добавить событие\n"
        "2. Введите описание\n"
        "3. Выберите время (кнопки или вручную)\n"
        "4. Выберите повторение\n\n"
        "📌 <b>Как просмотреть события:</b>\n"
        "Нажмите 📋 Мои события\n\n"
        "📌 <b>Как изменить время:</b>\n"
        "В списке событий нажмите ⏳ Изм. время\n\n"
        "📌 <b>Время:</b>\n"
        "Все время указывается <b>по Москве</b>.\n"
        "Напоминания приходят за 5 минут и в момент события.\n\n"
        "📌 <b>Команды:</b>\n"
        "/start — главное меню\n"
        "/menu — повторить меню\n"
        "/add — добавить событие\n"
        "/list — мои события\n"
        "/cancel — отменить действие\n"
        "/help — эта помощь",
        parse_mode="HTML",
        reply_markup=get_main_menu_keyboard()
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
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱ Через 15 мин", callback_data="time_15m"),
         InlineKeyboardButton(text="⏱ Через 1 час", callback_data="time_1h")],
        [InlineKeyboardButton(text="📅 Завтра 9:00", callback_data="time_tom_9"),
         InlineKeyboardButton(text="📅 Завтра 18:00", callback_data="time_tom_18")],
        [InlineKeyboardButton(text="📝 Ввести вручную", callback_data="time_manual")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_text")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
    ])
    await message.answer("Когда напомнить?", reply_markup=kb)

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
    # ⭐ ИСПРАВЛЕНО: split("_", 1) чтобы правильно разобрать "time_tom_9" и "time_tom_18"
    preset = callback.data.split("_", 1)[1]
    
    if preset == 'manual':
        await state.set_state(AddEvent.time)
        await callback.message.edit_text(
            "Введите время в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b> (по Москве)", 
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
    await state.set_state(AddEvent.repeat)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Не повторять", callback_data="repeat_none"),
         InlineKeyboardButton(text="Ежедневно", callback_data="repeat_daily")],
        [InlineKeyboardButton(text="Еженедельно", callback_data="repeat_weekly")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_time_select")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
    ])
    await callback.message.edit_text("Нужно ли повторять событие?", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "back_to_time_select", AddEvent.repeat)
async def back_to_time_select(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AddEvent.time_select)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱ Через 15 мин", callback_data="time_15m"),
         InlineKeyboardButton(text="⏱ Через 1 час", callback_data="time_1h")],
        [InlineKeyboardButton(text="📅 Завтра 9:00", callback_data="time_tom_9"),
         InlineKeyboardButton(text="📅 Завтра 18:00", callback_data="time_tom_18")],
        [InlineKeyboardButton(text="📝 Ввести вручную", callback_data="time_manual")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_text")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
    ])
    await callback.message.edit_text("Когда напомнить?", reply_markup=kb)
    await callback.answer()

@router.message(AddEvent.time)
async def process_time_manual(message: types.Message, state: FSMContext):
    try:
        datetime.strptime(message.text, '%d.%m.%Y %H:%M')
        await state.update_data(time=moscow_to_utc(message.text))
        await state.set_state(AddEvent.repeat)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Не повторять", callback_data="repeat_none"),
             InlineKeyboardButton(text="Ежедневно", callback_data="repeat_daily")],
            [InlineKeyboardButton(text="Еженедельно", callback_data="repeat_weekly")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_time_select")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
        ])
        await message.answer("Нужно ли повторять событие?", reply_markup=kb)
    except ValueError:
        await message.answer(
            "Неверный формат. Используйте <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>", 
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
            ])
        )

@router.callback_query(AddEvent.repeat, F.data.startswith("repeat_"))
async def process_repeat(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    repeat = callback.data.split("_")[1]
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO events (user_id, text, event_time, repeat_type, notified_5min) VALUES (?, ?, ?, ?, 0)",
            (callback.from_user.id, data['text'], data['time'], repeat)
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
            "SELECT id, text, event_time, repeat_type FROM events WHERE user_id = ? ORDER BY event_time ASC", 
            (message.from_user.id,)
        )
        events = await cursor.fetchall()
    if not events:
        await message.answer("Нет событий.", reply_markup=get_main_menu_keyboard())
        return
    text = "📋 <b>Ваши события:</b>\n\n"
    kb = []
    for e_id, txt, tm, rep in events:
        icon = {"none": "🔹", "daily": "🔁", "weekly": "🔂"}.get(rep, "")
        text += f"{icon} <b>{utc_to_moscow(tm)}</b>\n{txt}\n\n"
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
        "Введите новое время (ДД.ММ.ГГГГ ЧЧ:ММ):", 
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
    try:
        new_msk = datetime.strptime(message.text, '%d.%m.%Y %H:%M').strftime('%d.%m.%Y %H:%M')
        new_utc = moscow_to_utc(new_msk)
        eid = (await state.get_data())['edit_event_id']
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE events SET event_time = ?, notified_5min = 0 WHERE id = ?", (new_utc, eid))
            await db.commit()
        await state.clear()
        await message.answer(f"✅ Время изменено на {new_msk} (МСК)", parse_mode="HTML", reply_markup=get_main_menu_keyboard())
    except ValueError:
        await message.answer(
            "Неверный формат.", 
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
            ])
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
