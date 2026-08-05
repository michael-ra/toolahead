#!/usr/bin/env python3
"""Fuehrt einen einmaligen Prefetch-Replay aus, sonst den Original-Call.

Dieses Programm wird nicht direkt als Hook ausgefuehrt. Der PreToolUse-Hook
schreibt einen Aufruf davon in ``updatedInput.command``; dadurch erzeugt das
normale Bash-Tool den Tool-Result mit der echten stdout/stderr/Exit-Code-
Semantik. Das Token ist single-use und kurzlebig.
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request


def _exit_code(value) -> int:
    try:
        code = int(value)
    except (TypeError, ValueError):
        return 1
    return code if 0 <= code <= 125 else 1


def _fallback(encoded: str) -> int:
    try:
        command = base64.urlsafe_b64decode(encoded.encode()).decode()
    except (ValueError, UnicodeDecodeError):
        print("prefetch replay failed and fallback was invalid", file=sys.stderr)
        return 1
    return _exit_code(subprocess.run(command, shell=True).returncode)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url")
    parser.add_argument("--token")
    parser.add_argument("--file")
    parser.add_argument("--timeout", type=float, default=130)
    parser.add_argument("--fallback-b64", required=True)
    args = parser.parse_args()

    try:
        if args.file:
            try:
                with open(args.file, encoding="utf-8") as handle:
                    result = json.load(handle)
            finally:
                try:
                    os.unlink(args.file)
                except OSError:
                    pass
        elif args.url and args.token:
            query = urllib.parse.urlencode({"token": args.token})
            url = f"{args.url}?{query}"
            with urllib.request.urlopen(url, timeout=args.timeout) as response:
                result = json.loads(response.read())
        else:
            return _fallback(args.fallback_b64)
        if not result.get("ok"):
            return _fallback(args.fallback_b64)
        sys.stdout.write(result.get("stdout", ""))
        sys.stdout.flush()
        sys.stderr.write(result.get("stderr", ""))
        sys.stderr.flush()
        return _exit_code(result.get("exit_code", 0))
    except Exception:
        return _fallback(args.fallback_b64)


if __name__ == "__main__":
    raise SystemExit(main())
