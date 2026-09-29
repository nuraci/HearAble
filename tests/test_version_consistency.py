"""The version is written by hand in two places, so something has to check it.

It had already gone stale once: `hearable.__version__` said 1.0.3 while the
newest tag was v1.2.0, and ten benchmark artefacts from two campaigns recorded a
release that had not existed for weeks. Nothing noticed, because nothing looked.

These three tests look.
"""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugin"))

from hearable import __version__ as PACKAGE_VERSION

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def plugin_version() -> str:
    """Read it as text: importing the plugin package pulls in enigma2."""
    source = (ROOT / "plugin/HearAbleOSD/__init__.py").read_text()
    match = re.search(r'^__version__ = "([^"]+)"', source, re.MULTILINE)
    assert match, "the plugin has no __version__"
    return match.group(1)


def newest_tag() -> str | None:
    result = subprocess.run(["git", "-C", str(ROOT), "tag", "--list", "v*",
                             "--sort=-v:refname"], capture_output=True, text=True)
    tags = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return tags[0] if tags else None


class VersionConsistency(unittest.TestCase):
    def test_the_version_looks_like_a_version(self):
        self.assertRegex(PACKAGE_VERSION, SEMVER)

    def test_the_plugin_says_the_same_thing_as_the_package(self):
        """The decoder's copy cannot import the package, so it repeats it.

        A plugin that reports a version the tree never had is worse than one
        that reports nothing: it answers the question wrongly.
        """
        self.assertEqual(plugin_version(), PACKAGE_VERSION)

    def test_the_version_is_not_behind_the_newest_tag(self):
        """Ahead of the newest tag is normal — that is unreleased work.

        Behind it is the failure that happened: a released version, and a tree
        still calling itself something older.
        """
        tag = newest_tag()
        if tag is None:
            self.skipTest("no tags in this checkout")
        tagged = tuple(int(part) for part in tag.lstrip("v").split("."))
        current = tuple(int(part) for part in PACKAGE_VERSION.split("."))
        self.assertGreaterEqual(
            current, tagged,
            f"__version__ {PACKAGE_VERSION} è più vecchia del tag {tag}: "
            "aggiorna hearable/__init__.py e plugin/HearAbleOSD/__init__.py")


if __name__ == "__main__":
    unittest.main()
