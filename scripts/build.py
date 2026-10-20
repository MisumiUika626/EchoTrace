import shutil
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
npm = shutil.which("npm.cmd") or shutil.which("npm")
if not npm:
    raise SystemExit("请先安装 Node.js 22+，再运行此脚本。")
temp = ROOT / "web" / ".build-tmp"
temp.mkdir(exist_ok=True)
os.environ.update(TEMP=str(temp), TMP=str(temp))
subprocess.run([npm, "ci"], cwd=ROOT / "web", check=True)
subprocess.run([npm, "run", "build"], cwd=ROOT / "web", check=True)
shutil.copytree(ROOT / "web" / "dist", ROOT / "echotrace" / "static", dirs_exist_ok=True)
shutil.copytree(ROOT / "prompts", ROOT / "echotrace" / "templates", dirs_exist_ok=True)
print("前端与提示词已打包。运行 echotrace 即可启动。")
