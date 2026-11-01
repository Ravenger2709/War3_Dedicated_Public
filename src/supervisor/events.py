"""Conservative interpretation of upstream GHost++ log messages.

Events describe observations of GHost, not successful game simulation or iCCup E2E.
"""
import re

SENSITIVE = re.compile(r"password|passwd|autologincache|\btoken\b|cd.?key|secret", re.I)


def sanitize(line):
    if SENSITIVE.search(line):
        return "[redacted sensitive diagnostic]"
    return line.rstrip("\r\n")[:4096]


def classify(line):
    # Prefer failure over optimistic lifecycle messages.
    line = line.rstrip("\r\n")
    if line == "[GHOST] offline initialization complete":
        return "initialized"
    if line == "[GHOST] offline initialization failed" or line.startswith("[GHOST] stopped auto hosting, "):
        return "fatal"
    if re.fullmatch(r"\[GAME: [^\]\r\n]+\] error listening on port \d+", line):
        return "fatal"
    if re.fullmatch(r"\[GAME: [^\]\r\n]+\] listening on port \d+", line):
        return "lobby"
    if re.fullmatch(r"\[GAME: [^\]\r\n]+\] started loading with \d+ players", line):
        return "in_game"
    if re.fullmatch(r"\[GHOST\] deleting (?:current )?game \[[^\]\r\n]+\]", line):
        return "game_deleted"
    return None
