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
TOKEN = os.environ.get("BOT_TOKEN", "8941985228:AAF7tkzYPRmMcaVhkYrxse0oP3sdp3nNrXo")
DB_NAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders.db")
MOSCOW_OFFSET = timezone(timedelta(hours=3))

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def moscow_to_utc(time_str: str) -> str:
    """Конвертирует строку ДД.ММ.ГГГГ ЧЧ:ММ (МСК) → UTC"""
    dt = datetime.strptime(time_str, '%d.%m.%Y %H:%M').replace(tzinfo=MOSCOW_OFFSET)
    return dt.astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M')

def utc_to_moscow(time_str: str) -> str:
    """Конвертирует строку ДД.ММ.ГГГГ ЧЧ:ММ (UTC) → МСК"""
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
        # Завтра 9:00 МСК = 6:00 UTC
        tomorrow = (now_utc + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
        target = tomorrow
    elif preset == 'tom_18':
        # Завтра 18:00 МСК = 15:00 UTC
        tomorrow = (now_utc + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
        target = tomorrow
    else:
        target = now_utc + timedelta(minutes=5)
    return target.strftime('%d.%m.%Y %H:%M')

# --- СОСТОЯНИЯ (FSM) ---
class AddEvent(StatesGroup):
    text = State()
    time = State()          # Ожидание ручного ввода времени
    time_select = State()   # Ожидание выбора кнопки
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

# --- ФОНОВАЯ ПРОВЕРКА НАПОМИНАНИЙ ---
async def reminder_loop(bot: Bot):
    while True:
        await asyncio.sleep(5)
        now = datetime.now(timezone.utc)
        now_str = now.strftime('%d.%m.%Y %H:%M')
        in_5min_str = (now + timedelta(minutes=5)).strftime('%d.%m.%Y %H:%M')
        
        async with aiosqlite.connect(DB_NAME) as db:
            # 1. За 5 минут
            cursor = await db.execute(
                "SELECT id, user_id, text, event_time FROM events "
                "WHERE event_time <= ? AND event_time > ? AND notified_5min = 0",
                (in_5min_str, now_str)
            )
            for e_id, user_id, text, e_time in await cursor.fetchall():
                moscow_time = utc_to_moscow(e_time)
                try:
                    await bot.send_message(user_id, f"⏳ <b>5 минут до события:</b>\n\n{text}\n<i>Время: {moscow_time}</i>", parse_mode="HTML")
                except Exception as e:
                    print(f"Ошибка отправки 5-min уведомления: {e}")
                await db.execute("UPDATE events SET notified_5min = 1 WHERE id = ?", (e_id,))

            # 2. В момент события
            cursor = await db.execute(
                "SELECT id, user_id, text, event_time, repeat_type FROM events WHERE event_time <= ?",
                (now_str,)
            )
            for e_id, user_id, text, e_time, repeat in await cursor.fetchall():
                try:
                    await bot.send_message(user_id, f"⏰ <b>Напоминание:</b>\n\n{text}", parse_mode="HTML")
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
                
                await db.execute("UPDATE events SET event_time = ?, notified_5min = 0 WHERE id = ?", 
                                 (new_dt.strftime('%d.%m.%Y %H:%M'), e_id))
            await db.commit()

# --- РОУТЕР ---
router = Router()

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Я бот-напоминалка (время по Москве).\n\n"
                         "/add — добавить событие\n/list — мои события\n/cancel — отмена")

@router.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    if await state.get_state():
        await state.clear()
        await message.answer("❌ Действие отменено.")
    else:
        await message.answer("Нечего отменять.")

# --- ДОБАВЛЕНИЕ СОБЫТИЯ ---
@router.message(Command("add"))
async def cmd_add(message: types.Message, state: FSMContext):
    await state.set_state(AddEvent.text)
    await message.answer("Введите описание события:", 
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add")]]))

@router.callback_query(F.data == "cancel_add")
async def cancel_add(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Отменено.")
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
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add")]
    ])
    await message.answer("Когда напомнить?", reply_markup=kb)

@router.callback_query(AddEvent.time_select, F.data.startswith("time_"))
async def handle_preset_time(callback: types.CallbackQuery, state: FSMContext):
    preset = callback.data.split("_")[1]
    if preset == 'manual':
        await state.set_state(AddEvent.time)
        await callback.message.edit_text("Введите время в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b> (по Москве)", parse_mode="HTML")
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
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add")]
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
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add")]
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
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add")]
        ])
        await message.answer("Нужно ли повторять событие?", reply_markup=kb)
    except ValueError:
        await message.answer("Неверный формат. Используйте <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>", parse_mode="HTML",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add")]]))

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
    await callback.message.edit_text("✅ Событие добавлено!")
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
        await message.answer("Нет событий.")
        return
    text = "📋 <b>Ваши события:</b>\n\n"
    kb = []
    for e_id, txt, tm, rep in events:
        icon = {"none": "🔹", "daily": "🔁", "weekly": "🔂"}.get(rep, "")
        text += f"{icon} <b>{utc_to_moscow(tm)}</b>\n{txt}\n\n"
        kb.append([InlineKeyboardButton(text="⏳ Изм. время", callback_data=f"edit_time_{e_id}"), 
                   InlineKeyboardButton(text="❌ Удалить", callback_data=f"del_{e_id}")])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

# --- РЕДАКТИРОВАНИЕ ---
@router.callback_query(F.data.startswith("edit_time_"))
async def process_edit_time(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(edit_event_id=int(callback.data.split("_")[-1]))
    await state.set_state(EditEvent.time)
    await callback.message.edit_text("Введите новое время (ДД.ММ.ГГГГ ЧЧ:ММ):", 
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit")]]))
    await callback.answer()

@router.callback_query(F.data == "cancel_edit")
async def cancel_edit(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Отменено.")
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
        await message.answer(f"✅ Время изменено на {new_msk} (МСК)", parse_mode="HTML")
    except ValueError:
        await message.answer("Неверный формат.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_edit")]]))

@router.callback_query(F.data.startswith("del_"))
async def process_delete(callback: types.CallbackQuery):
    eid = int(callback.data.split("_")[-1])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM events WHERE id = ?", (eid,))
        await db.commit()
    await callback.message.edit_text("❌ Удалено.")
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
