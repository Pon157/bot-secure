import logging
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict
from uuid import uuid4
import html
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    ChatMemberHandler,
)
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError

# ========== КОНФИГУРАЦИЯ БЕЗОПАСНОСТИ ==========
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # Замени на свой токен
OWNER_ID = 123456789  # Твой Telegram ID (главный владелец проекта)
MASTER_IDS = [123456789, 987654321]  # ID доверенных лиц (могут назначать бота в чаты)
TRUSTED_USERS = []  # Сюда будут добавляться пользователи через команду .addtrusted

# Базы данных (в памяти, для продакшена лучше заменить на SQLite/Redis)
authorized_chats = set()  # ID чатов, где бот активирован
chat_settings = {}  # Настройки для каждого чата
warn_counts = defaultdict(int)  # {chat_id_user_id: warns}
muted_users = {}  # {chat_id_user_id: unmute_time}
captcha_store = {}  # {chat_id_user_id: captcha_data}
user_messages = defaultdict(list)  # Для антифлуда {chat_id_user_id: [timestamps]}
trusted_users = set(TRUSTED_USERS)  # Доверенные юзеры для модерации в чатах

# Константы для настроек по умолчанию
DEFAULT_SETTINGS = {
    'antiflood_enabled': True,
    'antiflood_limit': 5,  # сообщений за 3 секунды
    'antispam_links': True,  # блокировать ссылки
    'antispam_media_limit': 3,  # макс медиа в мин
    'captcha_enabled': True,
    'min_account_age_days': 1,  # мин возраст аккаунта (дни)
    'require_profile_pic': False,
    'max_warns': 3,  # макс предупреждений до бана
    'log_channel': None,  # ID чата/канала для логов (если None, то лог в ЛС владельцу)
}

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int = None) -> bool:
    """Проверка, является ли пользователь администратором чата или глобальным мастером/владельцем."""
    if not update.effective_chat:
        return False
    
    user_id = user_id or update.effective_user.id
    
    # Глобальные права
    if user_id == OWNER_ID or user_id in MASTER_IDS:
        return True
    
    # Если чат не группа/супергруппа, то админ только если глобальный
    if update.effective_chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return False
    
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except:
        return False

async def log_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, level: str = "INFO"):
    """Отправка логов владельцу или в канал."""
    settings = chat_settings.get(chat_id, DEFAULT_SETTINGS.copy())
    log_target = settings.get('log_channel', OWNER_ID)  # Если канал не указан, шлем владельцу
    
    try:
        # Форматируем лог с эмодзи
        emoji = "ℹ️"
        if level == "WARN":
            emoji = "⚠️"
        elif level == "ERROR":
            emoji = "🚨"
        elif level == "BAN":
            emoji = "🔨"
        
        await context.bot.send_message(
            chat_id=log_target,
            text=f"{emoji} <b>[Чат {chat_id}]</b>\n{text}",
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"Не удалось отправить лог: {e}")

async def restrict_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, until_date=None, permissions=None):
    """Ограничить пользователя (заглушка)."""
    if not until_date:
        until_date = datetime.now() + timedelta(hours=1)
    
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=permissions,
            until_date=until_date
        )
    except Exception as e:
        logger.error(f"Ошибка рестрикта: {e}")

async def ban_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, reason: str = "Не указана"):
    """Забанить пользователя."""
    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await log_action(context, chat_id, f"🔨 Пользователь {user_id} забанен.\nПричина: {reason}", "BAN")
        return True
    except Exception as e:
        await log_action(context, chat_id, f"🚨 Ошибка бана {user_id}: {e}", "ERROR")
        return False

async def mute_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, until: datetime, reason: str):
    """Замутить пользователя (лишить права писать)."""
    permissions = {
        'can_send_messages': False,
        'can_send_media_messages': False,
        'can_send_polls': False,
        'can_send_other_messages': False,
        'can_add_web_page_previews': False,
        'can_change_info': False,
        'can_invite_users': False,
        'can_pin_messages': False,
    }
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=permissions,
            until_date=until
        )
        muted_users[f"{chat_id}_{user_id}"] = until
        await log_action(context, chat_id, f"🔇 Пользователь {user_id} замучен до {until}.\nПричина: {reason}", "WARN")
        return True
    except Exception as e:
        await log_action(context, chat_id, f"🚨 Ошибка мута {user_id}: {e}", "ERROR")
        return False

async def unmute_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    """Снять мут."""
    permissions = {
        'can_send_messages': True,
        'can_send_media_messages': True,
        'can_send_polls': True,
        'can_send_other_messages': True,
        'can_add_web_page_previews': True,
        'can_invite_users': True,
    }
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=permissions
        )
        if f"{chat_id}_{user_id}" in muted_users:
            del muted_users[f"{chat_id}_{user_id}"]
        await log_action(context, chat_id, f"🔊 Пользователь {user_id} размучен.")
        return True
    except Exception as e:
        await log_action(context, chat_id, f"🚨 Ошибка снятия мута {user_id}: {e}", "ERROR")
        return False

async def warn_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, reason: str, admin_id: int):
    """Выдать предупреждение."""
    key = f"{chat_id}_{user_id}"
    warn_counts[key] = warn_counts.get(key, 0) + 1
    current_warns = warn_counts[key]
    settings = chat_settings.get(chat_id, DEFAULT_SETTINGS.copy())
    
    await log_action(context, chat_id, f"⚠️ Пользователь {user_id} получил предупреждение ({current_warns}/{settings['max_warns']}).\nПричина: {reason}", "WARN")
    
    # Если превышен лимит - бан
    if current_warns >= settings['max_warns']:
        await ban_user(context, chat_id, user_id, f"Превышение лимита предупреждений ({settings['max_warns']})")
        # Сбрасываем счетчик после бана
        if key in warn_counts:
            del warn_counts[key]

async def check_new_member(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, user):
    """Проверка нового участника (капча, валидация)."""
    settings = chat_settings.get(chat_id, DEFAULT_SETTINGS.copy())
    if not settings.get('captcha_enabled', True):
        return
    
    # Проверка возраста аккаунта
    min_age = settings.get('min_account_age_days', 1)
    if min_age > 0 and user:
        account_age = (datetime.now() - user.created_at).days
        if account_age < min_age:
            await ban_user(context, chat_id, user_id, f"Аккаунт слишком новый ({account_age} дней)")
            return
    
    # Проверка аватара
    if settings.get('require_profile_pic', False) and not user.photos:
        await ban_user(context, chat_id, user_id, "Нет фото профиля")
        return
    
    # Отправка капчи
    captcha_text = str(uuid4())[:6]  # Простая капча из 6 символов
    captcha_store[f"{chat_id}_{user_id}"] = {
        'text': captcha_text,
        'time': datetime.now()
    }
    
    # Создаем клавиатуру с кнопкой для прохождения капчи
    keyboard = [[InlineKeyboardButton("✅ Я человек", callback_data=f"captcha_{user_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"👋 Привет, {user.mention_html()}!\n"
                 f"Введи код для подтверждения, что ты не бот: <code>{captcha_text}</code>\n"
                 f"У тебя 2 минуты.",
            parse_mode='HTML',
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Ошибка отправки капчи: {e}")

# ========== КОМАНДЫ БОТА ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветственное сообщение."""
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    await update.message.reply_text(
        "🛡️ <b>Безопасность Ваших чатов</b>\n\n"
        "Я - бот для защиты от спама и рейдеров.\n"
        "Чтобы установить меня в чат, добавьте меня в группу и напишите там /setup.",
        parse_mode='HTML'
    )

async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Активация бота в чате (только для владельца/мастеров)."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Проверка прав
    if user_id != OWNER_ID and user_id not in MASTER_IDS:
        await update.message.reply_text("❌ У вас нет прав на установку бота.")
        return
    
    # Проверка типа чата
    if update.effective_chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await update.message.reply_text("❌ Эта команда работает только в группах.")
        return
    
    # Добавляем чат в авторизованные
    authorized_chats.add(chat_id)
    if chat_id not in chat_settings:
        chat_settings[chat_id] = DEFAULT_SETTINGS.copy()
    
    # Назначаем себя администратором (пользователь должен дать права)
    await update.message.reply_text(
        "✅ Бот успешно активирован!\n"
        "⚠️ Убедитесь, что я имею права администратора.\n\n"
        "Доступные команды:\n"
        "/settings - настройки чата\n"
        "/ban [причина] - заблокировать\n"
        "/unban - разблокировать\n"
        "/mute [время] [причина] - ограничить\n"
        "/unmute - снять ограничение\n"
        "/warn [причина] - предупреждение\n"
        "/unwarn - снять предупреждение\n"
        "/clear [кол-во] - очистка\n"
        "/pin - закрепить\n"
        "/unpin - открепить"
    )
    await log_action(context, chat_id, f"🟢 Бот активирован пользователем {user_id}")

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройка параметров чата."""
    if not await is_admin(update, context):
        await update.message.reply_text("❌ Только для администраторов.")
        return
    
    chat_id = update.effective_chat.id
    if chat_id not in authorized_chats:
        await update.message.reply_text("❌ Бот не активирован. Напишите /setup.")
        return
    
    settings = chat_settings.get(chat_id, DEFAULT_SETTINGS.copy())
    
    # Простое меню настроек
    text = (
        f"⚙️ <b>Настройки чата</b>\n\n"
        f"🔹 Антифлуд: {'✅' if settings['antiflood_enabled'] else '❌'} (лимит: {settings['antiflood_limit']} / 3с)\n"
        f"🔹 Блокировка ссылок: {'✅' if settings['antispam_links'] else '❌'}\n"
        f"🔹 Лимит медиа: {settings['antispam_media_limit']} в минуту\n"
        f"🔹 Капча при входе: {'✅' if settings['captcha_enabled'] else '❌'}\n"
        f"🔹 Мин. возраст аккаунта: {settings['min_account_age_days']} дн.\n"
        f"🔹 Требовать аватар: {'✅' if settings['require_profile_pic'] else '❌'}\n"
        f"🔹 Макс. предупреждений: {settings['max_warns']}\n\n"
        f"Используйте /set [параметр] [значение] для изменения."
    )
    
    await update.message.reply_text(text, parse_mode='HTML')

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Бан пользователя."""
    if not await is_admin(update, context):
        return
    
    chat_id = update.effective_chat.id
    if chat_id not in authorized_chats:
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение пользователя.")
        return
    
    user_id = update.message.reply_to_message.from_user.id
    reason = ' '.join(context.args) if context.args else "Не указана"
    
    if await ban_user(context, chat_id, user_id, reason):
        await update.message.reply_text(f"✅ Пользователь забанен.\nПричина: {reason}")

async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Мут пользователя."""
    if not await is_admin(update, context):
        return
    
    chat_id = update.effective_chat.id
    if chat_id not in authorized_chats:
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение пользователя.")
        return
    
    user_id = update.message.reply_to_message.from_user.id
    args = context.args
    
    # Парсим время и причину
    mute_time = timedelta(hours=1)  # По умолчанию 1 час
    reason = "Не указана"
    
    if args:
        # Проверяем, есть ли временной аргумент (число + суффикс)
        time_match = re.match(r'^(\d+)([smhd]?)$', args[0])
        if time_match:
            value, unit = time_match.groups()
            value = int(value)
            if unit == 's':
                mute_time = timedelta(seconds=value)
            elif unit == 'm':
                mute_time = timedelta(minutes=value)
            elif unit == 'h':
                mute_time = timedelta(hours=value)
            elif unit == 'd':
                mute_time = timedelta(days=value)
            else:
                mute_time = timedelta(minutes=value)  # По умолчанию минуты
            reason = ' '.join(args[1:]) if len(args) > 1 else "Не указана"
        else:
            reason = ' '.join(args)
    
    until = datetime.now() + mute_time
    
    if await mute_user(context, chat_id, user_id, until, reason):
        await update.message.reply_text(f"✅ Пользователь замучен до {until}.\nПричина: {reason}")

async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выдать предупреждение."""
    if not await is_admin(update, context):
        return
    
    chat_id = update.effective_chat.id
    if chat_id not in authorized_chats:
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение пользователя.")
        return
    
    user_id = update.message.reply_to_message.from_user.id
    reason = ' '.join(context.args) if context.args else "Нарушение правил"
    admin_id = update.effective_user.id
    
    await warn_user(context, chat_id, user_id, reason, admin_id)
    await update.message.reply_text(f"⚠️ Предупреждение выдано.\nПричина: {reason}")

async def unwarn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Снять предупреждение."""
    if not await is_admin(update, context):
        return
    
    chat_id = update.effective_chat.id
    if chat_id not in authorized_chats:
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение пользователя.")
        return
    
    user_id = update.message.reply_to_message.from_user.id
    key = f"{chat_id}_{user_id}"
    
    if key in warn_counts:
        warn_counts[key] = max(0, warn_counts[key] - 1)
        if warn_counts[key] == 0:
            del warn_counts[key]
        await update.message.reply_text("✅ Предупреждение снято.")
        await log_action(context, chat_id, f"✅ Снято предупреждение с {user_id}")
    else:
        await update.message.reply_text("❌ У пользователя нет предупреждений.")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очистка сообщений."""
    if not await is_admin(update, context):
        return
    
    chat_id = update.effective_chat.id
    if chat_id not in authorized_chats:
        return
    
    amount = 10  # По умолчанию
    if context.args:
        try:
            amount = int(context.args[0]) + 1  # +1 для команды
            amount = min(amount, 101)  # Максимум 100
        except:
            pass
    
    if update.message.reply_to_message:
        # Удаляем цепочку с reply_to_message
        messages = [update.message.message_id, update.message.reply_to_message.message_id]
        await context.bot.delete_messages(chat_id, messages)
    else:
        # Очищаем последние N
        await update.message.delete()
        # В супергруппах можно использовать delete_messages
        # Но проще ответить и удалить
    
    await log_action(context, chat_id, f"🧹 Очищено {amount-1} сообщений админом {update.effective_user.id}")

async def pin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Закрепить сообщение."""
    if not await is_admin(update, context):
        return
    
    chat_id = update.effective_chat.id
    if chat_id not in authorized_chats:
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Ответьте на сообщение для закрепления.")
        return
    
    try:
        await context.bot.pin_chat_message(chat_id, update.message.reply_to_message.message_id)
        await log_action(context, chat_id, f"📌 Закреплено сообщение админом {update.effective_user.id}")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

# ========== ОБРАБОТЧИКИ СОБЫТИЙ ==========

async def handle_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка новых участников."""
    chat_id = update.effective_chat.id
    if chat_id not in authorized_chats:
        # Если бота добавили без /setup - выходим
        if update.message and update.message.new_chat_members:
            for member in update.message.new_chat_members:
                if member.id == context.bot.id:
                    await update.message.reply_text(
                        "❌ Я не активирован в этом чате.\n"
                        "Владелец бота должен написать /setup."
                    )
                    await context.bot.leave_chat(chat_id)
        return
    
    if not update.message or not update.message.new_chat_members:
        return
    
    for member in update.message.new_chat_members:
        if member.id == context.bot.id:
            continue  # Себя не проверяем
        
        # Проверяем нового участника
        await check_new_member(context, chat_id, member.id, member)
        await log_action(context, chat_id, f"👤 Новый участник: {member.id}")

async def handle_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выхода участников."""
    chat_id = update.effective_chat.id
    if chat_id not in authorized_chats:
        return
    
    if update.message and update.message.left_chat_member:
        member = update.message.left_chat_member
        await log_action(context, chat_id, f"🚪 Участник покинул чат: {member.id}")

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Основной антиспам и проверка сообщений."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if chat_id not in authorized_chats:
        return
    
    # Игнорируем админов
    if await is_admin(update, context, user_id):
        return
    
    settings = chat_settings.get(chat_id, DEFAULT_SETTINGS.copy())
    
    # Проверка мута
    mute_key = f"{chat_id}_{user_id}"
    if mute_key in muted_users:
        if datetime.now() < muted_users[mute_key]:
            # Пытаемся удалить сообщение замученного
            try:
                await update.message.delete()
            except:
                pass
            return
        else:
            # Время мута истекло
            del muted_users[mute_key]
            await unmute_user(context, chat_id, user_id)
    
    # Антифлуд
    if settings['antiflood_enabled']:
        now = datetime.now()
        key = f"{chat_id}_{user_id}"
        user_messages[key].append(now)
        # Оставляем только последние 10 секунд
        user_messages[key] = [t for t in user_messages[key] if (now - t).seconds < 3]
        
        if len(user_messages[key]) > settings['antiflood_limit']:
            await update.message.delete()
            await warn_user(context, chat_id, user_id, "Флуд", context.bot.id)
            return
    
    # Блокировка ссылок
    if settings['antispam_links'] and update.message.text:
        # Простая проверка на ссылки
        if re.search(r'(https?://|www\.)[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}', update.message.text, re.I):
            await update.message.delete()
            await warn_user(context, chat_id, user_id, "Ссылка", context.bot.id)
            return

async def handle_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатия на кнопку капчи."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    if not data.startswith('captcha_'):
        return
    
    user_id = int(data.split('_')[1])
    chat_id = update.effective_chat.id
    
    if query.from_user.id != user_id:
        await query.edit_message_text("❌ Это не ваша капча.")
        return
    
    key = f"{chat_id}_{user_id}"
    if key not in captcha_store:
        await query.edit_message_text("❌ Капча устарела или уже использована.")
        return
    
    # Удаляем сообщение с капчей
    await query.message.delete()
    
    # Очищаем капчу
    if key in captcha_store:
        del captcha_store[key]
    
    await log_action(context, chat_id, f"✅ Пользователь {user_id} прошел капчу")

async def handle_text_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка текстового ввода для капчи."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = update.message.text
    
    if chat_id not in authorized_chats:
        return
    
    key = f"{chat_id}_{user_id}"
    if key not in captcha_store:
        return
    
    captcha_data = captcha_store[key]
    
    # Проверяем время (2 минуты)
    if (datetime.now() - captcha_data['time']).seconds > 120:
        del captcha_store[key]
        await update.message.reply_text("❌ Время вышло. Попробуйте заново.")
        await ban_user(context, chat_id, user_id, "Не прошел капчу")
        return
    
    if text.strip() == captcha_data['text']:
        # Успешно
        del captcha_store[key]
        await update.message.reply_text("✅ Капча пройдена! Добро пожаловать.")
        await log_action(context, chat_id, f"✅ Пользователь {user_id} успешно прошел капчу")
    else:
        # Неверно
        await update.message.delete()
        # Можно дать еще попытку, но в целях безопасности - бан
        await ban_user(context, chat_id, user_id, "Неверная капча")
        if key in captcha_store:
            del captcha_store[key]

async def handle_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отслеживание обновлений прав участников."""
    if not update.chat_member:
        return
    
    chat_id = update.chat_member.chat.id
    user_id = update.chat_member.new_chat_member.user.id
    
    # Если бота лишили прав администратора - выходим из чата
    if user_id == context.bot.id:
        old_status = update.chat_member.old_chat_member.status
        new_status = update.chat_member.new_chat_member.status
        
        if old_status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER] and \
           new_status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            await context.bot.leave_chat(chat_id)
            if chat_id in authorized_chats:
                authorized_chats.remove(chat_id)

# ========== ЗАПУСК БОТА ==========

def main():
    """Запуск бота."""
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("ОШИБКА: Замените BOT_TOKEN на реальный токен!")
        return
    
    # Создаем приложение
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Команды
    application.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("setup", setup))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("warn", warn_command))
    application.add_handler(CommandHandler("unwarn", unwarn_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("pin", pin_command))
    
    # Обработчики событий
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_members))
    application.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, handle_left_member))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_captcha))
    application.add_handler(MessageHandler(filters.ALL, handle_messages))  # Для антиспама
    application.add_handler(CallbackQueryHandler(handle_captcha, pattern="^captcha_"))
    application.add_handler(ChatMemberHandler(handle_chat_member_update, ChatMemberHandler.CHAT_MEMBER))
    
    # Запуск
    print("🛡️ Бот запущен и готов к работе!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
