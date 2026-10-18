"""A scripted stand-in for LiteLLM: each model answers from its own queue."""

from typing import Any

from pydantic import ValidationError

from cagent.llm import ModelConfig, SchemaError, TierConfig

CONFIG = ModelConfig(
    tiers={"small": TierConfig(model="fake/small"), "large": TierConfig(model="fake/large")}
)


class FakeCompleter:
    def __init__(self, script: dict[str, list[Any]]):
        """script maps a model to its answers in order: a dict (validated against the
        requested schema) or an exception instance to raise."""
        self.script = {model: list(answers) for model, answers in script.items()}
        self.calls: list[tuple[str, str]] = []  # (model, user prompt)

    async def complete(self, model, system, user, schema, timeout_s):
        self.calls.append((model, user))
        answer = self.script[model].pop(0)
        if isinstance(answer, Exception):
            raise answer
        try:
            return schema.model_validate(answer)
        except ValidationError as e:  # as LiteLLMCompleter reports a bad answer
            raise SchemaError(str(e)) from e

    @property
    def models(self) -> list[str]:
        return [m for m, _ in self.calls]
