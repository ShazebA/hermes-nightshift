# From polling to webhooks

Polling is the default because it needs no public endpoint. A poll costs roughly 6 to 8 `gh` calls per repo (one per watcher, plus one deployment-status call per environment walked), and 2 more per new failure. That is well inside GitHub's rate limit for a handful of repos every 15 minutes.

Hermes already has the primitive for switching: a webhook route can **fire an existing cron job** (`hermes webhook subscribe --cron-job`, see `hermes_cli/subcommands/webhook.py`). The webhook becomes a faster trigger for the same `nightshift-watch` job. The pipeline, dedupe and skill are unchanged, so webhook mode cannot drift from poll mode.

## Setup

1. Run `scripts/install-hermes.sh --apply`. Along with the cron scripts, it installs the route filter as `~/.hermes/scripts/nightshift-webhook-filter.py`. Create the `nightshift-watch` cron job first: `webhook subscribe --cron-job` looks the job up when you run it and fails if the job doesn't exist yet.

2. Expose the Hermes webhook gateway publicly (a tunnel or reverse proxy), then add the route. The installer prints this command; it never runs it:

   ```bash
   hermes webhook subscribe nightshift \
       --events workflow_run,deployment_status \
       --cron-job nightshift-watch \
       --script nightshift-webhook-filter.py \
       --description "Nightshift: run triage when GitHub reports a failed run or deploy"
   ```

3. In the GitHub repo settings, add a webhook pointing at `https://<your-host>/webhooks/nightshift`, using the HMAC secret Hermes prints. Select the *Workflow runs* and *Deployment statuses* events.

4. Slow the poll down instead of removing it, so a missed or filtered delivery is still caught:

   ```bash
   hermes cron edit <nightshift-watch-id> --schedule "every 2h"
   ```

The webhook payload is only a trigger. `nightshift tick` re-reads state through `gh`, so signature handling, ordering and retries stay Hermes's concern.

## The route filter

GitHub sends `workflow_run` on `requested`, `in_progress` and `completed`, and `deployment_status` for every state change. Most of those are not failures, and each one would otherwise cost a full tick. Hermes runs the `--script` with its own Python interpreter and passes the payload as JSON on stdin. It drops the event when the script prints nothing, prints `[SILENT]`, or exits non-zero.

The filter keeps only these events:

| Event | Kept when |
| --- | --- |
| `workflow_run` | `action` is `completed` and `conclusion` is `failure`, `timed_out` or `startup_failure` |
| `deployment_status` | `deployment_status.state` is `failure` or `error` |

Everything else is dropped, including payloads it can't parse or doesn't recognise. The poll catches anything the filter misses.

The filter can't read the Nightshift config, so it doesn't filter by branch. A failed run on a PR branch still fires the job. The tick only watches configured branches, so it then ends with `{"wakeAgent": false}`. That costs a few `gh` calls but no model run.

- **Event type.** Hermes does not pass the `X-GitHub-Event` header to the script, so the filter works out the event from the payload. It checks `deployment_status` first, because GitHub embeds the deploying `workflow_run` inside deployment-status payloads for Actions-driven deploys.
- **Output.** For a kept event, the filter prints a small JSON object: event, repo, conclusion or state, workflow or environment, branch, sha and URL. That object replaces the payload Hermes hands to the job as per-run context. Commit messages, author emails and other free text from GitHub never reach the model prompt or the cron prompt-injection scanner. The tick reads what it needs through `gh` anyway.
- **Dependencies.** The filter uses only the standard library, because it runs inside Hermes's interpreter, not Nightshift's.

The tests in `tests/test_webhook_filter.py` run it the same way, with `python -I -S` and no site-packages, against synthetic payloads in `tests/fixtures/webhooks/`.

## Scoping a tick to one repo

`nightshift tick --repo OWNER/REPO` and `nightshift poll --repo OWNER/REPO` poll only that configured repo. An unknown repo exits 2 and lists the configured ones.

**A webhook-fired job still polls every repo.** Hermes passes the rendered event only to the model, as transient prompt context. The job's pre-run script (`nightshift-watch.sh`, which runs `tick`) gets no arguments or environment from the event (`cron/scheduler.py`, `cron/scheduler_script.py`), so the route cannot forward `--repo`. In practice a kept event is already a real failure, and a full tick of a few repos costs only a few dozen `gh` calls.

If one busy repo should not trigger polls of the others, give it its own job and route. Copy `nightshift-watch.sh` to `nightshift-watch-webapp.sh`, add `--repo acme/webapp` to its `tick` line, then:

```bash
hermes cron create "every 2h" "Follow the nightshift-triage skill for each brief listed above." \
    --name nightshift-watch-webapp --script nightshift-watch-webapp.sh --skill nightshift-triage --deliver local
hermes webhook subscribe nightshift-webapp --events workflow_run,deployment_status \
    --cron-job nightshift-watch-webapp --script nightshift-webhook-filter.py
```

Then point that repo's GitHub webhook at `/webhooks/nightshift-webapp`. Keep the repo in the shared config, because `--repo` only accepts configured repos. The main job's slow poll then acts as its backstop. Both jobs share one dedupe state, so a failure that one job has already briefed is skipped by the other. The exception is when both run at the same moment, so keep the main poll slow.
