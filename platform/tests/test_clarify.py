import pytest

from cagent.clarify import AnswerError, apply_answer, extract, question
from cagent.gaps import Gap
from cagent.generate import GenerationError, generate
from cagent.llm import ModelUnavailable, Router

from .conftest import spec_with
from .fakellm import CONFIG, FakeCompleter

ORDERS_RELEASE = {"name": "order-service", "source": {"kind": "release", "ref": "1.4.2"}}


def gaps_of(spec, catalog) -> list[Gap]:
    with pytest.raises(GenerationError) as err:
        generate(spec, catalog)
    return err.value.gaps


def answer_all(spec, catalog, answers=None):
    """Close gaps one at a time, taking the default unless answers names a value."""
    answers = answers or {}
    for _ in range(20):
        try:
            generate(spec, catalog)
            return spec
        except GenerationError as e:
            gap = e.gaps[0]
            spec = apply_answer(spec, gap, answers.get((gap.kind, gap.target, gap.key)))
    raise AssertionError("did not converge")


def test_question_names_the_gap_and_what_to_choose():
    gap = Gap("unknown_template", "mysq", "mysq: unknown middleware template 'mysq'")
    assert question(gap) == (
        "mysq: unknown middleware template 'mysq'. Which template should mysq use?"
    )
    gap = Gap("missing_env", "web", "web: missing required env MODE", key="MODE")
    assert question(gap).endswith("What value should web get for MODE?")


def test_defaults_close_every_choice_gap(catalog):
    spec = spec_with(
        services=[
            {
                "name": "order-service",
                "source": {"kind": "release", "ref": "1.4.2"},
                "env": {"SPRING_PROFILES_ACTIVE": "dev"},
                "publish": {"grpc": 18080},
            }
        ],
        middleware=[{"template": "mysq", "version": "5.7"}, {"template": "redis", "preset": "x"}],
    )
    done = answer_all(spec, catalog)
    assert {m.template for m in done.middleware} == {"mysql", "redis", "kafka"}
    assert done.services[0].publish == {"http": 18080}
    generate(done, catalog)


def test_missing_env_needs_a_value(catalog):
    spec = spec_with(services=[ORDERS_RELEASE])
    gap = next(g for g in gaps_of(spec, catalog) if g.kind == "missing_env")
    with pytest.raises(AnswerError, match="an answer is required"):
        apply_answer(spec, gap, None)
    with pytest.raises(AnswerError, match="an answer is required"):
        apply_answer(spec, gap, "  ")
    after = apply_answer(spec, gap, "dev")
    assert after.services[0].env["SPRING_PROFILES_ACTIVE"] == "dev"
    assert spec.services[0].env == {}  # the input spec is unchanged


def test_choice_answers_must_be_an_option(catalog):
    spec = spec_with(middleware=[{"template": "mysql", "version": "5.7"}, {"template": "kafka"}])
    gap = next(g for g in gaps_of(spec, catalog) if g.kind == "unsupported_version")
    with pytest.raises(AnswerError, match="choose one of 8.0, 8.4"):
        apply_answer(spec, gap, "9.0")
    assert apply_answer(spec, gap, "8.0").middleware[0].version == "8.0"


@pytest.mark.parametrize(
    "middleware, bindings, kind, answer, check",
    [
        (
            [{"template": "mysql"}, {"template": "mysql", "name": "b"}, {"template": "kafka"}],
            {},
            "binding",
            "b",
            lambda s: s.bindings == {"order-service/mysql": "b"},
        ),
        (
            [{"template": "mysql", "version": "8.0"}, {"template": "kafka"}],
            {},
            "version_mismatch",
            "8.4",
            lambda s: s.middleware[0].version == "8.4",
        ),
        (
            [{"template": "mysql"}],
            {},
            "no_provider",
            "kafka",
            lambda s: [m.template for m in s.middleware] == ["mysql", "kafka"],
        ),
    ],
)
def test_matching_gaps(catalog, middleware, bindings, kind, answer, check):
    if kind == "version_mismatch":
        desc = catalog.services["order-service"]
        req = desc.requires[0].model_copy(update={"version": ">=8.4"})
        catalog.services["order-service"] = desc.model_copy(
            update={"requires": [req, desc.requires[1]]}
        )
    spec = spec_with(middleware=middleware, bindings=bindings)
    gap = next(g for g in gaps_of(spec, catalog) if g.kind == kind)
    assert check(apply_answer(spec, gap, answer))


def test_no_provider_names_a_second_instance_apart(catalog):
    spec = spec_with(middleware=[{"template": "kafka", "name": "mysql"}])
    gap = next(g for g in gaps_of(spec, catalog) if g.kind == "no_provider")
    after = apply_answer(spec, gap, "mysql")
    assert [m.instance for m in after.middleware] == ["mysql", "mysql-2"]


def test_no_source_switches_the_source_kind(catalog):
    desc = catalog.services["order-service"]
    catalog.services["order-service"] = desc.model_copy(
        update={"sources": desc.sources.model_copy(update={"ci": None})}
    )
    spec = spec_with(services=[{"name": "order-service", "source": {"kind": "ci", "ref": "1.4.2"}}])
    gap = next(g for g in gaps_of(spec, catalog) if g.kind == "no_source")
    assert apply_answer(spec, gap, "release").services[0].source.kind == "release"


@pytest.mark.parametrize("kind", ["unregistered", "bind_render"])
def test_unanswerable_gaps_are_refused(kind):
    with pytest.raises(AnswerError, match="cannot be answered"):
        apply_answer(spec_with(), Gap(kind, "x", "x"), "y")


def test_choice_without_options_cannot_be_answered():
    gap = Gap("binding", "order-service/mysql", "no candidates")
    with pytest.raises(AnswerError, match="cannot be answered"):
        apply_answer(spec_with(), gap, "mysql")


PURPOSE = "Run order-service 1.4.2 against MySQL 8.0 and Kafka; check that orders are stored."


def extraction(**overrides):
    return {
        "name": "Orders POC",
        "services": [{"name": "order-service", "source": {"kind": "release", "ref": "1.4.2"}}],
        "middleware": [{"template": "mysql", "version": "8.0"}, {"template": "kafka"}],
        "checks": ["orders are stored"],
        **overrides,
    }


async def test_extract_turns_the_answer_into_a_spec(catalog):
    fake = FakeCompleter({"fake/large": [extraction()]})
    spec = await extract(Router(CONFIG, fake), PURPOSE, catalog)
    assert spec.name == "orders-poc" and spec.purpose == PURPOSE
    assert [(s.name, s.source.kind, s.source.ref) for s in spec.services] == [
        ("order-service", "release", "1.4.2")
    ]
    assert [(m.template, m.version) for m in spec.middleware] == [("mysql", "8.0"), ("kafka", None)]
    assert spec.checks == ["orders are stored"]
    assert fake.models == ["fake/large"]  # extraction starts on the top tier
    assert fake.thinks == [False]


async def test_extract_prompt_shows_the_catalog_and_the_purpose(catalog):
    fake = FakeCompleter({"fake/large": [extraction()]})
    await extract(Router(CONFIG, fake), PURPOSE, catalog)
    prompt = fake.calls[0][1]
    assert "- order-service: sources release, snapshot, ci, workdir" in prompt
    assert "- mysql: provides mysql, versions 8.0, 8.4" in prompt
    assert prompt.endswith(PURPOSE)


async def test_extract_keeps_unknown_names_so_they_become_questions(catalog):
    answer = extraction(
        services=[
            {"name": "Billing Service", "source": {"kind": "workdir", "ref": "/src/billing"}},
            {"name": "billing-service", "source": {"kind": "workdir", "ref": "/src/billing"}},
        ],
        middleware=[{"template": "Postgres"}, {"template": "postgres"}, {"template": "mysql"}],
    )
    fake = FakeCompleter({"fake/large": [answer]})
    spec = await extract(Router(CONFIG, fake), PURPOSE, catalog)
    assert [s.name for s in spec.services] == ["billing-service"]
    assert [m.template for m in spec.middleware] == ["postgres", "mysql"]
    gaps = {g.kind for g in gaps_of(spec, catalog)}
    assert {"unregistered", "unknown_template"} <= gaps


async def test_extract_moves_middleware_listed_as_a_service(catalog):
    # Seen from qwen3.5 without thinking: MySQL listed as a service with a release source.
    answer = extraction(
        services=[ORDERS_RELEASE, {"name": "mysql", "source": {"kind": "release", "ref": "8.0"}}],
        middleware=[{"template": "mysql", "version": "8.0"}, {"template": "order-service"}],
    )
    spec = await extract(Router(CONFIG, FakeCompleter({"fake/large": [answer]})), "x", catalog)
    assert [s.name for s in spec.services] == ["order-service"]
    assert [(m.template, m.version) for m in spec.middleware] == [("mysql", "8.0")]


async def test_extract_gives_up_when_every_tier_fails(catalog):
    bad = {"name": "x", "services": "not a list"}
    fake = FakeCompleter({"fake/large": [bad, bad]})
    with pytest.raises(ModelUnavailable):
        await extract(Router(CONFIG, fake), PURPOSE, catalog)


async def test_extract_retries_an_empty_ref(catalog):
    empty = extraction(
        services=[{"name": "order-service", "source": {"kind": "release", "ref": ""}}]
    )
    fake = FakeCompleter({"fake/large": [empty, extraction()]})
    spec = await extract(Router(CONFIG, fake), PURPOSE, catalog)
    assert spec.services[0].source.ref == "1.4.2" and len(fake.calls) == 2
