# Production operator logs

Production stdout and AMP Console use concise English messages:

```text
2026-10-30 20:00:01 INFO native: Lobby opened: Footman Ultimate AI (0 humans, 6 computers).
2026-10-30 20:00:02 INFO supervisor: Lobby is ready.
2026-10-30 20:00:10 INFO native: Player joined: ExamplePlayer
2026-10-30 20:00:10 INFO native: Participants: 1 humans, 5 computers.
2026-10-30 20:00:11 INFO native: Lobby message: Players not yet pinged 3 times; cannot start.
2026-10-30 20:00:20 INFO native: Countdown started (5 seconds).
2026-10-30 20:00:25 INFO native: Game started: Footman Ultimate AI.
2026-10-30 20:10:00 INFO native: Player left: ExamplePlayer
2026-10-30 20:10:01 INFO native: Game ended: Footman Ultimate AI.
2026-10-30 20:10:01 INFO native: Lobby rehosted: Footman Ultimate AI (0 humans, 6 computers).
```

These are illustrative messages, not a record of gameplay executed by these
tests. Runtime stdout timestamps are UTC, followed by `INFO`, `WARN` or `ERROR`,
component, and prose. The AMP template removes that duplicate UTC prefix with
the supported [Generic console filter](https://github.com/CubeCoders/AMP/wiki/Configuring-the-%27Generic%27-AMP-module#console).
AMP shows its own entry timestamp in the browser's local timezone (for Minsk,
UTC+3). This avoids two different times on the same console row. Existing
console history is not rewritten. Structured JSONL and Docker stdout retain
UTC timestamps for diagnostics. Readiness and Analytics patterns accept the
line both before and after filtering.
The change applies to `ICCupSupervisor` and `OfflineProductionSupervisor`.
The legacy generic supervisor and command/HTTP response formats are unchanged.

## Quiet console, complete machine diagnostics

Native `[HOSTBOT_STATE]` heartbeats and `[HOSTBOT_REFRESH]` requests are still
passed to the same typed parsers and controller. `/status` and the publication
request mailbox retain their existing fields and behavior. These native lines
are not printed as escaped JSON envelopes in AMP Console.

Lobby chat and commands are shown as `Player chat [name]: message`, including
repeated messages. They never match the Analytics join/leave patterns. Existing
sensitive-content redaction also applies to chat: messages containing configured
sensitive keywords are omitted from the console. This pattern-based filter
cannot detect every possible secret. Safe native lines and console text remain
in the existing private diagnostic journal and Docker logs respectively.

Both readers journal the raw native line before queueing its controller callback.
The callback may then update typed state, emit a summary, and return; that return
does not remove or skip the already-written raw state record.

The runtime retains redacted native diagnostics and structured lifecycle events
in `runtime_dir/supervisor.jsonl`, normally
`/data/state/supervisor/supervisor.jsonl`. Entries remain one JSON object per
line with `time`, `event`, and event fields. The journal rotates at 10 MiB,
retaining `.jsonl.1`, `.jsonl.2`, and `.jsonl.3`, with mode `0600` inside the
existing private runtime directory. Console filtering does not discard journal
records. Existing sensitive-diagnostic redaction applies to both outputs.

Only changed human/computer counts and lifecycle transitions produce population,
countdown, game, and rehost messages. Countdown ticks and unchanged four-second
heartbeats are silent. Actual native `[GAME: lobby] [Local]: message` records
preserve explanations
such as ping requirements and start-vote rejections, with a `Lobby message:`
prefix; identical immediate repetitions are suppressed for five seconds. The
alternate `[LOCAL: lobby] message` form is also accepted for compatibility.

Refresh requests alone never produce a success message. Increasing native
`publication_refresh_successes` counters produce the first acknowledgement
summary and then at most one per five minutes, only if more acknowledgements
arrived. Increasing failure counters produce at most one warning per minute.
The counters accumulate while a report is suppressed. Ten-second refreshes
therefore do not produce ten-second console spam. Periodic worker status,
publication and directory codes remain journal-only.

An acknowledgement is a publication response, **not directory visibility**.
The logger never treats ACK status zero as an exact directory match. It makes
no visibility claim from acknowledgement counts or chat-return timing.

## Named human events and AMP Analytics

Only anchored native peer-lifecycle messages can create named Analytics events:

```text
[GAME: lobby] player [Name|external IP] joined the game
[GAME: lobby] deleting player [Name]: reason
```

The pinned native join message belongs to the accepted peer path, including the
scored path; rejected join diagnostics have different wording. Native computer
slots and virtual-host bookkeeping do not create these peer messages. Ordinary
chat-prefixed lookalikes, local messages, aggregate counts, unknown-player
deletions and stale controller generations do not produce named join events.
IPs and leave reasons are omitted from the operator event. The report is an
observation of native acceptance, not account authentication.

Names are preserved verbatim, never lowercased, shortened, escaped or replaced
with a shared alias. A name must be 1–64 UTF-8 bytes, have no leading/trailing
whitespace, contain only Python-printable characters, contain none of
`[]|<>"\` or Unicode replacement character U+FFFD, and pass the existing
sensitive-content sanitizer unchanged. Unsafe names are omitted from Analytics
instead of collapsing to `<redacted>`. The roster remains bounded.

Repeated native joins are deduplicated. A native deletion emits a leave only
for an already tracked name. The same exact name appearing in another tracked
game remains one Analytics user until its last tracked peer leaves. Game
deletion, game retirement and native stop/recovery clear tracked peers with
matching `Player left:` identities exactly once. Counts never fabricate names.
Native stdout is the event source. The coordinated native `CONSOLE_Print`
already escapes embedded LF, CR and NUL as literal `\n`, `\r` and `\0` before
emitting a physical line. This framing prevents multiline chat from masquerading
as separate lifecycle records; the logger tests include escaped chat lookalikes.
Anchored matching alone does not authenticate the origin of a physical line.

Stock AMP Generic can use its existing `.NET` `username` capture; no custom
Analytics service, frontend change or user actions are required. The agreed
join pattern is:

```regex
^(?:[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} )?INFO native: Player joined: (?<username>(?!\s)[^\x00-\x1F\x7F\[\]|<>"\\\uFFFD]{1,64}(?<!\s))$
```

The leave pattern replaces `Player joined:` with `Player left:`. This regex is
a deliberate superset of the emitter's UTF-8 and printable-character checks.
Only these named event lines match; `Lobby message:` and population summaries
cannot populate Analytics.

The readiness regex must match only the supervisor's exact ready states:

```regex
^(?:[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} )?INFO supervisor: (Lobby is ready|Countdown is running|Game is running)\.$
```

Native `Lobby opened:` or `Lobby rehosted:` may precede realm publication
confirmation and are not readiness proof. Install the matching AMP template
with the runtime: the old JSON-state ready regex is incompatible with this
console format. `Console.UserActions` remains `{}`.

## Verification and rollout boundary

`tests/phase3/test_operator_logs.py` checks heartbeat suppression with preserved
JSONL, private rotation, secret redaction, acknowledgement cadence, local start
explanations, safe named identities, rejected/chat/AI/virtual-host lookalikes,
roster cleanup, readiness text, unchanged typed status and refresh requests.
An offline production supervisor runs a synthetic Python subprocess to verify
the reader, journal and shutdown path; no native binary or engine runs.

Checks use pinned rc6 image
`sha256:d38c7bb7b36bb317f1306bd68c73339141d103e677440fb99074e685cf4aeeaa`,
UID 1000, no network, read-only root and source mount, dropped capabilities and
temporary storage. The new runtime module and controller change alter the
production source digest. A changed runtime requires a fresh image and release
acceptance. Verify an accepted peer join and leave through the installed AMP
player list and Analytics when upgrading the template.
