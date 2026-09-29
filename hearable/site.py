"""Where this particular installation's machines are.

Addresses and a MAC are facts about one household, not about HearAble, and they
used to sit in forty-odd files as default arguments. That is convenient right up
to the moment the repository is shared, or an address changes — which happened
here when the decoder's Ethernet port became the private link and its old
address stopped existing. Every tool still carrying that address then reported
`No route to host`, which reads exactly like a switched-off decoder and is not.

So: one file, not forty. `config/site.json` is gitignored and holds the real
values; `config/site.example.json` is tracked and holds placeholders. An
environment variable beats both, which is what a one-off run against a different
box uses:

    HEARABLE_DECODER_HOST=192.168.1.99 tools/sf8008_install_plugin.sh

Nothing here fails hard. A missing file gives the example's placeholders, so a
fresh clone runs far enough to tell you what it is missing rather than dying on
an import.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "config" / "site.json"
EXAMPLE = ROOT / "config" / "site.example.json"

ENV = {
    "decoder_host": "HEARABLE_DECODER_HOST",
    "decoder_user": "HEARABLE_DECODER_USER",
    "t9_host": "HEARABLE_T9_HOST",
    "t9_user": "HEARABLE_T9_USER",
    "t9_mac": "HEARABLE_T9_MAC",
    "link_decoder": "HEARABLE_LINK_DECODER",
    "link_t9": "HEARABLE_LINK_T9",
    "link_broadcast": "HEARABLE_LINK_BROADCAST",
}


def _read(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in loaded.items() if not k.startswith("_")}


def load() -> dict:
    """The example's values, overlaid by site.json, overlaid by the environment."""
    values = _read(EXAMPLE)
    values.update(_read(SITE))
    for key, variable in ENV.items():
        from_env = os.environ.get(variable)
        if from_env:
            values[key] = from_env
    return values


def get(key: str, default: str = "") -> str:
    return str(load().get(key, default))


def configured() -> bool:
    """True when someone has actually filled this in.

    The placeholder MAC is the giveaway: no real adapter is all zeroes.
    """
    return get("t9_mac") not in ("", "00:00:00:00:00:00")
