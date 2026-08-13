"""Config loading. Resolves the repo root so stages work from any cwd."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def repo_root() -> Path:
    """Repo root = nearest ancestor of this file containing config.toml."""
    env = os.environ.get("H2BANK_ROOT")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config.toml").is_file():
            return parent
    # Fall back to <repo>/pipeline/h2bank/config.py -> <repo>
    return here.parents[2]


@dataclass(frozen=True)
class Config:
    root: Path
    raw: dict[str, Any] = field(repr=False)

    # -- paths -----------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.root / self.raw["paths"]["db"]

    @property
    def pdf_dir(self) -> Path:
        return self.root / self.raw["paths"]["pdf_dir"]

    @property
    def schema_path(self) -> Path:
        return self.root / self.raw["paths"]["schema"]

    @property
    def candidates_path(self) -> Path:
        return self.root / self.raw["paths"]["candidates"]

    # -- sections --------------------------------------------------------
    @property
    def crawl(self) -> dict[str, Any]:
        return self.raw["crawl"]

    @property
    def compress(self) -> dict[str, Any]:
        return self.raw["compress"]

    @property
    def split(self) -> dict[str, Any]:
        return self.raw["split"]

    @property
    def tag(self) -> dict[str, Any]:
        return self.raw["tag"]


def load_dotenv(root: Path) -> list[str]:
    """Populate os.environ from a gitignored `.env`, if present.

    Lets secrets like ANTHROPIC_API_KEY live in a local file instead of being
    exported by hand every session. Existing environment variables always win,
    so CI (which injects real secrets) is unaffected.
    """
    env_path = root / ".env"
    loaded: list[str] = []
    if not env_path.is_file():
        return loaded
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def load_config(path: Path | None = None) -> Config:
    root = repo_root()
    load_dotenv(root)
    cfg_path = path or (root / "config.toml")
    with cfg_path.open("rb") as fh:
        raw = tomllib.load(fh)
    return Config(root=root, raw=raw)
