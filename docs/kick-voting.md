# Kick voting

Kick voting is available only after every player has loaded into a running
match. It is unavailable in the lobby, countdown, loading, saved games, and
exiting games. It does not start the match or change its map.

Type `!kick <player>` to propose removing a player. The name must be an exact
case-insensitive nickname or a unique case-insensitive partial nickname. An
exact nickname takes precedence over partial matches. Unknown, ambiguous,
computer, and self targets are rejected without starting a ballot.

The initiator immediately contributes one Yes vote. Other eligible humans can
use `!yes` or repeat `!kick <same player>`. `!votekick <player>` uses the same
rules. Repeating a vote does not add another Yes, and selecting another target
does not redirect an active ballot. `!kick` alone shows status or usage.

Only connected human players in the same game can participate. Computer slots,
virtual players, and fake players do not vote. Connected human observers count
as humans. The target cannot vote. At least three humans must be present, and
at least two distinct eligible humans must vote Yes.

The required Yes count is `max(2, ceil(0.70 × eligible humans))`, where eligible
humans exclude the target. The electorate is captured when the ballot starts.

| Humans in this game | Eligible humans | Required Yes |
| ---: | ---: | ---: |
| 3 | 2 | 2 |
| 4 | 3 | 3 |
| 5 | 4 | 3 |
| 6 | 5 | 4 |
| 7 | 6 | 5 |
| 8 | 7 | 5 |
| 9 | 8 | 6 |
| 10 | 9 | 7 |
| 11 | 10 | 7 |
| 12 | 11 | 8 |

Any human membership change cancels the ballot. Leaving, joining, or replacing
a participant cannot lower the quorum or inherit a previous vote. A departing
target cancels the ballot without a successful kick. A ballot expires after
60 seconds. Completion, cancellation, and expiration each start a 30-second
cooldown in that game. Commands also have a one-second per-player throttle.
Each running game owns its ballot and cooldown independently; identical player
names or reused player IDs in another game do not share votes.

`tests/phase4/native-kick-vote-test.py` exercises the actual native driver with
synthetic W3GS peers and action traffic. It checks the quorum boundaries above,
loaded-session admission, distinct votes, name resolution, unrelated computer
slots, cancellation, timer boundaries, and parallel game namespaces. Driver
hooks only age timers, clear the command throttle, and inspect state; socket
chat and disconnects drive the actual native behavior. The tests use no
Warcraft engine, iCCup authentication, account, or live realm. Their result is
offline native protocol evidence, not a real-client or release acceptance gate.

Run in the existing offline native-test environment after building the matching
test driver:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/repo \
  WAR3_KICK_NATIVE_DRIVER=/reviewed-build/native-driver \
  python3 /repo/tests/phase4/native-kick-vote-test.py
```

Use the established native-test container restrictions: no network, read-only
root and checkout, no added capabilities, and only a private temporary directory
for synthetic state. The environment variable selects a test executable; it
does not alter production configuration.
