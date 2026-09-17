from __future__ import annotations

import argparse
import os
import time

from v2r_auto.automation_service import UnifiedAutomationBackend
from v2r_auto.control_server import launch_control_window
from v2r_auto.remote_control import RemoteHttpsControlServer


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--public-host",
        default=os.environ.get("V2R_CONTROL_PUBLIC_HOST", ""),
        help="원격 PC가 접속할 공인 IP 주소",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("V2R_CONTROL_PORT", "8765")),
    )
    args = parser.parse_args()
    backend = UnifiedAutomationBackend()
    try:
        server = RemoteHttpsControlServer(
            backend,
            data_dir=backend.data_dir,
            public_host=args.public_host,
            port=args.port,
        )
    except OSError:
        server = RemoteHttpsControlServer(
            backend,
            data_dir=backend.data_dir,
            public_host=args.public_host,
            port=0,
        )
    server.start()
    launch_control_window(
        server.local_url,
        backend.data_dir / "control-browser-profile",
    )
    try:
        while not backend.shutdown_event.wait(0.5):
            time.sleep(0)
    except KeyboardInterrupt:
        pass
    finally:
        backend.close()
        server.close()


if __name__ == "__main__":
    main()
