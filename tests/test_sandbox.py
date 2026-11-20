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


def test_log_only_repro_is_never_run_or_marked_reproduced(tmp_path, capsys):
    """Action steps get `gh run view ... --log-failed`; running that in Docker would fail for
    an unrelated reason and be recorded as reproduced."""
    import json

    import pytest

    from nightshift import cli
    calls = []
    brief = {"repro": {"command": "gh run view 1 -R o/r --log-failed  # action step"},
             "culprit": {"first_red_sha": "abc"}}
    with pytest.raises(ValueError, match="not a local repro"):
        run_repro(brief, RepoConfig(name="o/r"), SandboxConfig(), run=lambda a, **k: calls.append(a))
    assert calls == []

    from conftest import RED_MAIN
    cfgp = tmp_path / "n.yaml"
    cfgp.write_text(f"state_dir: {tmp_path / 's'}\nrepos:\n  - name: acme/webapp\nkanban:\n  enabled: false\n"
                    "sandbox:\n  enabled: true\n")
    cli.main(["-c", str(cfgp), "poll", "--fixtures", str(RED_MAIN)])
    path = next((tmp_path / "s").rglob("ci-ci-main-9002.json"))
    b = json.loads(path.read_text())
    b["repro"]["command"] = brief["repro"]["command"]
    path.write_text(json.dumps(b))
    capsys.readouterr()
    assert cli.main(["-c", str(cfgp), "repro", "ci-ci-main-9002", "--execute"]) == 3
    assert "not locally reproducible" in capsys.readouterr().err
    assert json.loads(path.read_text())["repro"]["sandbox"] is None
