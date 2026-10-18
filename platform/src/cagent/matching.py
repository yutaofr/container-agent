"""Resolve each service requirement to the middleware instance that provides it."""

from dataclasses import dataclass

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from cagent.catalog import Catalog, Requirement
from cagent.gaps import Gap
from cagent.spec import ClusterSpec, MiddlewareRef


@dataclass(frozen=True)
class Binding:
    consumer: str  # service name
    requirement: Requirement
    provider: str  # middleware instance name


def instance_version(ref: MiddlewareRef, catalog: Catalog) -> str:
    return ref.version or catalog.middleware[ref.template].default_version


def _satisfies(version: str, specifier: str | None) -> bool:
    if specifier is None:
        return True
    try:
        return Version(version) in SpecifierSet(specifier)
    except InvalidVersion:
        return False


def _fitting_versions(versions: list[str], specifier: str | None) -> tuple[str, ...]:
    """The versions satisfying specifier, lowest first."""
    fit = [v for v in versions if _satisfies(v, specifier)]
    return tuple(sorted(fit, key=Version))


def match(spec: ClusterSpec, catalog: Catalog) -> tuple[list[Binding], list[Gap]]:
    """Return one binding per satisfied requirement, and a gap per unsatisfied one.

    Services and templates missing from the catalog are skipped here; the generator
    reports them.
    """
    instances = {m.instance: m for m in spec.middleware if m.template in catalog.middleware}
    bindings: list[Binding] = []
    gaps: list[Gap] = []
    for svc in spec.services:
        desc = catalog.services.get(svc.name)
        if desc is None:
            continue
        for req in desc.requires:
            key = f"{svc.name}/{req.capability}"
            providers = [
                name
                for name, ref in instances.items()
                if catalog.middleware[ref.template].provides.capability == req.capability
            ]
            if key in spec.bindings:
                chosen = spec.bindings[key]
                if chosen not in providers:
                    gaps.append(
                        Gap(
                            "binding",
                            key,
                            f"{key}: binding names {chosen!r}, which does not provide it",
                            tuple(providers),
                            providers[0] if providers else None,
                        )
                    )
                    continue
                providers = [chosen]
            fitting = [
                p
                for p in providers
                if _satisfies(instance_version(instances[p], catalog), req.version)
            ]
            if len(fitting) == 1:
                bindings.append(Binding(svc.name, req, fitting[0]))
            elif fitting:
                gaps.append(
                    Gap(
                        "binding",
                        key,
                        f"{key}: ambiguous, candidates {', '.join(fitting)}; set bindings[{key!r}]",
                        tuple(fitting),
                        fitting[0],
                    )
                )
            elif providers:
                found = ", ".join(
                    f"{p} is {instance_version(instances[p], catalog)}" for p in providers
                )
                tpl = catalog.middleware[instances[providers[0]].template]
                versions = _fitting_versions(tpl.versions, req.version)
                gaps.append(
                    Gap(
                        "version_mismatch",
                        key,
                        f"{key}: needs version {req.version}; {found}",
                        versions,
                        versions[-1] if versions else None,
                        providers[0],
                    )
                )
            else:
                templates = tuple(
                    sorted(
                        t.name
                        for t in catalog.middleware.values()
                        if t.provides.capability == req.capability
                        and _fitting_versions(t.versions, req.version)
                    )
                )
                gaps.append(
                    Gap(
                        "no_provider",
                        key,
                        f"{key}: no middleware in the spec provides {req.capability}",
                        templates,
                        templates[0] if templates else None,
                    )
                )
    return bindings, gaps
