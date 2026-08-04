# Nadgryzieni scheduled wrappers

The active jobs are script-only Hermes cron jobs:

- `nadgryzieni-primary.sh`: Saturday 04:00 local time, covering Friday night → Saturday morning.
- `nadgryzieni-retry.sh`: Tuesday 04:00 local time, covering Monday evening → Tuesday morning.

The retry wrapper invokes the pipeline only when the previous Saturday run completed successfully and found no new episode. A successful retry clears the marker, so it is attempted at most once for that Saturday run.

Both wrappers invoke the repository-root `nadgryzieni_pipeline.py`. The pipeline keeps its retry marker and process lock outside Git at:

```text
~/.hermes/profiles/r2-d2/state/
```

The marker contains no credentials. Patreon credentials, when configured, are read only from the protected `PATREON_RSS_URL` environment variable and are never logged.
