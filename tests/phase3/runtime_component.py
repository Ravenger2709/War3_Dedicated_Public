"""Synthetic subprocess fixtures, used only by offline controller tests."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import time

from src.runtime.files import atomic_json

p=argparse.ArgumentParser();p.add_argument('role');p.add_argument('root',type=Path);p.add_argument('generation',type=int)
a=p.parse_args();stop=False

def requested(*_):
 global stop
 stop=True

signal.signal(signal.SIGTERM,requested)
sock=None
if a.role=='native':
 sock=socket.socket();sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);sock.bind(('127.0.0.1',6113));sock.listen()
 print('[GHOST] offline initialization complete',flush=True)
 db=a.root/'persistent-count';db.write_text(str(int(db.read_text())+1 if db.exists() else 1))
 (a.root/'native.pid').write_text(str(os.getpid()))
started=time.monotonic()
try:
 while not stop:
  controls={}
  try:controls=json.loads((a.root/'control.json').read_text())
  except (FileNotFoundError,ValueError):pass
  now=time.monotonic()
  if a.role=='native':
   if not controls.get('native_stale'):
    print('[HOSTBOT_STATE] '+json.dumps({'version':1,'phase':controls.get('phase','LOBBY'),'game_name':'Synthetic Lobby','map_name':'Synthetic Map','players':2,'max_players':12,'autostart_players':10,'votes':0,'votes_required':2,'countdown':0,'host_counter':1,'refresh_count':0,'refresh_skipped':0,'refresh_sequence':0}),flush=True)
  elif a.role=='star':
   if not controls.get('star_stale'):
    terminal=controls.get('terminal_engine',False)
    stamp=max(started,now-1) if controls.get('terminal_engine_stale') else now
    atomic_json(a.root/'star-status.json',{'version':1,'worker_pid':os.getpid(),'generation':a.generation,'phase':'ERROR' if terminal else 'RUNNING','launcher_pid':os.getpid(),'launcher_alive':not terminal,'game_engine_observed':terminal or controls.get('engine',False),'observed_monotonic_seconds':stamp})
    if terminal:raise SystemExit(70)
  elif a.role=='relay':
   if not controls.get('relay_stale'):
    authenticated=not controls.get('disconnect',False)
    atomic_json(a.root/'bridge/relay-status.json',{'schema':1,'relay_pid':os.getpid()+int(controls.get('wrong_relay_pid',False)),'relay_started_monotonic_seconds':started,'session_generation':1,'connected':authenticated,'authenticated':authenticated,'realm_connection_healthy':authenticated,'expected_identity_confirmed':authenticated,'advertisement_accepted':authenticated and controls.get('phase')!='IN_GAME','native_intent_seen':True,'native_heartbeat_fresh':True,'native_pid':int((a.root/'native.pid').read_text()),'native_host_counter':1,'observed_monotonic_seconds':now,'last_ack_monotonic_seconds':now,'advertisements_sent':1,'publication_acks':1,'server_publication_status':0,'server_proof_status':controls.get('proof_status',0)})
  time.sleep(.05)
finally:
 if sock:sock.close()
