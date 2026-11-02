import re
import time
import hashlib
import secrets
import sqlite3
from decimal import Decimal, InvalidOperation


def parse_offer(args):
    parts = (args or '').split()
    if len(parts) != 3:
        raise ValueError('Формат: /offer @адресат https://t.me/nft/PlushPepe-2687 20')
    target, url, amount = parts
    target = target.lstrip('@')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', target):
        raise ValueError('Укажите корректный @username адресата.')
    if not re.fullmatch(r'https://t\.me/nft/[A-Za-z0-9]+-\d+', url):
        raise ValueError('Нужна ссылка на коллекционный подарок: https://t.me/nft/Название-Номер')
    try:
        value = Decimal(amount.replace(',', '.'))
        if not value.is_finite() or not Decimal('1') <= value <= Decimal('1000000000') or value != value.to_integral_value():
            raise ValueError()
    except (InvalidOperation, ValueError):
        raise ValueError('Укажите целое положительное количество Stars.')
    return target.lower(), url, str(int(value))


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('''CREATE TABLE IF NOT EXISTS offers (
            id TEXT PRIMARY KEY, buyer INTEGER NOT NULL, username TEXT NOT NULL,
            target TEXT NOT NULL, url TEXT NOT NULL, amount TEXT NOT NULL,
            seller INTEGER, state TEXT NOT NULL DEFAULT 'new')''')
        self.db.commit()
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(offers)')}
        for name, definition in [('currency', "TEXT NOT NULL DEFAULT 'TON'"),
                                 ('expires', 'REAL'), ('sent_at', 'REAL'), ('recipient', 'TEXT')]:
            if name not in columns:
                self.db.execute(f'ALTER TABLE offers ADD COLUMN {name} {definition}')
        self.db.execute('CREATE TABLE IF NOT EXISTS inline_messages (message_id TEXT PRIMARY KEY, offer_id TEXT NOT NULL)')
        self.db.commit()
        self.db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS inline_offers (fingerprint TEXT PRIMARY KEY, offer_id TEXT NOT NULL)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS business_connections (
            id TEXT PRIMARY KEY, owner INTEGER NOT NULL, enabled INTEGER NOT NULL, can_reply INTEGER NOT NULL)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS business_messages (
            connection TEXT NOT NULL, chat INTEGER NOT NULL, message INTEGER NOT NULL, offer_id TEXT NOT NULL,
            PRIMARY KEY(connection, chat, message))''')
        self.db.commit()

    def save_connection(self, connection):
        can_reply = bool(connection.rights and connection.rights.can_reply)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO business_connections VALUES(?,?,?,?)',
                            (connection.id, connection.user.id, connection.is_enabled, can_reply))

    def business_sent(self, key, owner, connection, chat, message):
        offer = self.get(key)
        if not offer or offer['buyer'] != owner:
            return
        now = time.time()
        with self.db:
            self.db.execute('UPDATE offers SET sent_at=?,expires=?,seller=? WHERE id=?',
                            (now, now + 21600, chat, key))
            self.db.execute('INSERT OR IGNORE INTO business_messages VALUES(?,?,?,?)',
                            (connection, chat, message, key))

    def setting(self, key):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, value))

    def can_create(self, uid):
        return self.setting(f'creator:{uid}') == 'enabled'

    def request_creator_access(self, uid):
        if not self.can_create(uid):
            self.set_setting(f'creator:{uid}', 'pending')

    def activate_creator(self, uid):
        if self.setting(f'creator:{uid}') not in {'pending', 'enabled'}:
            return False
        self.set_setting(f'creator:{uid}', 'enabled')
        return True

    def inline_offer(self, buyer, username, target, url, amount, recipient=None):
        # Telegram sends queries repeatedly while typing; reuse the same offer.
        fingerprint = hashlib.sha256(f'XTR|{buyer}|{username}|{target}|{url}|{amount}|{recipient or username}'.encode()).hexdigest()
        row = self.db.execute('SELECT offer_id FROM inline_offers WHERE fingerprint=?', (fingerprint,)).fetchone()
        if row:
            return self.get(row[0])
        key = self.create(buyer, username, target, url, amount, recipient=recipient)
        with self.db:
            self.db.execute('INSERT INTO inline_offers VALUES (?,?)', (fingerprint, key))
        return self.get(key)

    def create(self, buyer, username, target, url, amount, recipient=None):
        key = secrets.token_urlsafe(12)
        with self.db:
            self.db.execute('INSERT INTO offers(id,buyer,username,target,url,amount,currency,expires,recipient) VALUES(?,?,?,?,?,?,?,?,?)',
                            (key, buyer, username, target, url, amount, 'XTR', time.time() + 21600, recipient or username))
        return key

    def get(self, key):
        return self.db.execute('SELECT * FROM offers WHERE id=?', (key,)).fetchone()

    def mark_sent(self, key, buyer, message_id):
        offer = self.get(key)
        if not offer or offer['buyer'] != buyer:
            return
        now = time.time()
        with self.db:
            self.db.execute('UPDATE offers SET sent_at=?, expires=? WHERE id=? AND sent_at IS NULL AND state="new"',
                            (now, now + 21600, key))
            self.db.execute('INSERT OR IGNORE INTO inline_messages VALUES (?,?)', (message_id, key))

    def allowed(self, offer, uid, username):
        return bool(offer and uid != offer['buyer'] and (
            offer['seller'] == uid if offer['seller'] is not None
            else (username or '').lower() == offer['target']))

    def transition(self, key, uid, username, action):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            offer = self.get(key)
            if not self.allowed(offer, uid, username):
                return False
            if offer['state'] == 'new' and offer['expires'] and time.time() >= offer['expires']:
                return False
            transitions = {'accept': ('new', 'accepted'), 'decline': ('new', 'declined'),
                           'sent': ('accepted', 'pending'), 'paid': ('pending', 'paid')}
            if action not in transitions:
                return False
            before, after = transitions[action]
            return self.db.execute('UPDATE offers SET state=?,seller=? WHERE id=? AND state=?',
                                   (after, uid, key, before)).rowcount == 1


import asyncio
import time
import math
from contextlib import suppress
import logging
import os
from html import escape
from pathlib import Path
from urllib.parse import urlencode

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard, Message, LinkPreviewOptions
from aiogram.types import InlineQuery, InlineQueryResultArticle, InlineQueryResultsButton, InputTextMessageContent, ChosenInlineResult
from aiogram.types import BusinessConnection
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError, TelegramRetryAfter
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
INLINE_RECIPIENT = 'lovi_lvl'


def gift_recipient(offer):
    return offer['recipient'] or offer['username']


def access_error(offer, user):
    if not offer:
        return 'Офер не найден. Попросите автора создать новый.'
    if user.id == offer['buyer']:
        return 'Вы — автор офера. Принять или отклонить его может только продавец.'
    if offer['seller'] is not None:
        return 'Офер привязан к другому аккаунту продавца. Откройте его с аккаунта, для которого он создан.'
    current = f'@{user.username}' if user.username else 'аккаунт без username'
    return f'Офер создан для @{offer["target"]}. Вы вошли как {current}. Проверьте username продавца в запросе.'


def price(o):
    return f"{o['amount']} {'⭐ Stars' if o['currency'] == 'XTR' else 'TON'}"


def render(o):
    gift = escape(o['url'].rsplit('/', 1)[-1])
    text = (f'<b><a href="{o["url"]}">{gift}</a></b>\n\n'
            f'Предложение: <b>{price(o)}</b>\n'
            'Оплата после передачи.')
    key = o['id']
    rows = []
    if o['state'] == 'new' and o['expires'] and time.time() >= o['expires']:
        text += '\n\nСрок действия офера истёк.'
    elif o['state'] == 'new':
        rows += [[Button(text='Отклонить', callback_data=f'decline:{key}'),
                  Button(text='Принять', callback_data=f'accept:{key}')]]
    elif o['state'] == 'accepted':
        text += '\n\nПредложение принято. Оплата ещё не получена.'
        rows += [[Button(text='Передать NFT', callback_data=f'transfer:{key}')],
                 [Button(text='Как передать?', callback_data=f'help:{key}')]]
    elif o['state'] == 'pending':
        text += '\n\n⏳ <b>Оплата ожидается.</b>\nВы отметили передачу подарка. Бот не проверяет передачу и платёж.'
        rows += [[Button(text='Я получил оплату', callback_data=f'paid:{key}')]]
    elif o['state'] == 'paid':
        text += '\n\n✅ Получение оплаты подтверждено продавцом.'
    else:
        text += '\n\nПредложение отклонено.'
    if o['state'] == 'new' and o['expires'] and o['expires'] > time.time():
        minutes = min(360, max(0, math.ceil((o['expires'] - time.time()) / 60)))
        text += f'\n\n<i>Офер действителен ещё {minutes // 60} ч {minutes % 60} мин</i>'
    return text, Keyboard(inline_keyboard=rows)


def setup(store):
    dp = Dispatcher()

    @dp.business_connection()
    async def connection_changed(connection: BusinessConnection):
        store.save_connection(connection)

    @dp.message(Command('business'))
    async def business_status(message: Message):
        if message.chat.type != 'private':
            return
        rows = store.db.execute('SELECT * FROM business_connections WHERE owner=?', (message.from_user.id,)).fetchall()
        active = any(r['enabled'] and r['can_reply'] for r in rows)
        last_error = store.setting(f'business_error:{message.from_user.id}')
        if last_error:
            await message.answer('Последняя ошибка отправки бизнес-офера:\n' + escape(last_error))
        await message.answer(
            'Бизнес-подключение активно. В разрешённом чате продавца отправьте:\n'
            '<code>/offer @продавец https://t.me/nft/PlushPepe-2687 1000</code>\n'
            'Цена — в Stars. Команду должен отправить владелец бизнес-аккаунта.' if active else
            'Нет активного подключения с правом ответа. Включите Business Mode в BotFather, '
            'затем подключите @offersus_bot в настройках Telegram → Telegram Business → Чат-боты. '
            'Разрешите ответы в выбранных чатах.')

    @dp.business_message(Command('offer', 'o'))
    async def business_offer(message: Message, command: CommandObject, bot: Bot):
        if not message.business_connection_id or not message.from_user or message.sender_business_bot:
            return
        connection = await bot.get_business_connection(message.business_connection_id)
        store.save_connection(connection)
        if (not connection.is_enabled or not connection.rights or not connection.rights.can_reply
                or message.from_user.id != connection.user.id or message.chat.type != 'private'):
            return
        if not store.can_create(connection.user.id):
            store.set_setting(f'business_error:{connection.user.id}', 'Доступ к созданию оферов не активирован. Откройте /work в личке бота.')
            return
        try:
            args = command.args
            if command.command == 'o':
                if not message.chat.username:
                    raise ValueError('Для короткой команды у продавца должен быть публичный @username.')
                if len((args or '').split()) != 2:
                    raise ValueError('Формат: /o https://t.me/nft/PlushPepe-2687 20')
                args = f'@{message.chat.username} {args}'
            target, url, amount = parse_offer(args)
            if not connection.user.username:
                raise ValueError('Задайте публичный @username получателя подарка.')
            if not message.chat.username or target != message.chat.username.lower():
                raise ValueError('Адресат команды должен совпадать с @username собеседника этого чата.')
            if message.chat.id == connection.user.id:
                return
        except ValueError as exc:
            try:
                await bot.send_message(chat_id=message.chat.id, business_connection_id=connection.id,
                                       text='Автоматическое сообщение: ' + escape(str(exc)))
            except TelegramAPIError as error:
                store.set_setting(f'business_error:{connection.user.id}', error.message)
                logging.warning('Business validation reply failed: %s', error.message)
            return
        key = store.create(connection.user.id, connection.user.username, target, url, amount)
        # Bind to the actual conversation partner before exposing the buttons.
        with store.db:
            store.db.execute('UPDATE offers SET seller=? WHERE id=?', (message.chat.id, key))
        text, keyboard = render(store.get(key))
        try:
            sent = await bot.send_message(chat_id=message.chat.id, business_connection_id=connection.id,
                text=text, reply_markup=keyboard,
                link_preview_options=LinkPreviewOptions(url=url, is_disabled=False,
                                                        prefer_large_media=True, show_above_text=True))
        except TelegramAPIError as exc:
            store.set_setting(f'business_error:{connection.user.id}', exc.message)
            logging.warning('Business offer could not be sent: %s', exc.message)
            return
        store.set_setting(f'business_error:{connection.user.id}', '')
        store.business_sent(key, connection.user.id, connection.id, sent.chat.id, sent.message_id)

    @dp.chosen_inline_result()
    async def chosen(result: ChosenInlineResult):
        if result.inline_message_id:
            store.mark_sent(result.result_id, result.from_user.id, result.inline_message_id)

    @dp.inline_query()
    async def inline(query: InlineQuery):
        if not store.can_create(query.from_user.id):
            await query.answer([], cache_time=0, is_personal=True,
                               button=InlineQueryResultsButton(text='Открыть бота', start_parameter='setup'))
            return
        try:
            if not query.from_user.username:
                raise ValueError('Задайте @username в настройках Telegram')
            if query.query.startswith('offer:'):
                offer = store.get(query.query.removeprefix('offer:').strip())
                if not offer or offer['buyer'] != query.from_user.id:
                    raise ValueError('Офер недоступен')
                target, amount = offer['target'], offer['amount']
            else:
                target, url, amount = parse_offer(query.query)
                if target == query.from_user.username.lower():
                    raise ValueError('Укажите @username продавца')
                offer = store.inline_offer(query.from_user.id, query.from_user.username, target, url, amount,
                                           recipient=INLINE_RECIPIENT)
        except ValueError:
            await query.answer([], cache_time=0, is_personal=True,
                               button=InlineQueryResultsButton(text='Как создать офер — инструкция', start_parameter='setup'))
            return
        text, keyboard = render(offer)
        await query.answer([InlineQueryResultArticle(
            id=offer['id'], title=f'{price(offer)} · @{target}',
            description=offer['url'].rsplit('/', 1)[-1],
            input_message_content=InputTextMessageContent(
                message_text=text, parse_mode='HTML',
                link_preview_options=LinkPreviewOptions(url=offer['url'], is_disabled=False,
                                                        prefer_large_media=True, show_above_text=True)),
            reply_markup=keyboard)],
            cache_time=0, is_personal=True)

    async def show(message, offer):
        text, keyboard = render(offer)
        await message.answer(text, reply_markup=keyboard,
                             link_preview_options=LinkPreviewOptions(url=offer['url'], is_disabled=False, prefer_large_media=True, show_above_text=True))

    @dp.message(Command('work'))
    async def work(message: Message):
        if message.chat.type != 'private' or not message.from_user:
            return
        store.request_creator_access(message.from_user.id)
        await message.answer(
            'Создание оферов\n\nНажмите кнопку ниже — доступ активируется автоматически.\n'
            f'Получатель подарков в новых inline-оферах: @{INLINE_RECIPIENT}. '
            'Оплата и передача выполняются вручную.',
            reply_markup=Keyboard(inline_keyboard=[[Button(text='Оставить заявку inline', callback_data='work_apply')]]))

    @dp.message(CommandStart())
    async def start(message: Message, command: CommandObject):
        if message.chat.type != 'private':
            return
        if command.args and command.args != 'setup':
            offer = store.get(command.args)
            if not offer or not store.allowed(offer, message.from_user.id, message.from_user.username):
                await message.answer(escape(access_error(offer, message.from_user)))
                return
            await show(message, offer)
            return
        await message.answer(
            '🎁 Оферы на коллекционные подарки\n\n'
            'Здесь можно получать предложения о покупке подарков в Telegram, '
            'принимать или отклонять их и отмечать этапы сделки.')

    @dp.message(Command('offer'))
    async def create(message: Message, command: CommandObject, bot: Bot):
        if message.chat.type != 'private':
            return
        user = message.from_user
        if not store.can_create(user.id):
            await message.answer('Доступ к созданию оферов не активирован.')
            return
        if not user.username:
            await message.answer('Сначала задайте @username в настройках Telegram.')
            return
        try:
            target, url, amount = parse_offer(command.args)
            if target == user.username.lower():
                raise ValueError('Укажите другого пользователя.')
        except ValueError as exc:
            await message.answer(escape(str(exc)))
            return
        key = store.create(user.id, user.username, target, url, amount, recipient=INLINE_RECIPIENT)
        me = await bot.get_me()
        link = f'https://t.me/{me.username}?start={key}'
        share = 'https://t.me/share/url?' + urlencode({'url': link, 'text': f'Предложение для @{target}: {amount} Stars за подарок. Оплата после передачи.'})
        await message.answer(f'Офер для @{target} создан. Вы — автор офера. Получатель подарка: @{INLINE_RECIPIENT}.\n'
                             f'Отправьте адресату ссылку:\n{link}\n\n'
                             'Ссылка предназначена только указанному адресату. Не меняйте свой @username до завершения сделки.',
                             reply_markup=Keyboard(inline_keyboard=[
                                 [Button(text='Отправить офер в чат', switch_inline_query=f'offer:{key}')],
                                 [Button(text='Отправить ссылку', url=share)]]))

    @dp.callback_query(F.data.contains(':'))
    async def callback(query: CallbackQuery, bot: Bot):
        action, key = query.data.split(':', 1)
        offer = store.get(key)
        is_inline = bool(query.inline_message_id)
        if not is_inline and (not isinstance(query.message, Message) or query.message.chat.type != 'private'):
            await query.answer('Откройте предложение в личном чате с ботом.', show_alert=True)
            return
        if not store.allowed(offer, query.from_user.id, query.from_user.username):
            await query.answer(access_error(offer, query.from_user), show_alert=True)
            return
        business_id = query.message.business_connection_id if isinstance(query.message, Message) else None
        if business_id:
            connection = await bot.get_business_connection(business_id)
            if not connection.is_enabled or connection.user.id != offer['buyer']:
                await query.answer('Бизнес-подключение недоступно.', show_alert=True)
                return
        async def display(text, keyboard):
            if business_id:
                try:
                    await bot.edit_message_text(business_connection_id=business_id,
                        chat_id=query.message.chat.id, message_id=query.message.message_id,
                        text=text, parse_mode='HTML', reply_markup=keyboard,
                        link_preview_options=LinkPreviewOptions(url=offer['url'], is_disabled=False,
                                                                prefer_large_media=True, show_above_text=True))
                except TelegramBadRequest as exc:
                    if 'message is not modified' not in str(exc).lower():
                        raise
                return
            if is_inline:
                try:
                    await bot.edit_message_text(inline_message_id=query.inline_message_id,
                                                text=text, parse_mode='HTML', reply_markup=keyboard,
                                                link_preview_options=LinkPreviewOptions(
                                                    url=offer['url'], is_disabled=False,
                                                    prefer_large_media=True, show_above_text=True))
                except TelegramBadRequest as exc:
                    error = str(exc).lower()
                    if 'there is no text in the message to edit' in error:
                        # Previously sent GIF offers remain operable.
                        await bot.edit_message_caption(inline_message_id=query.inline_message_id,
                                                       caption=text, parse_mode='HTML', reply_markup=keyboard)
                    elif 'message is not modified' not in error:
                        raise
            else:
                await query.message.answer(text, reply_markup=keyboard,
                                           link_preview_options=LinkPreviewOptions(
                                               url=offer['url'], is_disabled=False,
                                               prefer_large_media=True, show_above_text=True))

        if action in {'transfer', 'help'}:
            if offer['state'] != 'accepted':
                await query.answer('Сначала примите предложение или откройте его актуальную карточку.', show_alert=True)
                return
            await query.answer()
            await display(
                'Откройте свой профиль → Подарки → нужный подарок → Передать. '
                'Выберите получателя и проверьте его профиль. Telegram может взимать комиссию.\n'
                'После передачи вернитесь сюда и нажмите «Я передал подарок».',
                Keyboard(inline_keyboard=[
                    [Button(text=f'Профиль @{gift_recipient(offer)}', url=f'https://t.me/{gift_recipient(offer)}?profile')],
                    [Button(text='Я передал подарок', callback_data=f'sent:{key}')]]))
            return
        if action == 'paid' and offer['state'] == 'pending':
            await query.answer()
            await display('Подтвердите, только если вы действительно получили оплату.',
                Keyboard(inline_keyboard=[[Button(text='Да, оплата получена', callback_data=f'confirm_paid:{key}')],
                                         [Button(text='Назад', callback_data=f'view:{key}')]]))
            return
        if action == 'view':
            await query.answer()
            await display(*render(offer))
            return
        if action == 'confirm_paid':
            action = 'paid'
        if not store.transition(key, query.from_user.id, query.from_user.username, action):
            await query.answer('Статус уже изменился. Откройте предложение по исходной ссылке.', show_alert=True)
            return
        await query.answer()
        await display(*render(store.get(key)))

    @dp.callback_query(F.data == 'work_apply')
    async def apply_work(query: CallbackQuery):
        if (not isinstance(query.message, Message) or query.message.chat.type != 'private'
                or query.message.chat.id != query.from_user.id):
            await query.answer('Откройте команду /work в личном чате с ботом.', show_alert=True)
            return
        if not store.activate_creator(query.from_user.id):
            await query.answer('Сначала отправьте /work в личном чате с ботом.', show_alert=True)
            return
        await query.answer('Доступ открыт')
        await query.message.answer(
            '✅ Доступ к созданию оферов открыт.\n\n'
            'Нажмите кнопку и выберите чат продавца. В запросе укажите:\n'
            '<code>@продавец https://t.me/nft/PlushPepe-2687 20</code>\n\n'
            f'Получатель подарка: @{INLINE_RECIPIENT}. Цена — в Stars.',
            reply_markup=Keyboard(inline_keyboard=[[Button(text='Создать офер inline', switch_inline_query='')]]))

    return dp


async def update_timers(bot, store):
    while True:
        for row in store.db.execute('SELECT * FROM business_messages').fetchall():
            offer = store.get(row['offer_id'])
            if not offer or offer['state'] != 'new':
                continue
            connection = store.db.execute('SELECT * FROM business_connections WHERE id=?', (row['connection'],)).fetchone()
            if not connection or not connection['enabled'] or not connection['can_reply']:
                continue
            text, keyboard = render(offer)
            try:
                await bot.edit_message_text(business_connection_id=row['connection'], chat_id=row['chat'],
                    message_id=row['message'], text=text, parse_mode='HTML', reply_markup=keyboard,
                    link_preview_options=LinkPreviewOptions(url=offer['url'], is_disabled=False,
                                                            prefer_large_media=True, show_above_text=True))
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
            except TelegramAPIError:
                pass
            if offer['expires'] and time.time() >= offer['expires']:
                with store.db:
                    store.db.execute('DELETE FROM business_messages WHERE connection=? AND chat=? AND message=?',
                                     (row['connection'], row['chat'], row['message']))
            await asyncio.sleep(0.05)
        for row in store.db.execute('SELECT message_id, offer_id FROM inline_messages').fetchall():
            offer = store.get(row['offer_id'])
            if not offer or offer['state'] != 'new':
                continue
            text, keyboard = render(offer)
            try:
                await bot.edit_message_text(inline_message_id=row['message_id'], text=text,
                    parse_mode='HTML', reply_markup=keyboard,
                    link_preview_options=LinkPreviewOptions(url=offer['url'], is_disabled=False,
                                                            prefer_large_media=True, show_above_text=True))
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
            except TelegramBadRequest as exc:
                if 'message is not modified' not in str(exc).lower():
                    with store.db:
                        store.db.execute('DELETE FROM inline_messages WHERE message_id=?', (row['message_id'],))
            except TelegramAPIError:
                logging.warning('Timer update temporarily failed')
            else:
                if offer['expires'] and time.time() >= offer['expires']:
                    with store.db:
                        store.db.execute('DELETE FROM inline_messages WHERE message_id=?', (row['message_id'],))
            await asyncio.sleep(0.05)
        await asyncio.sleep(30)


async def main():
    load_dotenv(ROOT / '.env')
    token = os.getenv('GIFT_OFFER_BOT_TOKEN')
    if not token:
        raise SystemExit('Заполните GIFT_OFFER_BOT_TOKEN в .env или переменных окружения')
    store = Store(ROOT / 'offers.sqlite3')
    async with Bot(token, default=DefaultBotProperties(parse_mode='HTML')) as bot:
        timer = asyncio.create_task(update_timers(bot, store))
        try:
            await setup(store).start_polling(bot)
        finally:
            timer.cancel()
            with suppress(asyncio.CancelledError):
                await timer


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
