"""Read-only readiness from the owner's expiring, catalog-bound snapshot."""


def cycle_allows_ready(cycle, native, *, generation, failed, listener, published):
    """Require current-session evidence, not just the presence of cycle config.

    The caller separately checks component ownership, freshness and realm auth.
    A running match needs no lobby advertisement. A lobby needs its own matching
    catalog identity and publication, even when other matches are running.
    """
    if failed or not generation or native.get('session_generation') != generation:
        return False
    if cycle.get('state') not in ('waiting_native_ipc','blocked_pending','capacity_full'):
        return False
    count=cycle.get('active_matches');maximum=cycle.get('max_matches')
    if (cycle.get('availability',{}).get('active_matches') != 'verified_snapshot'
            or type(count) is not int or type(maximum) is not int
            or not 0 <= count <= maximum <= 5 or maximum < 1
            or count != native.get('active_games')):
        return False
    phase=native.get('phase')
    if phase=='IN_GAME':
        return count > 0
    lobby=cycle.get('current_lobby')
    return bool(phase in ('LOBBY','COUNTDOWN') and listener and published
                and cycle.get('availability',{}).get('current_lobby')=='verified_snapshot'
                and isinstance(lobby,dict) and lobby.get('phase')==phase
                and type(lobby.get('host_counter')) is int
                and lobby['host_counter']==native.get('host_counter')
                and lobby.get('catalog_index')==native.get('catalog_index'))
