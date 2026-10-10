"""HearAble local realtime subtitle pipeline."""

# The one place the release version is written.
#
# Everything else imports it: the pipeline stamps it into every metrics file,
# the plugin keeps its own copy because it lives in the decoder's flash where
# this package cannot be imported, and a test refuses to let the two — or this
# string and the newest git tag — drift apart. They had drifted: this said
# 1.0.3 while the tags had reached v1.2.0, and ten artefacts from two campaigns
# went out claiming a version that had not existed for weeks.
#
# It is not derived from `git describe` on purpose. The decoder has no git, and
# a version that only resolves where the repository is checked out is missing
# exactly where the question gets asked.
__version__ = "1.5.3"
