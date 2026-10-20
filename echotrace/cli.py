import argparse
import socket
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn


def response_data(response):
    try:
        result = response.json()
    except ValueError as exc:
        raise SystemExit("本机服务未返回 EchoTrace 数据，请确认端口对应本工作台。") from exc
    if not isinstance(result, dict) or "ok" not in result:
        raise SystemExit("本机服务响应格式不符，请确认端口对应 EchoTrace。")
    if not result["ok"]:
        raise SystemExit(result.get("error", {}).get("message", "本机服务未能完成请求。"))
    return result["data"]


def main():
    parser = argparse.ArgumentParser(description="EchoTrace 本地聊天工作台")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "import", "export", "distill"])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--name", default="")
    parser.add_argument("--me", default="")
    parser.add_argument("--contact", type=int)
    parser.add_argument("--uid", default="")
    parser.add_argument("--peer-uid", default="")
    parser.add_argument("--start", type=int, help="导出起点，Unix 秒")
    parser.add_argument("--end", type=int, help="导出终点，Unix 秒")
    parser.add_argument("--format", choices=["JSON", "STREAMING_JSONL", "TXT", "HTML", "EXCEL", "STREAMING_ZIP"], default="JSON")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--no-auto-import", action="store_true")
    parser.add_argument("--tier", choices=["quick", "standard", "deep"], default="standard")
    parser.add_argument("--model")
    parser.add_argument("--thinking", choices=["low", "medium", "high", "max"])
    parser.add_argument("--token-limit", type=int, default=500000)
    parser.add_argument("--summary", action="store_true", help="仅发送匿名统计摘要")
    parser.add_argument("--consent", action="store_true", help="同意本次蒸馏向配置的模型服务商发送上下文")
    args = parser.parse_args()
    if args.command == "import":
        if not args.file or not args.name or not args.me:
            parser.error("import 需要 --file、--name、--me")
        from echotrace.config import Config
        from echotrace.db import Database
        from echotrace.ingest.cleaner import import_files
        from echotrace.util import dumps
        db = Database(args.data_dir)
        result = import_files(db, Config(db), [args.file], args.name, args.uid or args.name, args.me.split(","))
        print(dumps(result))
        return
    if args.command in ("export", "distill"):
        import httpx
        from echotrace.util import dumps

        if args.command == "distill":
            if not args.contact or not args.consent:
                parser.error("distill 需要 --contact 与 --consent；请先在设置页配置模型并确认隐私说明")
            path = f"/contacts/{args.contact}/distill"
            payload = {"consent": True, "tier": args.tier, "send_raw": not args.summary, "token_limit": args.token_limit}
            payload.update({k: v for k, v in (("model", args.model), ("thinking", args.thinking)) if v})
        else:
            if not args.name or not args.me or not (args.uid or args.peer_uid) or args.start is None or args.end is None:
                parser.error("export 需要 --name、--me、--uid（QQ号）或 --peer-uid，以及 --start / --end（Unix秒）")
            path = "/qce/export"
            payload = {"name": args.name, "me": args.me.split(","), "uin": args.uid, "peer_uid": args.peer_uid,
                       "start": args.start, "end": args.end, "format": args.format, "output_dir": args.output_dir,
                       "auto_import": not args.no_auto_import}
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{args.port}/api", timeout=30, trust_env=False) as client:
                job = response_data(client.post(path, json=payload))
                previous = ""
                while job["status"] in ("queued", "running"):
                    stage = f"{job['id']} · {job['progress']:.0%} · {job['stage']}"
                    if stage != previous:
                        print(stage, flush=True)
                        previous = stage
                    time.sleep(1)
                    job = response_data(client.get("/jobs/" + job["id"]))
                print(dumps(job))
                if job["status"] != "succeeded":
                    raise SystemExit(1)
        except httpx.HTTPError as exc:
            raise SystemExit("无法连接本机工作台，请先运行 echotrace serve。") from exc
        except KeyboardInterrupt:
            print("已退出进度查看，后台任务仍保留，可在任务中心暂停或取消。")
        return
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", args.port))
        except OSError:
            parser.error(f"端口 {args.port} 已被占用。可打开已运行的工作台，或用 --port 选择其他端口。")
    from echotrace.app import create_app
    app = create_app(args.data_dir)
    if not args.no_browser:
        timer = threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}"))
        timer.daemon = True
        timer.start()
    print(f"EchoTrace · http://127.0.0.1:{args.port} · 数据：{args.data_dir.resolve()}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
