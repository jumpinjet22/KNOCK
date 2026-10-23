"""The package version is stated in three places; keep them in step so a
release bump can't leave one behind (it once did: __version__ stayed 0.1.0
while the others moved on)."""

import re
import tomllib
from pathlib import Path

import knock
from knock.api.app import app

ROOT = Path(__file__).resolve().parent.parent


def test_the_version_is_the_same_everywhere() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]

    assert knock.__version__ == pyproject
    assert app.version == pyproject


def test_the_version_is_a_plain_release_number() -> None:
    # Prerelease wording (-alpha/-beta) lives in the git tag and image tag,
    # which is how the existing v0.x.y-alpha releases were done.
    assert re.fullmatch(r"\d+\.\d+\.\d+", knock.__version__)
