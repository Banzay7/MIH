"""`mihctl.sh update`: the stand follows main by itself, only after CI, and comes back on failure.

Runs the real script against a throw-away git origin; docker and the deploy step are stubs.
"""

import http.server
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "deploy" / "vps-test" / "mihctl.sh"

pytestmark = pytest.mark.skipif(
    not (shutil.which("git") and shutil.which("flock") and shutil.which("bash")),
    reason="needs git, flock and bash",
)


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}  # fmt: skip
    out = subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)
    return out.stdout.decode().strip()


class Stand:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.work = tmp / "work"
        self.clone = tmp / "stand"
        git(tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(tmp, "clone", "-q", str(self.origin), str(self.work))
        (self.work / "deploy" / "vps-test").mkdir(parents=True)
        shutil.copy(SCRIPT, self.work / "deploy" / "vps-test" / "mihctl.sh")
        (self.work / "deploy" / "vps-test" / ".gitignore").write_text("state/\n.env\n")
        self.commit("first")
        git(tmp, "clone", "-q", str(self.origin), str(self.clone))
        self.here = self.clone / "deploy" / "vps-test"
        self.state = self.here / "state"
        self.env = {"MIH_UPDATE_REQUIRE_CI": "0"}
        # Stubs: docker says the container does not exist (no pushes); deploy and rollback
        # record that they ran and what image is current.
        bin_ = tmp / "bin"
        bin_.mkdir()
        (bin_ / "docker").write_text("#!/bin/sh\nexit 1\n")
        self.deploy = tmp / "deploy.sh"
        self.deploy_ok(True)
        rollback = tmp / "rollback.sh"
        rollback.write_text(f"#!/bin/sh\necho rollback >> {tmp}/calls\n")
        for f in (bin_ / "docker", rollback):
            f.chmod(0o755)
        self.path = f"{bin_}:{os.environ['PATH']}"
        self.rollback = rollback

    def deploy_ok(self, ok: bool) -> None:
        state = self.tmp / "stand" / "deploy" / "vps-test" / "state"
        self.deploy.write_text(
            "#!/bin/sh\n"
            f"echo deploy-$(git -C {self.tmp}/stand rev-parse --short HEAD) >> {self.tmp}/calls\n"
            f"echo previous > {state}/previous_image\n"
            f"echo new-$(git -C {self.tmp}/stand rev-parse --short HEAD) > {state}/current_image\n"
            f"exit {0 if ok else 1}\n"
        )
        self.deploy.chmod(0o755)

    def commit(self, name: str) -> str:
        (self.work / f"{name}.txt").write_text(name)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", name)
        git(self.work, "push", "-q", "-f", "origin", "HEAD:main")
        return git(self.work, "rev-parse", "HEAD")

    def head(self) -> str:
        return git(self.clone, "rev-parse", "HEAD")

    def calls(self) -> list[str]:
        f = self.tmp / "calls"
        return f.read_text().split() if f.exists() else []

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        (self.here / ".env").write_text("".join(f"{k}={v}\n" for k, v in self.env.items()))
        full = {**os.environ, "PATH": self.path, "MIH_UPDATE_DEPLOY": str(self.deploy),
                "MIH_UPDATE_ROLLBACK": str(self.rollback), **env}  # fmt: skip
        return subprocess.run(["bash", str(self.here / "mihctl.sh"), *args], env=full,
                              capture_output=True, text=True)  # fmt: skip


@pytest.fixture
def stand(tmp_path: Path) -> Stand:
    return Stand(tmp_path)


def test_up_to_date_does_nothing(stand):
    r = stand.run("update")
    assert r.returncode == 0, r.stderr
    assert "up to date" in r.stdout
    assert stand.calls() == []


def test_new_commit_is_deployed(stand):
    new = stand.commit("second")
    r = stand.run("update", "--auto")
    assert r.returncode == 0, r.stderr
    assert stand.head() == new
    assert stand.calls() == [f"deploy-{new[:7]}"]
    assert f"update {new[:12]}" in (stand.state / "history").read_text()


def test_rewritten_history_is_followed(stand):
    stand.commit("second")
    stand.run("update")
    git(stand.work, "reset", "-q", "--hard", "HEAD~1")
    rewritten = stand.commit("second-rewritten")
    r = stand.run("update")
    assert r.returncode == 0, r.stderr
    assert stand.head() == rewritten


def test_failed_start_goes_back_and_skips_that_commit(stand):
    old = stand.head()
    stand.deploy_ok(False)
    bad = stand.commit("broken")
    r = stand.run("update", "--auto")
    assert r.returncode == 1
    assert stand.head() == old
    assert stand.calls() == [f"deploy-{bad[:7]}", "rollback"]
    assert (stand.state / "bad_commit").read_text().strip() == bad
    r = stand.run("update", "--auto")
    assert "skipping" in r.stdout
    assert len(stand.calls()) == 2
    stand.deploy_ok(True)
    good = stand.commit("fixed")
    assert stand.run("update", "--auto").returncode == 0
    assert stand.head() == good
    assert not (stand.state / "bad_commit").exists()


def test_failed_build_does_not_roll_back(stand):
    stand.deploy.write_text(f"#!/bin/sh\necho deploy >> {stand.tmp}/calls\nexit 1\n")
    stand.commit("does-not-build")
    assert stand.run("update").returncode == 1
    assert stand.calls() == ["deploy"]


def test_local_changes_are_not_overwritten(stand):
    (stand.clone / "first.txt").write_text("edited on the server")
    stand.commit("second")
    r = stand.run("update")
    assert r.returncode != 0
    assert "local changes" in r.stderr
    assert (stand.clone / "first.txt").read_text() == "edited on the server"


@pytest.fixture
def github():
    answer = {"body": {}}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            data = json.dumps(answer["body"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield answer, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.mark.parametrize(
    ("runs", "deployed"),
    [
        ([], False),
        ([{"status": "in_progress", "conclusion": None}], False),
        ([{"status": "completed", "conclusion": "failure"}], False),
        (
            [
                {"status": "completed", "conclusion": "success"},
                {"status": "completed", "conclusion": "skipped"},
            ],
            True,
        ),  # fmt: skip
    ],
)
def test_waits_for_green_ci(stand, github, runs, deployed):
    answer, url = github
    answer["body"] = {"check_runs": runs}
    stand.env = {"MIH_UPDATE_REPO": "owner/repo"}
    old = stand.head()
    new = stand.commit("second")
    r = stand.run("update", "--auto", MIH_GITHUB_API=url)
    assert r.returncode == 0, r.stderr
    assert stand.head() == (new if deployed else old)
