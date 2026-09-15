# Nadgryzieni scheduled wrappers

The active jobs are Hermes cron jobs:

- `nadgryzieni-primary.sh`: Friday 18:00 local time. The Hermes job is browser-assisted: it inspects the rendered Patreon posts page, registers newly visible Afterparty metadata through `register-patreon-post.py`, then invokes this deterministic wrapper.
- `nadgryzieni-retry.sh`: Sunday and Tuesday 04:00 local time. This remains script-only and is state-gated.
- `nadgryzieni-upcoming.sh`: invoked every five minutes; the repository gate performs one source probe in each of four UTC windows (04:30, 10:30, 16:30, 22:30) and publishes the validated `upcoming.json` collection. Stable video IDs are re-read in every slot, so title or start-time changes update the existing event.

The active Buffer handoff is the script-only `nadgryzieni-buffer-after-upcoming` job. It ticks at minutes 01, 06, 11, …, 56, reads the completed YouTube probe slot, and runs `nadgryzieni_buffer_reconcile.sh` only once for a new published slot. The slot is marked handled only after Buffer mutation and readback both succeed, so failures retry on the next tick without generating routine API traffic between discovery probes. Each pass reads every future event from `upcoming.json` and reconciles both the 24-hour advance campaign and one-hour reminder campaign for every configured channel. Existing matching future posts are updated in place, missing posts are created, and sent posts are not recreated automatically. The former daily reconciliation, afternoon correction, and single-reminder jobs are paused.

The retry wrapper invokes the pipeline only when the previous Friday run completed successfully and found no new episode. A successful retry clears the marker; if the Sunday attempt fails, the Tuesday tick remains eligible, while a successful Sunday attempt makes Tuesday a no-op.

Both archive wrappers invoke the repository-root `nadgryzieni_pipeline.py`. The pipeline keeps its retry marker and process lock outside Git at:

```text
~/.hermes/profiles/r2-d2/state/
```

`patreon_posts.json` is a reviewed, tracked fallback manifest. Browser-assisted entries may include title, date, and duration because Patreon can block the pipeline's non-browser post-page fetch. `cron/register-patreon-post.py` validates and atomically updates those entries; it never accepts credentials or private feed values.

Patreon credentials, when configured, are read only from the protected `PATREON_RSS_URL` environment variable and are never logged.
