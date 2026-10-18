"""Generate a compose document from a ClusterSpec and the catalog."""

import difflib
import hashlib
import re
from collections import defaultdict
from typing import Any

import yaml

from cagent.catalog import Catalog, Healthcheck
from cagent.gaps import Gap
from cagent.matching import Binding, match
from cagent.render import RenderError, escape_compose, render
from cagent.spec import ClusterSpec, Source

# Shared healthcheck timing: up to ~2.5 minutes after a 10 s grace period.
HEALTH_TIMING = {"interval": "5s", "timeout": "3s", "retries": 30, "start_period": "10s"}


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

    if gaps:
        raise GenerationError(gaps)
    return {"name": spec.name, "services": dict(sorted(services.items()))}


def to_yaml(compose: dict[str, Any]) -> str:
    return yaml.safe_dump(compose, sort_keys=False, default_flow_style=False, allow_unicode=True)
