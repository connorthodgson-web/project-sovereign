"""Speech-to-text integration scaffold."""

from app.config import settings


class SpeechToTextClient:
    """Adapter for a future speech-to-text provider.

    TODO:
    - Select the STT provider and SDK.
    - Require VOICE_API_KEY or provider-specific credentials.
    - Define streaming vs batch transcription interfaces.
    """

    def __init__(self) -> None:
        self.api_key = settings.voice_api_key

    def is_configured(self) -> bool:
        return bool(self.api_key)

