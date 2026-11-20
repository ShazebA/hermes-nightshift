import subprocess

from nightshift.hermes_check import hermes_checks

RUNNING = "✓ Gateway running (pid 123)\n"
STOPPED = "✗ Gateway is not running — cron jobs will NOT fire\n"
LISTING = "  abc [active]\n    Name:      nightshift-watch\n  def [active]\n    Name:      nightshift-digest\n"


def fake(status, listing):
    def run(argv, **kw):
        out = status if argv[1:] == ["cron", "status"] else listing
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
    return run


def home(tmp_path, base_url="http://127.0.0.1:1/v1", skill=True):
    (tmp_path / "config.yaml").write_text(f"model:\n  base_url: {base_url}\n")
    if skill:
        d = tmp_path / "skills" / "devops" / "nightshift-triage"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("x")
    return tmp_path


def as_dict(checks):
    return {name.split(" (")[0]: ok for name, ok, _ in checks}


def test_healthy_install(tmp_path):
    c = as_dict(hermes_checks(home=home(tmp_path), run=fake(RUNNING, LISTING), probe=lambda u: True))
    assert all(c.values()) and len(c) == 5


def test_stopped_gateway_missing_job_skill_and_model_are_each_reported(tmp_path):
    c = as_dict(hermes_checks(home=home(tmp_path, skill=False),
                              run=fake(STOPPED, "    Name:      nightshift-watch\n"), probe=lambda u: False))
    assert c["Hermes scheduler running"] is False
    assert c["cron job nightshift-watch registered"] is True
    assert c["cron job nightshift-digest registered"] is False
    assert c["skill nightshift-triage installed"] is False
    assert c["model server reachable"] is False


def test_unanswered_port_is_down_and_http_error_is_up():
    import http.server
    import threading

    from nightshift.hermes_check import model_server_up

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    assert model_server_up(f"http://127.0.0.1:{srv.server_port}/v1") is True  # 401 = server is up
    srv.server_close()
    assert model_server_up(f"http://127.0.0.1:{srv.server_port}/v1", timeout=1) is False
