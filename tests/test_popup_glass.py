import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import glass_style
import popup_glass


class PopupGlassTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"HOME": str(root), "XDG_STATE_HOME": str(root / "state")})
        self.env.start()
        self.toggles = root / "state/omarchy/toggles/hypr"
        self.toggles.mkdir(parents=True)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_off_keeps_popups_solid(self):
        (self.toggles / "window-glassy.lua").write_text("")
        self.assertFalse(glass_style.enabled())
        self.assertEqual(glass_style.popup_alpha(), 1.0)

    def test_levels_follow_the_window_style(self):
        popup_glass.set_enabled(True)
        self.assertEqual(glass_style.popup_alpha(), glass_style.TRANSPARENT_ALPHA)
        (self.toggles / "window-glassy.lua").write_text("")
        self.assertEqual(glass_style.popup_alpha(), glass_style.GLASSY_ALPHA)
        (self.toggles / "window-opaque.lua").write_text("")    # opaque wins, as in Omarchy's command
        self.assertEqual(glass_style.popup_alpha(), 1.0)

    def test_marker_is_private_and_removed_again(self):
        popup_glass.set_enabled(True)
        popup_glass.set_enabled(True)                         # twice is fine
        marker = popup_glass.marker_file()
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        self.assertEqual(marker.read_bytes(), b"")
        popup_glass.set_enabled(False)
        self.assertFalse(marker.exists())
        popup_glass.set_enabled(False)                        # already off

    def test_a_link_is_never_followed(self):
        target = Path(self.tmp.name) / "elsewhere"
        target.write_text("keep")
        marker = popup_glass.marker_file()
        marker.parent.mkdir(parents=True)
        marker.symlink_to(target)
        with self.assertRaises(RuntimeError):
            popup_glass.set_enabled(True)
        popup_glass.set_enabled(False)                        # removes the link, not its target
        self.assertFalse(os.path.lexists(marker))
        self.assertEqual(target.read_text(), "keep")

    def test_no_config_file_is_written(self):
        popup_glass.set_enabled(True)
        written = sorted(str(p.relative_to(self.tmp.name)) for p in Path(self.tmp.name).rglob("*") if p.is_file())
        self.assertEqual(written, [".config/omarchy/theme-manager/popup-glass"])


if __name__ == "__main__":
    unittest.main()
