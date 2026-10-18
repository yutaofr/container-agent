"""Generate a compose document from a ClusterSpec and the catalog."""

import difflib
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import yaml

from cagent.catalog import Catalog, Healthcheck, MiddlewareTemplate, TemplateInit
from cagent.gaps import Gap
from cagent.matching import Binding, match
from cagent.render import RenderError, escape_compose, render
from cagent.spec import ClusterSpec, Source

# Shared healthcheck timing: up to ~2.5 minutes after a 10 s grace period.
HEALTH_TIMING = {"interval": "5s", "timeout": "3s", "retries": 30, "start_period": "10s"}

# Where an init service sees each consumer's staged scripts: <INIT_ROOT>/<consumer>/NN-file.
INIT_ROOT = "/cagent-init"
INIT_LABELS = {"cagent.role": "init"}  # the Runner's verify and signals read this label


class GenerationError(Exception):
    """The spec cannot be turned into a compose file. gaps lists every reason."""

    def __init__(self, gaps: list[Gap]):
        super().__init__("\n".join(str(g) for g in gaps))
        self.gaps = gaps

    @property
    def problems(self) -> list[str]:
        return [str(g) for g in self.gaps]


def image_tag(name: str, source: Source) -> str:
    """Tag the Runner builds for a service source (M4). Docker tags allow [A-Za-z0-9_.-]."""
    if source.kind == "workdir":
        ref = hashlib.sha256(source.ref.encode()).hexdigest()[:12]
    else:
        ref = re.sub(r"[^A-Za-z0-9_.-]", "-", source.ref)
    return f"cagent/{name}:{f'{source.kind}-{ref}'[:128]}"


def _healthcheck(hc: Healthcheck, ports: dict[str, int], values: dict[str, str]) -> dict[str, Any]:
    if hc.cmd is not None:
        test = [escape_compose(render(part, values)) for part in hc.cmd]
    else:
        port = ports[hc.port] if hc.port else next(iter(ports.values()))
        url = f"http://localhost:{port}{hc.http}"
        probe = f"test \"$(curl -s -o /dev/null -w '%{{http_code}}' {url})\" = {hc.expect}"
        test = ["CMD-SHELL", escape_compose(probe)]
    return {"test": test, **HEALTH_TIMING}


@dataclass
class _InitWork:
    """What one middleware instance's init service creates."""

    keyspaces: list[str] = field(default_factory=list)
    topics: dict[str, int] = field(default_factory=dict)  # name -> partitions
    scripts: list[tuple[str, str]] = field(default_factory=list)  # (consumer, capability)
    consumers: set[str] = field(default_factory=set)  # services that wait for it


def _plan_init(
    bindings: list[Binding], templates: dict[str, MiddlewareTemplate], gaps: list[Gap]
) -> dict[str, _InitWork]:
    """Init work per provider instance, consumers in name order. A requirement asking for
    something its template cannot do is a gap."""
    work: dict[str, _InitWork] = {}
    for b in sorted(bindings, key=lambda b: (b.consumer, b.requirement.capability)):
        init = b.requirement.init
        if init is None or not (init.scripts or init.keyspaces or init.topics):
            continue
        tpl = templates[b.provider]
        can = tpl.init or TemplateInit()
        problems = []
        if init.keyspaces and can.keyspaces is None:
            problems.append(f"{tpl.name} cannot create keyspaces")
        if init.topics and can.topics is None:
            problems.append(f"{tpl.name} cannot create topics")
        if init.scripts and can.scripts is None:
            problems.append(f"{tpl.name} cannot run scripts")
        elif can.scripts is not None:
            suffix = can.scripts.suffix
            problems += [
                f"{s}: {tpl.name} runs {suffix} scripts"
                for s in init.scripts
                if not s.endswith(suffix)
            ]
        target = f"{b.consumer}/{b.requirement.capability}"
        if problems:
            gaps += [Gap("unsupported_init", target, f"{target}: {p}") for p in problems]
            continue
        w = work.setdefault(b.provider, _InitWork())
        w.consumers.add(b.consumer)
        w.keyspaces += [k for k in init.keyspaces if k not in w.keyspaces]
        for topic in init.topics:
            w.topics[topic.name] = max(w.topics.get(topic.name, 0), topic.partitions)
        if init.scripts:
            w.scripts.append((b.consumer, b.requirement.capability))
    return work


def _guarded(command: str, what: str) -> str:
    """Run command; on failure, name what failed and stop. set -e alone misses a failure on
    the left of && inside a template's command."""
    return f'{{ {command}; }} || {{ echo "cagent init failed: {what}" >&2; exit 1; }}'


def _init_script(tpl: MiddlewareTemplate, values: dict[str, str], w: _InitWork) -> str:
    """The init service's shell program: keyspaces and topics, which are idempotent, then
    each staged script not yet marked, consumers in name order, files in staged order."""
    init = tpl.init or TemplateInit()
    lines = ["set -eu"]
    if init.keyspaces:
        lines += [
            _guarded(render(init.keyspaces.run, {**values, "keyspace": k}), f"keyspace {k}")
            for k in w.keyspaces
        ]
    if init.topics:
        lines += [
            _guarded(
                render(init.topics.run, {**values, "topic": name, "partitions": str(n)}),
                f"topic {name}",
            )
            for name, n in sorted(w.topics.items())
        ]
    if init.scripts and w.scripts:
        s = init.scripts
        dirs = " ".join(f"{INIT_ROOT}/{consumer}/*" for consumer, _ in w.scripts)
        lines += [
            f"for file in {dirs}; do",
            '  [ -f "$file" ] || continue',
            '  sha=$(sha256sum "$file" | cut -d " " -f 1)',
            f'  if {render(s.marked, values)}; then echo "skip $file"; continue; fi',
            '  echo "run $file"',
            f"  {_guarded(render(s.run, values), '$file')}",
            f"  {_guarded(render(s.mark, values), 'marking $file')}",
            "done",
        ]
    return "\n".join(lines)


def generate(spec: ClusterSpec, catalog: Catalog) -> dict[str, Any]:
    """Return the compose document as a dict. Raises GenerationError listing every problem."""
    gaps: list[Gap] = []
    services: dict[str, dict[str, Any]] = {}

    for ref in spec.middleware:
        tpl = catalog.middleware.get(ref.template)
        if tpl is None:
            names = sorted(catalog.middleware)
            closest = difflib.get_close_matches(ref.template, names, n=1, cutoff=0)
            gaps.append(
                Gap(
                    "unknown_template",
                    ref.instance,
                    f"{ref.instance}: unknown middleware template {ref.template!r}",
                    tuple(names),
                    closest[0] if closest else None,
                )
            )
            continue
        version = ref.version or tpl.default_version
        if version not in tpl.versions:
            gaps.append(
                Gap(
                    "unsupported_version",
                    ref.instance,
                    f"{ref.instance}: {tpl.name} {version} unsupported; "
                    f"use {', '.join(tpl.versions)}",
                    tuple(tpl.versions),
                    tpl.default_version,
                )
            )
            continue
        preset = tpl.presets.get(ref.preset or tpl.default_preset)
        if preset is None:
            gaps.append(
                Gap(
                    "unknown_preset",
                    ref.instance,
                    f"{ref.instance}: unknown preset {ref.preset!r}; use {', '.join(tpl.presets)}",
                    tuple(tpl.presets),
                    tpl.default_preset,
                )
            )
            continue
        values = {"host": ref.instance, "port": str(tpl.port), **tpl.connection}
        svc: dict[str, Any] = {"image": tpl.image.replace("{version}", version)}
        if tpl.command:
            svc["command"] = [escape_compose(c) for c in tpl.command]
        if tpl.env:
            svc["environment"] = {
                k: escape_compose(render(v, values)) for k, v in sorted(tpl.env.items())
            }
        if ref.publish:
            svc["ports"] = [f"{ref.publish}:{tpl.port}"]
        svc["healthcheck"] = _healthcheck(tpl.healthcheck, {"default": tpl.port}, values)
        svc["mem_limit"] = preset.mem_limit
        svc["cpus"] = preset.cpus
        services[ref.instance] = svc

    bindings, match_gaps = match(spec, catalog)
    gaps += match_gaps
    by_consumer: dict[str, list[Binding]] = defaultdict(list)
    for b in bindings:
        by_consumer[b.consumer].append(b)
    instances = {m.instance: m for m in spec.middleware}
    templates = {i: catalog.middleware[m.template] for i, m in instances.items() if i in services}
    init_work = _plan_init([b for b in bindings if b.provider in templates], templates, gaps)
    initialized = {(c, provider) for provider, w in init_work.items() for c in w.consumers}

    for ref in spec.services:
        desc = catalog.services.get(ref.name)
        if desc is None:
            gaps.append(Gap("unregistered", ref.name, f"{ref.name}: not registered in the catalog"))
            continue
        if getattr(desc.sources, ref.source.kind) is None:
            declared = tuple(
                k for k in ("release", "snapshot", "ci", "workdir") if getattr(desc.sources, k)
            )
            gaps.append(
                Gap(
                    "no_source",
                    ref.name,
                    f"{ref.name}: its descriptor declares no {ref.source.kind} source",
                    declared,
                    declared[0] if declared else None,
                )
            )
        ports = {p.name: p.container for p in desc.runtime.ports}
        env = dict(desc.runtime.env.defaults)
        depends: dict[str, dict[str, str]] = {}
        for b in by_consumer[ref.name]:
            tpl = catalog.middleware[instances[b.provider].template]
            values = {"host": b.provider, "port": str(tpl.port), **tpl.connection}
            for var, template in b.requirement.bind.items():
                try:
                    env[var] = render(template, values)
                except RenderError as e:
                    key = f"{ref.name}/{b.requirement.capability}"
                    gaps.append(Gap("bind_render", key, f"{key}: {var}: {e}", key=var))
            depends[b.provider] = {"condition": "service_healthy"}
            if (ref.name, b.provider) in initialized:
                depends[f"{b.provider}-init"] = {"condition": "service_completed_successfully"}
        env.update(ref.env)
        for var in desc.runtime.env.required:
            if var not in env:
                gaps.append(
                    Gap(
                        "missing_env",
                        ref.name,
                        f"{ref.name}: missing required env {var}",
                        key=var,
                    )
                )
        for name in sorted(set(ref.publish) - set(ports)):
            gaps.append(
                Gap(
                    "unknown_port",
                    ref.name,
                    f"{ref.name}: cannot publish unknown port {name}; "
                    f"ports are {', '.join(ports) or 'none'}",
                    tuple(ports),
                    next(iter(ports)) if len(ports) == 1 else None,
                    name,
                )
            )

        svc = {"image": image_tag(ref.name, ref.source)}
        if env:
            svc["environment"] = {k: escape_compose(v) for k, v in sorted(env.items())}
        published = [f"{host}:{ports[n]}" for n, host in sorted(ref.publish.items()) if n in ports]
        if published:
            svc["ports"] = published
        if depends:
            svc["depends_on"] = dict(sorted(depends.items()))
        if desc.runtime.healthcheck is not None:
            svc["healthcheck"] = _healthcheck(desc.runtime.healthcheck, ports, {})
        services[ref.name] = svc

    taken = {s.name for s in spec.services} | set(instances)
    for provider, w in sorted(init_work.items()):
        name = f"{provider}-init"
        if name in taken:
            gaps.append(
                Gap(
                    "unsupported_init",
                    name,
                    f"{name}: name taken by the init service of {provider}",
                )
            )
            continue
        tpl = templates[provider]
        values = {"host": provider, "port": str(tpl.port), **tpl.connection}
        init_svc: dict[str, Any] = {
            "image": services[provider]["image"],
            "entrypoint": ["sh", "-c", escape_compose(_init_script(tpl, values, w))],
            "depends_on": {provider: {"condition": "service_healthy"}},
            "restart": "no",
            "mem_limit": (tpl.init or TemplateInit()).mem_limit,
            "labels": dict(INIT_LABELS),
        }
        if w.scripts:
            init_svc["volumes"] = [
                f"./init/{consumer}/{capability}:{INIT_ROOT}/{consumer}:ro"
                for consumer, capability in w.scripts
            ]
        services[name] = init_svc

    if gaps:
        raise GenerationError(gaps)
    return {"name": spec.name, "services": dict(sorted(services.items()))}


def to_yaml(compose: dict[str, Any]) -> str:
    return yaml.safe_dump(compose, sort_keys=False, default_flow_style=False, allow_unicode=True)
