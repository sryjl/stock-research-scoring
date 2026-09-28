# -*- coding: utf-8 -*-
"""reports.py — 财报原文获取层（FinancialReportProvider）。

职责边界很清楚：**只负责「拿到 PDF 并把它落到本地缓存」**，不解析、不分类、
不算指标。解析在 :mod:`research.pdftext`，语义在 :mod:`research.asset_semantics`。

Provider 优先级（高到低）：

1. :class:`ExchangeOfficialProvider` —— 巨潮资讯网（证监会指定的信息披露网站）。
   它的 PDF 可以直接下载，是「已知原文」里最稳的一环。
2. :class:`EastMoneyAnnouncementProvider` —— 东方财富公告接口。列表和元数据很全，
   但 PDF 主机（pdf.dfcfw.com）有反爬 JS 挑战，**不逆向**，只当元数据来源。
3. :class:`IFindReportProvider` —— 同花顺 iFinD。本机没有 iFinDPy 也没有凭据，
   所以它的 ``available()`` 返回 False，只是留一个接口形状，不是硬依赖。
4. :class:`FallbackProvider` —— 把上面几个串起来按序尝试，全失败才报错。

缓存的两层去重（"同一 document_hash 永远不要重复下载和解析"）：

* ``doc_key = sha256(source:source_id)`` —— 下载**前**就知道该存哪，
  同一个公告条目不会下第二次。
* ``document_hash = sha256(pdf 字节)`` —— 下载**后**才知道，用来把不同来源的
  同一份文件合并成一份，也是 LLM 缓存的 key 的一部分。
"""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE, "data", "reports")

#: rows 缓存的版本指纹。**抽取语义一变就必须 +1。**
#:
#: 缓存里那些行是按当时那套规则算出来的（字体宽度怎么判、没有 ToUnicode 时
#: 怎么兜底、同视觉行怎么合并），而它们没有任何别的方式能自证新旧。不加指纹
#: 的后果不是「慢一点」，是「改了没生效」：修好的解析器对着旧缓存跑，读回来
#: 的还是修复前那份行——这类问题在数据上看不出任何异常，只会让人以为修复
#: 本身没用。指纹只影响重新解析（秒级），**不影响 PDF 缓存**，不会触发重下。
#:
#: 改动记录：2 → 3 是「无 ToUnicode 的 Adobe-GB1 字体按字符集定义派生 CID」，
#: 它让 600036 半年报那 41,089 个片段从乱码变回中文。不带这一版的旧缓存必须
#: 作废，否则修好的解码器会对着旧缓存跑。
ROW_CACHE_VERSION = 3

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 只认这四类定期报告，其余公告（分红、股东会、章程…）一律不要
REPORT_KINDS = {
    "A": "年度报告",
    "H1": "半年度报告",
    "Q1": "第一季度报告",
    "Q3": "第三季度报告",
}

_KIND_PAT = [
    ("H1", re.compile(r"(20\d{2})\s*年\s*半年度报告")),
    ("Q1", re.compile(r"(20\d{2})\s*年\s*第一季度报告")),
    ("Q3", re.compile(r"(20\d{2})\s*年\s*第三季度报告")),
    ("A", re.compile(r"(20\d{2})\s*年\s*年度报告")),
]


class ReportError(Exception):
    """财报获取失败。"""


def parse_report_period(title):
    """从公告标题里解析报告期，返回 ``(period, kind)``。

    "三角轮胎2026年半年度报告" → ``("2026H1", "H1")``；
    "…2026年半年度报告摘要" → ``(None, None)``（摘要没有附注，要丢掉）。
    """
    t = (title or "").replace(" ", "")
    if "摘要" in t or "正文" in t and "报告" not in t:
        return None, None
    for kind, pat in _KIND_PAT:
        m = pat.search(t)
        if m:
            return f"{m.group(1)}{kind}", kind
    return None, None


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _http(url, data=None, headers=None, timeout=30, retries=3):
    hdr = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdr.update(headers)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), dict(r.headers)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(0.4 * (attempt + 1))
    raise ReportError(f"请求失败 {url}: {last}")


def _http_json(url, data=None, headers=None, timeout=30):
    raw, _ = _http(url, data=data, headers=headers, timeout=timeout)
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as e:
        raise ReportError(f"返回的不是 JSON: {url}") from e


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #
class ReportProvider:
    """财报来源。子类实现 :meth:`list_reports`，可选实现 :meth:`fetch_pdf`。"""

    name = "base"
    supports_pdf = False

    def available(self):
        return True

    def list_reports(self, code, kinds=("A", "H1", "Q1", "Q3")):
        raise NotImplementedError

    def fetch_pdf(self, meta):
        raise ReportError(f"{self.name} 不支持下载 PDF")

    def __repr__(self):
        return f"<{type(self).__name__} name={self.name}>"


def _dedupe(metas):
    """同一报告期只留最新一版。

    更正稿 / 修订稿 / 英文版会和原稿共用一个报告期。优先中文版，再在同
    语言内选最新披露；否则晚发的英文翻译会覆盖原始中文报告，令中文
    财报表提取无端失败。
    """
    best = {}
    for m in metas:
        key = m.get("report_period")
        if not key:
            continue
        cur = best.get(key)
        rank = ("英文" not in (m.get("title") or "")
                and "English" not in (m.get("title") or ""), m.get("publish_date") or "")
        cur_rank = (("英文" not in (cur.get("title") or "")
                     and "English" not in (cur.get("title") or "")),
                    cur.get("publish_date") or "") if cur else None
        if cur is None or rank > cur_rank:
            best[key] = m
    return sorted(best.values(), key=lambda m: (m.get("publish_date") or ""),
                  reverse=True)


def _meta(code, title, publish_date, source, source_id, pdf_url, kind, period):
    return {
        "stock_code": code,
        "report_type": kind,
        "report_period": period,
        "publish_date": publish_date,
        "title": title,
        "pdf_url": pdf_url,
        "source": source,
        "source_id": str(source_id),
        "document_hash": None,          # 下载后才知道
        "local_cache_path": None,
    }


# --------------------------------------------------------------------------- #
# 1. 交易所官方：巨潮资讯网
# --------------------------------------------------------------------------- #
class ExchangeOfficialProvider(ReportProvider):
    """巨潮资讯网（www.cninfo.com.cn）。

    它是沪深交易所指定的信息披露网站，A 股定期报告的原文都在这里，
    而且 ``static.cninfo.com.cn`` 的 PDF 可以直接下载、没有反爬挑战。
    """

    name = "cninfo"
    supports_pdf = True
    QUERY = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
    SEARCH = "http://www.cninfo.com.cn/new/information/topSearch/query"
    STATIC = "http://static.cninfo.com.cn/"

    def __init__(self):
        self._org = {}

    # ---- 组织代码：cninfo 的 stock 参数要「代码,orgId」，不是纯代码 ----
    def org_id(self, code):
        if code in self._org:
            return self._org[code]
        raw, _ = _http(self.SEARCH,
                       data=urllib.parse.urlencode(
                           {"keyWord": code, "maxNum": "10"}).encode(),
                       headers={"Content-Type": "application/x-www-form-urlencoded",
                                "X-Requested-With": "XMLHttpRequest"})
        try:
            hits = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            hits = []
        org = None
        for h in hits if isinstance(hits, list) else []:
            if str(h.get("code")) == str(code):
                org = h.get("orgId")
                break
        self._org[code] = org
        return org

    def list_reports(self, code, kinds=("A", "H1", "Q1", "Q3")):
        org = self.org_id(code)
        if not org:
            return []
        column = "sse" if str(code).startswith(("6", "9")) else "szse"
        # 巨潮的分类码只有深圳那套有文档，上海股票同样吃这几个码
        cats = ";".join(f"category_{k}_szsh" for k in
                        ("ndbg", "bndbg", "yjdbg", "sjdbg"))
        payload = {
            "pageNum": "1", "pageSize": "60", "column": column,
            "tabName": "fulltext", "plate": "", "stock": f"{code},{org}",
            "searchkey": "", "secid": "", "category": cats, "trade": "",
            "seDate": "", "sortName": "", "sortType": "", "isHLtitle": "true",
        }
        raw, _ = _http(self.QUERY, data=urllib.parse.urlencode(payload).encode(),
                       headers={"Content-Type": "application/x-www-form-urlencoded",
                                "X-Requested-With": "XMLHttpRequest",
                                "Referer": "http://www.cninfo.com.cn/new/"
                                           "commonUrl?url=disclosure/list/notice"})
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return []
        out = []
        for a in data.get("announcements") or []:
            title = re.sub(r"<[^>]+>", "", a.get("announcementTitle") or "")
            period, kind = parse_report_period(title)
            if not period or kind not in kinds or kind != "A" and kind == "":
                continue
            if kind not in REPORT_KINDS:
                continue
            ts = a.get("announcementTime")
            date = (time.strftime("%Y-%m-%d", time.localtime(ts / 1000))
                    if isinstance(ts, (int, float)) else None)
            adj = a.get("adjunctUrl") or ""
            out.append(_meta(code, title, date, self.name, adj,
                             self.STATIC + adj if adj else None, kind, period))
        return out

    def fetch_pdf(self, meta):
        if not meta.get("pdf_url"):
            raise ReportError("没有 pdf_url")
        raw, hdr = _http(meta["pdf_url"], timeout=120)
        if not raw.startswith(b"%PDF"):
            raise ReportError("下载到的不是 PDF")
        return raw


# --------------------------------------------------------------------------- #
# 2. 东方财富
# --------------------------------------------------------------------------- #
class EastMoneyAnnouncementProvider(ReportProvider):
    """东方财富公告接口。

    列表 / 元数据 / ``attach_url`` 都能拿到，但 PDF 主机 ``pdf.dfcfw.com``
    回的是 ``EO_Bot_Ssid`` 的 JS 挑战页——**不逆向、不写死绕过**，所以这里
    ``supports_pdf = False``。它的价值是「官方源拿不到时还能列公告、看标题」。
    """

    name = "eastmoney"
    supports_pdf = False
    LIST = "https://np-anotice-stock.eastmoney.com/api/security/ann"
    DETAIL = "https://np-cnotice-stock.eastmoney.com/api/content/ann"

    def list_reports(self, code, kinds=("A", "H1", "Q1", "Q3")):
        q = urllib.parse.urlencode({
            "sr": "-1", "page_size": "100", "page_index": "1", "ann_type": "A",
            "client_source": "web", "stock_list": code, "f_node": "0", "s_node": "0"})
        data = _http_json(f"{self.LIST}?{q}")
        out = []
        for it in (data.get("data") or {}).get("list") or []:
            title = it.get("title_ch") or it.get("title") or ""
            period, kind = parse_report_period(title)
            if not period or kind not in kinds:
                continue
            out.append(_meta(code, title, (it.get("notice_date") or "")[:10],
                             self.name, it.get("art_code"), None, kind, period))
        return out

    def detail(self, art_code):
        q = urllib.parse.urlencode({"art_code": art_code,
                                    "client_source": "web", "page_index": "1"})
        return (_http_json(f"{self.DETAIL}?{q}").get("data") or {})

    def attach_url(self, art_code):
        return self.detail(art_code).get("attach_url")


# --------------------------------------------------------------------------- #
# 3. 同花顺 iFinD（占位）
# --------------------------------------------------------------------------- #
class IFindReportProvider(ReportProvider):
    """同花顺 iFinD。

    只在**本机真的装了 iFinDPy 且登录成功**时才可用。本机两样都没有，
    所以 :meth:`available` 返回 False——保持接口形状，不做硬依赖。
    """

    name = "ifind"
    supports_pdf = True

    def __init__(self):
        self._mod = None
        self._tried = False

    def available(self):
        if self._tried:
            return self._mod is not None
        self._tried = True
        try:
            import iFinDPy as ths          # noqa: F401
            self._mod = ths
        except Exception:
            self._mod = None
        return self._mod is not None

    def list_reports(self, code, kinds=("A", "H1", "Q1", "Q3")):
        if not self.available():
            return []
        # 没授权时连调用都不发：iFinD 未登录会弹窗/阻塞，比抛错更难处理
        return []

    def fetch_pdf(self, meta):
        raise ReportError("iFinD 未授权")


# --------------------------------------------------------------------------- #
# 4. 组合
# --------------------------------------------------------------------------- #
class FallbackProvider(ReportProvider):
    """按优先级依次尝试，返回第一个成功的来源。"""

    name = "fallback"

    def __init__(self, providers=None):
        self.providers = providers if providers is not None else [
            ExchangeOfficialProvider(),
            EastMoneyAnnouncementProvider(),
            IFindReportProvider(),
        ]
        self.last_errors = []

    def list_reports(self, code, kinds=("A", "H1", "Q1", "Q3")):
        self.last_errors = []
        for p in self.providers:
            if not p.available():
                continue
            try:
                got = p.list_reports(code, kinds)
            except Exception as e:                      # 单个源挂了不影响其它源
                self.last_errors.append(f"{p.name}: {e}")
                continue
            if got:
                return _dedupe(got)
        return []

    def fetch_pdf(self, meta):
        """按「谁有 PDF 谁能下」的顺序试，而不是按列表顺序。"""
        for p in self.providers:
            if not p.supports_pdf:
                continue
            try:
                return p.fetch_pdf(meta)
            except Exception as e:
                self.last_errors.append(f"{p.name}: {e}")
        # 列表来源不支持下载时，用它的 id 去官方源换一份
        if meta.get("source") == EastMoneyAnnouncementProvider.name:
            official = ExchangeOfficialProvider()
            try:
                for cand in official.list_reports(meta["stock_code"]):
                    if cand.get("report_period") == meta.get("report_period"):
                        return official.fetch_pdf(cand)
            except Exception as e:
                self.last_errors.append(f"cninfo 补抓: {e}")
        raise ReportError(f"没有可用的 PDF 来源：{self.last_errors}")


# --------------------------------------------------------------------------- #
# 本地缓存
# --------------------------------------------------------------------------- #
class ReportCache:
    """PDF 与解析结果的本地下沉。

    目录结构::

        data/reports/
            index.json                  document_hash → 缓存文件名
            <doc_key>.pdf               原始 PDF
            <doc_key>.meta.json         元数据（含 source / source_id / hash / 期间）
            <doc_key>.rows.json         解析后的版面行（不重复解析）
    """

    def __init__(self, root=None):
        self.root = root or CACHE_DIR
        self.index_path = os.path.join(self.root, "index.json")
        self._index = None

    def _ensure(self):
        os.makedirs(self.root, exist_ok=True)

    @property
    def index(self):
        if self._index is None:
            try:
                with open(self.index_path, "r", encoding="utf-8") as f:
                    self._index = json.load(f)
            except (OSError, ValueError):
                self._index = {}
        return self._index

    def _save_index(self):
        self._ensure()
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.index, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.index_path)

    @staticmethod
    def doc_key(meta):
        return _sha(f"{meta.get('source')}:{meta.get('source_id')}".encode())[:20]

    def paths(self, meta):
        k = self.doc_key(meta)
        return (os.path.join(self.root, k + ".pdf"),
                os.path.join(self.root, k + ".meta.json"),
                os.path.join(self.root, k + ".rows.json"))

    def have_pdf(self, meta):
        pdf, _, _ = self.paths(meta)
        return os.path.exists(pdf) and os.path.getsize(pdf) > 0

    def load_pdf(self, meta):
        pdf, _, _ = self.paths(meta)
        with open(pdf, "rb") as f:
            return f.read()

    def load_rows(self, meta_or_key):
        """缓存里的 ``(rows, diag)``；不可用或版本不符时返回 ``None``。

        载荷是 ``{"v": ROW_CACHE_VERSION, "rows": [...], "diag": {...}}``。
        **裸列表**（本版之前写的缓存）与版本号不符的一律当未命中——重新解析
        一遍 PDF，但不重新下载，PDF 还在本地。
        """
        _, _, rows_path = self.paths(meta_or_key)
        try:
            with open(rows_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("v") != ROW_CACHE_VERSION:
            return None
        rows = payload.get("rows")
        if not isinstance(rows, list):
            return None
        return rows, payload.get("diag") or {}

    def save(self, meta, pdf):
        """落盘 PDF + 元数据，返回补全后的 meta。"""
        self._ensure()
        pdf_path, meta_path, _ = self.paths(meta)
        digest = _sha(pdf)
        # 已经下过同一份内容（哪怕来自另一个源）就直接复用，不重复占用磁盘
        existing = self.index.get(digest)
        if existing and os.path.exists(os.path.join(self.root, existing)):
            pdf_path = os.path.join(self.root, existing)
        else:
            with open(pdf_path, "wb") as f:
                f.write(pdf)
            self.index[digest] = os.path.basename(pdf_path)
            self._save_index()
        meta = dict(meta)
        meta["document_hash"] = digest
        meta["local_cache_path"] = os.path.relpath(pdf_path, BASE).replace("\\", "/")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        return meta

    def save_rows(self, meta, rows, diag=None):
        self._ensure()
        _, _, rows_path = self.paths(meta)
        tmp = rows_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": ROW_CACHE_VERSION, "rows": rows, "diag": diag or {}},
                      f, ensure_ascii=False)
        os.replace(tmp, rows_path)


def default_provider():
    return FallbackProvider()


# --------------------------------------------------------------------------- #
# 取用入口
# --------------------------------------------------------------------------- #
class ReportStore:
    """把「列表 → 下载 → 解析 → 缓存」串成一条线，供业务层调用。

    重复调用同一份报告时**不会**再发网络请求、也不会再解析一遍 PDF：
    第一次之后全部命中本地缓存。
    """

    def __init__(self, provider=None, cache=None):
        self.provider = provider or default_provider()
        self.cache = cache or ReportCache()
        self.stats = {"list": 0, "download": 0, "cache_hit_pdf": 0,
                      "parse": 0, "cache_hit_rows": 0}

    def reports(self, code, kinds=("A", "H1", "Q1", "Q3")):
        self.stats["list"] += 1
        return self.provider.list_reports(code, kinds)

    def latest(self, code, period=None):
        """取指定报告期（默认最新一份有 PDF 的定期报告）的 meta。"""
        for m in self.reports(code):
            if period is None or m.get("report_period") == period:
                return m
        return None

    def ensure_pdf(self, meta):
        """确保 PDF 在本地，返回补全后的 meta（含 document_hash）。"""
        if self.cache.have_pdf(meta):
            self.stats["cache_hit_pdf"] += 1
            pdf = self.cache.load_pdf(meta)
            return self.cache.save(meta, pdf) if not meta.get("document_hash") else meta
        raw = self.provider.fetch_pdf(meta)
        self.stats["download"] += 1
        return self.cache.save(meta, raw)

    def rows_with_diag(self, meta):
        """解析后的版面行 + 抽取诊断，返回 ``(rows, diag)``。

        ``diag`` 是「这次抽取可不可信」的证据（见
        :func:`research.pdftext.extraction_diag`）。它必须跟着 rows 一起
        缓存、一起取出来——抽不出报表时，**「文本层坏了」和「表真的不在」
        是两句完全不同的话**，而决定说得准不准的信息全在 diag 里。
        """
        cached = self.cache.load_rows(meta)
        if cached is not None:
            self.stats["cache_hit_rows"] += 1
            return cached
        import research.pdftext as pdftext
        runs, doc = pdftext.extract_runs(self.cache.load_pdf(meta))
        rows = pdftext.to_rows(runs)
        diag = pdftext.extraction_diag(doc, runs)
        self.stats["parse"] += 1
        self.cache.save_rows(meta, rows, diag)
        return rows, diag

    def rows(self, meta):
        """解析后的版面行。第二次调用直接读缓存。"""
        return self.rows_with_diag(meta)[0]

    def document(self, code, period=None):
        """一步到位：返回 ``(meta, rows)``；拿不到返回 ``(None, None)``。"""
        meta = self.latest(code, period)
        if not meta:
            return None, None
        try:
            meta = self.ensure_pdf(meta)
            return meta, self.rows(meta)
        except ReportError:
            return None, None
