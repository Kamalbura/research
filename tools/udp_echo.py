#!/usr/bin/env python3
"""Simple UDP echo server for local testing.

Usage: python tools/udp_echo.py --host 127.0.0.1 --port 47001
"""
import argparse
import signal
import socket
import threading
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--host', default='192.168.0.101')
    p.add_argument('--port', type=int, default=47001)
    p.add_argument('--timeout', type=float, default=1.0,
                   help='socket recv timeout in seconds (used for responsive shutdown)')
    args = p.parse_args()

    stop_event = threading.Event()

    def _handle_signal(signum, frame):
        print(f'received signal {signum}, shutting down...')
        stop_event.set()

    # install signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except AttributeError:
        # SIGTERM may not exist on some platforms (e.g., Windows old py versions)
        pass

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((args.host, args.port))
        s.settimeout(args.timeout)
        print(f'UDP echo server listening on {args.host}:{args.port} (timeout={args.timeout}s)')

        while not stop_event.is_set():
            try:
                data, addr = s.recvfrom(65536)
            except socket.timeout:
                # loop again, checking stop_event so Ctrl-C is responsive
                continue
            except OSError:
                # socket closed from another thread or during shutdown
                break

            # echo back exactly what we received
            try:
                s.sendto(data, addr)
            except OSError:
                # peer gone or socket closed, ignore and continue
                continue

    except Exception as exc:
        print(f'udp_echo encountered error: {exc}')
    finally:
        try:
            s.close()
        except Exception:
            pass
        # give a moment for prints to flush
        time.sleep(0.05)
        print('udp_echo exiting')


if __name__ == '__main__':
    main()
