# Nadgryzieni scheduled wrappers

The active jobs are Hermes cron jobs:

- `nadgryzieni-primary.sh`: Friday 18:00 local time. The Hermes job is browser-assisted: it inspects the rendered Patreon posts page, registers newly visible Afterparty metadata through `register-patreon-post.py`, then invokes this deterministic wrapper.
- `nadgryzieni-retry.sh`: Sunday and Tuesday 04:00 local time. This remains script-only and is state-gated.
- `nadgryzieni-upcoming.sh`: invoked by the frequent scheduler tick; the repository wrapper performs at most one source probe per UTC day in its 04:30 UTC window and publishes the validated `upcoming.json` collection.

The Buffer campaign jobs are profile-level script-only jobs and use `nadgryzieni_buffer_reconcile.sh`:

- `nadgryzieni-buffer-campaign-reconciliation`: daily 07:15 local time.
- `nadgryzieni-buffer-campaign-correction`: daily 15:15 local time.

Each Buffer pass reads every future event from `upcoming.json` and reconciles both the 24-hour advance campaign and one-hour reminder campaign for every configured channel. The former 00:05 single-reminder job is paused.

The retry wrapper invokes the pipeline only when the previous Friday run completed successfully and found no new episode. A successful retry clears the marker; if the Sunday attempt fails, the Tuesday tick remains eligible, while a successful Sunday attempt makes Tuesday a no-op.

Both archive wrappers invoke the repository-root `nadgryzieni_pipeline.py`. The pipeline keeps its retry marker and process lock outside Git at:

```text
~/.hermes/profiles/r2-d2/state/
```

`patreon_posts.json` is a reviewed, tracked fallback manifest. Browser-assisted entries may include title, date, and duration because Patreon can block the pipeline's non-browser post-page fetch. `cron/register-patreon-post.py` validates and atomically updates those entries; it never accepts credentials or private feed values.

Patreon credentials, when configured, are read only from the protected `PATREON_RSS_URL` environment variable and are never logged.
