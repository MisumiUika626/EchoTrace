"""Start the installed CLI in an empty directory; use only loopback HTTP."""
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    output = ROOT / "data" / "startup" / str(time.time_ns())
    output.mkdir(parents=True)
    data = output / "app-data"
    entry = Path(sys.executable).with_name("echotrace.exe" if os.name == "nt" else "echotrace")
    assert entry.is_file(), "Install EchoTrace in this Python environment first"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    command = [str(entry), "serve", "--port", str(port), "--data-dir", str(data), "--no-browser"]
    with (output / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=output, stdout=log, stderr=log,
                                   env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        try:
            deadline = time.monotonic() + 30
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=2) as client:
                while True:
                    assert process.poll() is None, "CLI exited; inspect server.log"
                    try:
                        response = client.get("/api/health")
                        response.raise_for_status()
                        break
                    except httpx.HTTPError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("CLI did not become ready")
                        time.sleep(.05)
                health = response.json()["data"]
                assert health["status"] == "ok" and health["local_only"]
                assert health["llm"]["probed"] is False and health["qce"]["probed"] is False
                assert client.get("/api/contacts").json()["data"] == []
                page = client.get("/")
                assert page.status_code == 200 and 'id="root"' in page.text
                assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', page.text)
                assert assets and all(client.get(asset).status_code == 200 for asset in assets)
                assert client.get("/api/settings", headers={"Origin": "https://external.invalid"}).status_code == 403
                assert data.joinpath("echotrace.db").is_file()
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
    result = {"status": "passed", "entry": "echotrace serve", "empty_data_directory": True,
              "database_created": True, "static_assets_checked": len(assets),
              "local_api": True, "external_origin_rejected": True,
              "browser_auto_open_tested": False, "clean_machine_tested": False,
              "health": health}
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**result, "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
