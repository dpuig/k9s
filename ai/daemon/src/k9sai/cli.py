"""k9sai-daemon entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from k9sai import config, kube, server
from k9sai.backend import SDKBackend, build_config
from k9sai.tasks import Deps


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="k9sai-daemon")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the daemon on a unix socket")
    s.add_argument("--socket", type=Path, default=None)
    s.add_argument("--config", type=Path, default=None)
    c = sub.add_parser("check-config", help="validate config and the SDK lockdown")
    c.add_argument("--config", type=Path, default=None)
    sub.add_parser("socket-path", help="print the default socket path")
    args = p.parse_args(argv)

    if args.cmd == "socket-path":
        print(server.default_socket())
        return 0
    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        print(f"k9sai: config error: {e}", file=sys.stderr)
        return 2
    if args.cmd == "check-config":
        for b in cfg.backends.values():
            build_config(b, "check", [])  # raises if builtin tools are not locked down
            print(f"ok  {b.name}: {b.type} {b.model or b.model_path}")
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    deps = Deps(cfg=cfg, backend=SDKBackend, kube=kube.connect)
    asyncio.run(server.serve(deps, args.socket or server.default_socket()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
