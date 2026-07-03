# Ship-it mandate (yt-transcriptor)

The user (project owner) granted this project a standing mandate on 2026-07-03:

- **Features ship activated, not dormant.** We do not build a feature and then leave
  it disabled behind a default-off flag. When work is finished and verified, it goes
  live by default.
- **Always keep a safe fallback / escape hatch.** Activation is only "safe to ship on"
  when there is a graceful degradation path (e.g. `USE_YTFETCH=0` falls back to direct
  yt-dlp; any ytfetch failure degrades automatically). Ship active *with* the hatch.
- **Autonomy to decide.** This project may make activation/rollout decisions on its own
  without round-tripping for approval — provided the fallback rule above holds and the
  change is verified. Surface what was shipped; don't ask permission to flip a verified,
  reversible flag.

Scope: this mandate covers activation/rollout of already-built, verified, reversible
changes. It does NOT cover irreversible or outward-facing actions (pushing/merging to
shared branches, publishing, deleting data) — those still get confirmed.
