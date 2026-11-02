# Player message interval

AMP exposes **Lobby message interval (seconds)** under Lobby / Messages. The default is 20 seconds; accepted values are integers from 10 to 3600. Standalone operators set `gameplay.lobby_message_interval` in `config/supervisor.json` (the native equivalent is `bot_lobby_message_interval`). Apply changes with the next normal instance restart.

This one global setting controls recurring auto-start, vote-start and help hints in every lobby/match. Vote reminders follow the auto-start notice by about 3.5 seconds and help hints by about 7 seconds, so the three messages do not arrive together. Each message recomputes current human counts when sent. With auto-start disabled, vote and help hints still recur. Loaded matches receive the help hint; lobby-only start notices stop during countdown, loading and gameplay.

Explicit commands answer immediately. The interval changes chat cadence only: auto-start eligibility still runs on its existing ten-second check. MOTD remains a separate join message and a custom `config/motd.txt` remains authoritative. This setting does not change empty-slot refresh frequency.
