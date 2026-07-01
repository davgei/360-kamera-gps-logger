"""OSC HTTP client for the Insta360 ONE X (OSC model 'Insta360 One2', apiLevel 2).

Verified command sequence (Insta360 official OSC docs, adversarially checked):
setOptions captureMode=video -> startCapture -> stopCapture (returns
results.fileUrls). All commands POST to /osc/commands/execute with the
X-XSRF-Protected header and must be issued strictly sequentially. Stdlib only.
"""

from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_HOST = "192.168.42.1"

_HEADERS = {
    "Content-Type": "application/json;charset=utf-8",
    "Accept": "application/json",
    "X-XSRF-Protected": "1",
}


class OscError(Exception):
    pass


class OneXCamera:
    def __init__(self, host: str = DEFAULT_HOST, timeout: float = 15.0) -> None:
        self.host = host
        self.timeout = timeout
        # Serialize control-plane calls; the ONE X is sensitive to overlapping requests.
        self._lock = threading.Lock()

    def _execute(self, name: str, parameters: dict | None = None) -> dict:
        body: dict = {"name": name}
        if parameters is not None:
            body["parameters"] = parameters
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"http://{self.host}/osc/commands/execute", data=data, method="POST", headers=_HEADERS
        )
        with self._lock:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    result = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                try:
                    result = json.loads(exc.read().decode("utf-8"))
                except Exception:
                    raise OscError(f"{name}: HTTP {exc.code} {exc.reason}")
        if result.get("state") == "error":
            error = result.get("error", {})
            raise OscError(f"{name}: {error.get('code', 'error')} — {error.get('message', '')}")
        return result

    def get_info(self) -> dict:
        request = urllib.request.Request(f"http://{self.host}/osc/info", method="GET")
        with self._lock, urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_state(self) -> dict:
        request = urllib.request.Request(
            f"http://{self.host}/osc/state", data=b"", method="POST", headers=_HEADERS
        )
        with self._lock, urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def set_video_mode(self) -> None:
        self._execute("camera.setOptions", {"options": {"captureMode": "video"}})

    def start_capture(self) -> None:
        self._execute("camera.startCapture")

    def stop_capture(self) -> list[str]:
        result = self._execute("camera.stopCapture")
        return result.get("results", {}).get("fileUrls", [])

    def download(self, url: str, dest: Path) -> None:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=self.timeout) as response, open(dest, "wb") as out:
            shutil.copyfileobj(response, out)
