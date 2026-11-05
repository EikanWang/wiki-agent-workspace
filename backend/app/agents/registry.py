"""Provider registry: builds adapters from configuration and reports capabilities."""

from __future__ import annotations

import asyncio
from typing import Any

from ..config import Settings
from .base import AgentAdapter, Capabilities, ProviderError
from .codex import CodexAdapter
from .opencode import OpenCodeCliAdapter, OpenCodeServerAdapter


class ProviderRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.adapters: dict[str, AgentAdapter] = {}
        oc = settings.provider("opencode")
        if oc.enabled:
            self.adapters["opencode"] = OpenCodeServerAdapter(oc) if oc.mode == "server" else OpenCodeCliAdapter(oc)
        cx = settings.provider("codex")
        if cx.enabled:
            self.adapters["codex"] = CodexAdapter(cx)
        self._caps: dict[str, Capabilities] = {}

    def get(self, name: str) -> AgentAdapter:
        a = self.adapters.get(name)
        if a is None:
            raise ProviderError("PROVIDER_UNAVAILABLE", f"unknown or disabled provider '{name}'")
        return a

    async def capabilities(self, name: str) -> Capabilities:
        if name not in self._caps:
            self._caps[name] = await self.get(name).capabilities()
        return self._caps[name]

    async def refresh(self) -> dict[str, Capabilities]:
        results = await asyncio.gather(*(a.capabilities() for a in self.adapters.values()), return_exceptions=True)
        out: dict[str, Capabilities] = {}
        for name, res in zip(self.adapters.keys(), results):
            if isinstance(res, Exception):
                out[name] = Capabilities(provider=name, available=False, error=str(res))
            else:
                out[name] = res
        self._caps = out
        return out

    async def summary(self) -> dict[str, Any]:
        if not self._caps:
            await self.refresh()
        return {name: c.as_dict() for name, c in self._caps.items()}

    def default_provider(self) -> str | None:
        d = self.settings.default_provider
        if d and d in self.adapters:
            return d
        return None
