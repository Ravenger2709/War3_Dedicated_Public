# Returning to hosting after a game

The original Star client owns authentication and Guard traffic for the lifetime
of the realm session. The relay publishes the native GHost lobby within that
same session.

The first real Footman client test exposed a lifecycle gap: after a game and
automatic native rehost, the new host counter accepted a direct W3GS join, but
the lobby disappeared from exact iCCup lookup. Advertisement responses still
returned status 0. An accepted advertisement is therefore not sufficient proof
of directory visibility or successful rehosting.

The relay now returns the existing session to chat before the next lobby:

1. Withdraw the old advertisement with `SID_STOPADV`.
2. On the next fresh native publication, replay the bounded `SID_ENTERCHAT`
   frame captured from the original Star client.
3. Require the server's expected-identity response, then replay the original
   `SID_JOINCHANNEL` frame.
4. Require a subsequent channel event before allowing native publication.

No password, authentication or Guard packet is constructed or replayed by this
path. Initial chat entry remains entirely owned by Star. The original control
frames are kept in memory and cleared when the Star session changes. Missing
frames, a mismatched identity or an eight-second timeout fail closed and use
the existing supervised recovery path.

Chat-entry requests share a FIFO under the transport send lock. Responses to
original Star requests are forwarded; only the response owned by the relay's
current session/epoch is consumed. A preceding channel event cannot release
the gate. Publication/query responses retain their existing epoch, native
generation and host-counter correlation.

The normal GHost path also queues `QueueEnterChat` after `QueueGameUncreate`.
Protocol references describe [STOPADV withdrawal](https://bnetdocs.org/packet/271/sid-stopadv),
[chat entry](https://bnetdocs.org/packet/145/sid-enterchat) and the
[server chat-entry response](https://bnetdocs.org/packet/186/sid-enterchat).

The offline relay suite covers the same-name/new-counter lifecycle, fresh
identity and channel confirmation, original/replay response interleaving,
retired epochs, missing original frames and timeout. A real post-match exact
lookup and repeated client admission remain separate acceptance checks; an
offline mock does not establish iCCup behavior.
