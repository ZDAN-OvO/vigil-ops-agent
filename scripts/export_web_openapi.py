"""生成页面 API 契约。"""

import argparse
import json

from aiops.config import PROJECT_ROOT, Settings
from aiops.web.app import build_web_app

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
path = PROJECT_ROOT / "web" / "openapi.json"
content = json.dumps(
    build_web_app(Settings(_env_file=None), None).openapi(), ensure_ascii=False, indent=2
) + "\n"
if args.check:
    if path.read_text() != content:
        raise SystemExit("接口契约已变化，请重新执行 uv run python scripts/export_web_openapi.py")
else:
    path.write_text(content)
