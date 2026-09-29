"""HearAble OSD plugin for Enigma2.

The plugin runs in the decoder's flash, where the `hearable` package is not
installed, so it carries its own copy of the release version. `tests/` refuses
to let the two disagree, and `tools/sf8008_install_plugin.sh` refuses to install
a plugin whose version does not match the tree it came from.
"""

__version__ = "1.5.1"
