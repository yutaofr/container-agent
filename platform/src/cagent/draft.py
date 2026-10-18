"""Draft a descriptor for a project with none (M7b design 5.4).

The Runner's read-only inspect returns the project's build files. A deterministic skeleton
decides what is built and how: the module, name, stack, build command, and output. A model
fills in only how the service runs (ports, environment, healthcheck, requirements, probes),
and its answer is checked before the user sees it. The user saves the draft into the project;
read_back then validates the file actually on disk, which is what the Runner builds from.
"""

import difflib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from cagent.catalog import (
    TOPIC_PATTERN,
    Catalog,
    Env,
    Healthcheck,
    HttpProbe,
    Port,
    Probe,
    Requirement,
    RequirementInit,
    Runtime,
    ServiceDescriptor,
    Topic,
)
from cagent.llm import ModelUnavailable, Router
from cagent.render import placeholders

_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(:[^}]*)?\}")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_LOCAL = re.compile(r"\b(localhost|127\.0\.0\.1)\b")
_JAR_IN_DOCKERFILE = re.compile(r"target/|build/libs")
MASK = "******"  # what the Runner's redaction leaves in place of a secret


class DraftError(Exception):
    """No usable descriptor: say why, so the user can act."""


@dataclass(frozen=True)
class Skeleton:
    name: str
    module: str  # "." for the project root
    stack: str
    workdir: dict[str, str]


@dataclass(frozen=True)
class Draft:
    path: str  # where the user saves it, relative to the project root
    text: str
    descriptor: ServiceDescriptor
    note: str = ""  # what the user should know before saving: init scripts, topics, omissions


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-_") or "app"


def _pom(text: str) -> tuple[str | None, bool, bool]:
    """(artifactId, packaging is pom, Spring Boot plugin in build/plugins)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None, False, False
    for el in root.iter():
        el.tag = el.tag.rsplit("}", 1)[-1]  # drop the Maven namespace
    plugins = root.findall("./build/plugins/plugin/artifactId")
    return (
        root.findtext("artifactId"),
        root.findtext("packaging") == "pom",
        any(p.text == "spring-boot-maven-plugin" for p in plugins),
    )


def _skeleton(module: str, files: dict[str, str]) -> Skeleton | None:
    def f(name: str) -> str | None:
        return files.get(name if module == "." else f"{module}/{name}")

    def path(name: str) -> str:
        return name if module == "." else f"{module}/{name}"

    pom, gradle = f("pom.xml"), f("build.gradle.kts") or f("build.gradle")
    artifact, aggregator, maven_boot = _pom(pom) if pom else (None, False, False)
    gradle_boot = gradle is not None and "org.springframework.boot" in gradle
    if module == ".":
        name, maven_build = artifact or "app", "mvn -q package -DskipTests"
        gradle_task = "./gradlew -q bootJar"
    else:
        name = artifact or module.rsplit("/", 1)[-1]
        maven_build = f"mvn -q -pl {module} -am package -DskipTests"
        gradle_task = f"./gradlew -q :{module.replace('/', ':')}:bootJar"
    if pom and maven_boot and not aggregator:
        return Skeleton(
            _slug(name),
            module,
            "java-springboot",
            {"build": maven_build, "output": path("target/*.jar")},
        )
    if gradle_boot and not pom:
        return Skeleton(
            _slug(name),
            module,
            "java-springboot",
            {"build": gradle_task, "output": path("build/libs/*.jar")},
        )
    if (dockerfile := f("Dockerfile")) is not None:
        workdir = {"dockerfile": path("Dockerfile"), "context": module}
        if _JAR_IN_DOCKERFILE.search(dockerfile) and (pom or gradle):
            workdir = {"build": maven_build if pom else gradle_task, **workdir}
        return Skeleton(_slug(name), module, "dockerfile", workdir)
    return None


def skeletons(inspection: dict[str, Any]) -> list[Skeleton]:
    """One skeleton per deployable module: a Spring Boot build or its own Dockerfile. With
    modules, the root is an aggregator and is skipped."""
    modules = inspection["modules"] or ["."]
    found = [_skeleton(m, inspection["files"]) for m in modules]
    return [s for s in found if s is not None]


def pick(found: list[Skeleton], service: str) -> Skeleton:
    if not found:
        raise DraftError(
            f"{service}: no Spring Boot build or Dockerfile found; add a Dockerfile to the project"
        )
    if len(found) == 1:
        return found[0]
    names = [s.name for s in found]
    close = difflib.get_close_matches(service, names, n=1, cutoff=0.8)
    if not close:
        raise DraftError(f"no deployable module matches {service}: {', '.join(names)}")
    return found[names.index(close[0])]


def env_placeholders(text: str) -> tuple[list[str], list[str]]:
    """Environment variables a config references: (without a default, with a default)."""
    found = _PLACEHOLDER.findall(text)
    required = sorted({n for n, default in found if not default})
    return required, sorted({n for n, default in found if default} - set(required))


class _Port(BaseModel):
    name: str
    container: int


class _Requirement(BaseModel):
    capability: str
    version: str | None = None
    bind: dict[str, str] = {}


class _Topic(BaseModel):
    name: str
    partitions: int | None = None


class DraftAnswer(BaseModel):
    """What the model fills in; the skeleton supplies everything else. It is flatter than a
    descriptor: a JSON schema cannot say "exactly one of http or cmd", and a local model
    filled both."""

    ports: list[_Port] = []
    env_required: list[str] = []
    env_defaults: dict[str, str] = {}
    health_path: str | None = None  # an HTTP path that answers 200 when healthy
    health_cmd: list[str] | None = None  # a compose healthcheck test, when there is no path
    requires: list[_Requirement] = []
    probe_paths: list[str] = []  # HTTP paths that must answer 200 once the cluster is up

    def bound(self) -> set[str]:
        return {var for req in self.requires for var in req.bind}

    def without_bound(self) -> "DraftAnswer":
        """A bound variable needs no default or required entry: the bind sets it. Models
        copied the config's own default (often localhost) next to the bind."""
        bound = self.bound()
        return self.model_copy(
            update={
                "env_required": [v for v in self.env_required if v not in bound],
                "env_defaults": {k: v for k, v in self.env_defaults.items() if k not in bound},
            }
        )

    def runtime(self) -> Runtime:
        if self.health_path:
            health = Healthcheck(http=self.health_path)
        elif self.health_cmd:
            health = Healthcheck(cmd=self.health_cmd)
        else:
            health = None
        return Runtime(
            ports=[Port(name=p.name, container=p.container) for p in self.ports],
            env=Env(required=self.env_required, defaults=self.env_defaults),
            healthcheck=health,
        )


DRAFT_SYSTEM = (
    "You describe how one service runs in a docker compose test cluster. From its build file "
    "and configuration, give its container ports, its environment, an HTTP health path, the "
    "middleware capabilities it requires, and HTTP smoke probe paths.\n"
    "requires[].bind maps an environment variable the service reads to a template over the "
    "capability's values. For a service whose configuration reads ${DATABASE_URL}, "
    "${DATABASE_PASSWORD}, ${BROKERS} and ${CACHE_HOST}, requires is:\n"
    '[{"capability": "mysql", "bind": {"DATABASE_URL": "jdbc:mysql://{{host}}:{{port}}/{{db}}", '
    '"DATABASE_PASSWORD": "{{password}}"}}, {"capability": "kafka", "bind": {"BROKERS": '
    '"{{host}}:{{port}}"}}, {"capability": "redis", "bind": {"CACHE_HOST": "{{host}}"}}]\n'
    "Bind every placeholder that holds a connection setting (URL, host, port, user, password); "
    "list other placeholders without a default in env_required. Set version only when the "
    "build file pins one, as a specifier such as >=8.0. Never default a setting to localhost: "
    "inside a container that is the service itself. A Spring Boot service with actuator "
    "answers /actuator/health. Answer only with JSON matching the schema."
)


class TopicsAnswer(BaseModel):
    """Kafka topics, asked apart from the runtime: on qwen3.5, asking for both in one call
    made the model copy the worked example's variable names into its binds."""

    topics: list[_Topic] = []


TOPICS_SYSTEM = (
    "You list the Kafka topics one service reads or writes, from its configuration. Give each "
    "topic by its resolved name: for ${ORDERS_TOPIC:orders} the name is orders. List only "
    "topics; a broker address is not a topic. Answer only with JSON matching the schema."
)


def _capabilities(catalog: Catalog) -> dict[str, set[str]]:
    """capability -> the values every template providing it offers a bind template."""
    caps: dict[str, set[str]] = {}
    for t in catalog.middleware.values():
        values = {"host", "port", *t.connection}
        cap = t.provides.capability
        caps[cap] = caps[cap] & values if cap in caps else values
    return caps


def _module_files(sk: Skeleton, files: dict[str, str]) -> dict[str, str]:
    if sk.module == ".":
        return {p: c for p, c in files.items() if not p.startswith(".cagent/")}
    return {p: c for p, c in files.items() if p.startswith(f"{sk.module}/")}


def _flatten(doc: Any, prefix: str = "") -> dict[str, str]:
    if isinstance(doc, dict):
        out: dict[str, str] = {}
        for k, v in doc.items():
            out.update(_flatten(v, f"{prefix}{k}."))
        return out
    return {prefix.rstrip("."): str(doc)}


def topic_settings(texts: dict[str, str]) -> list[str]:
    """Configuration keys that name a topic, as "key: value", from YAML and properties."""
    found: dict[str, str] = {}
    for path, text in texts.items():
        if path.endswith((".yml", ".yaml")):
            try:
                found.update(_flatten(yaml.safe_load(text) or {}))
            except yaml.YAMLError:
                continue
        elif path.endswith(".properties"):
            for line in text.splitlines():
                key, sep, value = line.partition("=")
                if sep and not key.lstrip().startswith("#"):
                    found[key.strip()] = value.strip()
    return [f"{k}: {v}" for k, v in sorted(found.items()) if "topic" in k.lower()]


def draft_prompt(sk: Skeleton, files: dict[str, str], catalog: Catalog) -> str:
    texts = _module_files(sk, files)
    required, optional = env_placeholders("\n".join(texts.values()))
    caps = [
        f"- {cap}: "
        + ", ".join("{{" + v + "}}" for v in ["host", "port", *sorted(values - {"host", "port"})])
        for cap, values in sorted(_capabilities(catalog).items())
    ]
    return "\n".join(
        [
            f"Service `{sk.name}`, stack {sk.stack}, module `{sk.module}`.",
            "Files:",
            *(f"--- {p}\n{c}" for p, c in sorted(texts.items())),
            f"Environment placeholders without a default (set each one): "
            f"{', '.join(required) or 'none'}",
            f"Environment placeholders with a default (optional): {', '.join(optional) or 'none'}",
            "Capabilities you may require, and the values their bind templates may use:",
            *caps,
        ]
    )


def topics_prompt(texts: dict[str, str]) -> str:
    return "\n".join(
        [
            *(f"--- {p}\n{c}" for p, c in sorted(texts.items())),
            "Configuration values that name Kafka topics: " + "; ".join(topic_settings(texts)),
        ]
    )


def _bad_topics(topics: list[_Topic]) -> list[str]:
    return [
        f"topic {t.name!r} is not a valid Kafka topic name; give the resolved name, "
        "e.g. orders for ${ORDERS_TOPIC:orders}"
        for t in topics
        if not re.fullmatch(TOPIC_PATTERN, t.name)
    ]


async def draft_topics(router: Router, texts: dict[str, str]) -> tuple[list[Topic], list[str]]:
    """Topics from the configuration, by the top tier; an invalid name is asked again once,
    then left out with a note. A model failure leaves topics to the user, noted."""
    prompt, feedback = topics_prompt(texts), ""
    for _ in range(2):
        try:
            answer, _ = await router.ask(
                TOPICS_SYSTEM,
                prompt + feedback,
                TopicsAnswer,
                start=router.config.order[-1],
                think=False,
            )
        except ModelUnavailable as e:
            return [], [f"Topics were not drafted ({e}); add them under requires[].init.topics."]
        if not (bad := _bad_topics(answer.topics)):
            break
        feedback = f"\nYour previous answer was invalid: {'; '.join(bad)}"
    good = [t for t in answer.topics if re.fullmatch(TOPIC_PATTERN, t.name)]
    notes = [f"Left out {b}." for b in bad]
    return [Topic(name=t.name, partitions=t.partitions or 1) for t in good], notes


def _problems(answer: DraftAnswer, required: list[str], catalog: Catalog) -> list[str]:
    caps = _capabilities(catalog)
    problems = []
    for req in answer.requires:
        if req.capability not in caps:
            problems.append(f"no template provides {req.capability}; use {', '.join(sorted(caps))}")
            continue
        for var, template in req.bind.items():
            if not _ENV_NAME.fullmatch(var):
                problems.append(
                    "bind keys are environment variables the service reads, "
                    f"such as DB_URL; {var} is not"
                )
            elif unknown := placeholders(template) - caps[req.capability]:
                names = ", ".join("{{" + u + "}}" for u in sorted(unknown))
                problems.append(f"{req.capability} bind {var} uses unknown {names}")
    for var, value in answer.env_defaults.items():
        if _LOCAL.search(value):
            problems.append(
                f"{var}={value} points at the container itself; in a cluster, bind {var} "
                "to a capability instead"
            )
    covered = set(answer.env_required) | set(answer.env_defaults) | answer.bound()
    if missing := [v for v in required if v not in covered]:
        problems.append(
            f"placeholders {', '.join(missing)} are set nowhere: "
            "add each to env_required or to a requires bind"
        )
    return problems


_MIGRATIONS = ("db/migration/", "db/changelog/")
# Spring Boot runs classpath schema*.sql and data*.sql itself when this is set.
_SPRING_RUNS_SQL = re.compile(
    r"sql\.init\.mode\s*[=:]\s*always|sql:\s*\n\s+init:\s*\n\s+mode:\s*always"
)
_SPRING_SCRIPT = re.compile(r"src/main/resources/(schema|data)[^/]*\.sql$")


def _owned(resource: str, sk: Skeleton, modules: list[str]) -> bool:
    """A resource belongs to the module it sits in; one outside every module, to all."""
    if sk.module == "." or resource.startswith(f"{sk.module}/"):
        return True
    return not any(resource.startswith(f"{m}/") for m in modules)


def _init(
    sk: Skeleton,
    inspection: dict[str, Any],
    answer: DraftAnswer,
    catalog: Catalog,
    topics: list[Topic],
) -> tuple[dict[str, RequirementInit], list[str]]:
    """Init per required capability: scripts mapped by code, topics as drafted, and notes on
    everything found but left out."""
    required = {r.capability for r in answer.requires}
    runs: dict[str, str] = {}  # script suffix -> a required capability that runs it
    creates_topics = None
    for tpl in sorted(catalog.middleware.values(), key=lambda t: t.name):
        cap = tpl.provides.capability
        if tpl.init and cap in required:
            if tpl.init.scripts:
                runs.setdefault(tpl.init.scripts.suffix, cap)
            if tpl.init.topics and creates_topics is None:
                creates_topics = cap
    texts = "\n".join(_module_files(sk, inspection["files"]).values())
    spring_runs = bool(_SPRING_RUNS_SQL.search(texts))
    scripts: dict[str, list[str]] = {}
    notes, migrations = [], False
    for res in sorted(inspection["resources"]):
        if not _owned(res, sk, inspection["modules"]):
            continue
        if any(m in res for m in _MIGRATIONS):
            migrations = True
        elif spring_runs and _SPRING_SCRIPT.search(res):
            notes.append(f"Left out {res}: the application runs it itself (spring.sql.init.mode).")
        elif (cap := runs.get(_suffix(res))) is not None:
            scripts.setdefault(cap, []).append(res)
        else:
            notes.append(
                f"Left out {res}: the draft requires no capability that runs "
                f"{_suffix(res)} scripts."
            )
    out = {cap: RequirementInit(scripts=paths) for cap, paths in scripts.items()}
    if topics and creates_topics is not None:
        prev = out.get(creates_topics, RequirementInit())
        out[creates_topics] = prev.model_copy(update={"topics": topics})
    lead = []
    for cap, init in sorted(out.items()):
        if init.scripts:
            lead.append(
                f"Init scripts for {cap}, run once before the service starts: "
                f"{', '.join(init.scripts)}."
            )
        if init.topics:
            lead.append(f"Topics created in {cap}: {', '.join(t.name for t in init.topics)}.")
    if migrations:
        lead.append("Flyway or Liquibase migrations run when the application starts.")
    return out, lead + notes


def _creates_topics(answer: DraftAnswer, catalog: Catalog) -> bool:
    required = {r.capability for r in answer.requires}
    return any(
        t.init and t.init.topics and t.provides.capability in required
        for t in catalog.middleware.values()
    )


def _suffix(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name[name.rfind(".") :] if "." in name else ""


def _render(sk: Skeleton, service: str, descriptor: ServiceDescriptor) -> str:
    body = yaml.safe_dump(
        descriptor.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True
    )
    return (
        f"# Drafted by cagent for {service} from module {sk.module}. Review it, then save it\n"
        f"# as .cagent/services/{service}.yaml in the project root.\n{body}"
    )


async def draft(
    router: Router, service: str, inspection: dict[str, Any], catalog: Catalog
) -> Draft:
    """Draft service's descriptor on the top tier. An invalid answer is asked again once,
    with the reasons; a second invalid answer raises ModelUnavailable."""
    sk = pick(skeletons(inspection), service)
    files = _module_files(sk, inspection["files"])
    required, _ = env_placeholders("\n".join(files.values()))
    prompt = draft_prompt(sk, inspection["files"], catalog)
    errors: list[str] = []
    for _ in range(2):
        feedback = f"\nYour previous answer was invalid: {errors[-1]}" if errors else ""
        answer, _ = await router.ask(
            DRAFT_SYSTEM, prompt + feedback, DraftAnswer, start=router.config.order[-1], think=False
        )
        answer = answer.without_bound()
        problems = _problems(answer, required, catalog)
        if not problems:
            topics, topic_notes = [], []
            if _creates_topics(answer, catalog) and topic_settings(files):
                topics, topic_notes = await draft_topics(router, files)
            init, notes = _init(sk, inspection, answer, catalog, topics)
            notes += topic_notes
            try:
                descriptor = ServiceDescriptor(
                    name=service,
                    kind="app",
                    stack=sk.stack,
                    sources={"workdir": sk.workdir},
                    runtime=answer.runtime(),
                    requires=[
                        Requirement(**r.model_dump(), init=init.get(r.capability))
                        for r in answer.requires
                    ],
                    probes=[Probe(http=HttpProbe(path=p)) for p in answer.probe_paths],
                )
            except ValidationError as e:
                problems = [str(e)]
            else:
                path = f".cagent/services/{service}.yaml"
                text = _render(sk, service, descriptor)
                return Draft(path, text, descriptor, " ".join(notes))
        errors.append("; ".join(problems))
    raise ModelUnavailable(f"no valid draft for {service}: {' | '.join(errors)}")


def read_back(service: str, inspection: dict[str, Any]) -> ServiceDescriptor:
    """The descriptor the user saved, as the Runner will find it."""
    files = inspection["files"]
    path = f".cagent/services/{service}.yaml"
    if path not in files:
        path = ".cagent/service.yaml"
    if path not in files:
        raise DraftError(f"{service}: .cagent/services/{service}.yaml is not saved yet")
    try:
        doc = yaml.safe_load(files[path])
    except yaml.YAMLError as e:
        raise DraftError(f"{path} is not valid YAML: {e}") from e
    if isinstance(doc, dict) and doc.get("name") != service:
        raise DraftError(f"{path} names {doc.get('name')}, not {service}")
    if MASK in files[path]:
        raise DraftError(
            f"{path} holds a masked secret: a literal password or token cannot be read back; "
            "take it from a requires bind or remove it"
        )
    try:
        desc = ServiceDescriptor.model_validate(doc)
    except ValidationError as e:
        raise DraftError(f"{path}: {e}") from e
    if desc.sources.workdir is None:
        raise DraftError(f"{path} declares no workdir source")
    return desc
