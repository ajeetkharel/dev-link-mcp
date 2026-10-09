"""No confinement: commands run as the user, like a coding agent started in a terminal."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path


class Unconfined:
    name = "none"
    confined = False

    def available(self) -> str | None:
        return None

    def visible(self, path: Path) -> bool:
        return True

    def wrap(self, argv: Sequence[str], cwd: Path, extra_env: Mapping[str, str]) -> list[str]:
        """`argv` unchanged; the Sandbox puts `extra_env` straight into the process environment."""
        return list(argv)
