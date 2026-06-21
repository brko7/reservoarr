"""reservoarr Dispatcharr plugin.

Installs `reservoarr.py` into /data/reservoarr/ on first load (and on upgrades),
then exposes a "Generate Stream Profile" action that creates a StreamProfile
row pointing at it. The script itself is stdlib-only Python and reads tuning
from environment variables; this plugin intentionally does NOT expose those
as plugin fields — set them on the container if you need to override defaults.
See https://github.com/brko7/reservoarr/blob/main/docs/TUNABLES.md.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
from pathlib import Path

from apps.plugins.models import PluginConfig
from core.models import StreamProfile


class Plugin:
    name = "reservoarr"
    version = "6.2.2"
    description = "Delay-buffer stream profile that absorbs IPTV CDN gaps so Plex Live TV stops dying"
    author = "brko7"
    help_url = "https://github.com/brko7/reservoarr"

    dst_dir = "/data/reservoarr"
    dst_script = dst_dir + "/reservoarr.py"

    plugin_dir = Path(__file__).resolve().parent
    src_script = plugin_dir / "reservoarr.py"

    plugin_key = plugin_dir.name.replace(" ", "_").lower()

    @staticmethod
    def _parse_version(s):
        if not s:
            return None
        try:
            return tuple(int(x) for x in s.strip().lstrip("v").split("."))
        except (ValueError, AttributeError):
            return None

    def __init__(self):
        try:
            self.context = PluginConfig.objects.get(key=self.plugin_key)
            self.settings = self.context.settings or {}
        except PluginConfig.DoesNotExist:
            self.context = None
            self.settings = {}

        if not os.path.isfile(self.dst_script):
            self._install()
        else:
            local = self._read_installed_version()
            packaged = self._parse_version(self.version)
            if local is not None and packaged is not None and packaged > local:
                self._install()

        self.fields = [
            {
                "id": "profile_name",
                "label": "Profile Name *",
                "type": "string",
                "default": "reservoarr",
                "description": "Name shown in Dispatcharr's Stream Profile picker.",
            },
            {
                "id": "set_as_default",
                "label": "Set as default Stream Profile",
                "type": "boolean",
                "default": False,
                "description": "Make this the instance-wide default after creation. "
                               "Leave off to set it manually per-channel.",
            },
            {
                "id": "tuning_note",
                "label": "Tuning",
                "type": "info",
                "description": "Defaults match the v6.1 production-validated behaviour and "
                               "fit most providers. To override (cushion target, watchdog "
                               "thresholds, log dir, etc.), set RESV_* env vars on the "
                               "Dispatcharr container — see the Docs link.",
            },
        ]

        self.actions = [
            {
                "id": "generate_profile",
                "label": "Generate Stream Profile",
                "button_label": "Generate Stream Profile",
                "button_color": "green",
                "description": "Create the reservoarr Stream Profile (or update it if it exists).",
                "confirm": {
                    "required": True,
                    "title": "Create reservoarr Stream Profile?",
                    "message": "Refresh Dispatcharr in your browser after this completes for "
                               "the new profile to appear in the picker.",
                },
            },
            {
                "id": "reinstall",
                "label": "Reinstall script",
                "button_label": "Reinstall reservoarr.py",
                "button_color": "blue",
                "description": "Re-copy reservoarr.py into /data/reservoarr/ (useful after a "
                               "plugin update if the install step was skipped).",
            },
        ]

    def _read_installed_version(self):
        """Read the version stamped by the most recent _install() into
        <dst_dir>/.installed_version. None if missing or unparseable —
        the upgrade gate in __init__ treats either as "no upgrade", which is
        safe because a missing dst_script is handled separately."""
        sentinel = Path(self.dst_dir) / ".installed_version"
        try:
            return self._parse_version(sentinel.read_text().strip())
        except OSError:
            return None

    def _install(self):
        os.makedirs(self.dst_dir, exist_ok=True)
        shutil.copy2(self.src_script, self.dst_script)
        st = os.stat(self.dst_script)
        os.chmod(self.dst_script, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        with contextlib.suppress(OSError):
            (Path(self.dst_dir) / ".installed_version").write_text(self.version + "\n")

    @staticmethod
    def _valid_profile_name(name):
        return bool(name) and bool(re.match(r"^[\w .\-]{1,64}$", name))

    def _generate_profile(self):
        name = (self.settings.get("profile_name") or "reservoarr").strip()
        if not self._valid_profile_name(name):
            return {"status": "error",
                    "message": "Profile name must be 1-64 chars, letters/digits/space/.-_ only."}

        if not os.path.isfile(self.dst_script):
            self._install()

        existing = StreamProfile.objects.filter(name__iexact=name).first()
        defaults = {
            "command": self.dst_script,
            "parameters": "{streamUrl} {userAgent}",
            "is_active": True,
            "locked": False,
        }
        if existing:
            if existing.locked:
                return {"status": "error",
                        "message": f"Profile '{name}' exists and is locked — pick a different name."}
            for k, v in defaults.items():
                setattr(existing, k, v)
            existing.save()
            profile = existing
            verb = "Updated"
        else:
            profile = StreamProfile(name=name, **defaults)
            profile.save()
            verb = "Created"

        msg = f"{verb} '{name}' profile (command={self.dst_script})"

        if self.settings.get("set_as_default"):
            try:
                from core.models import CoreSettings
                CoreSettings._update_group(
                    "stream_settings", "Stream Settings",
                    {"default_stream_profile": profile.id},
                )
                msg += "; set as default Stream Profile"
            except Exception as e:
                msg += f"; (could not set as default: {type(e).__name__}: {e})"

        msg += ". Refresh Dispatcharr in your browser to see the change."
        return {"status": "ok", "message": msg}

    def _reinstall(self):
        try:
            self._install()
            return {"status": "ok",
                    "message": f"Reinstalled reservoarr.py at {self.dst_script}"}
        except OSError as e:
            return {"status": "error",
                    "message": f"Install failed: {type(e).__name__}: {e}"}

    def run(self, action: str, params: dict, context: dict):
        self.settings = context.get("settings", {}) or {}
        if action == "generate_profile":
            return self._generate_profile()
        if action == "reinstall":
            return self._reinstall()
        return {"status": "error", "message": f"Unknown action: {action}"}
