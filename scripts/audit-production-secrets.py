#!/usr/bin/env python3
"""Offline content audit: never emit a secret value, fingerprint or matching path."""
import argparse
import configparser
import json
from pathlib import Path
import sys
import tarfile
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.bridge.secrets import load_credentials
from src.runtime.files import private_bytes


class Matcher:
    def __init__(self,values):
        self.patterns=tuple({value.encode(encoding) for value in values if value
                             for encoding in ('utf-8','utf-16le')})
        if not self.patterns:raise ValueError('SECRET_PATTERNS_MISSING')
        self.overlap=max(map(len,self.patterns))-1
        self.bytes_scanned=0

    def scan(self,source):
        tail=b''
        while chunk:=source.read(1024*1024):
            self.bytes_scanned+=len(chunk);data=tail+chunk
            if any(pattern in data for pattern in self.patterns):raise ValueError('SECRET_CONTENT_FOUND')
            tail=data[-self.overlap:] if self.overlap else b''


def profile_patterns(profile):
    cfg=configparser.RawConfigParser();cfg.optionxform=str
    cfg.read_string(private_bytes(profile/'launcher.ini',16384).decode())
    values=[]
    for name in ('Password','ListLogins','AutoLoginCache'):
        value=cfg.get('General',name,fallback='')
        if not value:continue
        try:decoded=json.loads(value)
        except ValueError:decoded=value
        # QSettings serializes an absent password as an empty quoted string.
        # Quotes alone are not credential material and occur in public JSON.
        if isinstance(decoded,str) and decoded:
            values.extend((value,decoded))
    return values


def scan_tar(stream,matcher):
    files=0
    with tarfile.open(fileobj=stream,mode='r|*') as archive:
        for member in archive:
            from io import BytesIO
            matcher.scan(BytesIO((member.name+'\0'+member.linkname).encode()))
            if member.isfile():
                matcher.scan(archive.extractfile(member));files+=1
    return files


def image_layers(path,matcher):
    layers=0;files=0
    with tarfile.open(path,mode='r|*') as image:
        for member in image:
            if not member.isfile():continue
            # Scan manifest/config/attestation bytes as well as every rootfs,
            # including a secret deleted in a later filesystem layer.
            with tempfile.TemporaryFile() as blob:
                source=image.extractfile(member)
                while chunk:=source.read(1024*1024):blob.write(chunk)
                blob.seek(0)
                if member.name.endswith('/layer.tar') or member.name.startswith('blobs/sha256/'):
                    try:
                        files+=scan_tar(blob,matcher);layers+=1
                    except tarfile.ReadError:
                        blob.seek(0);matcher.scan(blob)
                else:matcher.scan(blob)
    if not layers:raise ValueError('IMAGE_LAYERS_MISSING')
    return layers,files


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image-archive',type=Path,required=True)
    p.add_argument('--credentials',type=Path,required=True)
    p.add_argument('--profile',type=Path,required=True)
    p.add_argument('--source-archive',type=Path,required=True)
    a=p.parse_args()
    try:
        credentials=load_credentials(a.credentials)
        values=[credentials.password,*profile_patterns(a.profile)]
        matcher=Matcher(values)
        layers,files=image_layers(a.image_archive,matcher)
        with a.source_archive.open('rb') as source:source_files=scan_tar(source,matcher)
        print(json.dumps({'result':'PASS','network':'none','image_layers_scanned':layers,
                          'image_files_scanned':files,'source_files_scanned':source_files,
                          'bytes_scanned':matcher.bytes_scanned,
                          'plaintext_password_or_profile_cache_matches':0,
                          'secret_values_or_fingerprints_emitted':False}))
        return 0
    except Exception as error:
        reason=str(error) if isinstance(error,ValueError) and str(error) in {'SECRET_CONTENT_FOUND','SECRET_PATTERNS_MISSING','IMAGE_LAYERS_MISSING'} else type(error).__name__
        print(json.dumps({'result':'FAIL','reason':'SECRET_AUDIT_REJECTED','error_code':reason}));return 1


if __name__=='__main__':raise SystemExit(main())
