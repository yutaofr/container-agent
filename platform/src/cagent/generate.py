"""Generate a compose document from a ClusterSpec and the catalog."""

import hashlib
import re
from collections import defaultdict
from typing import Any

import yaml

from cagent.catalog import Catalog, Healthcheck
from cagent.matching import Binding, match
from cagent.render import RenderError, escape_compose, render
from cagent.spec import ClusterSpec, Source

# Shared healthcheck timing: up to ~2.5 minutes after a 10 s grace period.
HEALTH_TIMING = {"interval": "5s", "timeout": "3s", "retries": 30, "start_period": "10s"}


class GenerationError(Exception):
    """The spec cannot be turned into a compose file. problems lists every reason."""

    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


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


def generate(spec: ClusterSpec, catalog: Catalog) -> dict[str, Any]:
    """Return the compose document as a dict. Raises GenerationError listing every problem."""
    problems: list[str] = []
    services: dict[str, dict[str, Any]] = {}

    for ref in spec.middleware:
        tpl = catalog.middleware.get(ref.template)
        if tpl is None:
            problems.append(f"{ref.instance}: unknown middleware template {ref.template!r}")
            continue
        version = ref.version or tpl.default_version
        if version not in tpl.versions:
            problems.append(
                f"{ref.instance}: {tpl.name} {version} unsupported; use {', '.join(tpl.versions)}"
            )
            continue
        preset = tpl.presets.get(ref.preset or tpl.default_preset)
        if preset is None:
            problems.append(
                f"{ref.instance}: unknown preset {ref.preset!r}; use {', '.join(tpl.presets)}"
            )
            continue
        values = {"host": ref.instance, "port": str(tpl.port), **tpl.connection}
        svc: dict[str, Any] = {"image": tpl.image.replace("{version}", version)}
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

    bindings, match_problems = match(spec, catalog)
    problems += match_problems
    by_consumer: dict[str, list[Binding]] = defaultdict(list)
    for b in bindings:
        by_consumer[b.consumer].append(b)
    instances = {m.instance: m for m in spec.middleware}

    for ref in spec.services:
        desc = catalog.services.get(ref.name)
        if desc is None:
            problems.append(f"{ref.name}: not registered in the catalog")
            continue
        if getattr(desc.sources, ref.source.kind) is None:
            problems.append(f"{ref.name}: its descriptor declares no {ref.source.kind} source")
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
                    problems.append(f"{ref.name}/{b.requirement.capability}: {var}: {e}")
            depends[b.provider] = {"condition": "service_healthy"}
        env.update(ref.env)
        if missing := [v for v in desc.runtime.env.required if v not in env]:
            problems.append(f"{ref.name}: missing required env {', '.join(missing)}")
        if unknown := sorted(set(ref.publish) - set(ports)):
            problems.append(
                f"{ref.name}: cannot publish unknown port {', '.join(unknown)}; "
                f"ports are {', '.join(ports) or 'none'}"
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

    if problems:
        raise GenerationError(problems)
    return {"name": spec.name, "services": dict(sorted(services.items()))}


def to_yaml(compose: dict[str, Any]) -> str:
    return yaml.safe_dump(compose, sort_keys=False, default_flow_style=False, allow_unicode=True)
