"""Cut an Arcade Nodes release: build and sign on the machine that holds the key, publish from one that can push.

Arcades install a release only when its manifest is signed by the key pinned in arcade/release.py, so the key stays
on one machine (the workhorse, 2026-11-13) and never goes to GitHub. Two steps:

1. On the key's machine, from a clone of github.com/sadfrogltc/arcade-nodes:

       python tools/release.py build --commit <sha> --channel live --key ~/.arcade-release/release.key --out DIR

   writes DIR/source.tar.gz (or source-test.tar.gz), its .sha256, source.rev and the signed manifest. It refuses a
   commit whose own arcade/release.py pins another key: such a release would be the last one its nodes accept.

2. On a machine with `gh` signed in, the DIR copied over:

       python tools/release.py publish --dir DIR --channel live

   checks the manifest against the pinned key again, moves `master` (or `test`) to the signed commit (fast-forward
   only) and puts the files on a GitHub release: a new one per live release (the newest is `latest`), the pre-release
   `channel-test` replaced for the test channel (arcade/update.py reads both).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from arcade import release as releaselib          # noqa: E402
from arcade import update as updatelib            # noqa: E402

REPO = "sadfrogltc/arcade-nodes"


def names(channel: str) -> dict[str, str]:
    stem = "source-test" if channel == "test" else "source"
    return {"archive": f"{stem}.tar.gz", "sums": f"{stem}.tar.gz.sha256", "rev": f"{stem}.rev",
            "manifest": f"{stem}.manifest.json"}


def git(*args: str, cwd: Path = ROOT, data: bytes | None = None) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, input=data, capture_output=True, check=True).stdout


def pinned_key(commit: str) -> str:
    """The PUBLIC_KEY the commit's own arcade/release.py pins."""
    for line in git("show", f"{commit}:arcade/release.py").decode().splitlines():
        if line.startswith("PUBLIC_KEY = "):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit(f"{commit[:7]}'s arcade/release.py pins no PUBLIC_KEY")


def build(commit: str, channel: str, key_path: Path, out: Path) -> dict:
    commit = git("rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    secret = key_path.read_text().strip()
    from nacl.signing import SigningKey
    ours = SigningKey(bytes.fromhex(secret)).verify_key.encode().hex()
    if pinned_key(commit) != ours:
        raise SystemExit(f"{commit[:7]} pins {pinned_key(commit)[:12]}..., not this key ({ours[:12]}...): "
                         "its nodes would accept no later release")
    n = names(channel)
    out.mkdir(parents=True, exist_ok=True)
    tar = git("archive", "--format=tar", f"--prefix=arcade-nodes-{commit[:7]}/", commit)
    archive = subprocess.run(["gzip", "-9n"], input=tar, capture_output=True, check=True).stdout
    sha = hashlib.sha256(archive).hexdigest()
    (out / n["archive"]).write_bytes(archive)
    (out / n["sums"]).write_text(f"{sha}  {n['archive']}\n")
    (out / n["rev"]).write_text(commit + "\n")
    manifest = releaselib.sign(secret, commit, sha, time.time())
    releaselib.verify(manifest, ours)
    (out / n["manifest"]).write_text(json.dumps(manifest, sort_keys=True))
    print(f"built {channel} release {commit[:7]}: {len(archive) / 1e6:.1f} MB, sha256 {sha[:16]}..., in {out}")
    return manifest


def publish(folder: Path, channel: str, remote: str) -> None:
    n = names(channel)
    manifest = releaselib.verify((folder / n["manifest"]).read_text())       # the key this checkout pins
    commit = str(manifest["revision"])
    archive = (folder / n["archive"]).read_bytes()
    if hashlib.sha256(archive).hexdigest() != manifest["sha256"]:
        raise SystemExit("the archive is not the one the manifest signs")
    if (folder / n["rev"]).read_text().strip() != commit:
        raise SystemExit("the revision file names another commit than the manifest")
    if pinned_key(commit) != releaselib.PUBLIC_KEY:
        raise SystemExit(f"{commit[:7]} pins another key than this checkout")
    branch = updatelib.TEST_BRANCH if channel == "test" else "master"
    git("push", remote, f"{commit}:refs/heads/{branch}")                      # fast-forward only: no --force
    files = [str(folder / n[k]) for k in ("archive", "sums", "rev", "manifest")]
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(manifest["published"])))
    if channel == "test":
        tag = updatelib.TEST_RELEASE_TAG
        if subprocess.run(["gh", "release", "view", tag, "-R", REPO], capture_output=True).returncode != 0:
            subprocess.run(["gh", "release", "create", tag, "-R", REPO, "--prerelease", "--target", commit,
                            "--title", "Test channel", "--notes", "Replaced at every test release."], check=True)
        subprocess.run(["gh", "release", "upload", tag, "-R", REPO, "--clobber", *files], check=True)
        subprocess.run(["gh", "release", "edit", tag, "-R", REPO, "--prerelease", "--target", commit,
                        "--notes", f"Test channel: {commit[:7]}, signed {when}. Replaced at every test release."],
                       check=True)
    else:
        tag = "r" + time.strftime("%Y.%m.%d-%H%M", time.gmtime(float(manifest["published"])))
        subprocess.run(["gh", "release", "create", tag, "-R", REPO, "--latest", "--target", commit,
                        "--title", f"Release {tag[1:]} ({commit[:7]})",
                        "--notes", f"Signed {when} with the key pinned in arcade/release.py. Arcades install it at "
                                   "their next check.", *files], check=True)
    print(f"published {channel} {commit[:7]} as {tag}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="step", required=True)
    b = sub.add_parser("build")
    b.add_argument("--commit", required=True)
    b.add_argument("--channel", choices=("live", "test"), default="live")
    b.add_argument("--key", type=Path, required=True)
    b.add_argument("--out", type=Path, required=True)
    u = sub.add_parser("publish")
    u.add_argument("--dir", type=Path, required=True)
    u.add_argument("--channel", choices=("live", "test"), default="live")
    u.add_argument("--remote", default=f"https://github.com/{REPO}.git")
    a = p.parse_args(argv)
    if a.step == "build":
        build(a.commit, a.channel, a.key.expanduser(), a.out)
    else:
        publish(a.dir, a.channel, a.remote)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
