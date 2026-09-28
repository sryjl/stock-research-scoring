# -*- coding: utf-8 -*-
"""app.py — 程序入口。

用法：
    python app.py [--port 8765] [--host 127.0.0.1] [--no-seed]

首次启动会初始化 SQLite 数据库并写入少量示例数据；
之后每次启动数据都从磁盘读取，重启不丢失。
"""
import argparse
import os
import sys

import db
import notifier
import seed
from server import run_server

import research.audit_job as audit_job
import research.engine as research_engine
import research.router as research_router
import research.rules as research_rules

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def main(argv=None):
    parser = argparse.ArgumentParser(description="A股股票观察价位管理工具")
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="端口（默认 8765）")
    parser.add_argument("--db", default=None, help="SQLite 数据库文件路径（默认 data/stocks.db）")
    parser.add_argument("--no-seed", action="store_true", help="不写入示例数据")
    args = parser.parse_args(argv)

    # 先把规则指纹打出来：万一端口被占/启动失败，屏幕上也能看到本次加载的是哪一版。
    # 改过 rules.py 却没重启的旧进程会继续按旧口径评分回写，靠这行才能发现。
    # 路由是独立版本号的另一套，同一个坑，所以同样在启动时打一份。
    print(research_rules.format_rule_fingerprint(), flush=True)
    print(research_router.format_router_fingerprint(), flush=True)

    db_path = args.db or db.DEFAULT_DB_PATH
    conn = db.init_db(db_path)
    if not args.no_seed:
        if seed.seed_if_empty(conn):
            print("已写入示例数据。")
    conn.close()

    research_engine.init()  # 研究模块独立数据库

    httpd = run_server(args.host, args.port)
    url = f"http://{args.host}:{args.port}"
    notifier.start()
    # 资产审计的串行 worker。队列就是库里 audit_status='RUNNING' 那一列，
    # 所以这里只是把它接上：上次没跑完的接着跑，新入队的立刻跑。
    audit_job.start()
    # flush=True：输出重定向到日志文件时 stdout 是块缓冲的，不刷就看不到这次启动的状态
    print("=" * 46, flush=True)
    print("  A股股票观察价位管理工具", flush=True)
    print(f"  请用浏览器打开： {url}", flush=True)
    print(f"  数据库文件：     {os.path.abspath(db_path)}", flush=True)
    print(f"  {research_rules.format_rule_fingerprint()}", flush=True)
    print(f"  {research_router.format_router_fingerprint()}", flush=True)
    print("  已开启交易时段卖出目标提醒（±1%）。", flush=True)
    print("  按 Ctrl+C 停止服务。", flush=True)
    print("=" * 46, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        sys.exit(0)


if __name__ == "__main__":
    main()
