"""Prepare a private native config and consume typed native lifecycle events."""
import json
import math
import os
from pathlib import Path
import re
import stat

from .files import atomic_text, no_symlinks, private_directory
from .presets import resolve_preset

PHASES={'IDLE','LOBBY','COUNTDOWN','IN_GAME'}
STATE_NUMBERS=('players','max_players','autostart_players','votes','votes_required','countdown','refresh_count','refresh_skipped','refresh_sequence','host_counter')
STATE_TIMES=('last_refresh_monotonic_ms','last_ack_monotonic_ms','uptime_seconds')


def _unique_state_fields(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('NATIVE_STATE_REJECTED')
        result[key]=value
    return result


def native_state(line):
    if not line.startswith('[HOSTBOT_STATE] '):return None
    obj=json.loads(line[len('[HOSTBOT_STATE] '):],object_pairs_hook=_unique_state_fields)
    if not isinstance(obj,dict) or type(obj.get('version')) is not int or obj['version']!=1 or type(obj.get('phase')) is not str or obj['phase'] not in PHASES:raise ValueError('NATIVE_STATE_REJECTED')
    out={'version':1,'phase':obj['phase']}
    if 'session_generation' in obj:
        generation=obj['session_generation']
        if not isinstance(generation,str) or re.fullmatch(r'[0-9a-f]{32}',generation) is None:raise ValueError('NATIVE_STATE_REJECTED')
        out['session_generation']=generation
    for key in ('game_name','map_name'):
        value=obj.get(key,'')
        if not isinstance(value,str) or len(value.encode())>256 or any(ord(c)<32 or ord(c)==127 for c in value):raise ValueError('NATIVE_STATE_REJECTED')
        out[key]=value
    for key in STATE_NUMBERS:
        value=obj.get(key,0)
        if type(value) is not int or not 0<=value<2**32:raise ValueError('NATIVE_STATE_REJECTED')
        out[key]=value
    for key in ('computer_players','ai_target'):
        value=obj.get(key,0)
        if type(value) is not int or not 0<=value<=12:raise ValueError('NATIVE_STATE_REJECTED')
        out[key]=value
    if out['players']>12 or out['max_players']>12 or out['host_counter']>=2**28:raise ValueError('NATIVE_STATE_REJECTED')
    for key in ('active_games','publication_refresh_count','publication_refresh_successes','publication_refresh_failures',*STATE_TIMES):
        value=obj.get(key,0)
        if type(value) is not int or not 0<=value<(2**64 if key in STATE_TIMES else 2**32):raise ValueError('NATIVE_STATE_REJECTED')
        out[key]=value
    proof_keys={'catalog_request_id','catalog_index'}
    if proof_keys & obj.keys():
        if (not proof_keys <= obj.keys() or 'session_generation' not in out or
                not isinstance(obj['catalog_request_id'],str) or
                re.fullmatch(r'[0-9a-f]{32}',obj['catalog_request_id']) is None or
                type(obj['catalog_index']) is not int or not 0<=obj['catalog_index']<32 or
                not 0<out['host_counter']<2**28 or out['phase']=='IDLE'):
            raise ValueError('NATIVE_STATE_REJECTED')
        out['catalog_request_id']=obj['catalog_request_id']
        out['catalog_index']=obj['catalog_index']
    if 'matches_v1' in obj:
        telemetry=obj['matches_v1']
        if (type(telemetry) is not dict or set(telemetry)!={'version','active_count','retired_count','rotation_error_count','matches'}
                or type(telemetry['version']) is not int or telemetry['version']!=1
                or 'session_generation' not in out or 'active_games' not in obj
                or type(telemetry['active_count']) is not int or not 0<=telemetry['active_count']<=5
                or telemetry['active_count']!=out['active_games']
                or any(type(telemetry[k]) is not int or not 0<=telemetry[k]<2**64-1
                       for k in ('retired_count','rotation_error_count'))
                or type(telemetry['matches']) is not list
                or len(telemetry['matches'])!=telemetry['active_count']):
            raise ValueError('NATIVE_STATE_REJECTED')
        counters=set(); match_ids=set(); matches=[]
        for item in telemetry['matches']:
            if (type(item) is not dict or set(item) not in ({'host_counter','catalog_index','phase'},
                                                           {'host_counter','catalog_index','phase','match_id'})
                    or type(item['host_counter']) is not int or not 1<=item['host_counter']<2**28
                    or item['host_counter'] in counters
                    or ('match_id' in item and item['match_id'] is not None and
                        (type(item['match_id']) is not str or re.fullmatch(r'[0-9a-f]{32}',item['match_id']) is None
                         or item['match_id'] in match_ids))
                    or (item['catalog_index'] is not None and (type(item['catalog_index']) is not int
                        or not 0<=item['catalog_index']<32))
                    or type(item['phase']) is not str or item['phase'] not in ('loading','playing')):
                raise ValueError('NATIVE_STATE_REJECTED')
            counters.add(item['host_counter'])
            if item.get('match_id') is not None:match_ids.add(item['match_id'])
            matches.append(dict(item))
        out['matches_v1']={**telemetry,'matches':matches}
    for key in ('loading','realm_online','feedback_valid','ai_supported'):
        value=obj.get(key,False)
        if type(value) is not bool:raise ValueError('NATIVE_STATE_REJECTED')
        out[key]=value
    if 'catalog_request_id' in out and out['phase'] in ('LOBBY','COUNTDOWN') and out['loading']:
        raise ValueError('NATIVE_STATE_REJECTED')
    return out


def native_catalog_lobby_matches(state, *, generation, request_id, catalog_index, host_counter):
    """Compare parsed lobby identity only; caller must prove ownership and freshness.

    An IN_GAME state can describe a past match and is not a current lobby. This
    comparison supplies no evidence of capacity, publication or reservation.
    """
    if (type(state) is not dict or type(generation) is not str or
            re.fullmatch(r'[0-9a-f]{32}',generation) is None or
            type(request_id) is not str or re.fullmatch(r'[0-9a-f]{32}',request_id) is None or
            type(catalog_index) is not int or not 0<=catalog_index<32 or
            type(host_counter) is not int or not 1<=host_counter<2**28):
        return False
    return (state.get('version') == 1 and type(state.get('version')) is int and
            state.get('phase') in ('LOBBY','COUNTDOWN') and
            state.get('session_generation') == generation and
            state.get('catalog_request_id') == request_id and
            type(state.get('catalog_index')) is int and state['catalog_index'] == catalog_index and
            type(state.get('host_counter')) is int and state['host_counter'] == host_counter)


def refresh_event(line):
    if not line.startswith('[HOSTBOT_REFRESH] '):return None
    obj=json.loads(line[len('[HOSTBOT_REFRESH] '):])
    if not isinstance(obj,dict) or set(obj)!={'version','host_counter','sequence'} or obj['version']!=1 or type(obj['version']) is not int:raise ValueError('NATIVE_REFRESH_REJECTED')
    if type(obj['host_counter']) is not int or not 0<obj['host_counter']<2**28 or type(obj['sequence']) is not int or not 0<obj['sequence']<2**64:raise ValueError('NATIVE_REFRESH_REJECTED')
    return obj


def listener_owned(pid,port=6113):
    if not pid:return False
    try:
        sockets=set()
        for fd in Path('/proc',str(pid),'fd').iterdir():
            try:
                link=os.readlink(fd)
                if link.startswith('socket:['):sockets.add(link[8:-1])
            except OSError:pass
        for name in ('tcp','tcp6'):
            for row in Path('/proc/net',name).read_text().splitlines()[1:]:
                parts=row.split()
                if parts[3]=='0A' and int(parts[1].rsplit(':',1)[1],16)==port and parts[9] in sockets:return True
    except OSError:pass
    return False


_CATALOG_OVERRIDES = frozenset({
    'bot_preload_manifest_fd', 'bot_preload_manifest_sha256', 'bot_preload_ipc_dir',
    'bot_lifecycle_event_dir', 'bot_start_reservation_gate', 'bot_maxgames', 'autohost_maxgames',
})


def _catalog_overrides(values):
    if type(values) is not dict or set(values) not in (_CATALOG_OVERRIDES, _CATALOG_OVERRIDES | {'bot_preload_default_sha256'}):
        raise ValueError('NATIVE_CATALOG_OVERRIDES_REJECTED')
    if any(type(v) is not str or not v or any(ord(c) < 32 or ord(c) == 127 for c in v)
           for v in values.values()):
        raise ValueError('NATIVE_CATALOG_OVERRIDES_REJECTED')
    fd = values['bot_preload_manifest_fd']
    digest = values['bot_preload_manifest_sha256']
    ipc, journal = values['bot_preload_ipc_dir'], values['bot_lifecycle_event_dir']
    if (not re.fullmatch(r'[1-9][0-9]*', fd) or int(fd) < 3 or int(fd) > 0x7fffffff
            or not re.fullmatch(r'[0-9a-f]{64}', digest)
            or not Path(ipc).is_absolute() or not Path(journal).is_absolute()
            or '..' in Path(ipc).parts or '..' in Path(journal).parts or ipc == journal
            or values['bot_start_reservation_gate'] != '1'
            or values['bot_maxgames'] not in ('1', '2', '3', '4', '5')
            or values['autohost_maxgames'] != values['bot_maxgames']):
        raise ValueError('NATIVE_CATALOG_OVERRIDES_REJECTED')
    if 'bot_preload_default_sha256' in values and not re.fullmatch(r'[0-9a-f]{64}',values['bot_preload_default_sha256']):
        raise ValueError('NATIVE_CATALOG_OVERRIDES_REJECTED')
    return dict(values)


def prepare_native(config,username,publish=True, *, catalog_overrides=None, catalog_generation=None, catalog_default=None):
    if (catalog_overrides is None) != (catalog_generation is None):
        raise ValueError('NATIVE_CATALOG_OVERRIDES_REJECTED')
    if catalog_overrides is not None:
        catalog_overrides = _catalog_overrides(catalog_overrides)
        if type(catalog_generation) is not int or not 1 <= catalog_generation < 2**63:
            raise ValueError('NATIVE_CATALOG_GENERATION_REJECTED')
    cfg=config['iccup'];game=config['gameplay'];cwd=Path(config['hostbot']['cwd'])
    source=no_symlinks(cfg['native_config'])
    if not source.is_file() or source.stat().st_size>65536:raise ValueError('NATIVE_CONFIG_REJECTED')
    values={}
    for line in source.read_text(encoding='utf-8').splitlines():
        line=line.strip()
        if not line or line.startswith('#'):continue
        key,sep,value=line.partition('=');key=key.strip();value=value.strip()
        if not sep or not re.fullmatch(r'[A-Za-z0-9_]+',key) or key in values:raise ValueError('NATIVE_CONFIG_REJECTED')
        if key.startswith('bnet'):
            if value:raise ValueError('NATIVE_BNCS_MUST_BE_DISABLED')
            continue
        values[key]=value
    if catalog_default is None:
        game,ai_values,preset_mapcfg=resolve_preset(source,game)
    else:
        from .preload_catalog import PreparedPreset
        if not isinstance(catalog_default,PreparedPreset) or catalog_overrides is None or catalog_overrides.get('bot_preload_default_sha256') != catalog_default.map_sha256:
            raise ValueError('NATIVE_CATALOG_DEFAULT_REJECTED')
        game,ai_values,preset_mapcfg=dict(catalog_default.gameplay),dict(catalog_default.ai),catalog_default.mapcfg
    state_root=private_directory(Path(config['runtime_dir']).parent)
    native_dir=private_directory(state_root/'native')
    if not values.get('bot_motdfile'):
        from .motd import prepare_motd
        values['bot_motdfile']=prepare_motd(source,native_dir/'motd.txt',game['host_name'])
    maps=private_directory(native_dir/'mapcfg' if preset_mapcfg is not None else state_root/'mapcfg')
    operator_maps=no_symlinks(source.parent/'maps')
    if preset_mapcfg is not None:
        atomic_text(maps/'preset.cfg',preset_mapcfg)
        values.update(bot_defaultmap='preset.cfg',bot_mappath=str(source.parent.parent/'maps')+'/')
    elif operator_maps.exists():
        for path in operator_maps.glob('*.cfg'):
            no_symlinks(path)
            if not path.is_file():raise ValueError('NATIVE_MAP_CONFIG_REJECTED')
            target=maps/path.name
            if target.is_symlink():
                if target.resolve()!=path.resolve():raise ValueError('NATIVE_MAP_LINK_REJECTED')
            elif target.exists():raise ValueError('NATIVE_MAP_LINK_REJECTED')
            else:target.symlink_to(path)
    for name in ('sqlite','replays','logs'):private_directory(cwd/name)
    bridge=private_directory(cfg['bridge_dir'])
    values.update({'bnet_server':'','bot_bindaddress':'0.0.0.0','bot_hostport':'6113',
        'bot_language':'/opt/war3/share/language.cfg','bot_mapcfgpath':str(maps)+'/',
        'db_type':'sqlite3','db_sqlite3_file':str(cwd/'sqlite/ghost.dbs'),
        'bot_replaypath':str(cwd/'replays')+'/','bot_savereplays':'1','bot_log':'',
        'bot_hideipaddresses':'1','bot_reconnect':'0','bot_maxgames':'1',
        'autohost_maxgames':'1' if game['autohost_enabled'] else '0',
        'autohost_startplayers':str(game['autostart_players']),
        'autohost_gamename':game['game_name'],'bot_virtualhostname':game['host_name'][:15],
        'bot_statusname':game['host_name'],'bot_autostart_enabled':str(int(game['autostart_enabled'])),
        'bot_autostartplayers':str(game['autostart_players']),
        'bot_countdownseconds':str(game['countdown_seconds']),
        'bot_externalpublishpath':str(bridge/'native-intent.json') if publish else '',
        'bot_externalpublishrealm':'iccup.com' if publish else '',
        'bot_externalpublishhostname':username if publish else '',
        'bot_externalstatuspath':str(bridge/'native-feedback.cfg'),
        'bot_maxplayers':str(game['max_players']),
        'bot_vote_enabled':str(int(game['vote_enabled'])),'bot_vote_required_percent':str(game['vote_required_percent']),
        'bot_vote_min_players':str(game['vote_min_players']),'bot_vote_timeout':str(game['vote_timeout']),
        'bot_vote_cooldown':str(game['vote_cooldown']),
        'bot_lobby_refresh_enabled':str(int(game['lobby_refresh_enabled'])),
        'bot_lobby_refresh_interval':str(game['lobby_refresh_interval']),'bot_lobby_refresh_mode':game['lobby_refresh_mode']})
    values.update(ai_values)
    if catalog_overrides is not None: values.update(catalog_overrides)
    if not values.get('bot_defaultmap'):raise ValueError('NATIVE_MAP_REQUIRED')
    effective=native_dir/('ghost.cfg' if catalog_generation is None else f'ghost-catalog-{catalog_generation}.cfg')
    atomic_text(effective,''.join(k+' = '+v+'\n' for k,v in sorted(values.items())))
    return effective
