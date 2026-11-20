"""`nightshift` command line."""

from __future__ import annotations

import argparse
import dataclasses
import json
import shlex
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import __version__
from .brief import (
    CATEGORIES,
    SCHEMA_PATH,
    confidence_label,
    find_brief,
    load_brief,
    validate,
    write_brief,
)
from .config import Config, ConfigError, load_config
from .digest import render_digest
from .gh import FixtureGh, Gh
from .pickup import AlreadyClaimed, AlreadyResolved, claim, open_briefs
from .pipeline import Report, run_once
from .sandbox import run_repro, strip_checkout
from .sinks.aetherbus import broker_reachable, publish_brief
from .state import State, add_lesson, lesson_problems, load_lessons

WAKE_FALSE = '{"wakeAgent": false}'


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gh(args) -> Gh:
    return FixtureGh(args.fixtures) if getattr(args, "fixtures", None) else Gh()


def _scoped(args, cfg: Config) -> Config:
    """`--repo OWNER/REPO`: poll only that configured repo. An unknown repo raises ConfigError."""
    repo = getattr(args, "repo", None)
    return dataclasses.replace(cfg, repos=[cfg.repo(repo)]) if repo else cfg


# ------------------------------------------------------------------ poll / tick


def self_command(cfg: Config) -> str:
    """How the model should invoke Nightshift: absolute interpreter + absolute config, so it works
    from Hermes's `terminal` tool whatever PATH and cwd the gateway has."""
    cmd = [sys.executable, "-m", "nightshift"]
    if cfg.source_path:
        cmd += ["-c", str(cfg.source_path)]
    return shlex.join(cmd)


def _summary(report: Report, ns: str | None = None) -> list[str]:
    lines = []
    for h in report.new_failures:
        c = h.brief["classification"] if h.brief else {}
        lines.append(
            f"- id={h.incident.id} repo={h.incident.repo} "
            f"heuristic={c.get('category')}/{c.get('confidence', 0):.2f} "
            f"brief={h.brief_md} card={h.kanban_task_id or h.kanban} bus={h.aetherbus}")
        lines += [f"    note: {n}" for n in h.notes]
        if ns:
            lines.append(f"    classify: {ns} classify {h.incident.id} --category CATEGORY "
                         f"--confidence NUMBER --rationale \"SENTENCE\"")
            lines.append(f"    learn: {ns} learn {h.incident.repo} --pattern 'REGEX' --note 'FIX' "
                         f"--incident {h.incident.id}")
    notices = [h for h in report.handled if not h.incident.is_failure]
    if notices:
        lines.append("Notices (digest only):")
        lines += [f"- [{h.incident.kind}] {h.incident.repo} {h.incident.title}" for h in notices]
    if report.resolved:
        lines.append("Recovered (no action needed):")
        lines += [f"- id={r.id} repo={r.repo} {r.how} at {r.sha[:7]} card={r.kanban}"
                  for r in report.resolved]
    return lines


def cmd_poll(args, cfg: Config) -> int:
    out_dir = Path(args.briefs_dir).resolve() if args.briefs_dir else None
    if args.dry_run and out_dir is None:
        out_dir = Path("nightshift-dry-run").resolve()
    report = run_once(_scoped(args, cfg), _gh(args), dry_run=args.dry_run, briefs_dir=out_dir)
    if args.json:
        print(json.dumps({
            "new": [{"id": h.incident.id, "repo": h.incident.repo, "kind": h.incident.kind,
                     "brief_json": str(h.brief_json) if h.brief_json else None,
                     "kanban": h.kanban, "kanban_task_id": h.kanban_task_id,
                     "aetherbus": h.aetherbus, "aetherbus_event_id": h.aetherbus_event_id,
                     "notes": h.notes} for h in report.handled],
            "skipped_seen": report.skipped_seen, "errors": report.errors,
            "republished": report.republished,
            "resolved": [{"id": r.id, "repo": r.repo, "how": r.how, "sha": r.sha, "url": r.url,
                          "kanban": r.kanban} for r in report.resolved]}, indent=2))
    else:
        mode = " (dry run: no state, kanban or AetherBus)" if args.dry_run else ""
        print(f"nightshift: {len(report.new_failures)} new failure(s), "
              f"{len(report.handled) - len(report.new_failures)} notice(s), "
              f"{report.skipped_seen} already seen"
              + (f", {len(report.resolved)} recovered" if report.resolved else "") + mode)
        for line in _summary(report):
            print(line)
        for e in report.errors:
            print(f"error: {e}", file=sys.stderr)
    return 1 if report.errors and not report.handled else 0


def cmd_tick(args, cfg: Config) -> int:
    """Output contract for a Hermes cron `--script` job: a tiny summary the triage skill acts
    on, ending with {"wakeAgent": false} when no new failure needs the model."""
    report = run_once(_scoped(args, cfg), _gh(args))
    if report.blind:
        # Non-zero exit makes Hermes deliver a failure notice instead of a silent quiet night.
        print("nightshift: every watcher failed; nothing was observed this run", file=sys.stderr)
        for e in report.errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    if not report.new_failures:
        for e in report.errors:
            print(f"nightshift error: {e}")
        print(WAKE_FALSE)
        return 0
    print(f"NIGHTSHIFT: {len(report.new_failures)} new failure brief(s). For each id below, "
          "follow the nightshift-triage skill. Run the printed commands exactly, replacing only "
          "the CAPITALISED placeholders.")
    for line in _summary(report, self_command(cfg)):
        print(line)
    for e in report.errors:
        print(f"nightshift error: {e}")
    return 0


# ------------------------------------------------------------------ classify / learn


def cmd_classify(args, cfg: Config) -> int:
    path = find_brief(cfg.briefs_dir, args.id)
    b = load_brief(path)
    prev = b["classification"]
    new = {"category": args.category, "confidence": round(args.confidence, 2),
           "label": confidence_label(args.confidence), "source": args.source,
           "rationale": args.rationale.strip()[:400]}
    if prev.get("source") == "heuristic":
        new["heuristic"] = {k: prev[k] for k in ("category", "confidence", "rationale")}
    elif prev.get("heuristic"):
        new["heuristic"] = prev["heuristic"]
    b["classification"] = new
    b["updated_at"] = _now()
    b["handoff"]["prompt"] = ""  # re-derived from the new category
    errs = validate(b)
    if errs:
        print("invalid classification: " + "; ".join(errs), file=sys.stderr)
        return 2
    jp, mp = write_brief(b, path.parent.parent)
    print(f"updated {mp}: {new['category']} ({new['confidence']:.2f}, {new['source']})")
    if cfg.aetherbus.enabled:
        p = publish_brief(cfg.aetherbus, jp, b, cfg.state_dir)
        print(f"aetherbus: {p.status} {p.event_id or p.detail}")
    return 0


def cmd_learn(args, cfg: Config) -> int:
    cfg.repo(args.repo)
    sample = None
    if args.incident:
        b = load_brief(find_brief(cfg.briefs_dir, args.incident))
        w = b["what_broke"]
        sample = "\n".join([*w["log_excerpt"], w["signature"], w.get("error") or ""])
    errors, warnings = lesson_problems(args.pattern, sample)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    if errors:
        print("lesson not saved: " + "; ".join(errors), file=sys.stderr)
        return 2
    le = add_lesson(cfg.state_dir, args.repo, args.pattern, args.note, incident=args.incident)
    print(f"lesson {le.id} saved for {args.repo}: /{le.pattern}/ → {le.note}")
    return 0


def cmd_lessons(args, cfg: Config) -> int:
    repo = cfg.repo(args.repo)
    for le in repo.lessons + load_lessons(cfg.state_dir, args.repo):
        print(f"{le.id}\t{le.source}\t/{le.pattern}/\t{le.note}")
    return 0


# ------------------------------------------------------------------ repro


def cmd_repro(args, cfg: Config) -> int:
    path = find_brief(cfg.briefs_dir, args.id)
    b = load_brief(path)
    repo = cfg.repo(b["repo"])
    sha, inner = strip_checkout(b["repro"]["command"])
    if sha is None:
        # Action steps and very long scripts get a `gh run view` pointer, not a local command.
        # Running it in Docker would fail for an unrelated reason and look "reproduced".
        print(f"nightshift: {b['id']} is not locally reproducible; its repro reads the CI log:\n"
              f"  {b['repro']['command']}", file=sys.stderr)
        return 3
    if not args.execute:
        print(f"would check out {sha or b['culprit']['first_red_sha']} from "
              f"{repo.path or 'GitHub'} and run in {repo.sandbox_image or cfg.sandbox.image} "
              f"(network={cfg.sandbox.network}):\n  {inner}\nre-run with --execute")
        return 0
    if not cfg.sandbox.enabled:
        print("sandbox.enabled is false in the config; refusing to execute repo code",
              file=sys.stderr)
        return 2
    result = run_repro(b, repo, cfg.sandbox)
    b["repro"]["sandbox"] = result
    b["repro"]["verified"] = bool(result["reproduced"])
    b["updated_at"] = _now()
    write_brief(b, path.parent.parent)
    print(f"exit={result['exit_code']} reproduced={result['reproduced']}")
    print("\n".join(result["output_tail"][-15:]))
    return 0


# ------------------------------------------------------------------ next / claim


def cmd_next(args, cfg: Config) -> int:
    if args.repo:
        cfg.repo(args.repo)
    rows = open_briefs(cfg.briefs_dir, args.repo)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("nightshift: no open briefs")
        return 0
    for r in rows:
        print(f"{r['id']}\t{r['repo']}\t{r['kind']}\t{r['category']}/{r['confidence']:.2f}\t"
              f"{r['created_at']}\t{r['title']}")
        print(f"    brief: {r['brief_md']}")
    return 0


def cmd_claim(args, cfg: Config) -> int:
    try:
        res = claim(cfg, args.id, args.agent)
    except (AlreadyClaimed, AlreadyResolved) as e:
        print(f"nightshift: {e}", file=sys.stderr)
        return 1
    h = res.brief["handoff"]
    if args.json:
        print(json.dumps({"id": res.brief["id"], "repo": res.brief["repo"],
                          "claimed_by": h["claimed_by"], "claimed_at": h["claimed_at"],
                          "already_yours": res.already_yours, "brief_json": str(res.brief_json),
                          "brief_md": str(res.brief_md), "repro": res.brief["repro"]["command"],
                          "kanban": res.kanban, "kanban_task_id": h.get("kanban_task_id"),
                          "notes": res.notes}, indent=2))
    else:
        verb = "already claimed by you" if res.already_yours else "claimed"
        print(f"{res.brief['id']} {verb}: {h['claimed_by']} at {h['claimed_at']}")
        print(f"brief: {res.brief_md}")
        print(f"repro: {res.brief['repro']['command']}")
        print(f"kanban: {res.kanban} {h.get('kanban_task_id') or ''}".rstrip())
    for n in res.notes:
        print(f"nightshift: {n}", file=sys.stderr)
    return 0


# ------------------------------------------------------------------ digest


def cmd_digest(args, cfg: Config) -> int:
    state = State(cfg.state_dir)
    until = _now()
    size = state.ledger_size()
    if args.hours or args.since:
        since = args.since or (datetime.now(UTC) - timedelta(hours=args.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = state.read_ledger(since)
    else:
        since = state.last_digest
        rows = state.read_ledger(offset=min(state.digest_offset, size))
    for r in rows:  # pick up model/human reclassification since the ledger row was written
        if r.get("brief_json") and Path(r["brief_json"]).is_file():
            b = load_brief(Path(r["brief_json"]))
            r["classification"] = b["classification"]
            r["resolution"] = b.get("resolution")
    text = render_digest(rows, since, until)
    out = cfg.state_dir / "digests" / f"{until[:10]}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(text, end="")
    if not args.no_mark:
        state.last_digest, state.digest_offset = until, size
        state.save()
    return 0


# ------------------------------------------------------------------ brief / doctor


def cmd_brief(args, cfg: Config) -> int:
    if args.brief_action == "validate":
        b = json.loads(Path(args.target).read_text())
        errs = validate(b)
        print("ok" if not errs else "\n".join(errs))
        return 0 if not errs else 1
    path = find_brief(cfg.briefs_dir, args.target)
    print(path.read_text() if args.json else path.with_suffix(".md").read_text(), end="")
    return 0


def cmd_doctor(args, cfg: Config) -> int:
    print(f"config: {cfg.source_path}")
    checks = []
    gh = shutil.which("gh")
    checks.append(("gh on PATH", bool(gh), gh or "install https://cli.github.com"))
    if gh:
        p = subprocess.run([gh, "auth", "status"], capture_output=True, text=True)
        checks.append(("gh authenticated", p.returncode == 0, "run `gh auth login`"))
    hb = shutil.which(cfg.kanban.hermes_bin)
    checks.append(("hermes CLI (kanban sink)", bool(hb) or not cfg.kanban.enabled,
                   hb or "kanban.enabled is true but hermes is not on PATH"))
    if cfg.aetherbus.enabled:
        try:
            import aetherbus  # noqa: F401
            ok = True
        except ImportError:
            ok = False
        checks.append(("aetherbus importable", ok, "pip install -e /path/to/aetherbus"))
        checks.append(("AetherBus broker reachable", broker_reachable(cfg.aetherbus.url),
                       cfg.aetherbus.url))
    if cfg.sandbox.enabled:
        p = subprocess.run(["docker", "info"], capture_output=True, text=True) if shutil.which(
            "docker") else None
        checks.append(("docker daemon", bool(p and p.returncode == 0), "start Docker"))
    checks.append(("briefs dir writable", _writable(cfg.briefs_dir), str(cfg.briefs_dir)))
    if hb and not args.no_hermes:
        from .hermes_check import hermes_checks
        checks += hermes_checks(hb)
    for name, ok, hint in checks:
        print(f"[{'ok' if ok else '!!'}] {name}" + ("" if ok else f"  ({hint})"))
    return 0 if all(ok for _, ok, _ in checks) else 1


def _writable(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        t = p / ".probe"
        t.write_text("x")
        t.unlink()
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nightshift", description=__doc__)
    p.add_argument("--version", action="version", version=f"nightshift {__version__}")
    p.add_argument("-c", "--config", help="config file (default: ./nightshift.yaml, "
                   "$NIGHTSHIFT_CONFIG, ~/.config/nightshift/nightshift.yaml)")
    sub = p.add_subparsers(dest="command", required=True)

    poll = sub.add_parser("poll", help="poll repos, triage new failures, write briefs, hand off")
    poll.add_argument("--dry-run", action="store_true",
                      help="write briefs only (default ./nightshift-dry-run); no state or sinks")
    poll.add_argument("--fixtures", help="replay recorded gh output from this directory")
    poll.add_argument("--briefs-dir", help="override briefs_dir")
    poll.add_argument("--json", action="store_true")
    poll.add_argument("--repo", metavar="OWNER/REPO", help="poll only this configured repo")
    poll.set_defaults(func=cmd_poll)

    tick = sub.add_parser("tick", help="poll, formatted for a Hermes cron --script job")
    tick.add_argument("--fixtures", help=argparse.SUPPRESS)
    tick.add_argument("--repo", metavar="OWNER/REPO", help="poll only this configured repo")
    tick.set_defaults(func=cmd_tick)

    cl = sub.add_parser("classify", help="record a model/human classification on a brief")
    cl.add_argument("id")
    cl.add_argument("--category", required=True, choices=CATEGORIES)
    cl.add_argument("--confidence", required=True, type=float)
    cl.add_argument("--rationale", required=True)
    cl.add_argument("--source", default="model", choices=("model", "human"))
    cl.set_defaults(func=cmd_classify)

    le = sub.add_parser("learn", help="save a repo-specific lesson used by future briefs")
    le.add_argument("repo")
    le.add_argument("--pattern", required=True, help="regex matched against the failing log")
    le.add_argument("--note", required=True, help="what it means / how to fix it")
    le.add_argument("--incident", help="incident id the lesson came from")
    le.set_defaults(func=cmd_learn)

    ls = sub.add_parser("lessons", help="list lessons for a repo")
    ls.add_argument("repo")
    ls.set_defaults(func=cmd_lessons)

    rp = sub.add_parser("repro", help="re-run a brief's repro command in a Docker sandbox")
    rp.add_argument("id")
    rp.add_argument("--execute", action="store_true", help="actually run it (needs sandbox.enabled)")
    rp.set_defaults(func=cmd_repro)

    nx = sub.add_parser("next", help="list unclaimed briefs, newest first")
    nx.add_argument("--repo", help="only this OWNER/REPO")
    nx.add_argument("--json", action="store_true")
    nx.set_defaults(func=cmd_next)

    ca = sub.add_parser("claim", help="claim a brief for a coding agent (and unblock its card)")
    ca.add_argument("id")
    ca.add_argument("--agent", required=True, help="who is fixing it, e.g. claude-code")
    ca.add_argument("--json", action="store_true")
    ca.set_defaults(func=cmd_claim)

    dg = sub.add_parser("digest", help="summarise incidents since the last digest")
    dg.add_argument("--since", help="ISO timestamp (default: last digest)")
    dg.add_argument("--hours", type=float, help="look back N hours instead")
    dg.add_argument("--no-mark", action="store_true", help="don't advance the last-digest marker")
    dg.set_defaults(func=cmd_digest)

    br = sub.add_parser("brief", help="show or validate briefs")
    br.add_argument("brief_action", choices=("show", "validate"))
    br.add_argument("target", help="brief id (show) or path to a brief .json (validate)")
    br.add_argument("--json", action="store_true")
    br.set_defaults(func=cmd_brief)

    sub.add_parser("schema", help="print the path of the brief JSON Schema").set_defaults(
        func=lambda a, c: print(SCHEMA_PATH) or 0, needs_config=False)

    dr = sub.add_parser("doctor", help="check gh, AetherBus, docker and the Hermes install "
                        "(gateway, cron jobs, skill, model server)")
    dr.add_argument("--no-hermes", action="store_true", help="skip the Hermes install checks")
    dr.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "needs_config", True) is False:
        return args.func(args, None)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    try:
        return args.func(args, cfg)
    except (ConfigError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
