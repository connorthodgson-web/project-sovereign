"""Provider-aware model client with structured routing and controlled escalation."""

from __future__ import annotations

import json
from time import perf_counter
from typing import Any

import httpx

from app.config import settings
from core.logging import get_logger
from core.model_routing import (
    ModelProvider,
    ModelRequestContext,
    ModelRouter,
    ModelTier,
)
from core.request_trace import current_request_trace


class OpenRouterClient:
    """Thin adapter for routed model access with dynamic tier/provider selection."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: float = 20.0,
        model_router: ModelRouter | None = None,
    ) -> None:
        self.api_key = api_key or settings.openrouter_api_key
        self.model = model or settings.openrouter_model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.model_router = model_router or ModelRouter()
        self.logger = get_logger(__name__)

    def is_configured(self) -> bool:
        return self._openrouter_ready() or self._openai_ready()

    def require_configured(self) -> None:
        if not self.is_configured():
            raise RuntimeError(
                "No model provider is configured. Add OPENROUTER_API_KEY for Tier 1 and Tier 2, "
                "or enable OpenAI Tier 3 direct separately. Right now OpenAI Tier 3 direct is disabled or not configured."
            )

    def describe_model_strategy(self) -> str:
        if self.model_router.enabled():
            return self.model_router.describe_strategy()
        return f"OpenRouter via {self.model}"

    def prompt(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        label: str | None = None,
        context: ModelRequestContext | None = None,
    ) -> str:
        """Send a prompt, validate the result when possible, and escalate only when needed."""
        self.require_configured()
        preferred_tier: str | None = None
        last_error: Exception | None = None

        while True:
            selection = self.model_router.select(
                label=label,
                prompt=prompt,
                system_prompt=system_prompt,
                preferred_tier=preferred_tier,
                context=context,
            )
            trace = current_request_trace()
            if trace is not None:
                trace.record_openrouter(label)
                trace.record_model_selection(
                    f"{label or 'unlabeled'}:{selection.provider}:{selection.tier}:{selection.model}"
                )
                trace.set_metadata("model_label", self.describe_model_strategy())
                trace.set_metadata("model_provider", selection.provider)

            self.logger.info(
                "MODEL_CONTEXT label=%s context=%s",
                label or "unlabeled",
                json.dumps(selection.context.as_log_dict(), sort_keys=True),
            )
            self.logger.info(
                "MODEL_SELECTED label=%s model=%s",
                label or "unlabeled",
                selection.model,
            )
            self.logger.info("MODEL_PROVIDER=%s", selection.provider)
            self.logger.info("MODEL_TIER=%s", selection.tier)
            self.logger.info("MODEL_ESCALATION_ALLOWED=%s", selection.escalation_allowed)
            self.logger.info("TOKEN_ESTIMATE=%s", selection.token_estimate)
            started_at = perf_counter()
            try:
                response_text = self._send_request(
                    provider=selection.provider,
                    model=selection.model,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    context=selection.context,
                )
            except (httpx.HTTPError, RuntimeError) as exc:
                last_error = exc
                escalation_reason = "request_failed"
                next_tier = self.model_router.next_tier(
                    selection.tier,
                    context=selection.context,
                    reason=escalation_reason,
                )
                if next_tier is None:
                    raise
                self._log_escalation(
                    label=label,
                    from_tier=selection.tier,
                    to_tier=next_tier,
                    reason=escalation_reason,
                )
                preferred_tier = next_tier
                continue

            latency_ms = int((perf_counter() - started_at) * 1000)
            self.logger.info("MODEL_LATENCY_MS=%s", latency_ms)
            validation_reason = self.model_router.validation_reason(
                label=label,
                response_text=response_text,
                prompt=prompt,
                context=selection.context,
            )
            if validation_reason is None:
                return response_text

            next_tier = self.model_router.next_tier(
                selection.tier,
                context=selection.context,
                reason=validation_reason,
            )
            if next_tier is None:
                return response_text

            self._log_escalation(
                label=label,
                from_tier=selection.tier,
                to_tier=next_tier,
                reason=validation_reason,
            )
            preferred_tier = next_tier
            last_error = None
            if preferred_tier == selection.tier:
                break

        if last_error is not None:
            raise last_error
        raise RuntimeError("Model routing could not produce a stable response.")

    def _send_request(
        self,
        *,
        provider: str,
        model: str,
        prompt: str,
        system_prompt: str | None,
        context: ModelRequestContext,
    ) -> str:
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        if provider == ModelProvider.OPENAI:
            return self._send_openai_request(model=model, messages=messages, context=context)
        return self._send_openrouter_request(model=model, messages=messages)

    def _send_openrouter_request(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
    ) -> str:
        if not self._openrouter_ready():
            raise RuntimeError(
                "OpenRouter is not configured. Add OPENROUTER_API_KEY to enable Tier 1 and Tier 2 routing."
            )

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://project-sovereign.local",
                    "X-Title": "Project Sovereign",
                },
                json={
                    "model": model,
                    "messages": messages,
                },
            )
            response.raise_for_status()

        payload: dict[str, Any] = response.json()
        return self._extract_content(payload, provider="OpenRouter")

    def _send_openai_request(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        context: ModelRequestContext,
    ) -> str:
        if not self._openai_ready():
            if context.fallback_allowed and self._openrouter_ready():
                return self._send_openrouter_request(
                    model=self.model_router.tier_models[ModelTier.TIER_3],
                    messages=messages,
                )
            raise RuntimeError(
                "OpenAI Tier 3 direct is disabled or not fully configured. "
                "Set OPENAI_ENABLED=true, OPENAI_API_KEY, and OPENAI_MODEL_TIER_3 to enable it."
            )

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                },
            )
            response.raise_for_status()

        payload: dict[str, Any] = response.json()
        return self._extract_content(payload, provider="OpenAI")

    def _extract_content(self, payload: dict[str, Any], *, provider: str) -> str:
        try:
            return payload["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError) as exc:
            raise RuntimeError(f"{provider} returned an unexpected response shape.") from exc

    def _log_escalation(
        self,
        *,
        label: str | None,
        from_tier: str,
        to_tier: str,
        reason: str,
    ) -> None:
        trace = current_request_trace()
        if trace is not None:
            trace.record_escalation(f"{label or 'unlabeled'}:{from_tier}->{to_tier}:{reason}")
        self.logger.info("ESCALATION_TRIGGERED=true")
        self.logger.info("ESCALATION_REASON=%s", reason)
        self.logger.info(
            "MODEL_ESCALATION label=%s from_tier=%s to_tier=%s",
            label or "unlabeled",
            from_tier,
            to_tier,
        )

    def _openrouter_ready(self) -> bool:
        return bool(self.api_key)

    def _openai_ready(self) -> bool:
        return bool(
            settings.openai_enabled
            and settings.openai_api_key
            and self.model_router.openai_tier_3_model
        )
