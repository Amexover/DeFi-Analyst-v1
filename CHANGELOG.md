# Changelog

## 1.1.0

### Data correctness

- Reject non-finite numbers and invalid timestamps.
- Sort daily observations, deduplicate UTC dates and report missing days.
- Use completed UTC days by default for both TVL and fee/revenue history.
- Add include_current_day to REST and MCP for explicit partial-day opt-in.
- Select current TVL by the latest valid timestamp.
- Reject blank searches before consuming provider quota.
- Return provider errors for malformed nested history or market containers.

### Reliability and provenance

- Add bounded per-process TTL caches and coalesce identical concurrent misses.
- Add bounded retries for transient upstream failures.
- Respect numeric and HTTP-date Retry-After without retrying too early.
- Preserve actual source fetch times and disclose cache age and served_at.
- Preserve provider/status details in MCP tool error text.
- Add provider methodology, parent relationships and comparability limits.

### Project

- Expand deterministic REST, MCP, normalization and adapter tests.
- Disable live upstream traffic in the test suite.
- Add GitHub Actions checks for Python 3.11–3.14.
- Add an executable offline example and architecture/methodology documentation.
- Add the standard MIT software license and separate data attribution notice.

### Compatibility notes

The six tool names and existing REST paths remain unchanged. History semantics
changed intentionally: days now denotes exactly that many UTC calendar dates,
ending yesterday by default. Requests that need today's partial observations
must set include_current_day=true.

Provider summary totals retain their original meaning; they are not silently
recalculated from the new daily window. Clients can continue reading the old
fields; new window, coverage, scope and provenance fields are additive.
