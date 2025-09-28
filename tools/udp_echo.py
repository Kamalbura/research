#!/usr/bin/env python3
"""Simple UDP echo server for local testing.

Usage: python tools/udp_echo.py --host 127.0.0.1 --port 47001
"""
import argparse
import socket


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=47001)
    args = p.parse_args()

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind((args.host, args.port))
        print(f'UDP echo server listening on {args.host}:{args.port}')
        while True:
            data, addr = s.recvfrom(65536)
            # echo back exactly what we received
            s.sendto(data, addr)


if __name__ == '__main__':
    main()
