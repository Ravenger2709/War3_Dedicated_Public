# Native map-load limits and preload accounting

The compatibility patch rejects a named local map before using config-supplied
metadata when the archive cannot be read completely or exceeds 128 MiB. It
checks the uncompressed sizes of every MPQ member that `CMap::Load` can read:
16 MiB per member and 64 MiB summed across the listed members. These checks run
before extraction even when the host's default `common.j` or `blizzard.j` is missing. Every present listed member is
read completely in bounded chunks; a missing `war3map.w3i` or map script and a
short or failed read invalidate the map. Sizes and cumulative bytes are checked
again at the later CRC and metadata reads. Default `common.j` and `blizzard.j`
are each read with a 16 MiB file ceiling. Data is never intentionally truncated.

The 13-member sum conservatively includes both possible map-script paths even
though native CRC calculation uses at most one of them. Checks apply to native
loading, not just Python validation. Invalid input cannot be staged by the
existing next-preset method. Synthetic metadata-only maps with no local path
remain available for offline fixtures; they are not proof of real-map validity.

`NativePreloadPayloadBudget` is an admission calculation for a **future** cache,
not an active preload facility or an RSS limit. It requires the caller to supply
sizes for distinct prevalidated map identities, the default-map size, and a game
limit of 1–5. It counts each unique cache payload, two default-map copies,
`maxGames + 3` copies of the largest possible map (running games, lobby, and
old plus new pending objects), and 128 MiB reserved for explicit load buffers.
It rejects more than 32 distinct maps, files outside 1–128 MiB, and an estimate
above 512 MiB. An eventual cache must deduplicate identities before calling it,
call it before allocation, and ensure that all possible game maps are represented
by the provided sizes. There is currently no cache admission point that calls
this function, so these accounting rules are not enforced on GHost runtime RSS.
Allocator overhead, `std::string` capacity, StormLib internals and the rest of
GHost are outside this payload estimate and require a separate process/container
memory limit.

On Linux, map bytes and the StormLib archive open use the same pinned regular-file
inode; final symlinks are rejected and size/mtime/ctime are checked before and
after loading. This prevents a pathname replacement from switching the archive
between those two reads, and rejects detected in-place changes. A preload
implementation must still bind its earlier Python prevalidation to the native
input (for example, via an immutable private snapshot and verified digest). Oversized/invalid files fail closed but resource exhaustion and
untrusted parser faults are not made recoverable by this patch.

Offline compatibility survey before selecting limits: four local Afterlife and
Footman variants had archives of 3,273,827–4,710,257 bytes; among the members
GHost reads, the largest measured 789,007 bytes and each map's member sum was at
most 1,992,849 bytes. This is a sample, not a claim about all Warcraft maps.
The optional local-map test checks loader acceptance and size handling using
synthetic fallback metadata; it does not establish gameplay compatibility.
