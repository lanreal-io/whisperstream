"""Tests for the installed-apps enumeration added for the Power Mode picker.

Covers the pure, OS-independent logic (stub filtering, DisplayIcon->exe mapping,
Settings-style Uninstall listability filters) plus the never-raise contracts of
``resolve_app_by_path`` / ``list_installed_apps`` / ``installed_index``.

The registry-walking functions are integration-tested only for "never raises +
returns the right shape" (their data is machine-specific); the filter helpers are
unit-tested directly. ``_is_listable_uninstall_entry`` is driven with a patched
``winreg.QueryValueEx`` so it runs without touching a real registry key.
"""

import os
import sys
import unittest
from unittest.mock import patch

from whisperstream import app_directory as ad


class TestStubExeFilter(unittest.TestCase):
    """Installer/updater/uninstaller stub exes are never routable apps."""

    def test_known_stubs_are_filtered(self):
        for base in ("setup.exe", "update.exe", "uninstall.exe", "uninst.exe", "installer.exe"):
            self.assertTrue(ad._is_stub_exe(base), f"{base} should be a stub")

    def test_inno_uninstaller_family_filtered(self):
        # Inno Setup numbered uninstallers + "Uninstall <App>.exe" shortcuts.
        for base in ("unins000.exe", "unins001.exe", "uninstallfsreader.exe"):
            self.assertTrue(ad._is_stub_exe(base), f"{base} should be a stub")

    def test_real_apps_not_filtered(self):
        for base in ("chrome.exe", "code.exe", "discord.exe", "1password.exe"):
            self.assertFalse(ad._is_stub_exe(base), f"{base} is a real app")

    def test_empty_is_not_a_stub(self):
        self.assertFalse(ad._is_stub_exe(""))


class TestExeBasenameIfApp(unittest.TestCase):
    """DisplayIcon -> (basename, path) mapping, with index stripping + rejection
    of non-exe / missing / stub targets."""

    def test_real_exe_resolves(self):
        exe = sys.executable  # a guaranteed-existing .exe on Windows test hosts
        if not exe.lower().endswith(".exe"):
            self.skipTest("python executable is not a .exe on this platform")
        hit = ad._exe_basename_if_app(exe)
        self.assertIsNotNone(hit)
        base, path = hit
        self.assertEqual(base, os.path.basename(exe).lower())
        self.assertEqual(path, exe)

    def test_icon_index_suffix_is_stripped(self):
        exe = sys.executable
        if not exe.lower().endswith(".exe"):
            self.skipTest("python executable is not a .exe on this platform")
        hit = ad._exe_basename_if_app(f'"{exe}",0')
        self.assertIsNotNone(hit)
        self.assertEqual(hit[1], exe)

    def test_non_exe_icon_rejected(self):
        self.assertIsNone(ad._exe_basename_if_app(r"C:\Program Files\App\app.ico"))
        self.assertIsNone(ad._exe_basename_if_app(r"C:\Program Files\App\shell32.dll,5"))

    def test_missing_exe_rejected(self):
        self.assertIsNone(ad._exe_basename_if_app(r"C:\nope\does-not-exist.exe"))

    def test_empty_rejected(self):
        self.assertIsNone(ad._exe_basename_if_app(""))
        self.assertIsNone(ad._exe_basename_if_app(None))


class TestListableUninstallEntry(unittest.TestCase):
    """Settings-style Uninstall listability filters, driven via a patched
    winreg.QueryValueEx so no real registry key is needed."""

    def _run_with(self, values: dict) -> bool:
        """Evaluate the filter against a fake key whose values are ``values``."""
        def fake_query(_key, name):
            if name in values:
                return (values[name], 1)
            raise FileNotFoundError(name)  # winreg raises for absent values

        with patch.object(ad, "_read_reg_value", side_effect=lambda key, name: values.get(name)):
            return ad._is_listable_uninstall_entry(object())

    def test_clean_entry_is_listable(self):
        self.assertTrue(self._run_with({"DisplayName": "Google Chrome"}))

    def test_missing_display_name_rejected(self):
        self.assertFalse(self._run_with({}))

    def test_blank_display_name_rejected(self):
        self.assertFalse(self._run_with({"DisplayName": "   "}))

    def test_system_component_rejected(self):
        self.assertFalse(self._run_with({"DisplayName": "X", "SystemComponent": 1}))

    def test_system_component_zero_is_listable(self):
        self.assertTrue(self._run_with({"DisplayName": "X", "SystemComponent": 0}))

    def test_child_patch_entry_rejected(self):
        self.assertFalse(self._run_with({"DisplayName": "X", "ParentKeyName": "Y"}))
        self.assertFalse(self._run_with({"DisplayName": "X", "ParentDisplayName": "Y"}))

    def test_os_update_release_types_rejected(self):
        for rel in ("Security Update", "Update", "Hotfix"):
            self.assertFalse(self._run_with({"DisplayName": "KB123", "ReleaseType": rel}), rel)

    def test_runtime_and_redistributable_names_rejected(self):
        for name in (
            "Microsoft Visual C++ 2015-2022 Redistributable (x64)",
            "Microsoft Windows Desktop Runtime - 8.0.26 (x64)",
            "Microsoft .NET Runtime - 8.0.26 (x64)",
            "Microsoft Edge WebView2 Runtime",
        ):
            self.assertFalse(self._run_with({"DisplayName": name}), name)

    def test_real_app_with_runtime_word_kept(self):
        # "Node.js JavaScript Runtime" contains "runtime" but matches no runtime
        # marker (markers are specific, e.g. ".net runtime"), so it stays listable.
        self.assertTrue(self._run_with({"DisplayName": "Node.js JavaScript Runtime"}))


class TestResolveAppByPath(unittest.TestCase):
    """The Browse-for-an-app resolver: an existing .exe -> record, else None."""

    def test_real_exe_resolves_to_record(self):
        exe = sys.executable
        if not exe.lower().endswith(".exe"):
            self.skipTest("python executable is not a .exe on this platform")
        rec = ad.resolve_app_by_path(exe)
        self.assertIsInstance(rec, dict)
        self.assertEqual(rec["process_name"], os.path.basename(exe).lower())
        self.assertTrue(rec["installed"])
        self.assertIn("display_name", rec)
        self.assertIn("icon", rec)  # may be None on non-Windows; key must exist

    def test_missing_path_returns_none(self):
        self.assertIsNone(ad.resolve_app_by_path(r"C:\nope\missing.exe"))

    def test_non_exe_returns_none(self):
        self.assertIsNone(ad.resolve_app_by_path(r"C:\Windows\notepad"))  # no .exe

    def test_empty_returns_none(self):
        self.assertIsNone(ad.resolve_app_by_path(""))


class TestInstalledListContracts(unittest.TestCase):
    """``installed_index`` / ``list_installed_apps`` never raise and return the
    documented shapes. Data is machine-specific, so we assert structure + the
    'no stub leaked' and 'process names unique' invariants rather than contents."""

    def test_installed_index_is_a_dict(self):
        idx = ad.installed_index()
        self.assertIsInstance(idx, dict)

    def test_list_installed_apps_shape_and_invariants(self):
        apps = ad.list_installed_apps()
        self.assertIsInstance(apps, list)
        seen = set()
        for a in apps:
            self.assertIn("process_name", a)
            self.assertIn("display_name", a)
            self.assertIn("icon", a)
            self.assertTrue(a.get("installed"))
            pn = a["process_name"]
            # process names are unique (index dedupes by basename) ...
            self.assertNotIn(pn, seen, f"duplicate process_name {pn}")
            seen.add(pn)
            # ... and no installer/updater stub leaked into the picker list.
            self.assertFalse(ad._is_stub_exe(pn), f"stub {pn} leaked into list")

    def test_list_installed_apps_is_cached(self):
        # Second call returns the same cached object (no rebuild).
        self.assertIs(ad.list_installed_apps(), ad.list_installed_apps())


if __name__ == "__main__":
    unittest.main()
