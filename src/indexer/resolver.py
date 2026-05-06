# src/indexer/resolver.py
from collections import deque

from .models import ModuleInfo


def topological_sort(modules: dict[str, ModuleInfo]) -> list[str]:
    """
    Kahn's algorithm — topological sort of modules by dependency.
    Base modules always precede modules that depend on them.

    Edge cases:
    - Missing dep: skip, continue.
    - Circular dep: append remaining in alphabetical order.
    - Deterministic: sorted() at every step.
    """
    if not modules:
        return []

    in_degree: dict[str, int] = {name: 0 for name in modules}
    dependents: dict[str, list[str]] = {name: [] for name in modules}

    for name, info in modules.items():
        for dep in info.depends:
            if dep in modules:
                in_degree[name] += 1
                dependents[dep].append(name)

    queue = deque(sorted(name for name, deg in in_degree.items() if deg == 0))
    result: list[str] = []

    while queue:
        node = queue.popleft()
        result.append(node)
        for dependent in sorted(dependents[node]):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    # Circular deps: append remaining theo alphabetical order
    if len(result) < len(modules):
        remaining = sorted(set(modules) - set(result))
        result.extend(remaining)

    return result
