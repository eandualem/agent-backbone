"""Internal, process-pinned native environments for isolated providers.

No production caller installs these yet. A context is not proof of native account
isolation, nor permission to activate a reserved provider.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


@dataclass(frozen=True)
class NativeEnvironment(Mapping[str, str]):
    root: Path
    executable: str | None
    variables: Mapping[str, str]
    path_keys: frozenset[str] = frozenset()

    def __post_init__(self):
        object.__setattr__(self, "variables", MappingProxyType(dict(self.variables)))
        object.__setattr__(self, "path_keys", frozenset(self.path_keys))
        self.check()

    def __getitem__(self, key: str) -> str:
        return self.variables[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.variables)

    def __len__(self) -> int:
        return len(self.variables)

    def check(self) -> None:
        if not self.root.is_absolute() or self.root.resolve() != self.root:
            raise ValueError("Native environment root must be canonical and absolute")
        if not (_PATH_KEYS | self.path_keys) <= self.variables.keys():
            raise ValueError("Native environment is missing required paths")
        if self.executable is not None and not Path(self.executable).is_absolute():
            raise ValueError("Native executable must be absolute")
        for key, value in self.variables.items():
            if not isinstance(value, str) or "\0" in value:
                raise ValueError("Invalid native environment value")
            if key in _PATH_KEYS | self.path_keys:
                self.check_path(Path(value))

    def check_path(self, path: Path) -> Path:
        resolved = path.resolve()
        if not path.is_absolute() or not resolved.is_relative_to(self.root):
            raise ValueError("Native path escapes the captured runtime environment")
        return path

    def overlay(self, values: Mapping[str, str]) -> NativeEnvironment:
        self.check()
        for key, value in values.items():
            if key not in _OVERRIDES and self.variables.get(key) != value:
                raise ValueError("Agent environment overrides a captured runtime setting")
        return NativeEnvironment(self.root, self.executable, {**self, **values}, self.path_keys)


_PATH_KEYS = frozenset(
    {
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
        "TMPDIR",
        "TMP",
        "TEMP",
    }
)
# Runtime-specific keys are selected and validated by the adapter. Arbitrary
# environment overrides could select host accounts, credential helpers or servers.
_OVERRIDES = frozenset({"LANG", "LC_ALL", "TERM", "COLORTERM", "NO_COLOR"})
_CONTEXT: Mapping[str, NativeEnvironment] | None = None


def install_native_environments(environments: Mapping[str, NativeEnvironment]) -> None:
    """Pin once per provider process; switching a selection cannot retarget work."""
    global _CONTEXT
    captured = MappingProxyType(dict(environments))
    for env in captured.values():
        env.check()
    roots = [env.root for env in captured.values()]
    if any(
        a.is_relative_to(b) or b.is_relative_to(a)
        for index, a in enumerate(roots)
        for b in roots[index + 1 :]
    ):
        raise ValueError("Native runtime roots overlap")
    if _CONTEXT is not None and captured != _CONTEXT:
        raise ValueError("Native runtime context is already pinned")
    _CONTEXT = captured


def native_environment(runtime: str) -> NativeEnvironment | None:
    if _CONTEXT is None:
        return None
    if runtime not in _CONTEXT:
        raise ValueError("Runtime has no captured native environment")
    env = _CONTEXT[runtime]
    env.check()
    return env


def environment_value(env: Mapping[str, str], key: str) -> str:
    if isinstance(env, NativeEnvironment):
        env.check()
        return env.get(key, "")
    return env.get(key) or os.environ.get(key) or ""


def inherited_environment(env: Mapping[str, str]) -> dict[str, str]:
    return dict(env) if isinstance(env, NativeEnvironment) else {**os.environ, **env}
