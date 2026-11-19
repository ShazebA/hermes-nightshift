import subprocess

from nightshift.config import RepoConfig, SandboxConfig
from nightshift.sandbox import run_repro, strip_checkout


def test_checkout_is_split_from_the_container_command():
    assert strip_checkout("git checkout abc && npm ci && npx vitest run a.test.ts") == (
        "abc", "npm ci && npx vitest run a.test.ts")
    assert strip_checkout("make test") == (None, "make test")


def test_repro_checks_out_on_host_and_runs_only_the_rest_in_docker():
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        rc = 1 if argv[0] == "docker" else 0
        return subprocess.CompletedProcess(argv, rc, stdout="FAIL a.test.ts\n", stderr="")

    brief = {"repro": {"command": "git checkout abc && npm ci && npx vitest run a.test.ts"},
             "culprit": {"first_red_sha": "abc"}}
    res = run_repro(brief, RepoConfig(name="o/r", path="/src/r"), SandboxConfig(network="none"), run=fake_run)
    clone, checkout, docker = calls
    assert clone[:2] == ["git", "clone"] and "/src/r" in clone
    assert checkout[-1] == "abc"
    assert docker[:3] == ["docker", "run", "--rm"] and "--network=none" in docker
    assert docker[-1] == "npm ci && npx vitest run a.test.ts"
    assert res["reproduced"] is True and res["exit_code"] == 1
