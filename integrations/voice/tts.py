"""Text-to-speech integration scaffold."""

from app.config import settings


class TextToSpeechClient:
    """Adapter for a future text-to-speech provider.

    TODO:
    - Select the TTS provider and SDK.
    - Require VOICE_API_KEY or provider-specific credentials.
    - Define audio format, voice selection, and streaming behavior.
    """

    def __init__(self) -> None:
        self.api_key = settings.voice_api_key

    def is_configured(self) -> bool:
        return bool(self.api_key)

