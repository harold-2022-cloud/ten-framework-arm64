#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
#
"""python3 -m meeting_screen --meetings-dir DIR [--listen HOST:PORT]
[--showcase ID] [--recent-min N]: the demo screen's service."""

import argparse
import sys

from .server import Screen, make_server


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m meeting_screen",
        description="The meeting demo screen: the board's meetings, read from disk.",
    )
    p.add_argument(
        "--meetings-dir",
        required=True,
        help="where the meetings land (MEETINGS_DIR)",
    )
    p.add_argument(
        "--listen",
        default="127.0.0.1:8766",
        help="host:port (default 127.0.0.1:8766)",
    )
    p.add_argument(
        "--showcase", default=None, help="the meeting PageDown shows"
    )
    p.add_argument(
        "--recent-min",
        type=float,
        default=120.0,
        help="how long a finished meeting stays on, from its end (120)",
    )
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    screen = Screen(args.meetings_dir, args.showcase, args.recent_min)
    httpd = make_server(screen, args.listen)
    print(
        f"meeting-screen: {args.meetings_dir} on http://{args.listen}/",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
