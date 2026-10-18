import pytest
from pydantic import BaseModel

from cagent.llm import (
    ModelConfig,
    ModelUnavailable,
    Router,
    SchemaError,
    TierConfig,
    load_model_config,
)

from .conftest import REPO
from .fakellm import CONFIG, FakeCompleter


class Answer(BaseModel):
    text: str


async def test_first_valid_answer_wins():
    fake = FakeCompleter({"fake/small": [{"text": "hi"}]})
    answer, tier = await Router(CONFIG, fake).ask("sys", "user", Answer, start="small")
    assert (answer.text, tier) == ("hi", "small")


async def test_invalid_answer_is_retried_once_then_moves_up():
    fake = FakeCompleter(
        {
            "fake/small": [SchemaError("bad"), SchemaError("bad again")],
            "fake/large": [{"text": "ok"}],
        }
    )
    answer, tier = await Router(CONFIG, fake).ask("sys", "user", Answer, start="small")
    assert tier == "large" and fake.models == ["fake/small", "fake/small", "fake/large"]


async def test_transport_errors_move_up_without_retry():
    fake = FakeCompleter({"fake/small": [TimeoutError()], "fake/large": [{"text": "ok"}]})
    _, tier = await Router(CONFIG, fake).ask("sys", "user", Answer, start="small")
    assert tier == "large" and fake.models == ["fake/small", "fake/large"]


async def test_tiers_without_the_capability_are_skipped():
    config = ModelConfig(
        tiers={
            "small": TierConfig(model="fake/small", capabilities=[]),
            "large": TierConfig(model="fake/large"),
        }
    )
    fake = FakeCompleter({"fake/large": [{"text": "ok"}]})
    _, tier = await Router(config, fake).ask("sys", "user", Answer, start="small")
    assert tier == "large" and fake.models == ["fake/large"]


async def test_top_tier_failing_raises():
    fake = FakeCompleter({"fake/large": [SchemaError("x"), SchemaError("y")]})
    with pytest.raises(ModelUnavailable, match="large: invalid answer"):
        await Router(CONFIG, fake).ask("sys", "user", Answer, start="large")


def test_tier_at_clamps_to_the_ends():
    router = Router(CONFIG, FakeCompleter({}))
    assert [router.tier_at(i) for i in (-1, 0, 1, 5)] == ["small", "small", "large", "large"]


def test_shipped_model_config_loads():
    config = load_model_config(REPO / "platform" / "models.yaml")
    assert config.order == ["small", "large"]
    assert all(t.model.startswith("ollama_chat/") for t in config.tiers.values())
