# From polling to webhooks

Polling is the default because it needs no public endpoint. A poll costs roughly 6 to 8 `gh` calls per repo (one per watcher, plus one deployment-status call per environment walked), and 2 more per new failure. That is well inside GitHub's rate limit for a handful of repos every 15 minutes.

Hermes already has the primitive for switching: a webhook route can **fire an existing cron job** (`hermes webhook subscribe --cron-job`, confirmed in `hermes_cli/subcommands/webhook.py`). The webhook becomes a faster trigger for the same `nightshift-watch` job. The pipeline, dedupe and skill are unchanged, so webhook mode cannot drift from poll mode.

1. Expose the Hermes webhook gateway publicly (a tunnel or reverse proxy), then add a route:

   ```bash
   hermes webhook subscribe nightshift \
       --events workflow_run,deployment_status \
       --cron-job nightshift-watch \
       --description "Nightshift: run triage when GitHub reports a finished run or deploy"
   ```

2. In the GitHub repo settings, add a webhook pointing at `https://<your-host>/webhooks/nightshift`, using the HMAC secret Hermes prints. Select the *Workflow runs* and *Deployment statuses* events.

3. Slow the poll down instead of removing it, so a missed delivery is still caught:

   ```bash
   hermes cron edit <nightshift-watch-id> --schedule "every 2h"
   ```

The webhook payload is only a trigger. `nightshift tick` re-reads state through `gh`, so signature handling, ordering and retries stay Hermes's concern.

Not built yet:
- A `--repo` filter on `tick`, so a single-repo event polls only that repo.
- A `--script` filter that drops `workflow_run` events whose conclusion is `success`, to skip a pointless tick.
