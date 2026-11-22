"""
Автоматичний репост нових публікацій з одного Telegram-каналу в інший.

Режими (змінна MODE):
  copy    - публікація як новий пост від імені вашого каналу + підпис внизу (за замовчуванням)
  forward - пересилання з позначкою "Forwarded from"
"""
import asyncio
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, MediaCaptionTooLongError
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SOURCE = os.getenv("SOURCE", "Tsaplienko")
TARGET_RAW = os.environ["TARGET"]  # @username або числовий id (-100...)
MODE = os.getenv("MODE", "copy").lower()
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
SESSION = os.getenv("SESSION", "repost_session")
SESSION_STRING = os.getenv("SESSION_STRING")  # для GitHub Actions
RUN_ONCE = os.getenv("RUN_ONCE", "0") == "1"  # один прохід і вихід

# Підпис внизу кожної публікації (порожній FOOTER_TEXT = без підпису)
FOOTER_TEXT = os.getenv("FOOTER_TEXT", "🇺🇦 Review News / Підписатися")
FOOTER_URL = os.getenv("FOOTER_URL", "https://t.me/reviewnews")

TARGET = int(TARGET_RAW) if TARGET_RAW.lstrip("-").isdigit() else TARGET_RAW

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO
)
log = logging.getLogger("repost")

client = TelegramClient(
    StringSession(SESSION_STRING) if SESSION_STRING else SESSION, API_ID, API_HASH
)
lock = asyncio.Lock()  # щоб пости йшли суворо по черзі


def load_last_id():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text()).get("last_id")
    return None


def save_last_id(msg_id: int):
    current = load_last_id() or 0
    if msg_id > current:
        STATE_FILE.write_text(json.dumps({"last_id": msg_id}))


def u16(s: str) -> int:
    """Довжина в UTF-16 одиницях (так Telegram рахує зсуви форматування)."""
    return len(s.encode("utf-16-le")) // 2


def with_footer(msg):
    """Повертає (текст, entities) з доданим підписом-посиланням."""
    text = msg.raw_text or ""
    entities = list(msg.entities or [])
    if not FOOTER_TEXT:
        return text, entities
    sep = "\n\n" if text else ""
    offset = u16(text + sep)
    entities.append(
        MessageEntityTextUrl(offset=offset, length=u16(FOOTER_TEXT), url=FOOTER_URL)
    )
    return text + sep + FOOTER_TEXT, entities


def footer_markdown() -> str:
    return f"[{FOOTER_TEXT}]({FOOTER_URL})" if FOOTER_TEXT else ""


async def send_single(m):
    """Один пост як новий (без 'Переслано з') + підпис."""
    text, entities = with_footer(m)

    # Звичайний текст або текст із прев'ю посилання
    if not m.media or m.web_preview:
        await client.send_message(
            TARGET,
            text,
            formatting_entities=entities,
            link_preview=bool(m.web_preview),  # прев'ю лише якщо було в оригіналі
        )
        return

    # Фото, відео, документи, голосові тощо
    if m.photo or (m.document and not m.sticker):
        try:
            await client.send_file(
                TARGET, m.media, caption=text, formatting_entities=entities
            )
        except MediaCaptionTooLongError:
            # Підпис задовгий для медіа: медіа окремо, текст окремим повідомленням
            await client.send_file(TARGET, m.media)
            await client.send_message(
                TARGET, text, formatting_entities=entities, link_preview=False
            )
        return

    # Опитування, стікери, геолокація тощо: копіюємо як є, без підпису
    await client.send_message(TARGET, m)


async def send_album(msgs):
    """Альбом як новий + підпис у підписі першого елемента."""
    captions = [m.text or "" for m in msgs]  # markdown зі збереженням форматування
    footer = footer_markdown()
    if footer:
        captions[0] = (captions[0] + "\n\n" + footer) if captions[0] else footer
    try:
        await client.send_file(TARGET, [m.media for m in msgs], caption=captions)
    except MediaCaptionTooLongError:
        await client.send_file(TARGET, [m.media for m in msgs])
        await client.send_message(
            TARGET, "\n\n".join(c for c in captions if c), link_preview=False
        )


async def send(msgs, source_entity):
    if MODE == "forward":
        await client.forward_messages(TARGET, msgs, from_peer=source_entity)
    elif len(msgs) > 1:
        await send_album(msgs)
    else:
        await send_single(msgs[0])


async def relay(msgs):
    msgs = sorted(msgs, key=lambda m: m.id)
    async with lock:
        for attempt in range(3):
            try:
                source_entity = await client.get_input_entity(SOURCE)
                await send(msgs, source_entity)
                save_last_id(msgs[-1].id)
                log.info("Репост виконано: %s", [m.id for m in msgs])
                return
            except FloodWaitError as e:
                log.warning("FloodWait: чекаю %s с", e.seconds)
                await asyncio.sleep(e.seconds + 1)
            except Exception:
                log.exception("Помилка репосту %s", [m.id for m in msgs])
                return


@client.on(events.NewMessage(chats=SOURCE))
async def on_new(event):
    if event.message.grouped_id:  # альбоми обробляє events.Album
        return
    if event.message.action:  # службові повідомлення
        return
    await relay([event.message])


@client.on(events.Album(chats=SOURCE))
async def on_album(event):
    await relay(list(event.messages))


async def catch_up():
    """Підхоплює пости, пропущені, поки скрипт не працював."""
    last_id = load_last_id()
    if last_id is None:
        latest = await client.get_messages(SOURCE, limit=1)
        save_last_id(latest[0].id if latest else 0)
        log.info("Перший запуск: історію не репостимо, стартуємо з нових постів.")
        return

    missed = await client.get_messages(SOURCE, min_id=last_id, limit=None)
    missed = [m for m in missed if not m.action]
    if not missed:
        return
    log.info("Пропущених постів: %d", len(missed))

    groups, order = {}, []
    for m in sorted(missed, key=lambda x: x.id):
        key = m.grouped_id or f"single-{m.id}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(m)
    for key in order:
        await relay(groups[key])
        await asyncio.sleep(1)


async def main():
    await client.start()  # при першому запуску попросить номер і код
    await catch_up()
    if RUN_ONCE:
        log.info("Режим одного проходу завершено.")
        return
    log.info("Слухаю @%s -> %s (режим: %s)", SOURCE, TARGET_RAW, MODE)
    await client.run_until_disconnected()


if __name__ == "__main__":
    client.loop.run_until_complete(main())
