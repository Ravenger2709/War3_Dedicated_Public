"""Validate non-secret production settings independently of account files."""
import ipaddress
import math
import re
from pathlib import Path

ADAPTER='original-star-v1'
GAMEPLAY_DEFAULTS={
    'map_preset':'manual','map_cycle':['manual'],'max_concurrent_matches':3,
    'host_name':'War3HostBot','game_name':'War3 Dedicated','game_port':6113,
    'max_players':12,'autostart_enabled':True,'autostart_players':10,'countdown_seconds':5,
    'vote_enabled':True,'vote_required_percent':60,'vote_min_players':2,
    'vote_timeout':60,'vote_cooldown':30,'lobby_refresh_enabled':True,
    'lobby_refresh_interval':10,'lobby_refresh_mode':'slot_toggle','lobby_message_interval':20,
    'autohost_enabled':True,'reconnect_enabled':True,
}
ICCUP_DEFAULTS={
    'adapter':ADAPTER,'upstream':'57.129.6.91','relay_port':16112,
    'vendor':'/vendor/star','profile':'/vendor/profile',
    'redist':'/vendor/redist/vc_redist.x86.exe','game_metadata':'/vendor/game',
    'prefix':'/data/prefix','session':'/data/session',
    'manifest':'/opt/war3/config/star-vendor-files.json',
    'quit_helper':'/opt/war3/bin/quit-launcher.exe',
    'native_config':'/data/config/ghost.cfg','bridge_dir':'/data/state/bridge',
    'auth_timeout':180,'disconnect_grace':5,'publication_interval':30,
    'metadata_ttl':3,'native_state_ttl':15,
    'circuit_cooldown':300,
}


def bounded_int(value,lo,hi):
    return type(value) is int and lo<=value<=hi


def positive(value,lo,hi):
    return type(value) in (int,float) and math.isfinite(value) and lo<=value<=hi


def gameplay_config(raw):
    if not isinstance(raw,dict) or set(raw)-set(GAMEPLAY_DEFAULTS):raise ValueError('GAMEPLAY_SETTINGS_REJECTED')
    cfg={**GAMEPLAY_DEFAULTS,**raw}
    cycle=raw['map_cycle'] if 'map_cycle' in raw else [raw.get('map_preset','manual')]
    if (type(cycle) is not list or not 1<=len(cycle)<=32 or
        any(type(x) is not str or re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',x) is None for x in cycle)):
        raise ValueError('MAP_CYCLE_REJECTED')
    cfg['map_cycle']=list(cycle)
    cfg['map_preset']=cycle[0]
    if not bounded_int(cfg['max_concurrent_matches'],1,5):raise ValueError('MAP_CYCLE_MATCH_LIMIT_REJECTED')
    if not isinstance(cfg['map_preset'],str) or re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',cfg['map_preset']) is None:raise ValueError('GAMEPLAY_PRESET_REJECTED')
    for k in ('host_name','game_name'):
        value=cfg[k]
        if not isinstance(value,str) or not 1<=len(value.encode())<=24 or any(ord(c)<32 or c in '=[]' for c in value):raise ValueError('GAMEPLAY_NAME_REJECTED')
    if cfg['game_port']!=6113 or type(cfg['game_port']) is not int:raise ValueError('GAME_PORT_MUST_BE_6113')
    for key,lo,hi in (('max_players',2,12),('autostart_players',2,12),('countdown_seconds',0,60),('vote_required_percent',1,100),('vote_min_players',2,12),('vote_timeout',5,600),('vote_cooldown',0,600),('lobby_refresh_interval',10,3600),('lobby_message_interval',10,3600)):
        if not bounded_int(cfg[key],lo,hi):raise ValueError('GAMEPLAY_NUMBER_REJECTED')
    for key in ('autostart_enabled','vote_enabled','lobby_refresh_enabled','autohost_enabled','reconnect_enabled'):
        if type(cfg[key]) is not bool:raise ValueError('GAMEPLAY_BOOLEAN_REJECTED')
    if cfg['vote_min_players']>cfg['max_players'] or cfg['autostart_enabled'] and cfg['autostart_players']>cfg['max_players']:raise ValueError('GAMEPLAY_PLAYER_LIMIT_REJECTED')
    if cfg['lobby_refresh_mode']!='slot_toggle':raise ValueError('REFRESH_MODE_REJECTED')
    if re.fullmatch(r'[A-Za-z0-9_. -]{1,24}',cfg['host_name']) is None:raise ValueError('HOST_DISPLAY_NAME_REJECTED')
    return cfg


def iccup_config(raw):
    if not isinstance(raw,dict) or set(raw)-set(ICCUP_DEFAULTS):raise ValueError('ICCUP_SETTINGS_REJECTED')
    cfg={**ICCUP_DEFAULTS,**raw}
    if cfg['adapter']!=ADAPTER:raise ValueError('ICCUP_ADAPTER_REJECTED')
    ip=ipaddress.IPv4Address(cfg['upstream'])
    if not ip.is_global:raise ValueError('ICCUP_GATEWAY_REJECTED')
    if cfg['relay_port']!=16112 or type(cfg['relay_port']) is not int:raise ValueError('RELAY_PORT_MUST_BE_16112')
    for key,lo,hi in (('auth_timeout',30,600),('disconnect_grace',1,60),('publication_interval',5,300),('metadata_ttl',1,10),('native_state_ttl',5,60),('circuit_cooldown',30,3600)):
        if not positive(cfg[key],lo,hi):raise ValueError('ICCUP_TIMEOUT_REJECTED')
    for key in ('vendor','profile','redist','game_metadata','prefix','session','manifest','quit_helper','native_config','bridge_dir'):
        value=cfg[key]
        if not isinstance(value,str) or not Path(value).is_absolute() or any(ord(c)<32 for c in value) or '..' in Path(value).parts:raise ValueError('ICCUP_PATH_REJECTED')
    if cfg['prefix']==cfg['session'] or any(cfg[k]==cfg['prefix'] or Path(cfg[k]).is_relative_to(cfg['prefix']) for k in ('vendor','profile','game_metadata')):raise ValueError('ICCUP_PATH_OVERLAP')
    return cfg
