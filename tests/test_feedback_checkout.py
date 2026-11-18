"""Integration tests for the add-on's partial clone, with a real filtering Git server."""
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "smartmeter_llm"))
import feedback_checkout as checkout
import nuc_feedback_sync as sync


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def write(root, rel, data):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class FeedbackCheckoutTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.remote = self.root / "remote"
        self.repo = self.root / "feedback-repo"
        self.samples = self.root / "samples"
        self.remote.mkdir()
        self.url = self.remote.as_uri()  # file:// honors --depth and --filter
        git(self.remote, "init", "-q", "-b", "main")
        git(self.remote, "config", "user.name", "test")
        git(self.remote, "config", "user.email", "test@local")
        git(self.remote, "config", "uploadpack.allowFilter", "true")
        write(self.remote, checkout.MODEL, b"original model")
        write(self.remote, "training-data/seg/archive.jpg", os.urandom(2 * 1024 * 1024))
        self.recent_day = date.today().strftime("%Y%m%d")
        self.expired_day = (date.today() - timedelta(days=11)).strftime("%Y%m%d")
        write(self.remote, f"training-data/events/{self.recent_day}/frame", b"recent")
        write(self.remote, f"training-data/events/{self.expired_day}/frame", b"expired")
        git(self.remote, "add", ".")
        git(self.remote, "commit", "-qm", "initial corpus")
        # The configured feedback branch deliberately differs from origin/HEAD.
        git(self.remote, "branch", "feedback")
        self.archive = git(self.remote, "rev-parse", "HEAD:training-data/seg/archive.jpg")

    def prepare(self):
        checkout.prepare(self.repo, self.url, "feedback")

    def run_sync(self):
        subprocess.run([sys.executable, str(ROOT / "smartmeter_llm/nuc_feedback_sync.py"),
                        "--repo", self.repo, "--samples", self.samples, "--push"], check=True)

    def test_migrate_keep_unsent_work_and_recover_from_clone_failure(self):
        git(self.root, "clone", "-q", "--depth=1", "--branch", "feedback", self.url, str(self.repo))
        original = git(self.repo, "rev-parse", "HEAD")
        local = write(self.repo, "training-data/seg/pending.json", b"pending")
        self.prepare()
        self.assertEqual(local.read_bytes(), b"pending")
        git(self.repo, "add", "training-data")
        git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "unsent")
        self.prepare()
        self.assertNotEqual(git(self.repo, "rev-parse", "HEAD"), original)
        self.assertTrue(local.exists())
        git(self.repo, "push", "-q", "origin", "HEAD")

        # A failed fresh checkout (after checking the original is clean) is
        # recoverable. All committed data and the model remain available.
        original_git = checkout.git

        def fail_checkout(repo, *args):
            if args[0] == "checkout":
                raise OSError("injected checkout failure")
            return original_git(repo, *args)

        with patch.object(checkout, "git", side_effect=fail_checkout):
            self.prepare()
        self.assertFalse(local.exists())  # old working tree freed space for the retry
        self.assertEqual(git(self.repo, "show", "HEAD:training-data/seg/pending.json"), "pending")
        self.assertEqual((self.repo / checkout.MODEL).read_bytes(), b"original model")
        self.assertFalse(self.repo.with_suffix(".new").exists())
        self.prepare()
        self.assertFalse((self.repo / "training-data").exists())
        self.assertEqual(git(self.remote, "show", "feedback:training-data/seg/pending.json"), "pending")
        self.assertIn("?" + self.archive, git(self.repo, "rev-list", "--objects", "--all", "--missing=print"))
        self.assertEqual((self.repo / checkout.MODEL).read_bytes(), b"original model")
        index = (self.repo / ".git").stat().st_ino
        self.prepare()
        self.assertEqual((self.repo / ".git").stat().st_ino, index)

    def test_restart_between_renames_and_retention_without_samples(self):
        self.prepare()
        self.repo.rename(self.repo.with_suffix(".retired"))
        self.prepare()
        self.assertTrue((self.repo / checkout.MODEL).exists())
        self.assertFalse(self.repo.with_suffix(".retired").exists())
        self.run_sync()
        tree = git(self.remote, "ls-tree", "-r", "--name-only", "feedback").splitlines()
        self.assertNotIn(f"training-data/events/{self.expired_day}/frame", tree)
        self.assertIn(f"training-data/events/{self.recent_day}/frame", tree)
        self.assertIn("?" + self.archive, git(self.repo, "rev-list", "--objects", "--all", "--missing=print"))

    def test_sparse_sync_retries_retention_and_model(self):
        self.prepare()
        original_main = git(self.remote, "rev-parse", "main")
        seg = write(self.samples, "seg/new.jpg", b"new frame")
        disagreement = write(self.samples, "disagreements/new.json", b"disagree")
        control = write(self.samples, f"control/{self.recent_day}.jsonl", b"first reading\n")
        routine_stem = next(str(i) for i in range(1000) if sync.keep_routine(str(i)))
        routine = write(self.samples, f"{self.recent_day}/{routine_stem}.jpg", b"routine")
        write(self.samples, f"{self.recent_day}/{routine_stem}.json", b'{"kwh":36500,"w":0}')
        event = write(self.samples, f"events/{self.recent_day}/local", b"diagnostic")
        hook = write(self.remote, ".git/hooks/pre-receive", b"#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.run_sync()
        self.assertTrue(all(f.exists() for f in (seg, disagreement, routine, control, event)))
        self.assertEqual(git(self.remote, "rev-parse", "feedback"), original_main)
        hook.unlink()
        git(self.remote, "switch", "feedback")
        write(self.remote, "training-data/seg/upstream.jpg", b"another uploader")
        git(self.remote, "add", "training-data/seg/upstream.jpg")
        git(self.remote, "commit", "-qm", "remote advanced during outage")
        git(self.remote, "switch", "main")
        self.run_sync()
        self.assertEqual(git(self.remote, "show", "feedback:training-data/seg/upstream.jpg"),
                         "another uploader")
        for f in (seg, disagreement, routine, control):
            git(self.remote, "cat-file", "-e", "feedback:training-data/" + str(f.relative_to(self.samples)))
        self.assertTrue(all(not f.exists() for f in (seg, disagreement, routine)))
        self.assertTrue(control.exists())
        self.assertTrue(event.exists())
        self.assertFalse((self.repo / "training-data/seg").exists())
        tree = git(self.remote, "ls-tree", "-r", "--name-only", "feedback").splitlines()
        self.assertNotIn(f"training-data/events/{self.expired_day}/frame", tree)
        self.assertIn(f"training-data/events/{self.recent_day}/frame", tree)
        self.assertNotIn(f"training-data/events/{self.recent_day}/local", tree)
        self.assertEqual(git(self.remote, "rev-parse", "main"), original_main)

        # Simulate upstream retraining on the configured branch.
        git(self.remote, "switch", "feedback")
        write(self.remote, checkout.MODEL, b"retrained")
        git(self.remote, "commit", "-qam", "retrain")
        git(self.remote, "switch", "main")
        control.write_bytes(b"first reading\nsecond reading\n")
        seg.write_bytes(b"different bytes under an already uploaded name")
        self.run_sync()
        self.assertTrue(seg.exists())
        self.assertEqual(git(self.remote, "show", "feedback:training-data/seg/new.jpg"), "new frame")
        self.assertEqual(git(self.remote, "show", f"feedback:training-data/control/{control.name}"),
                         "first reading\nsecond reading")
        self.assertEqual((self.repo / checkout.MODEL).read_bytes(), b"retrained")
        self.assertIn("?" + self.archive, git(self.repo, "rev-list", "--objects", "--all", "--missing=print"))
        # Force the same rotation used when outgoing objects fill the cache.
        git(self.repo, "config", "smartmeter.rotateKiB", "1")
        self.prepare()
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD"), "1")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertTrue(seg.exists())


if __name__ == "__main__":
    unittest.main()
