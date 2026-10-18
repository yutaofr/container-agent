"""Model gateway and routing (design section 7).

Code names tiers, never models. A YAML file maps each tier to a LiteLLM model string, so
moving from local Ollama to Bedrock changes configuration, not code. Every answer must parse
into a Pydantic schema; an invalid answer is retried once on the same tier, then the call
moves up a tier, and only the top tier failing raises.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

M = TypeVar("M", bound=BaseModel)


class TierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str  # LiteLLM model string, e.g. ollama_chat/qwen3.5:latest or bedrock/…
    capabilities: list[str] = ["json"]
    timeout_s: float = 300


class ModelConfig(BaseModel):
    """tiers: name → model, in escalation order (first is cheapest)."""

    model_config = ConfigDict(extra="forbid")

    tiers: dict[str, TierConfig]

    @property
    def order(self) -> list[str]:
        return list(self.tiers)


def load_model_config(path: Path) -> ModelConfig:
    return ModelConfig.model_validate(yaml.safe_load(path.read_text()))


class SchemaError(Exception):
    """The model's answer did not match the schema."""


class ModelUnavailable(Exception):
    """Every eligible tier failed to give a valid answer."""


class Completer(Protocol):
    """One structured completion on one model; LiteLLMCompleter, or a fake in tests."""

    async def complete(
        self, model: str, system: str, user: str, schema: type[M], timeout_s: float
    ) -> M: ...


class LiteLLMCompleter:
    async def complete(
        self, model: str, system: str, user: str, schema: type[M], timeout_s: float
    ) -> M:
        import litellm  # imported lazily: heavy, and tests never need it

        litellm.suppress_debug_info = True
        response = await litellm.acompletion(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format=schema,
            temperature=0,
            timeout=timeout_s,
        )
        try:
            return schema.model_validate_json(response.choices[0].message.content or "")
        except ValidationError as e:
            raise SchemaError(str(e)) from e


class Router:
    def __init__(self, config: ModelConfig, completer: Completer):
        self.config, self.completer = config, completer

    def tier_at(self, index: int) -> str:
        """The tier at a position in the escalation order, clamped to the ends."""
        order = self.config.order
        return order[max(0, min(index, len(order) - 1))]

    async def ask(
        self,
        system: str,
        user: str,
        schema: type[M],
        start: str,
        needs: Sequence[str] = ("json",),
    ) -> tuple[M, str]:
        """Return the first valid answer and the tier that gave it, starting at `start`."""
        order = self.config.order
        errors: list[str] = []
        for tier in order[order.index(start) :]:
            cfg = self.config.tiers[tier]
            if not set(needs) <= set(cfg.capabilities):
                errors.append(f"{tier}: lacks {sorted(set(needs) - set(cfg.capabilities))}")
                continue
            for _ in range(2):  # one retry on the same tier before moving up
                try:
                    return await self.completer.complete(
                        cfg.model, system, user, schema, cfg.timeout_s
                    ), tier
                except SchemaError as e:
                    errors.append(f"{tier}: invalid answer: {str(e)[:200]}")
                except Exception as e:  # network, timeout, model not found: try the next tier
                    errors.append(f"{tier}: {type(e).__name__}: {str(e)[:200]}")
                    break
        raise ModelUnavailable("; ".join(errors))
