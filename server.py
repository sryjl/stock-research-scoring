# -*- coding: utf-8 -*-
"""server.py — 本地 HTTP 服务：REST API + 静态页面。

仅监听 127.0.0.1，供本机使用。无第三方依赖。
"""
import json
import mimetypes
import os
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import core
import db
import quotes
import views

import research.asset_engine as asset_engine
import research.audit_job as audit_job
import research.db as research_db
import research.engine as research_engine
import research.factor_audit as factor_audit
import research.pig_evidence as pig_evidence
import research.router as router
import research.rules as rules

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

STOCK_RE = re.compile(r"^/api/stocks/(\d+)$")
LOT_RE = re.compile(r"^/api/lots/(\d+)$")
LOT_SELLS_RE = re.compile(r"^/api/lots/(\d+)/sells$")
SELL_RE = re.compile(r"^/api/sells/(\d+)$")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


LABEL_MAX = 200


def _label(value):
    """请求体里的标签字段（``note`` / ``baseline_tag``）→ 落库用的短字符串。

    只做「去空白 + 截断」，不做白名单、不解释内容：这两个字段是**备注**，不进
    任何 hash、不参与评分（这一点在 factor_store.save 的 docstring 里写死了），
    所以约束它们的取值没有意义；但也不能让一次请求往库里塞一段无上限的文本。
    空串与 ``None`` 一律规整成 ``None``，免得同一件事在库里出现两种写法。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()[:LABEL_MAX]
    return text or None


class Handler(BaseHTTPRequestHandler):
    server_version = "WatchList/1.0"

    # ------------------------------------------------------------------ #
    # 基础输出
    # ------------------------------------------------------------------ #
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "请求体不是合法的 JSON")

    # ------------------------------------------------------------------ #
    # 路由
    # ------------------------------------------------------------------ #
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            self._dispatch("GET", path, parse_qs(parsed.query))
        else:
            self._serve_static(path)

    def do_POST(self):
        self._dispatch("POST", urlparse(self.path).path, {})

    def do_PUT(self):
        self._dispatch("PUT", urlparse(self.path).path, {})

    def do_DELETE(self):
        self._dispatch("DELETE", urlparse(self.path).path, {})

    def _dispatch(self, method, path, query=None):
        query = query or {}
        conn = db.connect()
        try:
            if method == "GET" and path == "/api/stocks":
                self._send_json([views.stock_view(s) for s in db.list_stocks(conn)])
                return
            if method == "POST" and path == "/api/stocks":
                data = self._read_json()
                self._require_fields(data, "code", "name")
                s = db.create_stock(conn, data)
                self._send_json(views.stock_view(s), 201)
                return

            m = STOCK_RE.match(path)
            if m:
                sid = int(m.group(1))
                if method == "GET":
                    s = db.get_stock(conn, sid)
                    if s is None:
                        raise ApiError(404, "股票不存在")
                    self._send_json(views.stock_view(s))
                    return
                if method == "PUT":
                    data = self._read_json()
                    s = db.update_stock(conn, sid, data)
                    if s is None:
                        raise ApiError(404, "股票不存在")
                    self._send_json(views.stock_view(s))
                    return
                if method == "DELETE":
                    db.delete_stock(conn, sid)
                    self._send_json({"ok": True})
                    return

            if method == "GET" and path == "/api/tags":
                self._send_json(db.list_tags(conn))
                return
            if method == "POST" and path == "/api/tags":
                data = self._read_json()
                name = (data.get("name") or "").strip()
                if not name:
                    raise ApiError(400, "标签名不能为空")
                self._send_json(db.create_tag(conn, name), 201)
                return

            m = LOT_RE.match(path)
            if m and method == "PUT":
                lot = db.update_lot(conn, int(m.group(1)), self._read_json())
                if lot is None:
                    raise ApiError(404, "Lot 不存在")
                self._send_json(lot)
                return

            m = LOT_SELLS_RE.match(path)
            if m:
                lot_id = int(m.group(1))
                if method == "GET":
                    self._send_json(db.list_sell_records(conn, lot_id))
                    return
                if method == "POST":
                    data = self._read_json()
                    self._require_fields(data, "sell_price")
                    self._send_json(db.add_sell_record(conn, lot_id, data), 201)
                    return

            m = SELL_RE.match(path)
            if m and method == "DELETE":
                db.delete_sell_record(conn, int(m.group(1)))
                self._send_json({"ok": True})
                return

            if method == "GET" and path == "/api/quotes/status":
                self._send_json({"trading": quotes.is_trading_time()})
                return
            if method == "GET" and path == "/api/quote/lookup":
                code = (query.get("code") or [""])[0].strip()
                if not code:
                    raise ApiError(400, "缺少参数 code")
                q = quotes.lookup(code)
                if q is None:
                    raise ApiError(404, "未找到该股票代码")
                self._send_json({"code": q.get("code"), "name": q.get("name"), "price": q.get("price")})
                return
            if method == "GET" and path == "/api/quote/search":
                kw = (query.get("q") or [""])[0].strip()
                self._send_json(quotes.search_stocks(kw))
                return

            # ---- 股票研究模块（独立） ----
            if method == "GET" and path == "/api/research/list":
                self._send_json(research_engine.list_stocks())
                return
            if method == "GET" and path == "/api/research/search":
                kw = (query.get("q") or [""])[0].strip()
                self._send_json(quotes.search_stocks(kw))
                return
            if method == "GET" and path == "/api/research/detail":
                code = (query.get("code") or [""])[0].strip()
                s = research_engine.get_stock(code)
                if s is None:
                    raise ApiError(404, "该股票尚未研究")
                self._send_json(s)
                return
            if method == "GET" and path == "/api/research/assets":
                code = (query.get("code") or [""])[0].strip()
                if not code:
                    raise ApiError(400, "缺少股票代码")
                force = (query.get("refresh") or [""])[0] in ("1", "true")
                # 资产快照要取 research.db，**不是**上面那个观察表连接。两个库都
                # 有 asset_semantic_snapshot，用错连接不会报错，只会让同一只股票
                # 出现两个数：审计页读 stocks.db、评分路径读 research.db，同一张
                # 表各写一份。青啤就这么同时显示过有息负债 4.08 亿和 1.85 亿。
                rconn = research_db.connect()
                try:
                    row = research_db.get_stock(rconn, code) or {}
                    if not row:
                        # 没研究过的代码：照旧 404，不要报一个永远不会有下文的
                        # 「审计中」——审计状态是挂在 research_stocks 的行上的。
                        raise ApiError(404, "该股票尚未研究")
                    status = audit_job.status_of(rconn, code)
                    if force or status == audit_job.UNAUDITED:
                        # 这里**不再同步跑审计**：那要下 PDF、调 LLM，几分钟起步，
                        # 挂在 HTTP 请求上只会把界面卡住（而且跑完不回头算分，
                        # 正是「先评分后审计」那个根因的一半）。排队交给后台
                        # worker，这个接口只报状态。
                        research_db.mark_stock_audit(rconn, code, audit_job.RUNNING)
                        audit_job.notify()
                        status = audit_job.RUNNING
                    payload = (asset_engine.audit_payload(rconn, code)
                               if status == audit_job.OK else None)
                finally:
                    rconn.close()
                self._send_json({
                    "status": status,
                    "status_label": audit_job.label_of(status),
                    "error": row.get("audit_error"),
                    "started_at": row.get("audit_started_at"),
                    "payload": payload,
                })
                return
            if method == "GET" and path == "/api/research/factor-audit":
                # canonical factor 层的**审计视图**：逐格回答「这个分数由什么
                # 构成」。数据全部来自已经落库的那一次分析（factor_store.load_layer），
                # 本接口不重算、不取数、不写表——重算会让界面与审计表在规则改动后
                # 各说各话。带上 ?code= 就是逐只明细，不带就是全库对账。
                #
                # 它**不进 /api/meta**，也不是指纹轴：它不改变任何评分行为，
                # 只是一个读法的出口。
                wanted = (query.get("code") or [""])[0].strip()
                aconn = research_db.connect()
                try:
                    payload = factor_audit.audit(aconn, wanted or None)
                finally:
                    aconn.close()
                self._send_json(payload)
                return
            if method == "GET" and path == "/api/research/pig-evidence":
                # 猪行业读数的**证据视图**：读数 + 每条读数背后的全部候选观测
                # （含原文段落、文件、页、解析器版本、冲突分组）。数据来自观测仓
                # 与行业序列，本接口不重算、不取数、不写表。
                #
                # 与 factor-audit 一样**不进 /api/meta**：它不改变任何评分行为。
                # ?candidates=0 只给概览（不带候选明细），界面点开某一行时再按
                # metric_id + period 取那一组——四家的完整载荷实测 1.8~2.0 MB。
                code = (query.get("code") or [""])[0].strip()
                if not code:
                    raise ApiError(400, "缺少股票代码")
                want_candidates = (query.get("candidates") or ["1"])[0] != "0"
                econn = research_db.connect()
                try:
                    payload = pig_evidence.build(
                        econn, code,
                        metric_id=(query.get("metric_id") or [None])[0],
                        period=(query.get("period") or [None])[0],
                        candidates=want_candidates)
                finally:
                    econn.close()
                self._send_json(payload)
                return
            if method == "POST" and path == "/api/research/analyze":
                data = self._read_json()
                code = (data.get("code") or "").strip()
                if not code:
                    raise ApiError(400, "缺少股票代码")
                force = bool(data.get("force_financials"))
                # note / baseline_tag：这一次重算的两张标签，原样落到 factor run
                # 那一行上。只做长度截断，不解释内容——它们不进任何哈希、不改分数，
                # 所以放宽到「调用方自己负责写清楚」是安全的。
                self._send_json(research_engine.analyze(
                    code, force, note=_label(data.get("note")),
                    baseline_tag=_label(data.get("baseline_tag"))))
                return
            if method == "POST" and path == "/api/research/refresh":
                data = self._read_json()
                code = (data.get("code") or "").strip()
                scope = (data.get("scope") or "price")
                if not code:
                    raise ApiError(400, "缺少股票代码")
                research_engine.analyze(code, force_financials=(scope == "financials"))
                self._send_json(research_engine.get_stock(code))
                return
            if method == "POST" and path == "/api/research/user-type":
                data = self._read_json()
                code = (data.get("code") or "").strip()
                research_engine.set_user_type(code, data.get("user_type"))
                self._send_json({"ok": True})
                return
            if method == "POST" and path == "/api/research/simulate":
                data = self._read_json()
                code = (data.get("code") or "").strip()
                price = data.get("price")
                if not code or price is None:
                    raise ApiError(400, "缺少 code 或 price")
                self._send_json(research_engine.simulate_price(code, float(price)))
                return
            if method == "POST" and path == "/api/quotes/refresh":
                self._refresh_quotes(conn)
                return

            if method == "GET" and path == "/api/meta":
                self._send_json({
                    "stock_types": [
                        {"value": core.STOCK_TYPE_TRADE, "label": core.STOCK_TYPE_LABELS[core.STOCK_TYPE_TRADE]},
                        {"value": core.STOCK_TYPE_HOLD, "label": core.STOCK_TYPE_LABELS[core.STOCK_TYPE_HOLD]},
                    ],
                    "lot_statuses": list(core.LOT_STATUSES),
                    "tier_multipliers": list(core.TIER_MULTIPLIERS),
                    # 本进程**实际加载**的评分规则指纹：loaded_rule_sha256 是导入时
                    # 固定下来的（进程内不变），disk_rule_sha256 是此刻磁盘现状，
                    # rule_source_dirty 是两者是否一致。旧进程 + 新文件会报出
                    # dirty=true，那才是「改了没重启」的判据——以前这里只报一个
                    # 现读磁盘的 sha256，旧进程报出来的和文件一模一样，等于没报。
                    # 另含 pid / process_started_at：发现版本不对时得知道该杀谁。
                    "rule": rules.rule_source_fingerprint(),
                    # 模型路由指纹与模型字典。路由是独立版本号的另一套东西，
                    # 改了 router.py 同样必须重启，所以单独报一份。
                    "router": router.router_source_fingerprint(),
                    "models": router.model_catalog(),
                    "profile_labels": router.PROFILE_LABELS,
                    "route_status_labels": router.ROUTE_STATUS_LABELS,
                    # 风险等级的中文名。前端**不许**再自己写一份——它抄过一次，
                    # 抄成了 GREEN=「低风险」而后端是「暂无明显风险信号」。
                    "risk_signal_labels": rules.RISK_SIGNAL_LABELS,
                    # 资产审计状态的中文名。同一条约定：前端只留一份兜底表，
                    # 并有一条测试逐字比对两边（少一个状态就会在页面上露出英文常量）。
                    "audit_status_labels": audit_job.AUDIT_STATUS_LABELS,
                })
                return

            raise ApiError(404, f"未知接口: {method} {path}")
        except ApiError as e:
            self._send_json({"error": e.message}, e.status)
        except sqlite3.IntegrityError as e:
            self._send_json({"error": f"数据冲突（可能代码已存在）: {e}"}, 400)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": str(e)}, 500)
        finally:
            conn.close()

    @staticmethod
    def _require_fields(data, *names):
        for n in names:
            if not str(data.get(n) or "").strip():
                raise ApiError(400, f"缺少字段: {n}")

    def _refresh_quotes(self, conn):
        data = self._read_json()
        force = bool(data.get("force"))
        trading = quotes.is_trading_time()
        stocks = db.list_stocks(conn)
        total = len(stocks)
        updated = 0
        fetched = 0
        skipped = False
        if trading or force:
            qs = quotes.fetch_quotes([s["code"] for s in stocks])
            fetched = len(qs)
            for s in stocks:
                q = qs.get(s["code"])
                if q and q["price"] is not None and q["price"] != s["current_price"]:
                    db.update_stock(conn, s["id"], {"current_price": q["price"]})
                    updated += 1
        else:
            skipped = True
        self._send_json({
            "trading": trading,
            "skipped": skipped,
            "updated": updated,
            "fetched": fetched,
            "total": total,
            "stocks": [views.stock_view(x) for x in db.list_stocks(conn)],
        })

    # ------------------------------------------------------------------ #
    # 静态文件
    # ------------------------------------------------------------------ #
    def _serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        if rel.startswith("static/"):
            rel = rel[len("static/"):]
        filepath = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not filepath.startswith(os.path.normpath(STATIC_DIR)):
            self._send_json({"error": "forbidden"}, 403)
            return
        if not os.path.isfile(filepath):
            self._send_json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(filepath)[0] or "application/octet-stream"
        with open(filepath, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 静默访问日志
        pass


def run_server(host="127.0.0.1", port=8765):
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd
