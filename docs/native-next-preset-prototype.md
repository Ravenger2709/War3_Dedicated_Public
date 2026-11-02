# Native next-preset prototype — not a Python integration

This is an internal C++ prototype, not a usable production Python interface.
There is no Python binding, IPC endpoint, request queue, authentication, or
idempotency protocol in this change. The test driver's stdin commands are
untrusted test instrumentation and must not be exposed as a production API.
A separate adapter must be designed and reviewed before Python can use this.

`CGHost::ApplyNativeNextPreset(CMap *, const NativeNextPreset &)` is synchronous
and **must be called on the GHost event-loop thread**, with a fully constructed,
previously validated map owned by that same GHost and a complete value-initialized
settings struct. `GameName` must be the prevalidated UTF-8 lobby name from Python;
the method also checks its length (1–24 bytes) and forbidden control and `=[]`
bytes. It returns `false` on validation or allocation failure without replacing the
prior pending preset. `true` replaces the single pending preset with deep copies;
the caller retains ownership of its map. It does not open a lobby immediately.

The next eligible autohost creation uses the pending name, map and settings.
Only successful construction and bind consumes the pending preset; failure keeps
it for a later autohost attempt. Defaults are temporarily set only during the
synchronous constructor and restored before returning to the event loop. Existing
lobby and match objects keep their own map, title and gameplay snapshots. After
consumption, further lobbies use the old defaults unless a new preset is staged.
Manual creation does not consume a pending preset. The method does not identify
requests: repeating an already consumed request can stage another lobby.

The method checks the Python preset numeric bounds: max 2–12; autostart
threshold 2–12 (bounded by max and human capacity only when enabled); countdown
0–60; vote percentage 1–100, timeout 5–600, cooldown 0–600, and refresh
interval 10–3600. Vote minimum may be 1 only with approved AI and otherwise is
2–12; it must fit human capacity. AI target is 2–12 when enabled and must fit
the map; skill is 0–2. Native AI layout and map-capacity checks can be stricter
than the Python numeric schema; this is not a proof of complete schema parity.
The method refuses matchmaking. It rejects an explicitly named local map whose data could not be loaded. It does
not verify map provenance, prevent file changes after prevalidation, or load a
map from a Python request. Loading must
fail before calling it if the prepared assets cannot be read and validated. The
caller must serialize requests and implement request identity, retry semantics
and a protected local transport. Allocation failure during game construction is
not handled as a recoverable error. There is no persistence, restart recovery,
controller, automatic rotation, or AMP integration.

On failed lobby setup or bind, `CreateGame` destroys the failed object before
queuing native BNCS creation and before publishing it. This does not provide
an acknowledgement or retry/idempotency contract for a future Python adapter.
The offline synthetic tests exercise the method directly through the test-only
driver, including a running game's W3GS actions after another map opens and
while its replacement fails to bind. They do not demonstrate a Python-to-production
GHost invocation or real map assets.

## Reservation gate and fenced closure

Staging does not create or reserve a lobby and does not grant permission to
start it. With the reservation gate enabled, a newly opened preset lobby still
requires the matching durable reservation marker. A rehost fences and records
closure of the previous counter before assigning a new counter; the renamed
lobby retains its own preset title and needs a new marker. A failed bind is not
published; in gated mode, destruction of its unstarted native lobby identity can
also produce a close tombstone. A pending preset is kept for a later attempt.
These statements do not provide a production path for Python to submit one.
