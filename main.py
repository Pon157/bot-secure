import os
import time
import asyncio
import logging
import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

# --- 1. НАСТРОЙКИ И ОКРУЖЕНИЕ ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

DB_PATH = "bot_security.db"

# Кэши в памяти для быстрой работы
SPAM_CACHE = {}          # user_id -> [timestamps]
SPAM_LIMIT = 5           # Сообщений
SPAM_TIME = 3            # За секунд
MUTE_DURATION = 300      # 5 минут за спам

JOIN_LOG = {}            # chat_id -> [timestamps]
RAID_LIMIT = 7           # Вступлений
RAID_TIME = 10           # За секунд (Если > 7 тел за 10 сек = Рейд)

CAPTCHA_PENDING = {}     # (user_id, chat_id) -> message_id (для удаления капчи)


# --- 2. БАЗА ДАННЫХ И ХЕЛПЕРЫ ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS authorized_chats (chat_id INTEGER PRIMARY KEY)")
        await db.execute("CREATE TABLE IF NOT EXISTS trusted_admins (user_id INTEGER PRIMARY KEY)")
        await db.execute("CREATE TABLE IF NOT EXISTS warns (user_id INTEGER, chat_id INTEGER, count INTEGER, PRIMARY KEY(user_id, chat_id))")
        await db.commit()

async def log_to_owner(text: str):
    """Отправка логов в ЛС владельцу"""
    try:
        await bot.send_message(OWNER_ID, f"🛡 <b>Security Log:</b>\n{text}", parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка отправки лога: {e}")

async def is_authorized_chat(chat_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM authorized_chats WHERE chat_id = ?", (chat_id,)) as cursor:
            return await cursor.fetchone() is not None

async def is_trusted_admin(user_id: int) -> bool:
    if user_id == OWNER_ID: return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM trusted_admins WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone() is not None


# --- 3. ПАНЕЛЬ ВЛАДЕЛЬЦА (Настройки доступов) ---
@router.message(Command("add_admin"), F.from_user.id == OWNER_ID)
async def cmd_add_admin(message: types.Message, command: CommandObject):
    if not command.args: return await message.reply("Использование: /add_admin <user_id>")
    try:
        new_admin = int(command.args)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR IGNORE INTO trusted_admins (user_id) VALUES (?)", (new_admin,))
            await db.commit()
        await message.reply(f"✅ Пользователь {new_admin} добавлен в админы бота.")
        await log_to_owner(f"➕ Добавлен модератор: {new_admin}")
    except ValueError:
        await message.reply("ID должен быть числом.")

@router.message(Command("auth_chat"))
async def cmd_auth_chat(message: types.Message, command: CommandObject):
    if not await is_trusted_admin(message.from_user.id): return
    chat_id = int(command.args) if command.args else message.chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO authorized_chats (chat_id) VALUES (?)", (chat_id,))
        await db.commit()
    await message.reply(f"✅ Чат {chat_id} успешно авторизован.")
    await log_to_owner(f"🔓 Авторизован чат: {chat_id} (Админ: {message.from_user.id})")


# --- 4. АНТИ-РЕЙД И КАПЧА (Контроль вступлений) ---
@router.my_chat_member()
async def on_bot_added(event: types.ChatMemberUpdated):
    """Если бота добавляют в левый чат — он мгновенно ливает"""
    if event.new_chat_member.status in ["member", "administrator"]:
        if not await is_authorized_chat(event.chat.id):
            await bot.leave_chat(event.chat.id)
            await log_to_owner(f"🚨 Попытка захвата! Бот добавлен в неавторизованный чат {event.chat.title} ({event.chat.id}). Вышел.")

@router.chat_member()
async def anti_raid_and_captcha(event: types.ChatMemberUpdated):
    """Контроль новых пользователей: Рейд-детектор + Капча"""
    if event.new_chat_member.status == "member":
        chat_id = event.chat.id
        user_id = event.new_chat_member.user.id
        now = time.time()

        # 1. Детектор рейда
        if chat_id not in JOIN_LOG: JOIN_LOG[chat_id] = []
        JOIN_LOG[chat_id] = [t for t in JOIN_LOG[chat_id] if now - t < RAID_TIME]
        JOIN_LOG[chat_id].append(now)

        if len(JOIN_LOG[chat_id]) > RAID_LIMIT:
            # Включен режим Осады: баним моментально
            try:
                await bot.ban_chat_member(chat_id, user_id)
                await log_to_owner(f"⚔️ <b>ОТБИТА АТАКА БОТОВ!</b>\nЧат: {event.chat.title}\nЮзер: {user_id} забанен автоматом.")
                return
            except TelegramBadRequest:
                pass

        # 2. Выдача Капчи (если не рейд)
        try:
            # Мутим юзера
            await bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(can_send_messages=False))
            
            # Отправляем кнопку
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🤖 Я человек", callback_data=f"captcha_{user_id}")
            ]])
            msg = await bot.send_message(chat_id, f"👋 {event.new_chat_member.user.mention_html()}, подтверди, что ты человек, чтобы писать в чат. У тебя 60 секунд.", reply_markup=keyboard, parse_mode="HTML")
            
            CAPTCHA_PENDING[(user_id, chat_id)] = msg.message_id
            
            # Запускаем таймер на кик
            asyncio.create_task(verify_captcha_timeout(chat_id, user_id, msg.message_id))
        except TelegramBadRequest:
            pass # Нет прав

async def verify_captcha_timeout(chat_id: int, user_id: int, message_id: int):
    """Ждем 60 секунд. Если юзер не нажал кнопку — кикаем."""
    await asyncio.sleep(60)
    if (user_id, chat_id) in CAPTCHA_PENDING:
        try:
            # Юзер не прошел капчу - удаляем из чата (кик, не пермабан)
            await bot.ban_chat_member(chat_id, user_id)
            await bot.unban_chat_member(chat_id, user_id)
            # Подчищаем сообщение с капчей
            await bot.delete_message(chat_id, message_id)
            del CAPTCHA_PENDING[(user_id, chat_id)]
        except TelegramBadRequest:
            pass

@router.callback_query(F.data.startswith("captcha_"))
async def process_captcha(callback: types.CallbackQuery):
    """Обработка нажатия на кнопку капчи"""
    target_user_id = int(callback.data.split("_")[1])
    
    if callback.from_user.id != target_user_id:
        return await callback.answer("Это не твоя кнопка!", show_alert=True)
    
    chat_id = callback.message.chat.id
    if (target_user_id, chat_id) in CAPTCHA_PENDING:
        try:
            # Возвращаем права
            await bot.restrict_chat_member(
                chat_id, target_user_id,
                permissions=ChatPermissions(
                    can_send_messages=True, can_send_audios=True, can_send_photos=True,
                    can_send_videos=True, can_send_documents=True, can_send_polls=True,
                    can_invite_users=True, can_pin_messages=False, can_change_info=False
                )
            )
            await callback.message.delete()
            del CAPTCHA_PENDING[(target_user_id, chat_id)]
            await callback.answer("Проверка пройдена! Добро пожаловать.", show_alert=True)
        except TelegramBadRequest:
            await callback.answer("Ошибка выдачи прав.", show_alert=True)


# --- 5. АНТИСПАМ И ОБРАБОТКА СООБЩЕНИЙ ---
@router.message(F.chat.type.in_(["group", "supergroup"]))
async def main_group_handler(message: types.Message):
    # Если чат перестал быть авторизованным — уходим
    if not await is_authorized_chat(message.chat.id):
        await bot.leave_chat(message.chat.id)
        return

    user_id = message.from_user.id
    if await is_trusted_admin(user_id): return # Админов не трогаем

    # Троттлинг (Антиспам)
    now = time.time()
    if user_id not in SPAM_CACHE: SPAM_CACHE[user_id] = []
    SPAM_CACHE[user_id] = [t for t in SPAM_CACHE[user_id] if now - t < SPAM_TIME]
    SPAM_CACHE[user_id].append(now)

    if len(SPAM_CACHE[user_id]) > SPAM_LIMIT:
        try:
            await bot.restrict_chat_member(
                message.chat.id, user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=int(time.time()) + MUTE_DURATION
            )
            await message.delete()
            msg = await message.answer(f"🚫 {message.from_user.full_name} замьючен на 5 минут за флуд.")
            await log_to_owner(f"⚔️ <b>Антиспам:</b> Мут 5 мин\nЧат: {message.chat.title}\nЮзер: {user_id}")
            
            # Удаляем сообщение о муте через 10 секунд, чтобы не засорять чат
            await asyncio.sleep(10)
            await msg.delete()
            return
        except TelegramBadRequest:
            pass


# --- 6. КОМАНДЫ МОДЕРАЦИИ ---
@router.message(Command("ban"), F.chat.type.in_(["group", "supergroup"]))
async def cmd_ban(message: types.Message):
    if not await is_trusted_admin(message.from_user.id): return
    if not message.reply_to_message: return await message.reply("Ответьте на сообщение!")
    
    target = message.reply_to_message.from_user
    try:
        await bot.ban_chat_member(message.chat.id, target.id)
        await message.reply(f"🔨 {target.full_name} забанен.")
        await log_to_owner(f"🔨 <b>БАН</b> в {message.chat.title}\nЦель: {target.id}\nМодератор: {message.from_user.id}")
    except Exception as e:
        await message.reply(f"Ошибка: {e}")

@router.message(Command("mute"), F.chat.type.in_(["group", "supergroup"]))
async def cmd_mute(message: types.Message, command: CommandObject):
    if not await is_trusted_admin(message.from_user.id): return
    if not message.reply_to_message: return await message.reply("Ответьте на сообщение!")
    
    target = message.reply_to_message.from_user
    minutes = int(command.args) if command.args and command.args.isdigit() else 10
    
    try:
        await bot.restrict_chat_member(
            message.chat.id, target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=int(time.time()) + (minutes * 60)
        )
        await message.reply(f"🤐 {target.full_name} в муте на {minutes} мин.")
        await log_to_owner(f"🤐 <b>МУТ</b> ({minutes}м) в {message.chat.title}\nЦель: {target.id}\nМодер: {message.from_user.id}")
    except Exception as e:
        await message.reply(f"Ошибка: {e}")

@router.message(Command("warn"), F.chat.type.in_(["group", "supergroup"]))
async def cmd_warn(message: types.Message):
    if not await is_trusted_admin(message.from_user.id): return
    if not message.reply_to_message: return await message.reply("Ответьте на сообщение!")
    
    target = message.reply_to_message.from_user
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT count FROM warns WHERE user_id = ? AND chat_id = ?", (target.id, message.chat.id)) as cursor:
            row = await cursor.fetchone()
            warns = row[0] + 1 if row else 1
        await db.execute("INSERT OR REPLACE INTO warns (user_id, chat_id, count) VALUES (?, ?, ?)", (target.id, message.chat.id, warns))
        await db.commit()

    if warns >= 3:
        try:
            await bot.ban_chat_member(message.chat.id, target.id)
            await message.reply(f"🚨 {target.full_name} получил 3/3 варнов и был забанен.")
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM warns WHERE user_id = ? AND chat_id = ?", (target.id, message.chat.id))
                await db.commit()
            await log_to_owner(f"🚨 <b>АВТО-БАН (3/3)</b> в {message.chat.title} | Юзер: {target.id}")
        except TelegramBadRequest:
            pass
    else:
        await message.reply(f"⚠️ {target.full_name} получил предупреждение ({warns}/3).")

@router.message(Command("lock"), F.chat.type.in_(["group", "supergroup"]))
async def cmd_lock(message: types.Message):
    """Экстренно закрывает чат для всех"""
    if not await is_trusted_admin(message.from_user.id): return
    try:
        await bot.set_chat_permissions(message.chat.id, ChatPermissions(can_send_messages=False))
        await message.answer("🔒 <b>Чат закрыт администратором.</b>", parse_mode="HTML")
        await log_to_owner(f"🔒 Экстренная блокировка чата: {message.chat.title}")
    except TelegramBadRequest:
        await message.reply("Не хватает прав для изменения настроек чата.")

@router.message(Command("unlock"), F.chat.type.in_(["group", "supergroup"]))
async def cmd_unlock(message: types.Message):
    """Снимает экстренную блокировку"""
    if not await is_trusted_admin(message.from_user.id): return
    try:
        await bot.set_chat_permissions(
            message.chat.id, 
            ChatPermissions(can_send_messages=True, can_send_photos=True, can_send_videos=True, can_send_audios=True, can_send_documents=True)
        )
        await message.answer("🔓 <b>Чат снова открыт.</b>", parse_mode="HTML")
        await log_to_owner(f"🔓 Разблокировка чата: {message.chat.title}")
    except TelegramBadRequest:
        await message.reply("Не хватает прав.")


# --- 7. ЗАПУСК ---
async def main():
    await init_db()
    dp.include_router(router)
    await log_to_owner("🟢 <b>Security Bot запущен и готов к защите проектов.</b>")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
