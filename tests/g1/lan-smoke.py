#!/usr/bin/env python3
"""Native LAN advertisement/download/solo-autostart probe, not a game engine.

Never emits calibration, ACK or a fabricated callback pair. Capture mode may
only be promoted after the actual user client's g1:hello action is observed.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import struct
import time
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('offline_lifecycle',ROOT/'tests/offline/ghost_lifecycle.py')
offline=importlib.util.module_from_spec(spec);spec.loader.exec_module(offline)

def discovery(server,broadcast,name,timeout=50):
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.bind((broadcast,6112));sock.settimeout(timeout)
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            data,source=sock.recvfrom(65535)
            if source[0]!=server or len(data)<24 or data[:2]!=b'\xf7\x30':continue
            if struct.unpack_from('<H',data,2)[0]!=len(data):continue
            title=data[20:].split(b'\0',1)[0].decode('utf8','replace')
            if title!=name:continue
            return dict(address=server,port=struct.unpack_from('<H',data,len(data)-2)[0],
                counter=struct.unpack_from('<I',data,12)[0],key=struct.unpack_from('<I',data,16)[0],
                name=title,war3_version=struct.unpack_from('<I',data,8)[0])
    raise TimeoutError('LAN advertisement not observed')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server',required=True);parser.add_argument('--broadcast',required=True)
    parser.add_argument('--name',default='G1 Bridge 1.26a');parser.add_argument('--exercise',action='store_true')
    parser.add_argument('--expected-map-sha256')
    args=parser.parse_args();info=discovery(args.server,args.broadcast,args.name)
    result={'advertisement':{k:v for k,v in info.items() if k!='key'},'synthetic_peer':args.exercise,'engine_executed':False}
    if args.exercise:
        if not args.expected_map_sha256:parser.error('--exercise requires --expected-map-sha256')
        peer=offline.Peer(info,'G1LANProbe',download=True)
        try:
            peer.sock.settimeout(.2);deadline=time.monotonic()+45
            while time.monotonic()<deadline:
                try:peer.receive()
                except socket.timeout:continue
                if peer.loaded and peer.counts[0x0c]>=5:break
            assert peer.loaded,'native solo autostart did not finish'
            assert peer.counts[0x0c]>=5,'game actions did not flow'
            digest=hashlib.sha256(peer.mapdata).hexdigest()
            assert digest==args.expected_map_sha256,'download hash mismatch'
            result.update(download_sha256=digest,download_bytes=len(peer.mapdata),solo_autostart=True,action_timeslots=peer.counts[0x0c])
        finally:peer.sock.close()
    print(json.dumps(result,indent=2))
if __name__=='__main__':main()
