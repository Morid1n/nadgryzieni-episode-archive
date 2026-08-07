# Nadgryzieni scheduled wrappers

The active jobs are Hermes cron jobs:

- `nadgryzieni-primary.sh`: Saturday 04:00 local time, covering Friday night → Saturday morning. The Hermes job is browser-assisted: it inspects the rendered Patreon posts page, registers newly visible Afterparty metadata through `register-patreon-post.py`, then invokes this deterministic wrapper.
- `nadgryzieni-retry.sh`: Tuesday 04:00 local time, covering Monday evening → Tuesday morning. This remains script-only and is state-gated.

The retry wrapper invokes the pipeline only when the previous Saturday run completed successfully and found no new episode. A successful retry clears the marker, so it is attempted at most once for that Saturday run.

Both wrappers invoke the repository-root `nadgryzieni_pipeline.py`. The pipeline keeps its retry marker and process lock outside Git at:

```text
~/.hermes/profiles/r2-d2/state/
```

`patreon_posts.json` is a reviewed, tracked fallback manifest. Browser-assisted entries may include title, date, and duration because Patreon can block the pipeline's non-browser post-page fetch. `cron/register-patreon-post.py` validates and atomically updates those entries; it never accepts credentials or private feed values.

Patreon credentials, when configured, are read only from the protected `PATREON_RSS_URL` environment variable and are never logged.
