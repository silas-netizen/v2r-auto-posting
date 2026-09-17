from __future__ import annotations

import time

from v2r_auto.automation_service import UnifiedAutomationBackend
from v2r_auto.control_server import (
    LocalHttpsControlServer,
    launch_control_window,
)


def main() -> None:
    backend = UnifiedAutomationBackend()
    try:
        server = LocalHttpsControlServer(
            backend,
            data_dir=backend.data_dir,
            port=8765,
        )
    except OSError:
        server = LocalHttpsControlServer(
            backend,
            data_dir=backend.data_dir,
            port=0,
        )
    server.start(open_browser=False)
    launch_control_window(
        server.url,
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
