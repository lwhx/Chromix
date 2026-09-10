#!/usr/bin/env python3
"""Summarize an existing capture with tshark; packet presence is not verification.

No capture is started and no network requests are sent. HTTP/2 inside TLS needs
the matching TLS key log. The original PCAP and optional key log stay untouched.
"""
from __future__ import annotations

import argparse
import json
import ipaddress
from pathlib import Path
import subprocess

from device_pool import file_hash, load_json


def packet_details(layers):
    fields = ('ip.src', 'ip.dst', 'ipv6.src', 'ipv6.dst', 'tcp.srcport', 'tcp.dstport',
              'udp.srcport', 'udp.dstport', 'tcp.stream', 'udp.stream',
              'tls.handshake.version', 'tls.handshake.ciphersuite',
              'tls.handshake.extension.type', 'tls.handshake.extensions_alpn_str',
              'http2.settings.id', 'http2.settings.value', 'http2.header.name',
              'http2.header.value', 'quic.version', 'dns.qry.name', 'dns.qry.type',
              'stun.type', 'http.request.method', 'http.host')
    return {field:[str(v) for v in values(layers, field)] for field in fields
            if list(values(layers, field))}


def route_check(packets, policy):
    if not isinstance(policy, dict) or set(policy) != {'client_ips', 'allowed_egress'}:
        raise ValueError('route policy requires client_ips and allowed_egress')
    if not isinstance(policy['client_ips'], list) or not policy['client_ips']:
        raise ValueError('client_ips must be a nonempty list')
    clients = {str(ipaddress.ip_address(value)) for value in policy['client_ips']}
    if not isinstance(policy['allowed_egress'], list):
        raise ValueError('allowed_egress must be a list')
    allowed = set()
    for item in policy['allowed_egress']:
        if not isinstance(item, dict) or set(item) != {'protocol', 'ip', 'port'}:
            raise ValueError('egress rule requires protocol, ip, port')
        if item['protocol'] not in ('tcp', 'udp') or type(item['port']) is not int or not 1 <= item['port'] <= 65535:
            raise ValueError('invalid egress protocol/port')
        allowed.add((item['protocol'], str(ipaddress.ip_address(item['ip'])), item['port']))
    checked, violations, ambiguous = [], [], []
    for packet in packets:
        layers = packet['_source']['layers']
        detail = packet_details(layers)
        sources = detail.get('ip.src', []) + detail.get('ipv6.src', [])
        targets = detail.get('ip.dst', []) + detail.get('ipv6.dst', [])
        frame = list(values(layers, 'frame.number'))
        if not any(str(ipaddress.ip_address(ip)) in clients for ip in sources):
            continue
        transports = [protocol for protocol in ('tcp', 'udp') if protocol in layers]
        if len(sources) != 1 or len(targets) != 1 or len(transports) != 1:
            ambiguous.append(frame)
            continue
        protocol = transports[0]
        ports = detail.get(protocol + '.dstport', [])
        if len(ports) != 1 or not ports[0].isdigit():
            ambiguous.append(frame)
            continue
        endpoint = (protocol, str(ipaddress.ip_address(targets[0])), int(ports[0]))
        checked.append(frame)
        if endpoint not in allowed:
            violations.append({'frames':frame, 'protocol':protocol, 'destination':endpoint[1], 'port':endpoint[2]})
    return {'status':'mismatch' if violations else 'inconclusive' if ambiguous or not checked else 'observed_match',
            'checked_packets':len(checked), 'violations':violations, 'ambiguous_frames':ambiguous,
            'route_verified':False, 'limitation':'Captured source-IP traffic only; no process attribution or capture completeness proof.'}


def values(tree, key):
    if isinstance(tree, dict):
        for name, value in tree.items():
            if name == key:
                yield from value if isinstance(value, list) else [value]
            else:
                yield from values(value, key)
    elif isinstance(tree, list):
        for value in tree:
            yield from values(value, key)


def summarize(packets):
    if not isinstance(packets, list):
        raise ValueError('tshark JSON must be a packet array')
    matches = {key:[] for key in ('tls', 'http2', 'quic', 'dns', 'proxy', 'webrtc')}
    details = []
    for packet in packets:
        layers = packet['_source']['layers']
        frames = list(values(layers, 'frame.number'))
        if len(frames) != 1 or not str(frames[0]).isdigit():
            raise ValueError('packet missing a unique frame number')
        frame = str(frames[0])
        details.append({'frame':frame, 'fields':packet_details(layers)})
        if '1' in {str(v) for v in values(layers, 'tls.handshake.type')}:
            matches['tls'].append(frame)
        for protocol in ('http2', 'quic', 'dns'):
            if protocol in layers:
                matches[protocol].append(frame)
        if 'CONNECT' in set(values(layers, 'http.request.method')):
            matches['proxy'].append(frame)
        if 'stun' in layers:
            matches['webrtc'].append(frame)
    return {'packet_count':len(packets), 'packet_details':details, 'protocols':{
        key:{'status':'observed' if frames else 'not_observed', 'frames':frames,
             'verified':False} for key, frames in matches.items()},
        'limitations':[
            'TLS means decoded ClientHello; encrypted payloads may hide HTTP/2.',
            'Proxy means HTTP CONNECT only, not proof of the effective route.',
            'WebRTC means STUN packets only, not ICE/TURN connection success.',
            'Absence is not lack of support; QUIC parameters may be encrypted.',
            'No TLS/HTTP2 fingerprint comparison, process attribution, or device binding was performed.',
        ]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pcap', type=Path, required=True)
    parser.add_argument('--keylog', type=Path)
    parser.add_argument('--tshark', default='tshark')
    parser.add_argument('--max-packets', type=int, default=10000)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--route-policy', type=Path, help='expected client IPs and exact allowed TCP/UDP endpoints')
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.max_packets <= 100000:
            raise ValueError('max-packets must be in [1, 100000]')
        if args.output.exists():
            raise ValueError('output must be a new file')
        before = file_hash(args.pcap)
        command = [args.tshark, '-n', '-r', str(args.pcap.resolve()), '-c', str(args.max_packets), '-T', 'json']
        if args.keylog:
            if not args.keylog.is_file():
                raise ValueError('key log does not exist')
            command += ['-o', f'tls.keylog_file:{args.keylog.resolve()}']
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=120, check=True)
        packets = json.loads(result.stdout)
        report = summarize(packets)
        if args.route_policy:
            policy_hash = file_hash(args.route_policy)
            report['route_check'] = route_check(packets, load_json(args.route_policy))
            if file_hash(args.route_policy) != policy_hash:
                raise ValueError('route policy changed during parsing')
            report['route_policy_sha256'] = policy_hash
        if before != file_hash(args.pcap):
            raise ValueError('capture changed during parsing')
        report.update({'capture_sha256':before, 'packet_limit':args.max_packets,
                       'possibly_truncated':report['packet_count'] >= args.max_packets,
                       'keylog_used':args.keylog is not None, 'parser_stderr':result.stderr,
                       'status':'observed', 'qualification':'not_verified'})
        if args.route_policy and report['possibly_truncated'] and report['route_check']['status'] == 'observed_match':
            report['route_check']['status'] = 'inconclusive'
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as stream:
            json.dump(report, stream, indent=2)
            stream.write('\n')
        print(json.dumps({'status':report['route_check']['status'] if args.route_policy else 'observed',
                          'qualification':'not_verified', 'output':str(args.output)}))
        return int(args.route_policy is not None and report['route_check']['status'] != 'observed_match')
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({'status':'error', 'message':str(error)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
