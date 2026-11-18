"""Prepare the add-on's private, model-only Git checkout under /data."""
import shutil
import subprocess
import sys
from pathlib import Path


MODEL = "scripts/ocr/model.npz"
# Git keeps locally produced evidence blobs even after their successful push.
MIN_ROTATE_KIB = 256 * 1024


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True,
                          stdout=subprocess.PIPE).stdout.strip()


def object_kib(repo):
    counts = dict(line.split(": ", 1) for line in git(repo, "count-objects", "-v").splitlines())
    return int(counts["size"]) + int(counts["size-pack"])


def prepare(repo: Path, url: str, branch: str):
    fresh = repo.with_name(repo.name + ".new")
    retired = repo.with_name(repo.name + ".retired")
    # A restart between the two renames leaves the old, working clone here.
    if retired.exists():
        if repo.exists():
            shutil.rmtree(retired)
        else:
            retired.rename(repo)

    replacing = repo.exists()
    if replacing:
        origin = git(repo, "config", "--get", "remote.origin.url")
        if origin != url or git(repo, "branch", "--show-current") != branch:
            raise ValueError(f"{repo}: origin/branch differ from the configured feedback target")
        limit = int(git(repo, "config", "--int", "--default", "0", "--get", "smartmeter.rotateKiB"))
        if limit and object_kib(repo) < limit:
            return
        # Let the regular sync finish interrupted uploads before replacing this
        # clone. Include ignored files, local branches, tags and the stash.
        if git(repo, "status", "--porcelain", "--ignored", "--untracked-files=all"):
            print("Feedback checkout has local files/changes; deferring replacement", flush=True)
            return
        git(repo, "fetch", "--filter=blob:none", "--no-tags", "origin", branch)
        if (git(repo, "rev-list", "-1", "HEAD", "--not", "FETCH_HEAD") or
                git(repo, "rev-list", "-1", "--all", "--not", "FETCH_HEAD", "--remotes=origin")):
            print("Feedback checkout has local commits; deferring replacement", flush=True)
            return
        # Release the old worktree's disk blocks before allocating a new
        # object store. Its index, object database and model remain usable.
        git(repo, "sparse-checkout", "set", "--no-cone", "/" + MODEL)

    # Never remove the usable checkout to make room for an unverified clone.
    if fresh.exists():
        shutil.rmtree(fresh)
    try:
        git(repo.parent, "clone", "--no-checkout", "--filter=blob:none", "--depth=1",
            "--single-branch", "--no-tags", "--branch", branch, "--", url, str(fresh))
        git(fresh, "sparse-checkout", "set", "--no-cone", "/" + MODEL)
        git(fresh, "checkout", branch)
        git(fresh, "config", "user.name", "smartmeter-ha")
        git(fresh, "config", "user.email", "smartmeter-ha@local")
        # Rename detection can otherwise lazy-fetch deleted event images.
        git(fresh, "config", "diff.renames", "false")
        git(fresh, "config", "smartmeter.rotateKiB", str(max(MIN_ROTATE_KIB, 2 * object_kib(fresh))))
        if replacing:
            repo.rename(retired)
        fresh.rename(repo)
    except (OSError, subprocess.CalledProcessError):
        if not replacing:
            raise
        print("Could not replace the feedback checkout; retrying with the existing clone",
              file=sys.stderr, flush=True)
        return
    finally:
        if retired.exists() and not repo.exists():
            retired.rename(repo)
        if fresh.exists():
            shutil.rmtree(fresh)
    if retired.exists():
        shutil.rmtree(retired)
    print(f"Feedback checkout ready at {repo} (sparse, model only)", flush=True)


if __name__ == "__main__":
    try:
        prepare(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
    except subprocess.CalledProcessError as exc:
        # Git already wrote the diagnostic. Its full argv can contain the URL.
        sys.exit(f"Feedback checkout: git {exc.cmd[1]} failed ({exc.returncode})")
