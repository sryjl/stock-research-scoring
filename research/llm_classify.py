# -*- coding: utf-8 -*-
"""llm_classify.py — 规则命不中时的语义分类兜底。

**这一层的职责只有一个：给一个会计子项名，判断它在经济上属于哪一类资产。**
它不许改金额、不许推算金额、不许决定折价、不许算清算价值、不许评分、不许
给投资意见。想让它「顺便算一下」是最容易把可复现的估值变成不可复现的赌博
的做法——同一个数字每次跑出来都不一样，而这套系统的全部价值就在于可复核。

几条硬性约束（写在代码里，也写在注释里，因为它们很容易被后来的人顺手破掉）：

* **只从环境变量读 Key**。不读 VS Code 的 settings.json，不读任何配置文件。
  优先 ``DEEPSEEK_*``；为了兼容现有环境也接受 ``ANTHROPIC_*`` 作为回退。
* Key **不写源码、不写前端、不写 SQLite、不进日志、不进 Git**。所有的报错
  信息都先过 :func:`_scrub`，防止它从异常文本里漏出去。
* API 不可用就返回 ``OTHER_UNKNOWN``，主流程照常走完。拿不到答案不是错误，
  拿不到答案却瞎猜才是。
* 调用次数由 :data:`LLM_MAX_CALLS_PER_REPORT` 卡死。
"""
import hashlib
import json
import os
import re
import urllib.error
import urllib.request

from . import asset_semantics as sem
from . import llm_cache

CLASSIFIER_VERSION = "asset_semantic_llm_v1"

#: 一份报告最多问几次。附注里的「其他」科目动辄几十个，不封顶会变成刷 API。
LLM_MAX_CALLS_PER_REPORT = 5

#: 只有金额够得上门槛的 UNKNOWN 才值得问——几十万的「其他」问出来也不影响结论
MIN_ASSET_RATIO = 0.01          # 该项 / 总资产
MIN_MARKET_CAP_RATIO = 0.02     # 该项 / 当前市值

#: 单次回复的输出上限。这个端点（Anthropic 兼容层）会**先吐一个 thinking
#: 块再吐答案**，而 thinking 也算在 ``max_tokens`` 里。设 300 时实测 3 次
#: 里 1 次预算在思考阶段就烧完，整条回复只剩 thinking 块、没有 text，等于
#: 白问一次。原样 150 字左右的判定 JSON 加上思考约需 200–250 token，
#: 1000 留够了余量——输出上限不是花销，模型不会为了凑满而多写。
MAX_TOKENS = 1000

_SYSTEM = (
    "你是财务报表附注的分类器。用户给你一个资产负债表科目的子项名称，"
    "你只能回答它在经济上属于哪一类资产。\n"
    "禁止：修改金额、推算金额、决定折价率、计算清算价值、打分、给出投资意见。\n"
    "只能从给定枚举里选一个。不确定就回答 OTHER_UNKNOWN。\n"
    "只输出 JSON，不要任何解释文字。"
)

_PROMPT = """会计科目：{account}
子项名称：{name}
（如提供了附注原文，参考它，但仍只回答分类）
{context}

从下列枚举中选一个 economic_class：
{classes}

输出 JSON，且只有这一个对象：
{{"economic_class": "...", "restricted": true/false, "liquidity": "HIGH/MEDIUM/LOW",
  "confidence": 0.0-1.0, "evidence": "一句话依据"}}
"""


def _scrub(text, *secrets):
    """把可能混进异常文本里的 Key 抹掉。日志和报错都要先过这里。"""
    out = str(text)
    for s in secrets:
        if s and len(s) >= 8:
            out = out.replace(s, "***")
    # 兜底：任何看起来像 Key 的长 token 一律打码
    return re.sub(r"\b(sk|ak|key)-?[A-Za-z0-9_\-]{16,}", "***", out)


def load_config(env=None):
    """从环境变量读配置。``DEEPSEEK_*`` 优先，``ANTHROPIC_*`` 仅作兼容回退。

    刻意**不**接受任何文件路径参数：只要这里能读文件，早晚有人会指向
    VS Code 的 settings.json。
    """
    env = env if env is not None else os.environ
    key = env.get("DEEPSEEK_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN")
    base = env.get("DEEPSEEK_BASE_URL") or env.get("ANTHROPIC_BASE_URL")
    model = env.get("DEEPSEEK_MODEL") or env.get("ANTHROPIC_MODEL")
    if not key:
        return None
    if not base:
        base = ("https://api.deepseek.com" if env.get("DEEPSEEK_API_KEY")
                else "https://api.anthropic.com")
    return {
        "api_key": key,
        "base_url": base.rstrip("/"),
        "model": model or ("deepseek-chat" if env.get("DEEPSEEK_API_KEY")
                           else "claude-haiku-4-5-20251001"),
        "is_deepseek": bool(env.get("DEEPSEEK_API_KEY")),
    }


class LLMClassifier:
    """带缓存、带调用上限的分类器。

    缓存键 = ``document_hash + paragraph_hash + classifier_version + model_name``。
    同一份财报的同一段附注、同一个分类器版本、同一个模型，答案必然相同，
    所以第二次开始不该再发请求——这既省调用，也让结果可复现。
    """

    def __init__(self, config=None, cache=None, timeout=20):
        self.config = config if config is not None else load_config()
        self.cache = cache if cache is not None else {}
        self.timeout = timeout
        self.calls = 0
        self.cache_hits = 0
        self.failures = 0
        self.skipped = 0
        #: 响应里**根本没有 text 块**的次数。以前这跟「模型没给出结论」在代码上
        #: 完全同形——都返回 ``None``、都不进 ``failures``——于是真正的原因被
        #: 静默吞掉。这个端点先吐 thinking 块，``max_tokens`` 一旦被思考烧完就
        #: 只剩它，所以 ``truncated`` 专门记这种，``empty`` 记其余没有 text 块的
        #: 情形。分开记才能回答「判定不可复现是因为模型拒答还是因为预算不够」。
        self.truncated = 0
        self.empty = 0

    # -- 对外 ---------------------------------------------------------------- #
    @property
    def available(self):
        return bool(self.config and self.config.get("api_key"))

    def gate(self, item, total_assets=None, market_cap=None):
        """§9 的调用门槛：只有 UNKNOWN、且金额够大时才值得问。

        金额门槛是这一层最重要的节流阀。「其他流动资产—其他 473 万」这种
        项目，问不问都不改变结论，但问一次就是一次外部调用、一份不确定性。

        本轮新增一条：**没有附注原文就不问**。§7 给模型的职责是「读附注做语义
        分类」，上下文为空时它手上只剩一个科目名，于是只有两条路可走——回
        ``OTHER_UNKNOWN``（被 :meth:`_parse_verdict` 作废，调用白花），或者
        **照着科目名编一句附注**。现役快照里华域「其他流动资产」的 evidence
        写着「一年内到期的发放贷款及垫款」，而华域根本没有这个科目；同一份空
        输入在历史上既回过 ``RECEIVABLE_NORMAL`` 也回过 ``OTHER_UNKNOWN``。
        这种判定不可复现，也不该进快照——要么有原文，要么不问。
        """
        if not self.available:
            self.skipped += 1
            return False
        if item.economic_class != sem.OTHER_UNKNOWN:
            return False
        if not (item.source_text or "").strip():
            self.skipped += 1
            return False
        amt = abs(item.amount or 0.0)
        if not amt:
            return False
        if total_assets and amt / total_assets >= MIN_ASSET_RATIO:
            return True
        if market_cap and amt / market_cap >= MIN_MARKET_CAP_RATIO:
            return True
        self.skipped += 1
        return False

    def classify(self, item, document_hash="", paragraph_hash=""):
        """返回 ``{"economic_class","restricted","liquidity","confidence","evidence"}``。

        任何失败路径都返回 ``None``，由调用方保持 OTHER_UNKNOWN——**不降级成
        猜测**，也不抛异常打断主流程。
        """
        if not self.available:
            return None
        if self.calls >= LLM_MAX_CALLS_PER_REPORT:
            self.skipped += 1
            return None

        key = self._cache_key(item, document_hash, paragraph_hash)
        if key in self.cache:
            self.cache_hits += 1
            return self.cache[key]

        self.calls += 1
        try:
            verdict = self._request(item)
        except Exception as exc:                  # 网络 / 解析 / 超时，一律降级
            self.failures += 1
            self.last_error = _scrub(exc, self.config.get("api_key"))
            return None
        if verdict:
            # 落盘实现多记一份附注来源，方便回查「这个分类是照着哪句话给的」。
            # 普通 dict 缓存没这个方法，退回它自己的协议。
            if hasattr(self.cache, "put"):
                self.cache.put(key, verdict, document_hash, paragraph_hash,
                               item.account, item.sub_item)
            else:
                self.cache[key] = verdict
        return verdict

    # -- 内部 ---------------------------------------------------------------- #
    def _cache_key(self, item, document_hash, paragraph_hash):
        raw = "|".join([
            document_hash or "", paragraph_hash or item.source_text or "",
            CLASSIFIER_VERSION, self.config.get("model", ""),
        ])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _request(self, item):
        prompt = _PROMPT.format(
            account=item.account, name=item.sub_item,
            context=f"附注原文：{item.source_text}" if item.source_text else "",
            classes=", ".join(sem.ECONOMIC_CLASSES))
        body = self._payload(prompt)
        url = self._endpoint()
        headers = {"Content-Type": "application/json"}
        if self.config["is_deepseek"]:
            headers["Authorization"] = "Bearer " + self.config["api_key"]
        else:
            headers["x-api-key"] = self.config["api_key"]
            headers["anthropic-version"] = "2023-06-01"

        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        text = self._extract_text(data)
        if not text.strip():
            # 没有 text 块。**不能跟「模型没给出结论」混为一谈**：这个端点先吐
            # thinking 块，``max_tokens`` 被思考烧完时整条回复只有它，表现与
            # 「问了个答不出来的问题」一模一样（都返回 None、都不进 failures），
            # 真正的原因就此静默消失。分开计数，才查得出「判定不可复现」是模型
            # 拒答还是预算不够。
            stop = data.get("stop_reason") if isinstance(data, dict) else None
            if stop == "max_tokens":
                self.truncated += 1
                self.last_error = "响应被 max_tokens 截断，没有 text 块"
            else:
                self.empty += 1
                self.last_error = f"响应没有 text 块（stop_reason={stop}）"
            return None
        return self._parse_verdict(text)

    def _endpoint(self):
        if self.config["is_deepseek"]:
            return self.config["base_url"] + "/chat/completions"
        return self.config["base_url"] + "/v1/messages"

    def _payload(self, prompt):
        if self.config["is_deepseek"]:
            return {
                "model": self.config["model"],
                "messages": [{"role": "system", "content": _SYSTEM},
                             {"role": "user", "content": prompt}],
                "temperature": 0,            # 分类任务不需要发挥
                "max_tokens": MAX_TOKENS,
            }
        return {
            "model": self.config["model"],
            "system": _SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
        }

    @staticmethod
    def _extract_text(data):
        if isinstance(data, dict) and "choices" in data:        # OpenAI 风格
            return (data["choices"][0].get("message") or {}).get("content", "")
        if isinstance(data, dict) and "content" in data:        # Anthropic 风格
            parts = data["content"]
            if isinstance(parts, list):
                return "".join(p.get("text", "") for p in parts
                               if isinstance(p, dict))
            return str(parts)
        return ""

    @staticmethod
    def _parse_verdict(text):
        """只取 JSON 对象，并逐字段校验；越界的类别一律作废。"""
        if not text:
            return None
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            return None
        cls = obj.get("economic_class")
        if cls not in sem.ECONOMIC_CLASSES or cls == sem.OTHER_UNKNOWN:
            return None                       # 越界或没结论，就当没问过
        try:
            conf = float(obj.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        return {
            "economic_class": cls,
            "restricted": bool(obj.get("restricted")),
            "liquidity": obj.get("liquidity"),
            "confidence": max(0.0, min(1.0, conf)),
            "evidence": str(obj.get("evidence") or "")[:300],
        }


def default_classifier(conn=None):
    """业务层的取用入口；没有 Key 时返回一个永远不可用的分类器。

    传 ``conn`` 就把缓存落到 :mod:`research.llm_cache` 的库表上，跨进程复用
    ——服务端每次请求都新建分类器，不落盘的话缓存键形同虚设，每审计一次就
    重打一次 API。

    **没有 Key 时不建表**：一个永远不会被问的分类器不需要在业务库里多一张表，
    而且建表要写连接，会在一台没配过 Key 的机器上留下无意义的空表。
    """
    clf = LLMClassifier()
    if conn is not None and clf.available:
        store = llm_cache.load_store(
            conn, CLASSIFIER_VERSION, clf.config.get("model"))
        if store is not None:
            clf.cache = store
    return clf
