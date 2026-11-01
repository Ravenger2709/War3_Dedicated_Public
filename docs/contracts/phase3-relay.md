# Phase 3 relay contract

The original Star process owns authentication, account proofs, Guard,
ENTERCHAT, and JOINCHANNEL. The relay forwards these frames unchanged. It never
accepts authentication packets from IPC, constructs a proof, replays a login,
starts a game engine, or restarts Star. The production supervisor owns Star
launch, exclusive account use, restart policy, and cohort backoff.

## Production command

```sh
python3 -m src.bridge.star_relay \
  --continuous --upstream "$GATEWAY_IPV4" \
  --profile /vendor/profile/launcher.ini \
  --intent /data/state/bridge/native-intent.json \
  --status /data/state/bridge/relay-status.json \
  --listen-port 16112 --game-port 6113 --publish \
  --publication-interval 30 \
  --refresh-request /data/state/bridge/refresh-request.json
```

`--publication-min-interval` is an alias for `--publication-interval`.
`--refresh-min-interval` defaults to 5 seconds and cannot be lower.
`--realm-idle-timeout` defaults to 60 seconds and cannot be lower than 10.
The listener binds only to `127.0.0.1`. The profile must select only that
loopback relay gateway. Runtime input/status directories must be owned by the
relay UID and mode 0700. Inputs must be owned regular single-link files with
mode 0600; symlinks, FIFOs, and oversized files are rejected. Status uses a new
private temporary file and atomic replacement on every write.

Without `--continuous`, the bounded research default remains 120 seconds,
validated to 30–180 seconds by the CLI. Research accepts only one Star session
per relay process. Its maintenance publication interval remains 2.5 seconds.
Continuous mode explicitly ignores `--duration`, including during unpublish or
game loading, and runs until SIGTERM/SIGINT or an unexpected worker failure.
The configurable maintenance interval cannot be below 2.5 seconds.

## Sequential original sessions

Continuous mode admits one Star connection at a time. It rejects connections
during upstream connection establishment, an active session, and teardown.
Both stream writers and all old forwarding tasks must finish closing before
the gate opens again. EOF, transport failure, malformed framing, and ambiguous
response timeouts revoke the session; the relay listener stays alive. Star
restart/backoff remains the supervisor's responsibility.

Each admitted connection increments `session_generation`, even if upstream
connection establishment fails. The next session resets identity, proof,
channel/welcome evidence, NETGAMEPORT registration, current native intent,
ACK/query FIFO ownership, publication acceptance, directory/exact-lookup
evidence, timestamps for current traffic, and lobby epoch. Historical totals
and explicit `ever_*` observations are diagnostic only.

The existing intent file is a sequence baseline at every new Star session.
A native process/generation first observed by the relay is also a baseline.
Continuous mode requires an observed sequence advance before adopting a live
intent. An existing file or replayed sequence cannot authorize publication,
refresh the native heartbeat, or restore acceptance. Superseded native
generations cannot be adopted again. Native can keep running while Star
recovers; its next normal heartbeat supplies fresh progress.

After expected server ENTERCHAT identity plus channel confirmation and fresh
native progress, initial publication is immediate. Original auth/Guard traffic
is never throttled by the maintenance interval. Native heartbeats remain every
3 seconds with a 10-second TTL; reading/validating them is independent of the
wire publication interval. A terminal unpublish withdraws the current lobby
without exiting continuous mode. A later lobby can publish on the same session.

## Publication, queries, and failure handling

Maintenance advertisements use the latest validated native frame, at most once
per configured interval (production default 30 seconds). Only one native
advertisement may be awaiting an ACK in continuous mode. A rejected native ACK
blocks acceptance and schedules retry after 30, 60, 120, 240, then 300 seconds,
or the configured maintenance interval if greater. Success resets backoff.
Changing lobby/native generation does not bypass this cooldown.

Native publication and query replies are correlated by FIFO position and lobby
epoch/key; normal heartbeat sequence changes do not invalidate the same lobby.
Original Star requests occupy the same FIFO and their responses are forwarded
unchanged. An 8-second timeout of any original or native publication/query
request ends the connection rather than dropping a queue entry and assigning
a late reply to another request. Deadlines are also checked when a continuous
session receives a reply. Queues are capped at 128 entries; drains and transport
closure have bounded waits. Old writes cannot adopt a newer session's transport
or cancel its cohort.

STOPADV is attempted on native unpublish, heartbeat expiry, graceful shutdown,
and authenticated session cleanup where a writable upstream remains. Stream
failure can prevent delivery; STOPADV has no ACK here, so a successful write is
not claimed as independently verified removal. Session teardown still closes
both transports. Repeated cancellation cannot release a half-closed session.

Accepted lobbies are queried at most once per 2.5 seconds, alternating an
unfiltered directory request and an exact-name lookup. Only one native query
may be pending. Exact lookup freshness is 8 seconds. A positive directory
discovery is retained for at most 60 seconds for the same lobby/epoch, separately
from the latest page's result. A later limited directory response need not
contain every game. No directory rank or bump is inferred.

## Refresh request IPC

The optional refresh file contains exactly these keys:

```json
{
  "version": 1,
  "pid": 1234,
  "generation": 7,
  "host_counter": 74565,
  "sequence": 1,
  "observed_monotonic_seconds": 1000.0
}
```

The supervisor receives the native `[HOSTBOT_REFRESH]` event, merges the
**current** native intent's PID and publisher generation, stamps the observation
using the same host monotonic clock, and atomically replaces the 0600 file.
`host_counter` is the lower 28 bits, not the BNCS counter with its realm nibble.
All integer fields reject booleans. PID is positive and below 2^31; generation
and refresh sequence are positive and below 2^64; host counter is positive and
below 2^28. Timestamp must be finite, nonnegative, no more than 10 seconds old,
and never in the future. Duplicate, missing, or extra keys are rejected.
PID/generation/counter must match the current fresh native publish intent.

The refresh sequence is independent of the native heartbeat sequence and must
increase within its PID/generation/counter context. Duplicate/replayed requests
are ignored. A new pending request supersedes the preceding pending request.
Requests expire after 10 seconds, including while waiting for cooldown.
Unpublish, replacement lobby, native generation change, or session loss cancels
pending/in-flight refreshes. Refresh never extends native heartbeat lifetime.

Explicit refresh uses its separate minimum interval, default 5 seconds, measured
both from the preceding injected advertisement and from the preceding explicit
refresh. It can advance a maintenance advertisement that would otherwise wait
30 seconds, allowing a live 10-second hint to be honored. ACK rejection cooldown
still applies. Ordinary native heartbeats do not bypass either throttle.
The native actor's feature policy remains default 120 seconds/minimum 30 seconds;
this IPC does not toggle slots or change gameplay state itself.

IPC carries only a refresh hint. It cannot supply packets, change account
identity, establish authentication, or supply an ACK. Success means an actual
ACK 0 of the advertisement associated with that request's native generation
and refresh sequence. It does not establish directory visibility or prove a bump.

## Status consumed by the supervisor

Status is schema 1 with additive fields. All `*_monotonic_seconds` values use
the host monotonic clock, or are null when unobserved. The supervisor must check
the observation timestamp, live relay PID, and session generation; an old file
or cumulative counter is not readiness. A relay PID can be reused by the OS;
`relay_started_monotonic_seconds` identifies this relay lifetime as well.

| Field | Meaning |
| --- | --- |
| `observed_monotonic_seconds` | Time of this atomic status snapshot; normally updated every 0.2 seconds. |
| `relay_pid`, `relay_started_monotonic_seconds` | Actual relay process and lifetime. |
| `continuous`, `experimental` | Explicit production mode and research marker. |
| `listener_ready`, `session_closing` | Relay listener state and teardown gate; this is not the W3GS player listener. |
| `session_generation`, `session_started_monotonic_seconds`, `session_closed_monotonic_seconds` | Current/last admitted original connection lifecycle. |
| `connected`, `authenticated` | Open upstream and current expected server identity plus channel evidence. |
| `authenticated_monotonic_seconds` | First identity/channel confirmation in this session. |
| `last_client_traffic_monotonic_seconds`, `last_server_traffic_monotonic_seconds`, `last_traffic_monotonic_seconds` | Actual traffic observations; sending an ad cannot manufacture server traffic. |
| `realm_connection_healthy` | Authenticated connection with actual server traffic no older than the configured realm idle timeout. |
| `native_pid`, `native_generation`, `native_sequence`, `native_host_counter` | Current adopted native publisher/lobby context; no packet or lobby name is exposed. |
| `last_native_intent_monotonic_seconds`, `native_heartbeat_fresh` | Last observed advancing intent and its 10-second freshness. |
| `advertisement_accepted` | Current session/lobby ACK 0, with fresh native heartbeat in continuous mode. |
| `last_ack_monotonic_seconds`, `last_ack_status` | Last actual current-lobby native publication ACK and numeric server status. Reset for a new Star session. Historical within a session after unpublish. |
| `last_ack_context` | Object with `session_generation`, `epoch`, `pid`, `generation`, `host_counter`, and nullable `refresh_sequence`. |
| `publication_failed`, `publication_retry_monotonic_seconds` | Rejection/transport failure and next allowed maintenance/refresh attempt. |
| `public_directory_seen_for_current_lobby` | Positive directory discovery for this lobby/epoch within 60 seconds. |
| `exact_lookup_visible` | Separate positive exact lookup within 8 seconds. |
| `visible_in_realm_query` | The latest directory page matched and that match remains fresh. |
| `refresh_requested_count` | Cumulative distinct validated refresh requests in this relay lifetime. |
| `refresh_sent_count` | Cumulative refresh advertisements handed to the upstream transport. |
| `refresh_successful_count` | Matching actual ACK 0 outcomes. |
| `refresh_failed_count` | Rejected ACK, expiry, supersession, or cancellation outcomes, including unsent hints. |
| `last_refresh_request_sequence`, `last_refresh_request_generation` | Last validated requested refresh sequence and native publisher generation. |
| `last_refresh_requested_monotonic_seconds`, `last_refresh_sent_monotonic_seconds` | Valid request observation and actual upstream write times. |
| `last_refresh_ack_monotonic_seconds`, `last_refresh_ack_context` | Actual refresh ACK time and ACK context plus numeric `status`; not overwritten by a later unsent hint. |
| `last_refresh_result` | Latest completed request: `pid`, `generation`, `host_counter`, `sequence`, `status` (`accepted`, `rejected`, `expired`, `superseded`, `cancelled`). |
| `refresh_request_error` | Fixed redacted IPC rejection code, separate from transport health. |
| `public_directory_position`, `bump_proven` | Always null/false until separately measured evidence exists. |

No `publication_refresh_*` aliases are emitted; use the four explicit refresh
counters above. Failed hints can exceed sent hints because expiry/supersession
can occur before a write. An ACK or refresh success is never directory proof.
LOBBY readiness must combine actual authenticated realm health, live relay and
native freshness, the independently checked native player listener, and current
accepted publication. Directory discovery and exact lookup are separate reports.

Continuous connection/drop logs contain only fixed `event`, `relay_pid`,
`session_generation`, `observed_monotonic_seconds`, and `error_present`. Events
are `RELAY_SESSION_ACCEPTED`, `RELAY_UPSTREAM_CONNECTED`,
`RELAY_SESSION_REJECTED`, and `RELAY_SESSION_CLOSED`. Neither status nor logs
include account names, gateway addresses, lobby names, credentials, packet
payloads, or arbitrary exception strings.

## Offline validation

Run `bash tests/phase3/relay-offline.sh`. It uses only the pinned local composite
image `sha256:fd37203f6094d366567f882ac1dfaa02087b003cfed3e9f8fbebd247cc397b16`,
selective read-only source/test mounts, non-root execution, a read-only root,
and `--network none`. Fixtures and real container-loopback peers are synthetic;
there is no account, Star binary, AMP, remote host, or WAN access.

The final offline run passed 106 tests (62 original plus 44 new); hashes and
scope are recorded in `tests/phase3/relay-results.json`.
The suite retains the original 62 regressions and adds production publication
throttling/backoff, independent heartbeat TTL, strict refresh IPC, real ACK
correlation, concurrent/closing-session rejection, reconnect baselines, stale
write races, original-response FIFO timeout, and live TCP shutdown coverage.
