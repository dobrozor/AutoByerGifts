import telebot
import sqlite3
import threading
import time
import logging
import requests
import os
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice
import sys

# --- 1. Настройка и константы ---
# Путь к файлу базы данных - используем относительный путь для переносимости
DB_PATH = "database.db"

# Токен бота - ВАЖНО: замените на свой токен
TOKEN = "8088988947:AAGVZihFRZP2WHhtI4gFTen6YaNv6cXj4mQ"
LINK_BUY_STARS = ""  # Если есть ссылка для спокупки звезд, можно вставить сюда

# Комиссия: 0.98 означает 2% комиссии на пополнение
DEP_COMMISSION = 0.98

# Константы для настроек
PRICE_VALUES = [15, 25, 50, 75, 100, 150, 200, 250, 300, 350, 400, 500, 1000, 2000, 2500, 5000, 10000, 20000]
SUPPLY_VALUES = [1000, 2500, 5000, 10000, 20000, 40000, 50000, 100000, 200000, 500000, 750000, 1000000]
ADMIN_IDS = [535541118]
LOG_CHANNEL_ID = -1003028753221  # Канал для логов/уведомлений

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(threadName)s | %(message)s',
    # Добавляем Handler для вывода в консоль
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

# Состояния пользователей
user_states = {}

# Глобальный объект для временного хранения списка чеков для меню возврата
temp_receipt_storage = {}

# Глобальный объект бота
bot = telebot.TeleBot(TOKEN)
_processed_gifts = set()


# --- 2. Функции работы с БД ---

def initialize_db():
    """Создание таблиц, если они не существуют."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Users (
                id INTEGER NOT NULL PRIMARY KEY,
                username TEXT NOT NULL,
                balance INTEGER NOT NULL DEFAULT 0
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Payments (
                id INTEGER NOT NULL,
                username TEXT NOT NULL,
                amount INTEGER NOT NULL,
                receipt TEXT NOT NULL PRIMARY KEY
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Settings (
                id INTEGER NOT NULL PRIMARY KEY,
                maxprice INTEGER NOT NULL DEFAULT 1000,
                minprice INTEGER NOT NULL DEFAULT 100,
                maxsupply INTEGER NOT NULL DEFAULT 50000,
                state INTEGER NOT NULL DEFAULT 0
            )
        ''')
        conn.commit()


# --- 3. Функции создания разметки ---

def get_main_menu_markup(user_id):
    """Генерирует разметку главного меню."""
    markup = InlineKeyboardMarkup()
    buybutton = InlineKeyboardButton("Пополнить баланс ⭐️", callback_data="buy_stars")
    giftsettingsbutton = InlineKeyboardButton("⚙️ Настройки автопокупки", callback_data="opensettings")
    profilebutton = InlineKeyboardButton("👤 Профиль", callback_data="openprofile")
    topbutton = InlineKeyboardButton("📊 Топ", callback_data="opentop")
    support_button = InlineKeyboardButton("Поддержка 🥷", url="https://t.me/m/O_p2YjunMGM6")
    channel_button = InlineKeyboardButton("Наш канал", url="https://t.me/Fruit_gift")

    markup.add(buybutton)
    markup.add(profilebutton, topbutton)
    markup.add(giftsettingsbutton)
    markup.add(support_button, channel_button)

    if user_id in ADMIN_IDS:
        admin_button = InlineKeyboardButton("✖️Админка✖️", callback_data="openadmin")
        markup.add(admin_button)

    return markup


def price_selection_markup(setting_type, values):
    """Генерирует разметку для выбора цены/саплая."""
    markup = InlineKeyboardMarkup()
    row = []
    suffix = "🧸" if setting_type == "maxsupply" else "⭐️"
    prefix = "setsupp" if setting_type == "maxsupply" else "setprice"

    for i, val in enumerate(values, start=1):
        row.append(InlineKeyboardButton(f"{str(val)}{suffix}", callback_data=f"{prefix}:{setting_type}:{val}"))
        if i % 2 == 0:
            markup.add(*row)
            row = []
    if row:
        markup.add(*row)

    markup.add(InlineKeyboardButton("◀️ Назад", callback_data="opensettings"))
    return markup


# --- 4. Обработчики бота ---

@bot.message_handler(commands=['start'])
def send_welcome(message):
    try:
        user_id = message.from_user.id
        username = message.from_user.username or "None"
        logging.info(f"Старт от {user_id} | @{username}")
        # Проверка на администратора (можно убрать отправку в лог, если не требуется для всех)
        if user_id not in ADMIN_IDS:
            bot.send_message(LOG_CHANNEL_ID, f"Старт от {user_id} | @{username}")

        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT 1 FROM Users WHERE id = ?', (user_id,))
            if not cursor.fetchone():
                cursor.execute('INSERT INTO Users (id, username, balance) VALUES (?, ?, ?)', (user_id, username, 0))
                conn.commit()
            # Убедимся, что запись в Settings есть (если нет, добавим дефолтную)
            cursor.execute('SELECT 1 FROM Settings WHERE id = ?', (user_id,))
            if not cursor.fetchone():
                cursor.execute('INSERT INTO Settings (id) VALUES (?)', (user_id,))
                conn.commit()

        markup = get_main_menu_markup(user_id)
        bot.send_message(
            message.chat.id,
            f"⭐️ Добро пожаловать в бота по автозакупке подарков ⭐️\n\nБот Может закупать только подарки для которых не требуется премиум.\nТакже в боте есть моментальный возврат звезд.",
            reply_markup=markup,
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Ошибка в send_welcome: {e}")


def _send_or_edit_main_menu(chat_id, message_id=None, call=None):
    """Отправляет или редактирует сообщение с главным меню."""
    user_id = chat_id if message_id is None else call.from_user.id
    markup = get_main_menu_markup(user_id)
    text = "⭐️ Добро пожаловать в бота по автозакупке подарков ⭐️\n\nБот Может закупать только подарки для которых не требуется премиум.\nТакже в боте есть моментальный возврат звезд."

    if message_id is None:
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
    else:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=markup, parse_mode="Markdown")
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" in str(e):
                pass
            else:
                logging.error(f"Ошибка редактирования сообщения: {e}")


@bot.callback_query_handler(func=lambda call: call.data == "openmain")
def open_main(call):
    _send_or_edit_main_menu(call.from_user.id, call.message.message_id, call)


@bot.callback_query_handler(func=lambda call: call.data == "opensettings")
def open_settings(call):
    try:
        user_id = call.from_user.id
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            # Должно быть в БД после /start, но на всякий случай проверяем
            cursor.execute('SELECT 1 FROM Settings WHERE id = ?', (user_id,))
            if not cursor.fetchone():
                cursor.execute('INSERT INTO Settings (id) VALUES (?)', (user_id,))
                conn.commit()

            cursor.execute('SELECT maxprice, minprice, maxsupply, state FROM Settings WHERE id = ?', (user_id,))
            settings = cursor.fetchone()

        max_price, min_price, max_supply, state = settings

        markup = InlineKeyboardMarkup()
        switch_text = "🟢 Включить" if state == 0 else "🔴 Выключить"
        status_text = "🔴 Выключено" if state == 0 else "🟢 Включено"

        markup.add(InlineKeyboardButton(switch_text, callback_data="switchstate"))
        markup.add(InlineKeyboardButton(f"⬇️ Лимит МИН цены ({min_price}⭐️)", callback_data="setminprice"),
                   InlineKeyboardButton(f"⬆️ Лимит МАКС цены ({max_price}⭐️)", callback_data="setmaxprice"))
        markup.add(
            InlineKeyboardButton(f"Лимит саплая(сколько покупать) ({max_supply}🧸)", callback_data="setmaxsupply"))
        markup.add(InlineKeyboardButton("◀️ Назад", callback_data="openmain"))

        bot.edit_message_text(
            f"⚙️ Настройки автопокупки\nСтатус: {status_text}\n\nЛимит Цены для покупки:\nОт **{min_price}** до **{max_price}**⭐️\n\n__Проверяйте чтобы максимальная цена была не меньше минимальной__\n\nЛимит саплая: **{max_supply}** 🧸",
            user_id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Ошибка в open_settings: {e}")


@bot.callback_query_handler(func=lambda call: call.data in ["setminprice", "setmaxprice"])
def handle_price_setting(call):
    setting_type = "minprice" if call.data == "setminprice" else "maxprice"
    markup = price_selection_markup(setting_type, PRICE_VALUES)
    bot.edit_message_text(
        f"Выберите значение для {'Минимальной' if setting_type == 'minprice' else 'Максимальной'} цены:",
        call.from_user.id,
        call.message.message_id,
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda call: call.data == "setmaxsupply")
def handle_supply_setting(call):
    setting_type = "maxsupply"
    markup = price_selection_markup(setting_type, SUPPLY_VALUES)
    bot.edit_message_text(
        f"Выберите значение для Максимального саплая 🧸:",
        call.from_user.id,
        call.message.message_id,
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith(("setprice:", "setsupp:")))
def set_setting_value(call):
    try:
        parts = call.data.split(":")
        # parts[0] - префикс, parts[1] - тип, parts[2] - значение
        setting_type = parts[1]
        value = int(parts[2])
        user_id = call.from_user.id

        # Использование SQL-запроса с параметрами для безопасности
        # Теперь даже при манипуляции с setting_type, запрос останется безопасным
        if setting_type in ["minprice", "maxprice", "maxsupply"]:
            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.cursor()
                # Белый список для названия столбца
                if setting_type == "minprice":
                    cursor.execute("UPDATE Settings SET minprice = ? WHERE id = ?", (value, user_id))
                elif setting_type == "maxprice":
                    cursor.execute("UPDATE Settings SET maxprice = ? WHERE id = ?", (value, user_id))
                elif setting_type == "maxsupply":
                    cursor.execute("UPDATE Settings SET maxsupply = ? WHERE id = ?", (value, user_id))
                conn.commit()

            # Переход обратно в настройки
            open_settings(call)
        else:
            bot.answer_callback_query(call.id, "Недопустимый тип настройки.")

    except Exception as e:
        logging.error(f"Ошибка в set_setting_value: {e}")


@bot.callback_query_handler(func=lambda call: call.data == "switchstate")
def switch_state(call):
    try:
        user_id = call.from_user.id
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT state FROM Settings WHERE id = ?', (user_id,))
            result = cursor.fetchone()

            if result is not None:
                current_state = result[0]
                new_state = 0 if current_state == 1 else 1
                cursor.execute('UPDATE Settings SET state = ? WHERE id = ?', (new_state, user_id))
                conn.commit()

        open_settings(call)

    except Exception as e:
        logging.error(f"Ошибка в switch_state: {e}")


@bot.callback_query_handler(func=lambda call: call.data == "opentop")
def open_top(call):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT username, balance 
                FROM Users 
                WHERE balance > 0 
                ORDER BY balance DESC 
                LIMIT 10
            """)
            top_users = cursor.fetchall()

        top_text = "🏆 Топ 10 пользователей по балансу:\n\n"
        for i, (username, balance) in enumerate(top_users, start=1):
            name_display = username if username and username != "None" else f"Аноним {i}"
            top_text += f"**{i}.** @{name_display} — ⭐ **{balance}**\n"

        markup = InlineKeyboardMarkup()
        mainmenubutton = InlineKeyboardButton("◀️ Назад", callback_data="openmain")
        markup.add(mainmenubutton)

        bot.edit_message_text(
            top_text,
            call.from_user.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Ошибка в open_top: {e}")


@bot.callback_query_handler(func=lambda call: call.data == "openprofile")
def open_profile(call):
    try:
        user_id = call.from_user.id
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT balance FROM Users WHERE id = ?', (user_id,))
            userbalance = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM Payments WHERE id = ?', (user_id,))
            userdeps = cursor.fetchone()[0]

        markup = InlineKeyboardMarkup()
        refundbutton = InlineKeyboardButton("🔄 Возврат звёзд", callback_data="open_refund")
        mainmenubutton = InlineKeyboardButton("◀️ Назад", callback_data="openmain")
        markup.add(refundbutton)
        markup.add(mainmenubutton)

        username_display = f"@{call.from_user.username}" if call.from_user.username else f"ID: {user_id}"

        bot.edit_message_text(
            f"👤 Профиль {username_display}\nБаланс - **{userbalance}** ⭐️\nАктивных пополнений - **{userdeps}**",
            user_id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Ошибка в open_profile: {e}")


# 🌟 НОВОЕ: Админка с тестовой оплатой
@bot.callback_query_handler(func=lambda call: call.data == "openadmin")
def open_admin(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "Вы не администратор.")
        return

    markup = InlineKeyboardMarkup()
    # Кнопка для тестовой оплаты
    test_button = InlineKeyboardButton(f"Тестовый платеж (+{TEST_PAY_AMOUNT}⭐️)", callback_data="test_pay")

    markup.add(test_button)
    markup.add(InlineKeyboardButton("◀️ Назад", callback_data="openmain"))

    bot.edit_message_text(
        "✖️ Панель Администратора ✖️",
        call.from_user.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )


@bot.callback_query_handler(func=lambda call: call.data == "test_pay")
def handle_test_pay(call):
    user_id = call.from_user.id
    if user_id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "Вы не администратор.")
        return

    try:
        amount = TEST_PAY_AMOUNT
        username = call.from_user.username or "None"

        # Генерируем уникальный тестовый ID чека
        test_receipt_id = f"TEST_PAY_{user_id}_{int(time.time() * 1000)}"

        # Считаем зачисляемую сумму с учетом комиссии (как при обычной оплате)
        amount_to_credit = int(amount * DEP_COMMISSION)

        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            # 1. Запись платежа (как при успешной оплате)
            cursor.execute('''INSERT INTO Payments (id, username, amount, receipt) VALUES (?, ?, ?, ?)''',
                           (user_id, username, amount, test_receipt_id))
            # 2. Зачисление на баланс с учетом комиссии
            cursor.execute('''UPDATE Users SET balance = balance + ? WHERE id = ?''', (amount_to_credit, user_id))
            conn.commit()

        bot.answer_callback_query(call.id, f"✅ Баланс пополнен на {amount_to_credit}⭐️ (Тестовый платеж).")
        logging.info(f"Тестовое пополнение {amount} ⭐️ для ADMIN {user_id} | Зачислено: {amount_to_credit}")

        # Обновляем меню профиля
        open_profile(call)

    except sqlite3.IntegrityError:
        # Если вдруг ID чека уже существует (очень маловероятно)
        bot.answer_callback_query(call.id, "Ошибка: Повторный тестовый платеж. Попробуйте снова.")
    except Exception as e:
        logging.exception(f"Ошибка при тестовом платеже: {e}")
        bot.answer_callback_query(call.id, f"Произошла ошибка при тестовом пополнении: {str(e)}")


@bot.callback_query_handler(func=lambda call: call.data == "buy_stars")
def ask_star_amount(call):
    # Удаление предыдущего сообщения
    bot.delete_message(call.message.chat.id, call.message.message_id)

    # 1. Всегда инициализируем клавиатуру
    markup = InlineKeyboardMarkup()

    # 2. Условно добавляем кнопку покупки
    if LINK_BUY_STARS:  # Проверяем, что ссылка не пустая
        link_button = InlineKeyboardButton("Купить звезды дешево 🌐",
                                           url=LINK_BUY_STARS)
        markup.add(link_button)

    # 3. ВСЕГДА добавляем кнопку "Назад" (выполняется независимо от условия выше)
    mainmenubutton = InlineKeyboardButton("◀️ Назад", callback_data="openmain")
    markup.add(mainmenubutton)

    # 4. ВСЕГДА отправляем сообщение
    bot.send_message(
        call.message.chat.id,
        f"Сколько звёзд вы хотите пополнить? (целое число, минимум 25)\nКомиссия на пополнение **2%**\nНет звезд? Купить их можно по кнопке ниже",
        reply_markup=markup,
        parse_mode="Markdown"
    )
    user_states[call.from_user.id] = 'waiting_for_star_amount'


@bot.message_handler(func=lambda message: user_states.get(message.from_user.id) == "waiting_for_star_amount")
def process_star_amount(message):
    user_id = message.from_user.id
    try:
        count = int(message.text.strip())
        if count < 25 or count > 10000:
            # Некорректный диапазон
            raise ValueError

        user_states.pop(user_id, None)
        amount_in_units = count

        # NOTE: provider_token=None означает, что используется Star Payment
        bot.send_invoice(
            chat_id=message.chat.id,
            title="Пополнение баланса",
            description=f"Пополнение баланса на {count}⭐️",
            invoice_payload=f"stars_{count}",  # Идентификатор пользователя уже есть в chat_id
            provider_token=None,
            currency="XTR",
            prices=[LabeledPrice(label=f"{count} звёзд", amount=amount_in_units)],
            start_parameter="stars_payment",
            need_phone_number=False,
            need_email=False,
            need_shipping_address=False,
            is_flexible=False,
        )
    except ValueError:
        # Оставляем состояние, чтобы пользователь мог ввести число снова
        bot.send_message(message.chat.id, "Введите корректное целое число от 25 и до 10000.")
    except telebot.apihelper.ApiTelegramException as e:
        # Ошибка при отправке инвойса (например, Star Payment не настроен)
        logging.error(f"Ошибка Telegram API при отправке инвойса: {e}")
        bot.send_message(message.chat.id,
                         f"Произошла ошибка при формировании счета (Telegram API Error: {e.error_code}).")
        user_states.pop(user_id, None)  # Сбрасываем состояние после критической ошибки
    except Exception as e:
        logging.error(f"Общая ошибка в process_star_amount: {e}")
        bot.send_message(message.chat.id, "Произошла неизвестная ошибка при формировании счета.")
        user_states.pop(user_id, None)  # Сбрасываем состояние


@bot.callback_query_handler(func=lambda call: call.data == "open_refund")
def open_refund(call):
    try:
        user_id = call.from_user.id

        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            # Получаем все активные чеки пользователя
            cursor.execute('SELECT amount, receipt FROM Payments WHERE id = ?', (user_id,))
            receipts = cursor.fetchall()

        markup = InlineKeyboardMarkup()

        if not receipts:
            bot.answer_callback_query(call.id, "Нет активных пополнений для возврата.")
            return

        # Словарь для временного хранения (индекс -> полный ID чека)
        user_receipt_map = {}

        for index, (amount, receipt_id) in enumerate(receipts, start=1):
            # 1. Формируем callback_data с коротким индексом
            callback_data_key = f"refund_idx:{index}"

            # 2. Сохраняем полный чек во временном хранилище
            user_receipt_map[index] = receipt_id

            # 3. Создаем кнопку
            button = InlineKeyboardButton(f"{amount}⭐️ Чек №{index} ({receipt_id[:4]}...)",
                                          callback_data=callback_data_key)
            markup.add(button)

        # Сохраняем временное хранилище для этого пользователя
        temp_receipt_storage[user_id] = user_receipt_map

        mainmenubutton = InlineKeyboardButton("◀️ Назад", callback_data="openprofile")
        markup.add(mainmenubutton)

        bot.edit_message_text(
            f"Для возврата звезд нужно оплатить **2% комиссию**, бот вернет **полную сумму пополнения**.\n\nВыберите транзакцию для возврата:",
            user_id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="Markdown"
        )
    except Exception as e:
        # Теперь эта ошибка не должна появляться, так как callback_data короткий.
        logging.error(f"Ошибка в open_refund: {e}")
        bot.answer_callback_query(call.id, "Произошла ошибка при загрузке чеков.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("refund_idx:"))
def handle_refund_payment(call):
    try:
        user_id = call.from_user.id
        # Получаем индекс из callback_data (например, 'refund_idx:1' -> 1)
        receipt_index = int(call.data.split(":")[1])

        # 1. Ищем полный ID чека во временном хранилище
        if user_id not in temp_receipt_storage or receipt_index not in temp_receipt_storage[user_id]:
            bot.answer_callback_query(call.id, "Ошибка: Информация о чеке устарела. Откройте меню возврата заново.")
            return

        receipt_id = temp_receipt_storage[user_id][receipt_index]

        # Очищаем временное хранилище после использования
        temp_receipt_storage.pop(user_id, None)

        # 2. Продолжаем логику поиска и выставления счета по полному ID чека
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            # Проверяем, что чек все еще существует в БД
            cursor.execute("SELECT amount FROM Payments WHERE receipt = ? AND id = ?", (receipt_id, user_id))
            result = cursor.fetchone()

        if not result:
            bot.answer_callback_query(call.id, "Ошибка: чек не найден в базе данных.")
            return

        amount = result[0]
        # Комиссия за возврат - 2% от суммы пополнения, минимум 1 звезда
        commission_amount = max(1, int(amount * (1 - DEP_COMMISSION)))

        bot.delete_message(call.message.chat.id, call.message.message_id)

        # NOTE: provider_token=None означает, что используется Star Payment
        bot.send_invoice(
            chat_id=call.message.chat.id,
            title="Оплата комиссии за возврат",
            description=f"Комиссия 2% за возврат {amount}⭐️ по чеку {receipt_id[:10]}...",
            invoice_payload=f"refund_{receipt_id}_{user_id}",
            # Используем полный receipt_id в payload инвойса (там лимит больше)
            provider_token=None,
            currency="XTR",
            prices=[LabeledPrice(label=f"Комиссия {commission_amount} звёзд", amount=commission_amount)],
            start_parameter="refund_commission",
            is_flexible=False,
        )
    except Exception as e:
        logging.error(f"Ошибка в handle_refund_payment: {e}")
        bot.send_message(call.message.chat.id, "Произошла ошибка при формировании счета комиссии.")
        # Также очищаем состояние, если произошла ошибка
        if user_id in temp_receipt_storage:
            temp_receipt_storage.pop(user_id)


# Обработчик Pre-Checkout Query для всех платежей (пополнение и комиссия)
@bot.pre_checkout_query_handler(func=lambda query: True)
def process_pre_checkout(pre_checkout_query):
    try:
        bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    except Exception as e:
        logging.error(f"Pre-checkout error: {e}")


# Обработчик успешной оплаты
@bot.message_handler(content_types=['successful_payment'])
def handle_successful_payment(message):
    payload = message.successful_payment.invoice_payload
    user_id = message.from_user.id
    amount_paid = message.successful_payment.total_amount  # Сумма в XTR (звездах)
    tranzid = message.successful_payment.telegram_payment_charge_id
    username = message.from_user.username or "None"

    if payload.startswith("refund_"):
        # --- Обработка оплаты комиссии за возврат ---
        try:
            # Payload: refund_{receipt_id}_{user_id}
            parts = payload.split("_")
            receipt_id = parts[1]

            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT amount FROM Payments WHERE receipt = ? AND id = ?', (receipt_id, user_id))
                row = cursor.fetchone()

                if row:
                    original_amount = row[0]
                    # 1. Возврат полной суммы (оригинального пополнения)
                    bot.refund_star_payment(user_id=user_id, telegram_payment_charge_id=receipt_id)

                    # 2. Удаление записи из Payments
                    cursor.execute('DELETE FROM Payments WHERE receipt = ?', (receipt_id,))

                    # 3. Уменьшение баланса пользователя на сумму, которая была зачислена
                    # При пополнении было зачислено: original_amount * DEP_COMMISSION
                    # При возврате эти деньги надо вычесть. Комиссия, которую он заплатил, остается у бота.
                    amount_to_deduct = int(original_amount * DEP_COMMISSION)
                    cursor.execute('UPDATE Users SET balance = balance - ? WHERE id = ?', (amount_to_deduct, user_id))
                    conn.commit()

                    bot.send_message(user_id,
                                     f"✅ Звёзды успешно возвращены! (Возвращено: **{original_amount}** ⭐️, списано с баланса: **{amount_to_deduct}** ⭐️).",
                                     parse_mode="Markdown")
                    _send_or_edit_main_menu(user_id)
                    logging.info(
                        f"Возврат {original_amount} ⭐️ для {user_id} | чек: {receipt_id} | Комиссия за возврат оплачена: {amount_paid} XTR")
                    bot.send_message(LOG_CHANNEL_ID,
                                     f"Возврат {original_amount} ⭐️ для {user_id} | чек: {receipt_id} | Ком. оплачена: {amount_paid} XTR")
                else:
                    bot.send_message(user_id, "Ошибка: ваши звёзды уже были потрачены на подарки.")
        except Exception as e:
            logging.exception(f"Ошибка при обработке возврата: {e}")
            bot.send_message(user_id, f"Произошла критическая ошибка при возврате: {str(e)}")

    else:
        # --- Обработка пополнения баланса ---
        try:
            amount_to_credit = int(amount_paid * DEP_COMMISSION)

            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.cursor()
                # 1. Запись платежа
                cursor.execute('''INSERT INTO Payments (id, username, amount, receipt) VALUES (?, ?, ?, ?)''',
                               (user_id, username, amount_paid, tranzid))
                # 2. Зачисление на баланс с учетом комиссии
                cursor.execute('''UPDATE Users SET balance = balance + ? WHERE id = ?''', (amount_to_credit, user_id))
                conn.commit()

            bot.send_message(user_id,
                             f"✅ Платеж на **{amount_paid}**⭐️ успешно получен! Зачислено: **{amount_to_credit}**⭐️",
                             parse_mode="Markdown")
            _send_or_edit_main_menu(user_id)
            logging.info(
                f"Пополнение {amount_paid} ⭐️ от {user_id} | @{username} | Зачислено: {amount_to_credit} | чек: {tranzid}")
            bot.send_message(LOG_CHANNEL_ID,
                             f"Пополнение {amount_paid} ⭐️ от {user_id} | @{username} | Зачислено: {amount_to_credit} | чек: {tranzid}")
        except Exception as e:
            logging.exception(f"Ошибка обработки платежа: {e}")
            bot.send_message(user_id, f"Ошибка обработки платежа: {str(e)}")


# --- 5. Воркер для автопокупки подарков ---

# 🚨 КОНСТАНТА: ID подарков, которые нужно игнорировать
IGNORED_GIFT_IDS = {
    "5170145012310081615",  # \ud83d\udc9d - 15 звезд
    "5170233102089322756",  # \ud83e\uddf8 - 15 звезд
    "5170250947678437525",  # \ud83c\udf81 - 25 звезд
    "5168103777563050263",  # \ud83c\udf39 - 25 звезд
    "5170144170496491616",  # \ud83c\udf82 - 50 звезд
    "5170314324215857265",  # \ud83d\udc90 - 50 звезд
    "5170564780938756245",  # \ud83d\ude80 - 50 звезд
    "5168043875654172773",  # \ud83c\udfc6 - 100 звезд
    "5170690322832818290",  # \ud83d\udc8d - 100 звезд
    "5170521118301225164",  # \ud83d\udc8e - 100 звезд
    "6028601630662853006"  # \ud83c\udf7e - 50 звезд
}


def safe_request(func, *args, retries=3, delay=3, **kwargs):
    """Обертка для повторных попыток при сетевых ошибках."""
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except requests.exceptions.RequestException as e:
            logging.warning(f"[safe_request] Ошибка сети: {e} (попытка {attempt + 1}/{retries})")
            time.sleep(delay)
        except telebot.apihelper.ApiTelegramException as e:
            logging.warning(f"[safe_request] Ошибка API: {e} (попытка {attempt + 1}/{retries})")
            if 'Too Many Requests' in str(e):
                time.sleep(delay * (attempt + 1))  # Экспоненциальная задержка
            else:
                time.sleep(delay)
        except Exception as e:
            logging.warning(f"[safe_request] Неизвестная ошибка: {e} (попытка {attempt + 1}/{retries})")
            time.sleep(delay)

    raise Exception("Не удалось выполнить запрос после нескольких попыток")


def gift_worker():
    """Основной цикл воркера для поиска и отправки подарков."""
    logging.info("✨ Gift worker запущен")

    while True:
        try:
            # 1. Получение списка доступных подарков
            available = safe_request(bot.get_available_gifts)

            if available is None or not available.gifts:
                logging.info("Fetched gifts: 0 gifts available from Telegram API. Looping...")
                time.sleep(5)
                continue

            logging.info(f"Fetched gifts: {len(available.gifts)} total. Preparing for purchase...")

            target_gifts = []
            filtered_out_count = 0

            for g in available.gifts:
                # 1. ИГНОРИРОВАНИЕ ПО ID
                if g.id in IGNORED_GIFT_IDS:
                    filtered_out_count += 1
                    continue

                # 3. Если total_count равен None, устанавливаем большое число по умолчанию.
                if g.total_count is None:
                    g.total_count = 1000000  # Устанавливаем высокий дефолт для "вечных" подарков

                target_gifts.append(g)

            if filtered_out_count > 0:
                logging.info(f"NOTE: Filtered out {filtered_out_count} gifts (0 star price or in IGNORED_GIFT_IDS).")

            if not target_gifts:
                logging.info("Target gifts list is empty after filtering. Looping...")
                time.sleep(5)
                continue

            # Сортируем по убыванию цены
            target_gifts.sort(key=lambda g: g.star_count, reverse=True)

            # 3. Выбор пользователей
            min_price_required = min(g.star_count for g in target_gifts) if target_gifts else 1

            logging.info(f"MIN price required for SQL query: {min_price_required}⭐️")

            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.cursor()
                # SQL-запрос
                cur.execute("""
                    SELECT U.id, U.balance, S.minprice, S.maxprice, S.maxsupply, S.state
                    FROM Users U
                    JOIN Settings S ON U.id = S.id
                    WHERE U.balance >= ?
                    ORDER BY U.balance DESC
                """, (min_price_required,))
                users = cur.fetchall()

                logging.info(f"SQL query finished. Fetched {len(users)} row(s).")

            if not users:
                logging.info(f"No users found with balance >= {min_price_required}⭐️. Looping...")
                time.sleep(5)
                continue

            logging.info(f"Found {len(users)} user(s) for processing.")

            # 4. Обработка пользователей и покупка
            for user_id, initial_balance, minp, maxp, maxs, state in users:

                with sqlite3.connect(DB_PATH) as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT balance FROM Users WHERE id = ?", (user_id,))
                    current_balance = cur.fetchone()[0]

                logging.info(
                    f"→ Пользователь {user_id}: balance={current_balance}, state={state}, range=[{minp}…{maxp}], maxsupply={maxs}")

                if state == 0:
                    logging.info(f"    — Автопокупка для пользователя {user_id} выключена (state=0). Пропускаем.")
                    continue

                # ФИЛЬТРАЦИЯ ТОЛЬКО ПО ЦЕНЕ
                user_gifts = [g for g in target_gifts if minp <= g.star_count <= maxp]

                logging.info(f"    — Найдено {len(user_gifts)} подарков, подходящих под настройки пользователя.")

                if not user_gifts:
                    continue

                # Сортируем по возрастанию цены, чтобы сначала купить самый дешевый подарок
                user_gifts.sort(key=lambda g: g.star_count)

                for gift in user_gifts:

                    if current_balance < gift.star_count or bot_stars < gift.star_count:
                        logging.info(
                            f"    — Не хватает средств (User: {current_balance}, Bot virtual: {bot_stars}) для {gift.star_count}⭐️")
                        continue

                    # ВКЛЮЧАЕМ USER.maxsupply (maxs) В РАСЧЕТ max_count
                    max_by_user = current_balance // gift.star_count
                    max_by_supply_global = gift.total_count
                    max_by_setting = maxs  # maxsupply из настроек пользователя

                    max_count = min(max_by_user, max_by_supply_global, max_by_setting)
                    if max_count <= 0:
                        continue

                    logging.info(
                        f"    — Отправляем {max_count}×gift({gift.id}, {gift.star_count}⭐️) пользователю {user_id}")

                    successful_buys = 0
                    for _ in range(max_count):
                        try:
                            # Отправка подарка
                            safe_request(
                                bot.send_gift,
                                user_id=user_id,
                                gift_id=gift.id,
                                pay_for_upgrade=False
                            )
                        except Exception as e:
                            logging.exception(f"Ошибка send_gift: user={user_id}, gift={gift.id} - {e}")
                            break
                        else:
                            successful_buys += 1
                            current_balance -= gift.star_count

                            gift.total_count -= 1

                    if successful_buys > 0:
                        # Обновление баланса пользователя в БД
                        with sqlite3.connect(DB_PATH) as conn:
                            cur = conn.cursor()
                            cur.execute("UPDATE Users SET balance = ? WHERE id = ?", (current_balance, user_id))
                            conn.commit()
                        logging.info(
                            f"    — Успешно куплено {successful_buys} подарков. Новый баланс: {current_balance}⭐️")
                        bot.send_message(user_id,
                                         f"🎁 Автопокупка! Куплено **{successful_buys}** подарков (по **{gift.star_count}**⭐️)!",
                                         parse_mode="Markdown")

                    if current_balance < min_price_required:
                        break

        except Exception:
            logging.exception("‼ Ошибка в gift_worker")

        time.sleep(5)


def run_gift_worker_forever():
    """Запускает gift_worker и обеспечивает его перезапуск при падении."""
    while True:
        try:
            gift_worker()
        except Exception:
            logging.exception("💥 gift_worker упал, перезапуск через 5 сек")
            time.sleep(5)


def start_worker():
    """Запуск воркера в отдельном потоке."""
    # Указываем имя потока для удобства логирования
    threading.Thread(target=run_gift_worker_forever, name="GiftWorker", daemon=True).start()


# --- 6. Запуск ---

def main_bot_loop():
    """Основной цикл работы бота."""
    global bot  # Используем глобальный объект бота

    # 1. Инициализация БД
    initialize_db()

    # 2. Запуск воркера
    start_worker()

    # 3. Запуск бота
    logging.info("Бот запущен и ожидает команд...")
    bot.infinity_polling(none_stop=True)


if __name__ == '__main__':
    while True:
        try:
            main_bot_loop()
        except Exception:
            logging.exception("💥 Основной цикл бота упал, перезапуск через 5 сек")
            time.sleep(5)