"""Supabase integration scaffold."""

from app.config import settings


class SupabaseClient:
    """Adapter for Supabase persistence and retrieval.

    TODO:
    - Install and configure the Supabase client library.
    - Require SUPABASE_URL and the appropriate Supabase key for the feature.
    - Define tables for tasks, runs, and memory records.
    """

    def __init__(self) -> None:
        self.url = settings.supabase_url
        self.anon_key = settings.supabase_anon_key
        self.service_role_key = settings.supabase_service_role_key

    def is_configured(self) -> bool:
        return bool(self.url and self.anon_key)
