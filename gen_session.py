"""Запустіть ОДИН раз локально, щоб отримати SESSION_STRING для GitHub Secrets."""
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("API_ID: "))
api_hash = input("API_HASH: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as c:
    print("\nВаш SESSION_STRING (тримайте в таємниці!):\n")
    print(c.session.save())
