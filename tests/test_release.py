import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

from tools import build_release as release
from tools import check_project as project_check


@unittest.skipIf(sys.platform == "win32",
                 "发布产物检查(macOS 路径/签名/归档)为 macOS 发布工作流")
class ReleaseFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root_patch = mock.patch.object(release, "ROOT", self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.temporary.cleanup()

    def write(self, relative, data=b"content"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def entries(self, *paths):
        return release.validate_payload(list(paths))

    def test_runtime_caches_and_generated_artifacts_are_excluded(self):
        expected = self.write("static/app.js")
        safe_template = self.write("static/.env.example")
        for relative in (
            "static/Data/config.json",
            "static/.venv/bin/python",
            "static/node_modules/pkg/index.js",
            "static/htmlcov/index.html",
            "static/.coverage.worker",
            "static/coverage.xml",
            "static/editor.swp",
            "static/backup~",
        ):
            self.write(relative)
        with mock.patch.object(release, "INCLUDE", ("static",)):
            files = release.iter_release_files()
        self.assertEqual(files, [safe_template, expected])

    def test_sensitive_file_in_included_tree_fails_closed(self):
        self.write("static/app.js")
        self.write("static/.env.production", b"TOKEN=dummy")
        self.write("static/cache.sqlite3", b"not-a-real-database")
        with mock.patch.object(release, "INCLUDE", ("static",)):
            with self.assertRaisesRegex(SystemExit, "敏感文件"):
                release.iter_release_files()

    def test_symlinked_required_source_is_rejected(self):
        target = self.write("target/server.py")
        (self.root / "server.py").symlink_to(target)
        with mock.patch.object(release, "INCLUDE", ("server.py",)):
            with self.assertRaisesRegex(SystemExit, "符号链接"):
                release.iter_release_files()

    def test_user_home_detection_covers_other_build_hosts(self):
        posix = b"/" + b"Users" + b"/realperson/private/project"
        linux = b"/" + b"home" + b"/realperson/private/project"
        windows = b"C:" + b"\\" + b"Users" + b"\\realperson\\private"
        escaped = b"\\/" + b"Users" + b"\\/realperson\\/private"
        encoded = b"%2F" + b"Users" + b"%2Frealperson%2Fprivate"
        for value in (posix, linux, windows, escaped, encoded):
            with self.subTest(value=value):
                self.assertIsNotNone(release.find_path_leak(value))
        placeholder = b"/" + b"Users" + b"/example/project"
        self.assertIsNone(release.find_path_leak(placeholder))

    def test_large_file_is_scanned_for_absolute_paths(self):
        leaked = b"/" + b"Users" + b"/realperson/private/project"
        source = self.write("static/large.bin", b"x" * (5 * 1024 * 1024 + 1) + leaked)
        with self.assertRaisesRegex(SystemExit, "绝对路径"):
            self.entries(source)

    def test_private_key_content_is_rejected(self):
        marker = b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----"
        source = self.write("static/innocent.txt", marker)
        with self.assertRaisesRegex(SystemExit, "密钥"):
            self.entries(source)

    def test_high_confidence_access_token_is_rejected(self):
        marker = b"AK" + b"IA" + b"A" * 16
        source = self.write("static/innocent.txt", marker)
        with self.assertRaisesRegex(SystemExit, "令牌"):
            self.entries(source)

    def test_archive_is_reproducible_and_metadata_is_normalized(self):
        regular = self.write("server.py", b"print('ok')\n")
        executable = self.write("start.command", b"#!/bin/bash\nexit 0\n")
        regular.chmod(0o600)
        executable.chmod(0o700)
        first = self.root / "dist" / "first.zip"
        second = self.root / "dist" / "second.zip"
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1704067201"}):
            release.write_archive(first, self.entries(executable, regular), "1.2.3")
            os.utime(regular, (1_800_000_000, 1_800_000_000))
            os.utime(executable, (1_900_000_000, 1_900_000_000))
            regular.chmod(0o666)
            executable.chmod(0o777)
            entries = self.entries(regular, executable)
            release.write_archive(second, entries, "1.2.3")
            release.verify_archive(second, entries, "1.2.3")

        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(stat.S_IMODE(second.stat().st_mode), 0o644)
        with zipfile.ZipFile(second) as archive:
            infos = {info.filename: info for info in archive.infolist()}
        regular_info = infos["总控台-1.2.3/server.py"]
        executable_info = infos["总控台-1.2.3/start.command"]
        self.assertEqual(regular_info.compress_type, zipfile.ZIP_STORED)
        self.assertEqual(regular_info.date_time, (2024, 1, 1, 0, 0, 0))
        self.assertEqual(
            (regular_info.external_attr >> 16) & 0xFFFF,
            stat.S_IFREG | 0o644,
        )
        self.assertEqual(
            (executable_info.external_attr >> 16) & 0xFFFF,
            stat.S_IFREG | 0o755,
        )

    def test_archive_and_checksum_verification_detect_tampering(self):
        source = self.write("server.py", b"original")
        entries = self.entries(source)
        output = self.root / "dist" / "console-1.0.0.zip"
        release.write_archive(output, entries, "1.0.0")
        release.write_checksum(output)
        release.verify_archive(output, entries, "1.0.0")
        release.verify_checksum(output)

        output.write_bytes(output.read_bytes() + b"tampered")
        with self.assertRaisesRegex(SystemExit, "可重复构建"):
            release.verify_archive(output, entries, "1.0.0")
        with self.assertRaisesRegex(SystemExit, "SHA-256"):
            release.verify_checksum(output)

    def test_output_inside_included_source_tree_is_rejected(self):
        with self.assertRaisesRegex(SystemExit, "排除目录"):
            release.validate_output_dir(self.root / "static" / "release-output")
        self.assertEqual(
            release.validate_output_dir(self.root / "dist" / "release-output"),
            (self.root / "dist" / "release-output").resolve(),
        )

    def test_source_date_epoch_is_validated_and_clamped(self):
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "not-a-number"}):
            with self.assertRaisesRegex(SystemExit, "整数"):
                release.archive_timestamp()
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "999999999999"}):
            self.assertEqual(release.archive_timestamp()[0], 2107)

    def test_version_requires_semver(self):
        self.write("VERSION", b"release-one\n")
        with self.assertRaisesRegex(SystemExit, "SemVer"):
            release.version()
        self.write("VERSION", b"1.0.0-01\n")
        with self.assertRaisesRegex(SystemExit, "SemVer"):
            release.version()


class ProjectReleaseManifestTests(unittest.TestCase):
    def test_required_open_source_documents_are_in_payload(self):
        names = {
            path.relative_to(release.ROOT).as_posix()
            for path in release.iter_release_files()
        }
        for required in release.REQUIRED_PROJECT_DOCS:
            with self.subTest(required=required):
                self.assertIn(required, names)
        self.assertIn("docs/screenshots/ops-launchpad.jpg", names)
        self.assertIn("docs/screenshots/ops-services.jpg", names)

    def test_required_third_party_licenses_are_in_payload(self):
        names = {
            path.relative_to(release.ROOT).as_posix()
            for path in release.iter_release_files()
        }
        self.assertIn("licenses/Geist-OFL-1.1.txt", names)
        self.assertIn("licenses/Lucide-LICENSE.txt", names)


class AssetProvenanceGateTests(unittest.TestCase):
    def write_provenance(self, text):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "ASSET_PROVENANCE.md").write_text(text, encoding="utf-8")
        return root

    def test_blocked_or_replacement_asset_prevents_release(self):
        root = self.write_provenance(
            "### Logo\n- 状态：`TO_REPLACE`\n"
            "### Illustration\n- 状态：`BLOCKED`\n"
        )
        with mock.patch.object(project_check, "ROOT", root):
            with self.assertRaisesRegex(
                project_check.CheckError,
                "Logo=TO_REPLACE.*Illustration=BLOCKED",
            ):
                project_check.check_asset_release_status()

    def test_review_required_asset_is_left_for_manual_release_decision(self):
        root = self.write_provenance(
            "### Icons\n- 状态：`CLEARED`\n"
            "### Font\n- 状态：`REVIEW_REQUIRED`\n"
        )
        with mock.patch.object(project_check, "ROOT", root):
            result = project_check.check_asset_release_status()
        self.assertIn("2 项素材", result)


if __name__ == "__main__":
    unittest.main()
