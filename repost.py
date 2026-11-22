"""
Автоматичний репост нових публікацій з одного Telegram-каналу в інший.

Режими (змінна MODE у .env):
  forward - пересилання з позначкою "Forwarded from" (зберігає авторство, рекомендовано)
  copy    - копія поста без позначки про пересилання
"""
import asyncio
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SOURCE = os.getenv("SOURCE", "Tsaplienko")
TARGET_RAW = os.environ["TARGET"]  # @username або числовий id (-100...)
MODE = os.getenv("MODE", "forward").lower()
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
SESSION = os.getenv("SESSION", "repost_session")
SESSION_STRING = os.getenv("SESSION_STRING")  # для GitHub Actions
RUN_ONCE = os.getenv("RUN_ONCE", "0") == "1"  # один прохід і вихід

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


async def send(msgs, source_entity):
    """Надсилає один пост або альбом у цільовий канал."""
    if MODE == "forward":
        await client.forward_messages(TARGET, msgs, from_peer=source_entity)
    elif len(msgs) > 1:
        await client.send_file(
            TARGET,
            [m.media for m in msgs],
            caption=[m.text or "" for m in msgs],
        )
    else:
        await client.send_message(TARGET, msgs[0])


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
