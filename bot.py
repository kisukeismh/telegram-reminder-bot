import os
import asyncio
import aiosqlite
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# --- НАСТРОЙКИ ---
TOKEN = os.environ.get("BOT_TOKEN", "8941985228:AAHrnzQV8pubS-kGH1RG_9vDnX5Hj8Juwk4")
DB_NAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders.db")

# --- СОСТОЯНИЯ (FSM) ---
class AddEvent(StatesGroup):
    text = State()
    time = State()
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
        try:
            await db.execute("ALTER TABLE events ADD COLUMN notified_5min INTEGER DEFAULT 0")
        except aiosqlite.OperationalError:
            pass
        await db.commit()

# --- ФОНОВАЯ ПРОВЕРКА НАПОМИНАНИЙ ---
async def reminder_loop(bot: Bot):
    while True:
        await asyncio.sleep(5)
        now = datetime.now()
        now_str = now.strftime('%d.%m.%Y %H:%M')
        in_5min_str = (now + timedelta(minutes=5)).strftime('%d.%m.%Y %H:%M')
        
        async with aiosqlite.connect(DB_NAME) as db:
            cursor = await db.execute(
                "SELECT id, user_id, text, event_time FROM events "
                "WHERE event_time <= ? AND event_time > ? AND notified_5min = 0",
                (in_5min_str, now_str)
            )
            events_5min = await cursor.fetchall()

            for e_id, user_id, text, e_time in events_5min:
                try:
                    await bot.send_message(
                        user_id,
                        f"⏳ <b>5 минут до события:</b>\n\n{text}\n<i>Время: {e_time}</i>",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    print(f"Не удалось отправить 5-min уведомление пользователю {user_id}: {e}")
                await db.execute("UPDATE events SET notified_5min = 1 WHERE id = ?", (e_id,))

            cursor = await db.execute(
                "SELECT id, user_id, text, event_time, repeat_type FROM events WHERE event_time <= ?",
                (now_str,)
            )
            events_now = await cursor.fetchall()

            for e_id, user_id, text, e_time, repeat in events_now:
                try:
                    await bot.send_message(
                        user_id,
                        f"⏰ <b>Напоминание:</b>\n\n{text}",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    print(f"Не удалось отправить напоминание пользователю {user_id}: {e}")

                current_dt = datetime.strptime(e_time, '%d.%m.%Y %H:%M')
                if repeat == 'daily':
                    new_dt = current_dt + timedelta(days=1)
                    await db.execute(
                        "UPDATE events SET event_time = ?, notified_5min = 0 WHERE id = ?",
                        (new_dt.strftime('%d.%m.%Y %H:%M'), e_id)
                    )
                elif repeat == 'weekly':
                    new_dt = current_dt + timedelta(weeks=1)
                    await db.execute(
                        "UPDATE events SET event_time = ?, notified_5min = 0 WHERE id = ?",
                        (new_dt.strftime('%d.%m.%Y %H:%M'), e_id)
                    )
                else:
                    await db.execute("DELETE FROM events WHERE id = ?", (e_id,))
            
            await db.commit()

# --- РОУТЕР И ХЕНДЛЕРЫ ---
router = Router()

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    text = ("Привет! Я бот-напоминалка.\n\n"
            "Команды:\n"
            "/add — добавить событие\n"
            "/list — мои события\n"
            "/cancel — отменить текущее действие")
    await message.answer(text)

# --- КОМАНДА ОТМЕНЫ ---
@router.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нечего отменять.")
        return
    
    await state.clear()
    await message.answer("❌ Действие отменено.")

# --- ДОБАВЛЕНИЕ СОБЫТИЯ ---
@router.message(Command("add"))
async def cmd_add(message: types.Message, state: FSMContext):
    await state.set_state(AddEvent.text)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add")]
    ])
    await message.answer("Введите описание события:", reply_markup=kb)

@router.callback_query(F.data == "cancel_add")
async def cancel_add(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Добавление события отменено.")
    await callback.answer()

@router.message(AddEvent.text)
async def process_text(message: types.Message, state: FSMContext):
    await state.update_data(text=message.text)
    await state.set_state(AddEvent.time)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_text")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add")]
    ])
    await message.answer(
        "Введите дату и время в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\n(Например: 08.07.2026 15:30)",
        parse_mode="HTML",
        reply_markup=kb
    )

@router.callback_query(F.data == "back_to_text", AddEvent.time)
async def back_to_text(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AddEvent.text)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add")]
    ])
    await callback.message.edit_text("Введите описание события:", reply_markup=kb)
    await callback.answer()

@router.message(AddEvent.time)
async def process_time(message: types.Message, state: FSMContext):
    try:
        datetime.strptime(message.text, '%d.%m.%Y %H:%M')
        await state.update_data(time=message.text)
        await state.set_state(AddEvent.repeat)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Не повторять", callback_data="repeat_none"),
             InlineKeyboardButton(text="Ежедневно", callback_data="repeat_daily")],
            [InlineKeyboardButton(text="Еженедельно", callback_data="repeat_weekly")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_time")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add")]
        ])
        await message.answer("Нужно ли повторять это событие?", reply_markup=kb)
    except ValueError:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_text")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add")]
        ])
        await message.answer(
            "Неверный формат. Попробуйте еще раз (ДД.ММ.ГГГГ ЧЧ:ММ):",
            reply_markup=kb
        )

@router.callback_query(F.data == "back_to_time", AddEvent.repeat)
async def back_to_time(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AddEvent.time)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_text")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_add")]
    ])
    await callback.message.edit_text(
        "Введите дату и время в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>\n(Например: 08.07.2026 15:30)",
        parse_mode="HTML",
        reply_markup=kb
    )
    await callback.answer()

@router.callback_query(AddEvent.repeat, F.data.startswith("repeat_"))
async def process_repeat(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    repeat_type = callback.data.split("_")[1]
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO events (user_id, text, event_time, repeat_type, notified_5min) VALUES (?, ?, ?, ?, 0)",
            (callback.from_user.id, data['text'], data['time'], repeat_type)
        )
        await db.commit()
        
    await state.clear()
    await callback.message.edit_text("✅ Событие успешно добавлено!")
    await callback.answer()

# --- ПРОСМОТР И РЕДАКТИРОВАНИЕ ---
@router.message(Command("list"))
async def cmd_list(message: types.Message):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT id, text, event_time, repeat_type FROM events WHERE user_id = ? ORDER BY event_time ASC", 
            (message.from_user.id,)
        )
        events = await cursor.fetchall()

    if not events:
        await message.answer("У вас нет запланированных событий.")
        return

    text = "📋 <b>Ваши события:</b>\n\n"
    kb_buttons = []
    
    for e in events:
        e_id, e_text, e_time, e_repeat = e
        repeat_str = {"none": "🔹", "daily": "🔁", "weekly": "🔂"}.get(e_repeat, "")
        text += f"{repeat_str} <b>{e_time}</b>\n{e_text}\n\n"
        
        kb_buttons.append([
            InlineKeyboardButton(text="⏳ Изменить время", callback_data=f"edit_time_{e_id}"),
            InlineKeyboardButton(text="❌ Удалить", callback_data=f"del_{e_id}")
        ])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

# --- ЛОГИКА РЕДАКТИРОВАНИЯ ВРЕМЕНИ ---
@router.callback_query(F.data.startswith("edit_time_"))
async def process_edit_time(callback: types.CallbackQuery, state: FSMContext):
    event_id = int(callback.data.split("_")[-1])
    await state.update_data(edit_event_id=event_id)
    await state.set_state(EditEvent.time)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_edit")]
    ])
    await callback.message.edit_text(
        "Введите <b>новое</b> время в формате ДД.ММ.ГГГГ ЧЧ:ММ:",
        parse_mode="HTML",
        reply_markup=kb
    )
    await callback.answer()

@router.callback_query(F.data == "cancel_edit")
async def cancel_edit(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Редактирование отменено.")
    await callback.answer()

@router.message(EditEvent.time)
async def process_new_time(message: types.Message, state: FSMContext):
    try:
        new_time = datetime.strptime(message.text, '%d.%m.%Y %H:%M').strftime('%d.%m.%Y %H:%M')
        data = await state.get_data()
        event_id = data['edit_event_id']
        
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "UPDATE events SET event_time = ?, notified_5min = 0 WHERE id = ?",
                (new_time, event_id)
            )
            await db.commit()
            
        await state.clear()
        await message.answer(f"✅ Время успешно изменено на <b>{new_time}</b>", parse_mode="HTML")
    except ValueError:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_edit")]
        ])
        await message.answer("Неверный формат. Попробуйте еще раз:", reply_markup=kb)

# --- УДАЛЕНИЕ ---
@router.callback_query(F.data.startswith("del_"))
async def process_delete(callback: types.CallbackQuery):
    event_id = int(callback.data.split("_")[-1])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM events WHERE id = ?", (event_id,))
        await db.commit()
    await callback.message.edit_text("❌ Событие удалено.")
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

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен.")
