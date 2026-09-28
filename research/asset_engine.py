# -*- coding: utf-8 -*-
"""asset_engine.py — 资产语义层的取用入口与落库。

把「拿财报 → 抽版面 → 经济分类 → 算资产指标 → 存快照」串成一条线，供
业务层和页面调用。

**版本隔离是这一层最重要的性质。** 旧的 SCORING_V1.1 快照记在
``research_snapshots`` 里，那是历史，是当时系统的真实输出，**永不被覆盖**。
资产语义层写自己的一张表 ``asset_semantic_snapshot``，版本号
``ASSET_SEMANTIC_ENGINE_V1.0``，两张表互不干涉。

这么做的理由不是洁癖：一旦新口径回填旧记录，就再也无法回答「修正资产口径
让哪些股票的结论变了」——因为没有「修正前」可比了。而那个对比正是这次升级
要交付的东西。

依赖边界（§27）：本阶段只动财报获取、资产语义、资产指标，以及依赖这些资产
指标的旧评分结果的**重算**。V2 专属评分模型的公式一个字都不改。
"""
import hashlib
import json
import os
from datetime import datetime

from . import asset_semantics as sem
from . import haircut as hc
from . import llm_cache
from . import llm_classify
from . import reports
from . import db

#: 资产语义层的独立版本号。与 SCORING_V1.1 / MODEL_ROUTER_V1.0 无关。
METRIC_VERSION = sem.VERSION

SCHEMA = """
-- 资产语义快照（ASSET_SEMANTIC_ENGINE_V1.0）。
-- 与 research_snapshots 分开：那张记「值多少分」，这张记「资产到底是什么」。
-- 版本号独立，且**只增不改**——同一份财报重复分析会复用已有行，不会覆盖。
CREATE TABLE IF NOT EXISTS asset_semantic_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    date TEXT NOT NULL,
    metric_version TEXT,
    report_period TEXT,
    source_document TEXT,
    source_page INTEGER,
    market_cap REAL,
    total_assets REAL,
    cash_tiers TEXT,
    net_cash TEXT,
    asset_value_profile TEXT,
    asset_consumption_rate REAL,
    classification_coverage REAL,
    classification_method TEXT,
    confidence REAL,
    items TEXT,
    restricted_cash REAL,
    note_conflicts TEXT,
    result_hash TEXT,
    llm_calls INTEGER DEFAULT 0,
    llm_skipped INTEGER DEFAULT 0,
    llm_truncated INTEGER DEFAULT 0,
    llm_empty INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_asset_snap_code
    ON asset_semantic_snapshot (stock_code, metric_version);
"""


#: 建表之后才加的列。``CREATE TABLE IF NOT EXISTS`` 对已经存在的表什么都不做，
#: 所以老库不会自己长出 result_hash 来——那条 dedup 查询会直接抛
#: "no such column"，而它恰好在写入路径上。补列只加不改，历史行留 NULL。
_ADDED_COLUMNS = {
    "result_hash": "TEXT",
    # 「问了几次」不够用：截断和空响应以前都混在「没结论」里，看不出来那次
    # 调用到底是因为模型拒答而白花，还是因为输出预算不够。分开记，才查得出
    # 一份快照的判定为什么不可复现。
    "llm_truncated": "INTEGER DEFAULT 0",
    "llm_empty": "INTEGER DEFAULT 0",
    # 「这份口径不是最新一期算的，因为什么」。报告期本身在快照里，但只用快照
    # 时看不出它是不是最新一期——而这件事决定了这个数能不能和别的股票比。
    "report_period_note": "TEXT",
}


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    have = {row[1] for row in conn.execute(
        "PRAGMA table_info(asset_semantic_snapshot)")}
    for name, ddl in _ADDED_COLUMNS.items():
        if name not in have:
            conn.execute(
                f"ALTER TABLE asset_semantic_snapshot ADD COLUMN {name} {ddl}")
    conn.commit()


# --------------------------------------------------------------------------- #
# 计算
# --------------------------------------------------------------------------- #
#: ``资产总计 / 市值`` 的合理区间。低于下限说明资产被当成了「分」量级的零头
#: （该是千元、万元却按元读了），高于上限说明资产被放大了（该是元却按万元读了）。
#:
#: 带子取得很宽，因为要拦的只是「差着量级」的错：实测 21 只已通过股票的
#: 资产总计/市值落在 **0.72~14.4**（最高的是银行，杠杆本来就在十倍上下），
#: 上下各留出两个数量级以上的余量。它拦得住 10³ 和 10⁶ 的错，拦不住 10 倍的错，
#: 而 10 倍级的量纲错在这个范围内根本不存在——`_UNIT_SCALES` 里没有这一档。
AMOUNT_RATIO_MIN = 0.02
AMOUNT_RATIO_MAX = 300.0


def _ratio_ok(total_assets, market_cap):
    """资产总计和市值是不是同一个量级。

    市值缺失或为 0 时**不做判断**（返回 True）：没有比对的另一半，量级就说不出
    对错，此时宁可放过——这条闸的作用是拦住量纲错误，不是拦住没有市值的股票。
    """
    if not total_assets or not market_cap or market_cap <= 0:
        return True
    return AMOUNT_RATIO_MIN <= total_assets / market_cap <= AMOUNT_RATIO_MAX


def _ratio_phrase(ratio):
    """比值的说法：大的按倍数、小的按分之一。

    ``"%.0f"`` 直接印会得到「相差 0 倍」——比值 2e-7 和 0 是两回事，而这个
    字符串正是审计失败时人要读的那一句。
    """
    if ratio >= 1:
        return "%.1f 倍" % ratio
    return "1/%.1f" % (1.0 / ratio)


def resolve_scale(rows, statement_title, market_cap):
    """定这份报表的金额量纲，返回 ``((单位名, 倍数), 说明)``。

    **声明优先，量级兜底**：

    1. 报表区间里自己声明了单位（「单位：千元」「货币单位均以人民币百万元
       列示」）→ 直接用它，不再校验。
    2. 没有声明时，拿全文档的「表首括号式」说明当候选（「（人民币百万元，
       百分比除外）」），**一个个拿市值去量**，第一个让 ``资产总计/市值``
       落进 :data:`AMOUNT_RATIO_MIN` ~ :data:`AMOUNT_RATIO_MAX` 的才采纳。
    3. 都不成立就维持按元，并在最后那道收尾校验里显形。

    为什么候选必须过市值这一关：招商银行半年报的资产负债表通篇没有单位声明
    （正文表格里同一句话标了 50 次，报表页上一个字都没有），不校验也猜得对；
    但 **600585 恰好相反**——正文表格标着「千元」，报表本身却是元，按票数采纳
    会把它**现在正确**的资产总计放大一千倍。这两者之间唯一用得上的区分就是
    量级自洽，而它正好是这一层要回答的问题本身：资产和市值对不对得上。

    说明文字只在**猜过**的时候非空，界面据此说清「这个倍数不是报表说的」。
    """
    start, _ = sem.find_statement(rows, statement_title)
    declared = sem.statement_scale(rows, start or 0)
    if declared[0] is not None:
        return declared, None
    if not market_cap or market_cap <= 0:
        return declared, None
    for cand in sem.caption_units(rows):
        try:
            bs = sem.parse_balance_sheet(rows, statement_title, scale=cand)
        except sem.BalanceSheetError:
            continue
        total = sem._asset_total(bs) if bs else None
        if total and _ratio_ok(total, market_cap):
            return cand, ("报表未声明金额单位，按全文表首说明取「%s」"
                          "（资产总计 %s ÷ 市值 %s = %.2f，量级自洽）"
                          % (cand[0], _cn_amount(total), _cn_amount(market_cap),
                             total / market_cap))
    return declared, None


def _cn_amount(value):
    """给说明文字用的金额写法：万亿 / 亿，保留一位小数。"""
    for unit, div in (("万亿", 1e12), ("亿", 1e8), ("万", 1e4)):
        if abs(value) >= div:
            return "%.1f%s" % (value / div, unit)
    return "%.0f" % value


def compute(rows, market_cap=None, report_meta=None, classifier=None,
            statement_title="合并资产负债表"):
    """从已解析的版面行算出完整资产指标。

    ``classifier`` 不传就纯规则跑。传了也只有跨过门槛的 UNKNOWN 才会问它，
    且问不到就保持 UNKNOWN——**这一层永远不会因为外部服务不可用而失败**。
    """
    meta = report_meta or {}
    doc = meta.get("document_hash") or meta.get("source_id") or ""
    llm_hook = llm_gate = None
    if classifier is not None and classifier.available:
        # 缓存键要严格是 §10 的四项组合。直接把 ``classify`` 挂上去的话
        # ``paragraph_hash`` 会一直是空串，键退化成「文档 + 版本 + 模型」——
        # 同一份财报里两个不同的「其他」子项会撞成一条缓存。
        llm_hook = lambda item: classifier.classify(
            item, doc, llm_cache.paragraph_hash(
                item.account, item.sub_item, item.source_text))
        llm_gate = lambda item, total_assets: classifier.gate(
            item, total_assets, market_cap)

    # 量纲先定死再解析：`build_economic_view` 会把同一个倍数喂给报表和所有附注，
    # 两边的「明细合计 == 科目余额」交叉验证只有在同一量纲下才成立。
    scale, scale_note = resolve_scale(rows, statement_title, market_cap)
    view = sem.build_economic_view(
        rows, statement_title, llm_hook=llm_hook, llm_gate=llm_gate, scale=scale)

    items = view["items"]
    bs = view["balance_sheet"]
    # 负债解析一次拿全：有息负债是它的一个子集，清算价值要扣的是它的全部。
    # 两者分开取数是「清算价值 = 资产 − 有息负债」这类命名错误的温床。
    # 传 rows 是为了找应付票据附注——票种和融资属性只在附注里，报表上看不出来。
    liabilities = sem.parse_liabilities(bs, rows=rows)
    # 对平闸门。**所有依赖有息负债的指标共用这一个判定**：解析没和报表的
    # 「负债合计」对上时，有息负债整个不可信，一律给 None 而不是给一个数。
    # 上一轮的 bug 正是从这里漏出去的——整段非流动负债没进解析，有息负债
    # 少算 256 亿，而那个数是「算错了但看起来很正常」的，会安静地参与评分。
    debt = liabilities.interest_bearing_total if liabilities.usable else None
    restricted = view.get("restricted_cash") or 0.0

    tiers = sem.cash_tiers(items, restricted)
    net = sem.net_cash(items, debt, restricted)
    profile = hc.asset_value_profile(
        items, liabilities.total_liabilities, market_cap,
        interest_bearing_debt=debt)
    profile["cash_tiers"] = tiers
    # 报表声明的金额单位（见 `sem.statement_scale`）。放在 profile 里而不新开
    # 一列：它只用于展示「原来是什么单位」，而 `_result_hash` 只取 profile 的
    # `scenarios` / `liabilities`，加这个键**不会**让 21 只下次审计各多写一行。
    profile["amount_unit"] = view.get("amount_unit")
    profile["amount_scale"] = view.get("amount_scale")
    if scale_note:
        profile["amount_note"] = scale_note
    # 收尾校验：倍数用完之后再看一眼资产和市值对不对得上。**声明过单位的也照查**
    # ——声明本身可能写错（写错单位、或只声明在别处的表上），而这一类错误在数字
    # 上完全看不出来：13,785,280 当作元还是百万元，都是一串正常的数字。查不过就
    # 不让这份口径进库（:func:`_unusable_reason` 会拦），界面如实说明原因。
    # `checked` 与 `valid` 分开：「没查」和「查过没问题」不是一回事。
    checked = bool(market_cap and market_cap > 0 and view["total_assets"])
    ratio = (view["total_assets"] / market_cap) if checked else None
    ok = _ratio_ok(view["total_assets"], market_cap)
    profile["amount_gate"] = {"checked": checked, "valid": ok, "ratio": ratio,
                              "unit": view.get("amount_unit"), "reason": None}
    if checked and not ok:
        profile["amount_gate"]["reason"] = (
            "资产总计与市值不在同一量级（%s ÷ %s = %s），金额单位可能识别错：%s"
            % (_cn_amount(view["total_assets"]), _cn_amount(market_cap),
               _ratio_phrase(ratio),
               scale_note or ("报表声明按「%s」计" % view["amount_unit"]
                              if view.get("amount_unit") else "报表未声明金额单位")))
    # 对平结果一并入库：清算价值是不是「有效基数」必须能从快照里看出来，
    # 光看那个数看不出来——算错的清算价值长得和算对的一模一样。
    profile["liabilities"] = liabilities.to_dict()
    # 有息负债闸门的开关状态单独记一笔，界面据此说明「这些指标为什么不显示」。
    profile["interest_debt_gate"] = {
        "valid": liabilities.usable,
        "reconciliation": liabilities.reconciliation,
        "reason": None if liabilities.usable else liabilities.status,
    }

    resolved = [it for it in items if it.economic_class != sem.OTHER_UNKNOWN]
    coverage = len(resolved) / len(items) if items else 0.0
    # 置信度按「金额覆盖」而不是「条目数」：一个占 60% 资产的科目没认出来，
    # 不该因为旁边还有二十个小科目认出来了就显得很可信。
    total_amt = sum(abs(it.amount or 0.0) for it in items) or 1.0
    resolved_amt = sum(abs(it.amount or 0.0) for it in resolved)
    amount_coverage = resolved_amt / total_amt

    method_counts = {}
    for it in items:
        method_counts[it.method] = method_counts.get(it.method, 0) + 1

    pages = [it.page for it in items if it.page]

    return {
        "metric_version": METRIC_VERSION,
        "report_period": meta.get("report_period"),
        "source_document": doc,
        "source_page": min(pages) if pages else None,
        "market_cap": market_cap,
        "total_assets": view["total_assets"],
        "cash_tiers": tiers,
        "net_cash": net,
        "asset_value_profile": profile,
        "asset_consumption_rate": hc.asset_consumption_rate(
            items, view["total_assets"]),
        "classification_coverage": amount_coverage,
        "classification_method": method_counts,
        "confidence": amount_coverage,
        "restricted_cash": restricted,
        "items": [it.to_dict() for it in items],
        "note_conflicts": view["note_conflicts"],
        "llm_calls": getattr(classifier, "calls", 0),
        "llm_skipped": getattr(classifier, "skipped", 0),
        "llm_truncated": getattr(classifier, "truncated", 0),
        "llm_empty": getattr(classifier, "empty", 0),
        "_view": view,
    }


def _text_layer_note(diag):
    """文本层看起来坏了就给一句实话，正常则返回 ``None``。

    「找不到报表」在文本层已坏时是**误导**：那张表就在那儿，只是它的字没解
    出来——招商银行半年报 41,089 个片段里只有 1 个含汉字，报的却是「找不到
    报表：合并资产负债表」，于是被当成版面问题查了很久。

    判据用**汉字片段占比**而不是 ``undecodable`` 的绝对值：一份用错编码解出来
    的 PDF 可能每个字都「解出来了」，只是解成了别的字，这时 undecodable 很小
    而占比塌到零。正常情况返回 ``None``，不去污染真正的「这张表不在」。
    """
    runs = diag.get("runs") or 0
    cjk = diag.get("cjk_runs") or 0
    if runs >= 500 and cjk * 20 < runs:
        msg = ("文本层可能不可信：%d 个文字片段里只有 %d 个含汉字，"
               "另有 %d 个字符解不出、%d 个片段靠兜底推出来"
               % (runs, cjk, diag.get("undecodable") or 0,
                  diag.get("synthetic_cmap") or 0))
        # 派生出来的片段单独说。它与「兜底推出来」不是一回事——那一个是猜码位，
        # 这一个是按字体声明的字符集算——但如果一份文档大量靠派生才解出来，
        # 而结果仍然不可信，那正是要看的信息。（旧缓存没有这个键，取出来是 0，
        # 消息与从前逐字相同。）
        if diag.get("derived_cmap"):
            msg += "（其中 %d 个片段是按 Adobe-GB1 的字符集定义派生的）" % diag["derived_cmap"]
        return msg
    return None


#: 最新一期抽不出报表时，往旧报告期回溯的**期数上限**。
#:
#: 上限的意义：回溯是「换一份有文本层的同类报表」，不是「一直翻到算出数为止」。
#: 翻得越远，口径离当前时点越远——3 期足够覆盖「最新一期是扫描件」这一类，
#: 又不会翻到半年前的时点上去。
MAX_PERIOD_LOOKBACK = 3


def _unusable_reason(metrics):
    """这份口径不能用的原因；能用就返回 ``None``。

    **不是「没抛异常」就算可用。** 两条判据：

    * ``build_economic_view`` 在找到了表、却认不出合计行时返回
      ``total_assets=None`` 而不抛错；002460 在库里就留着这样一行 NULL 快照
      ——它当时被当成了一次成功结果。所以「资产总计为空」必须与抛错同等对待，
      否则回溯会停在一个同样不可用的报告期上。
    * 资产总计与市值差着量级（:data:`AMOUNT_RATIO_MIN` ~
      :data:`AMOUNT_RATIO_MAX`）。这一类错误的特征是**数字长得完全正常**：
      13,785,280 是元还是百万元都读得通，只有和市值比才看得出差 10⁶。既然
      看不出来，就宁可不让它进库——库里的数字会被评分和界面当真。

    已通过的 16 只快照两个判据都不触发（资产总计非空，比值 0.72~14.4）。
    """
    if metrics is None:
        return "没有报告可算"
    if metrics.get("total_assets") is None:
        return "认不出资产合计行，资产总计为空"
    gate = (metrics.get("asset_value_profile") or {}).get("amount_gate") or {}
    if gate.get("checked") and not gate.get("valid"):
        return gate.get("reason") or "资产总计与市值量级不符"
    return None


def analyze(code, market_cap=None, store=None, classifier=None, refresh=False):
    """按股票代码跑完整条链：取财报 → 抽行 → 算指标。

    财报没下过就下、没解析过就解析；第二次调用全部命中缓存。

    最新一期抽不出可用口径时**往旧报告期回溯**（上限
    :data:`MAX_PERIOD_LOOKBACK` 期），第一个能用的即采用。中国建筑最新一期
    的报表页是扫描件、没有文本层，而它的上一期文本层完好——回溯到的那一期
    会写进 ``report_period`` 落库，并在详情页写明，**不会**假装自己是最新一期。
    """
    store = store or reports.ReportStore()
    metas = store.reports(code)[:MAX_PERIOD_LOOKBACK]
    if not metas:
        return None
    latest_period = metas[0].get("report_period")
    tried = []
    for meta in metas:
        meta = store.ensure_pdf(meta)
        rows, diag = store.rows_with_diag(meta)
        period = meta.get("report_period")
        try:
            metrics = compute(rows, market_cap, meta, classifier)
        except sem.BalanceSheetError as e:
            note = _text_layer_note(diag)
            tried.append((period, "%s（%s）" % (e, note) if note else str(e)))
            continue
        why = _unusable_reason(metrics)
        if why:
            note = _text_layer_note(diag)
            tried.append((period, "%s%s" % (why, "（%s）" % note if note else "")))
            continue
        metrics["stock_code"] = code
        metrics["report_title"] = meta.get("title")
        if period != latest_period:
            # 用了非最新期就把原因写进快照。**这句话必须落库**：报告期本身在
            # 快照里，但「为什么不是最新一期」只读快照时看不出来，而它正是
            # 判断这个数还能不能和别的股票比时最需要知道的一件事。
            metrics["report_period_note"] = (
                "最新一期 %s 抽不出可用口径（%s），本口径基于 %s"
                % (latest_period,
                   "；".join("%s：%s" % t for t in tried), period))
        return metrics
    # 每一期各自的原因都写上。只报最新一期那一条会引出「那上一期又为什么
    # 不行」——而这一层存在的意义就是让失败原因可查，不是让人再猜一轮。
    raise sem.BalanceSheetError(
        "最近 %d 期报告都抽不出可用口径：%s"
        % (len(tried), "；".join("%s：%s" % (p or "?", why)
                                for p, why in tried)))


# --------------------------------------------------------------------------- #
# 落库
# --------------------------------------------------------------------------- #
def _json(obj):
    return json.dumps(obj, ensure_ascii=False)


def _result_hash(metrics):
    """结果指纹：把这套口径算出来的东西压成一个短哈希。

    只用 ``source_document`` 判断「要不要再写一行」是不够的——同一份财报，
    抽取代码改过之后结论会变，而文档哈希没变，于是新的结果被当成重复而
    **静默丢弃**，界面继续显示旧数字。这个坑在开发期真的踩到了：修好附注
    号识别之后重跑审计，读回来的还是修复前那份快照。

    所以指纹要覆盖**算出来的结果**，不是输入。同样的结果不重复写；结果变了
    就新写一行——留着变化过程正是这张表存在的意义。
    """
    profile = metrics.get("asset_value_profile") or {}
    payload = json.dumps({
        "assets": metrics.get("total_assets"),
        "cash": metrics.get("cash_tiers"),
        "net": metrics.get("net_cash"),
        "value": profile.get("scenarios"),
        # 负债侧也要进指纹：整段非流动负债漏掉时，资产侧可能一个数都没变，
        # 光比资产+现金+净现金的话，这份「负债少了 256 亿」的结果会被当成
        # 重复结果**静默丢弃**，界面继续显示上一版。
        "liabilities": profile.get("liabilities"),
        "items": [[i.get("account"), i.get("sub_item"), i.get("amount"),
                   i.get("economic_class")] for i in metrics.get("items") or []],
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def save(conn, metrics, date=None):
    """写入资产语义快照。

    结果没变就不写第二行——同一个报告期重复分析几十次只会在页面上堆出一串
    一模一样的记录，把真正的变化淹掉。结果变了就追加一行，保留解释口径的
    演变过程。
    """
    ensure_schema(conn)
    code = metrics.get("stock_code")
    period = metrics.get("report_period")
    version = metrics.get("metric_version")
    digest = _result_hash(metrics)
    existing = conn.execute(
        "SELECT id, result_hash FROM asset_semantic_snapshot"
        " WHERE stock_code=? AND metric_version=? AND report_period=?"
        " ORDER BY id DESC LIMIT 1", (code, version, period)).fetchone()
    if existing and existing["result_hash"] == digest:
        return existing["id"], False

    cur = conn.execute(
        "INSERT INTO asset_semantic_snapshot (stock_code, date, metric_version,"
        " report_period, source_document, source_page, market_cap, total_assets,"
        " cash_tiers, net_cash, asset_value_profile, asset_consumption_rate,"
        " classification_coverage, classification_method, confidence, items,"
        " restricted_cash, note_conflicts, result_hash, llm_calls, llm_skipped,"
        " llm_truncated, llm_empty, report_period_note)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, date or datetime.now().strftime("%Y-%m-%d"), version, period,
         metrics.get("source_document"), metrics.get("source_page"),
         metrics.get("market_cap"), metrics.get("total_assets"),
         _json(metrics.get("cash_tiers")), _json(metrics.get("net_cash")),
         _json(metrics.get("asset_value_profile")),
         metrics.get("asset_consumption_rate"),
         metrics.get("classification_coverage"),
         _json(metrics.get("classification_method")),
         metrics.get("confidence"), _json(metrics.get("items")),
         metrics.get("restricted_cash"),
         _json(metrics.get("note_conflicts")), digest,
         metrics.get("llm_calls"), metrics.get("llm_skipped"),
         metrics.get("llm_truncated"), metrics.get("llm_empty"),
         metrics.get("report_period_note")))
    conn.commit()
    return cur.lastrowid, True


def load(conn, code, version=None):
    """读回最近一条资产语义快照。"""
    ensure_schema(conn)
    if version:
        row = conn.execute(
            "SELECT * FROM asset_semantic_snapshot WHERE stock_code=?"
            " AND metric_version=? ORDER BY id DESC LIMIT 1",
            (code, version)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM asset_semantic_snapshot WHERE stock_code=?"
            " ORDER BY id DESC LIMIT 1", (code,)).fetchone()
    if not row:
        return None
    out = dict(row)
    for key in ("cash_tiers", "net_cash", "asset_value_profile",
                "classification_method", "items", "note_conflicts"):
        if out.get(key):
            try:
                out[key] = json.loads(out[key])
            except ValueError:
                pass
    return out


def audit(conn, code):
    """§21 审计视图的数据：每一项资产的原始值、调整值、分类依据、来源。

    单独一个函数而不是塞进 ``load``，是因为它要按「值得怀疑的程度」排序——
    低置信度、有冲突、未分类的排在前面。审计的意义就是先看最可能错的地方，
    而不是从头顺着一百多行读下去。
    """
    snap = load(conn, code)
    if not snap:
        return None
    items = []
    for raw in snap.get("items") or []:
        adj = {s: hc.adjusted_value(_Item(raw), s) for s in hc.SCENARIOS}
        items.append({
            "account": raw["account"],
            "sub_item": raw["sub_item"],
            "amount": raw["amount"],
            "prior": raw["prior"],
            "adjusted": adj,
            "economic_class": raw["economic_class"],
            "restricted": raw["restricted"],
            "confidence": raw["confidence"],
            "method": raw["method"],
            "classifier_version": METRIC_VERSION,
            "source_page": raw["page"],
            "evidence": raw["evidence"],
            "source_text": raw["source_text"],
            "conflict": raw["conflict"],
        })
    items.sort(key=lambda d: (d["confidence"], -abs(d["amount"] or 0.0)))
    return {
        "stock_code": code,
        "metric_version": snap["metric_version"],
        "report_period": snap["report_period"],
        "source_document": snap["source_document"],
        "source_page": snap["source_page"],
        "market_cap": snap["market_cap"],
        "total_assets": snap["total_assets"],
        "cash_tiers": snap["cash_tiers"],
        "net_cash": snap["net_cash"],
        "asset_value_profile": snap["asset_value_profile"],
        "asset_consumption_rate": snap["asset_consumption_rate"],
        "classification_coverage": snap["classification_coverage"],
        "classification_method": snap["classification_method"],
        "confidence": snap["confidence"],
        "restricted_cash": snap["restricted_cash"],
        "note_conflicts": snap["note_conflicts"],
        "items": items,
    }


def audit_payload(conn, code):
    """§21 审计视图的完整数据：**每一笔资产的来龙去脉**。

    要求是「任何一个数字都能被追到它从哪一页、哪一句话来，以及被折了多少」。
    所以每一行都给全九个字段：指标 / 原始值 / 调整值 / 经济分类 / 折扣 /
    来源 / 期间 / 证据 / 置信度——少一个，审计就退化成「相信系统吧」。

    排序按「值得怀疑的程度」：低置信度、有冲突、未分类的在前。审计的意义是
    先看最可能错的地方，而不是从头顺着一百多行读下去。
    """
    snap = audit(conn, code)
    if not snap:
        return None
    rows = []
    for it in snap["items"]:
        obj = _Item(it)
        adj = {s: hc.adjusted_value(obj, s) for s in hc.SCENARIOS}
        rows.append({
            "metric": (it["account"] if it["sub_item"] == it["account"]
                       else f"{it['account']} / {it['sub_item']}"),
            "account": it["account"],
            "sub_item": it["sub_item"],
            "raw": it["amount"],
            "prior": it["prior"],
            "adjusted": adj,
            "economic_class": it["economic_class"],
            "economic_label": sem.CLASS_LABELS.get(it["economic_class"],
                                                   it["economic_class"]),
            "haircut": {s: hc.rate(it["economic_class"], s) for s in hc.SCENARIOS},
            "restricted": it["restricted"],
            "source_page": it["source_page"],
            "source_text": it["source_text"],
            "period": snap["report_period"],
            "evidence": it["evidence"],
            "confidence": it["confidence"],
            "method": it["method"],
            "conflict": it["conflict"],
        })
    return {
        "stock_code": code,
        "metric_version": snap["metric_version"],
        "haircut_version": hc.VERSION,
        "report_period": snap["report_period"],
        # 老快照没有这一列，取出来是 None——「没说过」和「说了没问题」必须分得开。
        "report_period_note": snap.get("report_period_note"),
        "source_document": snap["source_document"],
        "source_page": snap["source_page"],
        "market_cap": snap["market_cap"],
        "total_assets": snap["total_assets"],
        "cash_tiers": snap["cash_tiers"],
        "net_cash": snap["net_cash"],
        "asset_value_profile": snap["asset_value_profile"],
        "asset_consumption_rate": snap["asset_consumption_rate"],
        "classification_coverage": snap["classification_coverage"],
        "classification_method": snap["classification_method"],
        "confidence": snap["confidence"],
        "restricted_cash": snap["restricted_cash"],
        "note_conflicts": snap["note_conflicts"],
        "scenarios": list(hc.SCENARIOS),
        # LLM 那几次调用的去向。以前只有一个「问了几次」，截断和空响应都混在
        # 「模型没给结论」里，看不出是拒答还是输出预算不够——而「判定为什么
        # 不可复现」恰恰就要靠区分这两件事。
        "llm": {
            "calls": snap.get("llm_calls") or 0,
            "skipped": snap.get("llm_skipped") or 0,
            "truncated": snap.get("llm_truncated") or 0,
            "empty": snap.get("llm_empty") or 0,
        },
        "rows": rows,
    }


class _Item:
    """把存下来的 dict 包成折价引擎认的对象。"""

    __slots__ = ("amount", "economic_class")


    def __init__(self, raw):
        self.amount = raw.get("amount")
        self.economic_class = raw.get("economic_class")


def default_conn():
    return db.connect()
