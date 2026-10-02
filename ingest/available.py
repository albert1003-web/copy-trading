"""When a filing counts as public, for D0 (filings.available_at / available_basis).

  seen   detected live by a scheduled poll: available_at = first_seen_at
  filed  loaded by a backfill, so we never saw it go public: estimated as after the close on the filing date.
         21:00Z is after the 4 pm ET close in both EST and EDT, so D0 is the next trading-day open.
"""

SEEN = "seen"
FILED = "filed"
CLOSE_UTC = "T21:00:00Z"


def availability(filing_date: str | None, first_seen_at: str, *, backfill: bool) -> tuple[str, str]:
    """(available_at, available_basis) for a newly inserted filing."""
    if not backfill:
        return first_seen_at, SEEN
    return (filing_date + CLOSE_UTC if filing_date else first_seen_at), FILED


# ON CONFLICT clause: a backfilled row inserted without a date (House search page) gets its estimate once the
# index supplies the date. Otherwise available_at never changes, so a live poll never rewrites a backfill
# estimate (and a backfill never rewrites a live detection).
ON_CONFLICT_SQL = f"""
              available_at   = CASE WHEN filings.available_basis = '{FILED}' AND filings.filing_date IS NULL
                                         AND excluded.filing_date IS NOT NULL
                                    THEN excluded.filing_date || '{CLOSE_UTC}' ELSE filings.available_at END"""
