"""Theme Manager - our plugins' popups follow the window style.

Off by default. Switched on in Settings ("Plugin popups follow window
style"), Theme Manager creates the empty marker file
~/.config/omarchy/theme-manager/popup-glass (its own folder; 0600). That
file is the switch the other plugins look for (by name only):

- QML plugin panels (PopupGlass.qml) and the GTK popups (Theme Manager,
  App Manager) make their card see-through: glassy 0.6, transparent 0.9,
  opaque solid. The window style is Omarchy's own
  (omarchy-hyprland-window-transparency: Font & effects, or Omarchy's menu).
- Theme Manager's bar widget adds Hyprland's blur for our own layer popups
  and Omarchy's window transparency for our app windows at run time
  (`hyprctl eval`), again after every config reload. No config file is edited.

Omarchy's own popups are not touched. Off deletes the marker file.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import glass_style
from glass_style import enabled, popup_alpha, window_mode  # noqa: F401  (re-exported)


def marker_file() -> Path:
    return glass_style.marker_file()


def set_enabled(on: bool) -> None:
    """Create or remove the marker (never through a link; only a regular file
    or a link of ours named so is removed)."""
    path = marker_file()
    if on:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except FileExistsError:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                raise RuntimeError(f"{path} exists and is not a file") from None
            return
        os.close(fd)
    else:
        try:
            if not stat.S_ISDIR(os.lstat(path).st_mode):
                path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd in ("on", "off"):
        set_enabled(cmd == "on")
    print(f"{'on' if enabled() else 'off'} {window_mode()} alpha={popup_alpha()}")
