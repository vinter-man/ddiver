"""ddiver-core: Telegram delivery and everything before it.

Rules and matching, storage, the bot UI and delivery, the supervisor, health, metrics and the
bot client. It talks to Telegram through Telethon, on the bot side only: no user session is
opened, read or written here. ddiver is one app on top of it (a Telethon listener); an app with
a different source reuses the rest unchanged. Nothing in here imports ``ddiver``.
"""
