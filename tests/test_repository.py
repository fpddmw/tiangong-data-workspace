"""Bounded Git preparation checks use disposable, local-only working trees."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("repository", Path(__file__).parents[1] / "bin/repository.py")
repository = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repository)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, path, data):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode() if isinstance(data, str) else data)
        return target

    def git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.root), *arguments], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=repository.git_environment()).stdout

    def invoke(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = repository.main([*arguments, "--root", str(self.root)])
        return code, json.loads(output.getvalue()), output.getvalue()

    def reviewed_manifest(self, entries, indexed=True):
        self.git("init", "--quiet")
        self.write(repository.REVIEWED_FILES_PATH, json.dumps({
            "schema": repository.REVIEWED_FILES_SCHEMA,
            "files": {path: {"sha256": hashlib.sha256(data).hexdigest(), "allow": codes,
                             "reason": "Reviewed synthetic source fixture"}
                      for path, (data, codes) in entries.items()},
        }))
        if indexed:
            self.git("add", repository.REVIEWED_FILES_PATH)

    def test_source_and_empty_template_pass_without_initializing_root(self):
        self.write(".gitignore", ".env\nwork/\n")
        self.write("bin/example.py", "print('hello')\n")
        self.write(".env.example", "# Values belong in the local .env\nTOKEN=\nexport URL=\"\" # empty\n")
        self.write(".env", "PRIVATE=value")
        self.write("work/.foundry/run.json", "native bytes")
        before = {path.relative_to(self.root): (path.stat().st_mtime_ns, path.read_bytes())
                  for path in self.root.rglob("*") if path.is_file()}
        code, value, _ = self.invoke("files")
        self.assertEqual(code, 0)
        self.assertEqual(value["files"], [".env.example", ".gitignore", "bin/example.py"])
        self.assertEqual(value["git_metadata"], "temporary")
        self.assertFalse((self.root / ".git").exists())
        after = {path.relative_to(self.root): (path.stat().st_mtime_ns, path.read_bytes())
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_tracked_secret_is_rejected_even_after_ignore_and_is_never_opened(self):
        self.git("init", "--quiet")
        secret = self.write(".env", "SECRET=do-not-read")
        self.git("add", ".env")
        self.write(".gitignore", ".env\n")
        original_open = os.open

        def guarded_open(path, *args, **kwargs):
            if Path(path) == secret:
                raise AssertionError("A secret file must not be opened")
            return original_open(path, *args, **kwargs)

        with patch.object(os, "open", side_effect=guarded_open):
            code, value, output = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertIn({"path": ".env", "code": "tracked-but-ignored"}, value["issues"])
        self.assertIn({"path": ".env", "code": "environment-file"}, value["issues"])
        self.assertNotIn("do-not-read", output)

    def test_existing_git_index_and_metadata_are_unchanged(self):
        self.git("init", "--quiet")
        self.write("example.txt", "safe content")
        self.git("add", "example.txt")

        def metadata():
            return {str(path.relative_to(self.root)): (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
                    for path in (self.root / ".git").rglob("*") if path.is_file()}

        before = metadata()
        self.assertEqual(self.invoke("check")[0], 0)
        self.assertEqual(before, metadata())

    def test_symlink_candidate_is_not_followed(self):
        outside = self.write(".env", "a secret")
        self.write(".gitignore", ".env\n")
        (self.root / "exposed").symlink_to(outside)
        code, value, output = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertIn({"path": "exposed", "code": "symlink"}, value["issues"])
        self.assertNotIn("a secret", output)

    def test_relative_material_file_and_directory_aliases_are_retained(self):
        self.write("materials/package/source.json", "retained source")
        (self.root / "projects").mkdir()
        (self.root / "projects/source.json").symlink_to("../materials/package/source.json")
        (self.root / "projects/sources").symlink_to("../materials/package", target_is_directory=True)
        code, value, _ = self.invoke("files")
        self.assertEqual(code, 0)
        self.assertIn("projects/source.json", value["files"])
        self.assertIn("projects/sources", value["files"])
        self.assertEqual((self.root / "projects/source.json").readlink().as_posix(),
                         "../materials/package/source.json")

    def test_allowed_alias_target_is_scanned_through_its_own_path(self):
        token = "gl" + "pat-" + "z" * 22
        self.write("materials/source.json", token)
        (self.root / "alias").symlink_to("materials/source.json")
        code, value, output = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertEqual(value["issues"], [{"path": "materials/source.json", "code": "gitlab-token", "line": 1}])
        self.assertNotIn(token, output)

    def test_relative_secret_alias_is_rejected_without_reading_target(self):
        secret = self.write(".env", "never read this")
        self.write(".gitignore", ".env\n")
        (self.root / "alias").symlink_to(".env")
        original_open = os.open

        def guarded_open(path, *args, **kwargs):
            if Path(path) == secret:
                raise AssertionError("The secret alias target must not be opened")
            return original_open(path, *args, **kwargs)

        with patch.object(os, "open", side_effect=guarded_open):
            code, value, output = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertEqual(value["issues"], [{"path": "alias", "code": "symlink"}])
        self.assertNotIn("never read this", output)

    def test_environment_template_alias_cannot_bypass_empty_value_check(self):
        self.write("materials/template.txt", "TOKEN=private-value\n")
        (self.root / ".env.example").symlink_to("materials/template.txt")
        code, value, output = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertEqual(value["issues"], [{"path": ".env.example", "code": "symlink"}])
        self.assertNotIn("private-value", output)

    def test_absolute_outside_missing_and_ignored_aliases_are_rejected(self):
        self.write("source.json", "safe")
        self.write("ignored.json", "local only")
        self.write(".gitignore", "ignored.json\n")
        (self.root / "absolute").symlink_to(self.root / "source.json")
        (self.root / "outside").symlink_to("../outside.json")
        (self.root / "missing").symlink_to("missing.json")
        (self.root / "excluded").symlink_to("ignored.json")
        code, value, _ = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertEqual({item["path"] for item in value["issues"]},
                         {"absolute", "outside", "missing", "excluded"})
        self.assertEqual({item["code"] for item in value["issues"]}, {"symlink"})

    def test_symlinked_ancestor_is_rejected_before_reading(self):
        self.git("init", "--quiet")
        self.write("original/source.json", "retained")
        self.git("add", "original/source.json")
        (self.root / "original/source.json").unlink()
        (self.root / "original").rmdir()
        self.write("actual/source.json", "must be reached only through actual")
        (self.root / "original").symlink_to("actual", target_is_directory=True)
        self.assertEqual(repository.candidate_stat(self.root, "original/source.json")[1], "symlink")
        code, value, _ = self.invoke("check")
        # Git itself may refuse an indexed child below a newly linked ancestor
        # while enumerating attributes; either layer must fail closed.
        self.assertNotEqual(code, 0)

    def test_tokens_report_only_location(self):
        token = "gh" + "p_" + "a" * 36
        self.write("config.txt", "safe line\nvalue=" + token + "\n")
        code, value, output = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertIn({"path": "config.txt", "code": "github-token", "line": 2}, value["issues"])
        self.assertNotIn(token, output)

    def test_oversized_candidate_is_rejected_without_reading(self):
        large = self.write("large.bin", b"x" * (repository.MAX_FILE_BYTES + 1))
        original_open = os.open

        def guarded_open(path, *args, **kwargs):
            if Path(path) == large:
                raise AssertionError("An oversized candidate must not be opened")
            return original_open(path, *args, **kwargs)

        with patch.object(os, "open", side_effect=guarded_open):
            code, value, _ = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertEqual(value["issues"], [{"path": "large.bin", "code": "file-too-large"}])

    def test_environment_template_requires_empty_values_and_root_location(self):
        self.write(".env.example", "TOKEN=placeholder\n")
        self.write("nested/.env.example", "TOKEN=\n")
        code, value, output = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertIn({"path": ".env.example", "code": "nonempty-environment-template"}, value["issues"])
        self.assertIn({"path": "nested/.env.example", "code": "environment-file"}, value["issues"])
        self.assertNotIn("placeholder", output)

    def test_native_state_is_rejected_without_reading(self):
        self.write(".control/config.json", "sensitive state")
        self.write("work/.foundry/tasks/attempt.json", "native attempt")
        code, value, output = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertEqual({item["code"] for item in value["issues"]}, {"local-state"})
        self.assertNotIn("sensitive state", output)
        self.assertNotIn("native attempt", output)

    def test_materials_and_portable_control_files_are_allowed(self):
        for path in ("projects/example/candidate.json", "materials/source.pdf",
                     "research/findings.md", "reports/check.json", "work/runs/input.json",
                     "archive/evidence.json", "outputs/export.csv", "installation/README.md",
                     "runtime-manifests/tool.json", "runtime-installation.json", "skills-lock.json",
                     ".control/policy.json", ".control/agent.json", ".control/runtime.json",
                     "projects/tool/Cargo.lock", "projects/tool/uv.lock", "projects/tool/yarn.lock",
                     "projects/tool/poetry.lock"):
            self.write(path, "safe retained material")
        self.assertEqual(self.invoke("check")[0], 0)

    def test_native_sidecars_locks_and_sessions_are_never_read(self):
        paths = ("projects/task/state.sqlite", "projects/task/state.sqlite-wal",
                 "work/state.db-shm", "work/task.lock", "work/session.json", "work/private-session.json",
                 ".control/production/run.json", "nested/.control/policy.json",
                 "projects/.state/attempt.json", "work/.foundry/task.json")
        for path in paths:
            self.write(path, "private native bytes")
        original_open = os.open

        def guarded_open(path, *args, **kwargs):
            if Path(path).is_relative_to(self.root) and Path(path).relative_to(self.root).as_posix() in paths:
                raise AssertionError("Native state must not be opened")
            return original_open(path, *args, **kwargs)

        with patch.object(os, "open", side_effect=guarded_open):
            code, value, output = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertEqual(len(value["issues"]), len(paths))
        self.assertEqual({item["code"] for item in value["issues"]}, {"local-state"})
        self.assertNotIn("private native bytes", output)

    def test_lfs_large_file_is_streamed_beyond_size_limit(self):
        self.write(".gitattributes", "*.bin filter=lfs diff=lfs merge=lfs -text\n")
        token = "gl" + "pat-" + "a" * 22
        self.write("large.bin", b"x" * (repository.MAX_FILE_BYTES + 16) + b"\n" + token.encode() + b"\n")
        code, value, output = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertEqual(value["issues"], [{"path": "large.bin", "code": "gitlab-token", "line": 2}])
        self.assertNotIn(token, output)

    def test_embedded_installations_and_office_owner_files_are_never_read(self):
        paths = ("work/research/pydeps/scipy/core.so",
                 "projects/task/toolkit-035/unpacked/bin/tidas",
                 "projects/task/.npm/cache/package.json",
                 "projects/task/.pnpm-store/v3/package.json",
                 "projects/task/home/cache/pnpm/package.json",
                 "projects/task/foundry-managed-adoption-20261026/cache/components/tool/bin/tidas",
                 "projects/task/components/tool-123/root/bin/tidas",
                 "projects/task/runtime/python-3.13/bin/python3",
                 "projects/task/developer-toolchain/bin/worker",
                 "projects/task/foundry-managed-adoption-20261026/native-input/attempt.json",
                 "projects/task/foundry-managed-adoption-20261026/retained-original-native/state.json",
                 "projects/task/extracted/runtime/bin/worker",
                 "materials/~$source.docx")
        for path in paths:
            self.write(path, "machine-local bytes")
        original_open = os.open

        def guarded_open(path, *args, **kwargs):
            if Path(path).is_relative_to(self.root) and Path(path).relative_to(self.root).as_posix() in paths:
                raise AssertionError("Installed runtimes and Office owner files must not be opened")
            return original_open(path, *args, **kwargs)

        with patch.object(os, "open", side_effect=guarded_open):
            code, value, output = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertEqual(value["issues"], [{"path": path, "code": "local-state"} for path in sorted(paths)])
        self.assertNotIn("machine-local bytes", output)
        # The installation container itself may be a leaf alias, rather than
        # an enumerated directory with children; neither form is portable.
        for path in ("projects/task/components/alias/root", "projects/task/runtime/python-3.13",
                     "projects/task/extracted/runtime",
                     "projects/task/foundry-managed-adoption-20261026/retained-original-native"):
            self.assertEqual(repository.forbidden_path(path), "local-state")

    def test_toolkit_recovery_material_outside_unpacked_is_retained(self):
        for path in ("projects/task/toolkit-035/manifest.json",
                     "projects/task/toolkit-035/original.zip",
                     "projects/task/toolkit-035/source/unpacked/example.json",
                     "projects/task/release/cache/elementary-flows/flow.json",
                     "projects/task/components/tool-123/manifest.json",
                     "projects/task/components/tool-123/source.tar",
                     "projects/task/components/tool-123/receipt.json",
                     "projects/task/extracted/source/worker.rs",
                     "archive/worker-hash-diagnosis/repro-executable",
                     "projects/task/foundry-managed-adoption-20261026/manifest.json"):
            self.write(path, "retained recovery material")
        self.assertEqual(self.invoke("check")[0], 0)

    def test_lfs_allows_safe_large_file_without_creating_git_metadata(self):
        self.write(".gitattributes", "*.bin filter=lfs -text\n")
        self.write("large.bin", b"x" * (repository.MAX_FILE_BYTES + 1))
        self.assertEqual(self.invoke("check")[0], 0)
        self.assertFalse((self.root / ".git").exists())

    def test_local_only_lfs_attributes_do_not_admit_large_file(self):
        self.git("init", "--quiet")
        self.write(".git/info/attributes", "*.bin filter=lfs -text\n")
        self.write("large.bin", b"x" * (repository.MAX_FILE_BYTES + 1))
        code, value, _ = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertEqual(value["issues"], [{"path": "large.bin", "code": "file-too-large"}])

    def test_ignored_nested_lfs_attributes_do_not_admit_large_file(self):
        self.write(".gitignore", "materials/.gitattributes\n")
        self.write("materials/.gitattributes", "*.bin filter=lfs -text\n")
        self.write("materials/large.bin", b"x" * (repository.MAX_FILE_BYTES + 1))
        code, value, _ = self.invoke("files")
        self.assertEqual(code, 1)
        self.assertNotIn("materials/.gitattributes", value["files"])
        self.assertEqual(value["issues"], [{"path": "materials/large.bin", "code": "file-too-large"}])

    def test_portable_nested_lfs_attributes_admit_large_file(self):
        self.write("materials/.gitattributes", "*.bin filter=lfs -text\n")
        self.write("materials/nested/large.bin", b"x" * (repository.MAX_FILE_BYTES + 1))
        self.assertEqual(self.invoke("check")[0], 0)

    def test_secret_across_stream_boundary_has_correct_line(self):
        token = "gl" + "pat-" + "b" * 22
        padding = b"line\n" + b"x" * (repository.SCAN_CHUNK_BYTES - 10)
        self.write("split.txt", padding + b"\n" + token.encode() + b"\n")
        code, value, output = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertEqual(value["issues"], [{"path": "split.txt", "code": "gitlab-token", "line": 3}])
        self.assertNotIn(token, output)

    def test_literal_prefilters_preserve_all_secret_pattern_matches(self):
        examples = {
            "private-key": b"-----BEGIN " + b"PRIVATE KEY-----",
            "github-token": b"gh" + b"p_" + b"a" * 36,
            "gitlab-token": b"gl" + b"pat-" + b"a" * 22,
            "signed-jwt": b".".join((b"eyJ" + b"a" * 30, b"eyJ" + b"b" * 50, b"c" * 43)),
            "aws-access-key": b"AK" + b"IA" + b"A" * 16,
            "api-secret": b"sk" + b"-" + b"a" * 44,
            "supabase-secret": b"sb" + b"_secret_" + b"a" * 22,
            "slack-token": b"xox" + b"b-" + b"a" * 24,
            "stripe-secret": b"sk" + b"_live_" + b"a" * 24,
            "google-api-key": b"AI" + b"za" + b"a" * 35,
        }
        for code, token in examples.items():
            with self.subTest(code=code):
                findings, count = repository.scan_secrets(io.BytesIO(b"line\n" + token + b"\n"))
                self.assertEqual(findings, {code: 2})
                self.assertEqual(count, len(token) + 6)

    def test_signed_jwt_is_rejected_without_printing_it(self):
        token = ".".join(("eyJ" + "a" * 30, "eyJ" + "b" * 50, "c" * 43))
        self.write("response.json", "line\n" + token)
        code, value, output = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertEqual(value["issues"], [{"path": "response.json", "code": "signed-jwt", "line": 2}])
        self.assertNotIn(token, output)

    def test_exact_bound_control_original_is_portable_but_other_backups_are_not(self):
        self.write(".control/backups/policy-20260919-pre-codex-supervision.json", "{}")
        self.assertEqual(self.invoke("check")[0], 0)
        self.write(".control/backups/unselected.json", "{}")
        code, value, _ = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertIn({"path": ".control/backups/unselected.json", "code": "local-state"}, value["issues"])

    def test_reviewed_template_and_fixture_allow_only_matching_findings(self):
        token = ("gl" + "pat-" + "z" * 22).encode()
        template = b"TOKEN=\nMODE=development\n"
        self.write("materials/source/.env.example", template)
        self.write("tests/frozen-fixture.txt", token)
        self.reviewed_manifest({"materials/source/.env.example": (template, ["environment-file"]),
                                "tests/frozen-fixture.txt": (token, ["gitlab-token"])})
        self.assertEqual(self.invoke("check")[0], 0)
        self.write("materials/source/.env.example", b"TOKEN=\nMODE=test\n")
        code, value, _ = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertEqual(value["issues"], [{"path": "materials/source/.env.example", "code": "reviewed-file-mismatch"}])

    def test_reviewed_fixture_does_not_suppress_an_unlisted_secret(self):
        token = ("gl" + "pat-" + "z" * 22).encode()
        self.write("tests/frozen-fixture.txt", token)
        self.write("tests/unreviewed-fixture.txt", token)
        self.reviewed_manifest({"tests/frozen-fixture.txt": (token, ["github-token"])})
        code, value, output = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertEqual(value["issues"], [{"path": path, "code": "gitlab-token", "line": 1}
                                           for path in ("tests/frozen-fixture.txt", "tests/unreviewed-fixture.txt")])
        self.assertNotIn(token.decode(), output)

    def test_reviewed_manifest_cannot_admit_real_environment_or_native_state(self):
        paths = ("materials/.env", "materials/worker-local.env", "materials/state.db", "materials/private-session.json",
                 "materials/connections.local.json", "materials/worker-local-connection.json",
                 "materials/local-environment/connection.json", "materials/worker-target/bin/worker")
        for path in paths:
            self.write(path, "private bytes")
        self.reviewed_manifest({path: (b"private bytes", ["gitlab-token"]) for path in paths})
        original_open = os.open

        def guarded_open(path, *args, **kwargs):
            if Path(path).is_relative_to(self.root) and Path(path).relative_to(self.root).as_posix() in paths:
                raise AssertionError("Reviewed manifests must not admit private/native paths")
            return original_open(path, *args, **kwargs)

        with patch.object(os, "open", side_effect=guarded_open):
            code, value, _ = self.invoke("check")
            self.reviewed_manifest({"materials/.env": (b"private bytes", ["environment-file"])})
            forged_code, forged_value, _ = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertEqual({entry["code"] for entry in value["issues"]}, {"environment-file", "local-state"})
        self.assertEqual(forged_code, 1)
        self.assertIn({"path": repository.REVIEWED_FILES_PATH, "code": "reviewed-manifest-invalid"}, forged_value["issues"])

    def test_reviewed_manifest_cannot_exempt_symlinks_or_root_template_policy(self):
        self.write("materials/source.txt", "MODE=development\n")
        (self.root / "materials/.env.example").symlink_to("source.txt")
        self.write(".env.example", "MODE=development\n")
        self.reviewed_manifest({"materials/.env.example": (b"MODE=development\n", ["environment-file"]),
                                ".env.example": (b"MODE=development\n", ["gitlab-token"])})
        code, value, _ = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertEqual(value["issues"], [{"path": ".env.example", "code": "nonempty-environment-template"},
                                           {"path": "materials/.env.example", "code": "symlink"}])

    def test_unindexed_ignored_and_malformed_reviewed_manifests_fail_closed(self):
        self.reviewed_manifest({}, indexed=False)
        code, value, _ = self.invoke("check")
        self.assertEqual(code, 1)
        self.assertEqual(value["issues"], [{"path": repository.REVIEWED_FILES_PATH, "code": "reviewed-manifest-not-indexed"}])
        self.git("add", repository.REVIEWED_FILES_PATH)
        self.write(".gitignore", repository.REVIEWED_FILES_PATH + "\n")
        self.assertNotEqual(self.invoke("check")[0], 0)
        self.write(".gitignore", "")
        for data in ("not JSON", '{"schema":"wrong","files":{}}',
                     json.dumps({"schema": repository.REVIEWED_FILES_SCHEMA, "files": {
                         "materials/.env": {"sha256": "0" * 64, "allow": ["environment-file"], "reason": "forged"}}}),
                     json.dumps({"schema": repository.REVIEWED_FILES_SCHEMA, "files": {
                         "materials/state.db": {"sha256": "0" * 64, "allow": ["local-state"], "reason": "forged"}}})):
            with self.subTest(data=data):
                self.write(repository.REVIEWED_FILES_PATH, data)
                code, value, _ = self.invoke("check")
                self.assertEqual(code, 1)
                self.assertEqual(value["issues"], [{"path": repository.REVIEWED_FILES_PATH, "code": "reviewed-manifest-invalid"}])

    def test_parallel_cli_matches_serial_inspection_with_all_path_guards(self):
        token = ("gl" + "pat-" + "z" * 22).encode()
        template = b"TOKEN=\nMODE=development\n"
        self.write("materials/source/.env.example", template)
        self.write("tests/reviewed-fixture.txt", token)
        self.write("tests/unreviewed-fixture.txt", token)
        self.write("work/.state/run.json", "native bytes must not be read")
        self.write("materials/worker-local.env", "private bytes must not be read")
        for index in range(80):
            self.write(f"materials/source/part-{index:03}.json", "safe retained material")
        (self.root / "materials/file-alias.json").symlink_to("source/part-000.json")
        (self.root / "materials/directory-alias").symlink_to("source", target_is_directory=True)
        self.reviewed_manifest({"materials/source/.env.example": (template, ["environment-file"]),
                                "tests/reviewed-fixture.txt": (token, ["gitlab-token"])})
        self.write("missing.txt", "removed after staging")
        self.git("add", "missing.txt")
        (self.root / "missing.txt").unlink()
        before_index = (self.root / ".git/index").read_bytes()
        code, serial, _ = self.invoke("files")
        completed = subprocess.run(
            [sys.executable, "-B", str(Path(repository.__file__)), "files", "--workers", "2", "--root", str(self.root)],
            capture_output=True, text=True, env=repository.git_environment())
        self.assertEqual(code, 1)
        self.assertEqual(completed.returncode, code, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), serial)
        self.assertEqual((self.root / ".git/index").read_bytes(), before_index)
        self.assertNotIn(token.decode(), completed.stdout)

    def test_worker_count_is_bounded(self):
        for count in ("0", "9"):
            completed = subprocess.run(
                [sys.executable, "-B", str(Path(repository.__file__)), "check", "--workers", count, "--root", str(self.root)],
                capture_output=True, text=True, env=repository.git_environment())
            self.assertEqual(completed.returncode, 2)
            self.assertIn("invalid choice", completed.stderr)

    def test_negated_ignore_rule_keeps_tracked_candidate(self):
        self.git("init", "--quiet")
        self.write(".gitignore", "*.txt\n!keep.txt\n")
        self.write("keep.txt", "retained")
        self.git("add", "keep.txt")
        self.assertEqual(self.invoke("check")[0], 0)

    def test_global_excludes_and_external_git_environment_do_not_change_scope(self):
        self.write("keep.txt", "retained")
        with patch.dict(os.environ, {"GIT_DIR": "/not/the/workspace", "GIT_INDEX_FILE": "/not/an/index"}):
            code, value, _ = self.invoke("files")
        self.assertEqual(code, 0)
        self.assertEqual(value["files"], ["keep.txt"])

    def test_subdirectory_invocation_still_checks_the_entire_selected_root(self):
        self.write("root.txt", "retained root file")
        self.write("nested/child.txt", "retained child file")
        completed = subprocess.run(
            [sys.executable, str(Path(repository.__file__)), "--root", str(self.root), "files"],
            cwd=self.root / "nested", capture_output=True, text=True, check=True,
            env=repository.git_environment())
        self.assertEqual(json.loads(completed.stdout)["files"], ["nested/child.txt", "root.txt"])
        self.assertFalse((self.root / ".git").exists())


if __name__ == "__main__":
    unittest.main()
