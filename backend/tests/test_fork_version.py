"""The running build has to be identifiable, and it was not.

VERSION belongs to upstream and this fork deliberately does not bump it, so it
read 1.39.2 for every fork release. Beyond being wrong in the About panel, that
string is what decides whether a pre-upgrade backup is taken before startup
migrations - so while it never changed, that backup never fired for any fork
release at all.
"""
import os
import unittest
from unittest.mock import patch

try:
    from main import read_app_version, read_fork_release

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


@unittest.skipUnless(DEPS_AVAILABLE, "Backend dependencies are not installed")
class ForkVersionTests(unittest.TestCase):
    def test_the_fork_tag_is_preferred_over_upstreams_version_file(self):
        with patch("main.read_fork_release", return_value="bayton-v1.39.2-11"), \
                patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APP_VERSION", None)
            self.assertEqual(read_app_version(), "bayton-v1.39.2-11")

    def test_an_explicit_environment_override_still_wins(self):
        # Deployments that set APP_VERSION mean it deliberately.
        with patch.dict(os.environ, {"APP_VERSION": "set-by-hand"}):
            self.assertEqual(read_app_version(), "set-by-hand")

    def test_it_falls_back_to_the_version_file_without_a_tag(self):
        # The bystander: a checkout with no tags, or no git at all, must still
        # report something rather than failing to start.
        with patch("main.read_fork_release", return_value=""):
            os.environ.pop("APP_VERSION", None)
            self.assertNotEqual(read_app_version(), "")
            self.assertNotEqual(read_app_version(), "0.0.0")

    def test_reading_the_tag_never_raises(self):
        # It shells out to git. A deployment without git, or an export with no
        # .git directory, must degrade rather than crash startup.
        with patch("subprocess.run", side_effect=OSError("no git")):
            self.assertEqual(read_fork_release(), "")
