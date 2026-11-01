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


def native_state(line):
    if not line.startswith('[HOSTBOT_STATE] '):return None
    obj=json.loads(line[len('[HOSTBOT_STATE] '):])
    if not isinstance(obj,dict) or type(obj.get('version')) is not int or obj['version']!=1 or obj.get('phase') not in PHASES:raise ValueError('NATIVE_STATE_REJECTED')
    out={'version':1,'phase':obj['phase']}
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
    for key in ('loading','realm_online','feedback_valid','ai_supported'):
        value=obj.get(key,False)
        if type(value) is not bool:raise ValueError('NATIVE_STATE_REJECTED')
        out[key]=value
    return out


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


def prepare_native(config,username,publish=True):
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
    game,ai_values,preset_mapcfg=resolve_preset(source,game)
    state_root=private_directory(Path(config['runtime_dir']).parent)
    native_dir=private_directory(state_root/'native')
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
    if not values.get('bot_defaultmap'):raise ValueError('NATIVE_MAP_REQUIRED')
    effective=native_dir/'ghost.cfg'
    atomic_text(effective,''.join(k+' = '+v+'\n' for k,v in sorted(values.items())))
    return effective
