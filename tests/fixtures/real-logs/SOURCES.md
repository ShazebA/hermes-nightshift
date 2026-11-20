# Real failure logs (trimmed)

These are `gh run view <id> --log-failed` output from public repositories, trimmed to keep each job's first lines, its step headers and the window around the first `##[error]`. They are used as parser regression tests. Nightshift only reads them.

| File | Source run |
|---|---|
| fastapi-pytest.log | https://github.com/fastapi/fastapi/actions/runs/44375322280 |
| fastapi-missing-artifact.log | https://github.com/fastapi/fastapi/actions/runs/44377011394 |
| nextjs-api-502.log | https://github.com/vercel/next.js/actions/runs/44631178195 |
| hermes-pages-outage.log | https://github.com/NousResearch/hermes-agent/actions/runs/44621782982 |
| hermes-nix-build.log | https://github.com/NousResearch/hermes-agent/actions/runs/44624210497 |
| ruff-clippy.log | https://github.com/astral-sh/ruff/actions/runs/44619679195 |
| playwright-android.log | https://github.com/microsoft/playwright/actions/runs/44612888928 |
| playwright-bidi.log | https://github.com/microsoft/playwright/actions/runs/44646088926 |
| uv-long-script.log | https://github.com/astral-sh/uv/actions/runs/44624178401 |
| pydantic-action-step.log | https://github.com/pydantic/pydantic/actions/runs/44661974225 |
