"""Telegram integration scaffold."""

from app.config import settings


class TelegramClient:
    """Adapter for Telegram bot workflows.

    TODO:
    - Install and configure the Telegram bot SDK.
    - Require TELEGRAM_BOT_TOKEN.
    - Add inbound update handling and outbound messaging.
    """

    def __init__(self) -> None:
        self.bot_token = settings.telegram_bot_token

    def is_configured(self) -> bool:
        return bool(self.bot_token)

