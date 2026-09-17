#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETH/USDT DEX 报价监控演示 Web 服务器
=====================================

功能
----
- /                     -> 静态首页 index.html
- /static/<file>        -> 静态资源
- /api/quotes?kw=...     -> 现场运行报价采集，返回 JSON（支持关键词解析）
- /api/example          -> 初始化示例数据（页面加载即展示）
- /api/run-script       -> 与 /api/quotes 等价的"现场运行脚本"入口（语义清晰）
- /api/metrics          -> 监控指标定义 / 告警规则（供前端展示）

启动
----
    python3 web_server.py [--port 8080] [--host 0.0.0.0]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

# 确保能 import 同级模块
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import dex_quote_monitor as monitor  # noqa: E402

STATIC_DIR = os.path.join(HERE, "static")

# 服务器默认是否启用真实报价模式（可用 --real 在启动时打开）
SERVER_DEFAULT_REAL = False


# ----------------------------------------------------------------------------
# 关键词解析
# ----------------------------------------------------------------------------

def parse_keywords(kw: str):
    """
    简单关键词解析（不区分大小写、空格分隔）：
      - 平台名：Uniswap / 1inch / CowSwap / Tokenlon
      - 方向  ：ETH->USDT / USDT->ETH
      - 金额  ：纯数字或带 USD/K/M 后缀，如 50000 / 50K / 1M / 50000USD
      - ETH 中价 / gas：price=3000 / gas=30
    未匹配的关键词原样忽略（不报错）。
    """
    if not kw:
        return {}
    kw_lower = kw.lower()
    tokens = re.split(r"\s+", kw.strip())

    platforms = []
    directions = []
    amounts = []
    eth_price = monitor.DEFAULT_ETH_PRICE_USD
    gas_gwei = monitor.DEFAULT_GAS_PRICE_GWEI
    unmatched = []

    name_map = {
        "uniswap": "Uniswap",
        "1inch": "1inch",
        "cowswap": "CowSwap",
        "tokenlon": "Tokenlon",
    }
    dir_map = {
        "eth->usdt": "ETH->USDT",
        "usdt->eth": "USDT->ETH",
        "eth/usdt": "ETH->USDT",
        "usdt/eth": "USDT->ETH",
    }

    for t in tokens:
        low = t.lower()
        if low in name_map:
            platforms.append(name_map[low])
            continue
        if low in dir_map:
            directions.append(dir_map[low])
            continue
        # 金额：支持 50000 / 50K / 1M / 50000usd / 50kusd
        m = re.match(r"^(\d+(?:\.\d+)?)([km]?)(?:usd)?$", low)
        if m:
            num = float(m.group(1))
            unit = m.group(2)
            if unit == "k":
                num *= 1_000
            elif unit == "m":
                num *= 1_000_000
            amounts.append(num)
            continue
        # price=3000
        m2 = re.match(r"^price=(\d+(?:\.\d+)?)$", low)
        if m2:
            eth_price = float(m2.group(1))
            continue
        # gas=30
        m3 = re.match(r"^gas=(\d+(?:\.\d+)?)$", low)
        if m3:
            gas_gwei = float(m3.group(1))
            continue
        unmatched.append(t)

    return {
        "platforms": platforms or None,
        "directions": directions or None,
        "amounts": amounts or None,
        "eth_price": eth_price,
        "gas_gwei": gas_gwei,
        "unmatched": unmatched,
        "raw": kw,
    }


def run_query(kw: str, use_real: bool = False):
    """
    执行报价采集并返回结构化结果（dict）。
    use_real=True 时优先调用真实 API（CowSwap/ParaSwap/Uniswap 池子 slot0）；
    真实模式还会自动从链上获取 ETH 中价（覆盖关键词中的 price=）。
    """
    parsed = parse_keywords(kw)
    eth_price = parsed["eth_price"]
    gas_gwei = parsed["gas_gwei"]
    data_source = "simulated_quote (基于协议真实参数模型)"
    real_status = None

    if use_real and monitor._REAL_AVAILABLE and monitor.real_providers is not None:
        # 先做健康检查（uniswap_quote 在其中读取 slot0 会顺便预热中价缓存）
        try:
            real_status = monitor.real_providers.health_check()
        except Exception as e:
            real_status = {"_error": str(e)}
        # 自动从链上获取 ETH 中价（覆盖默认/关键词中的 price=）
        # 健康检查已预热缓存，此处优先用实时值，失败则用 60s 内缓存
        mp = monitor.real_providers.get_eth_usd_midprice()
        if mp and mp > 0:
            eth_price = mp
        data_source = "real_api (CowSwap/ParaSwap/Uniswap slot0) + 模拟回退"

    quotes = monitor.get_quotes_matrix(
        eth_price=eth_price,
        gas_gwei=gas_gwei,
        platforms=parsed["platforms"],
        directions=parsed["directions"],
        amounts=parsed["amounts"],
        use_real=use_real,
    )
    analysis = monitor.analyze(quotes)
    return {
        "snapshot_time": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "eth_price": eth_price,
        "gas_gwei": gas_gwei,
        "use_real": bool(use_real and monitor._REAL_AVAILABLE),
        "data_source": data_source,
        "real_providers_status": real_status,
        "parsed_keywords": parsed,
        "platforms": parsed["platforms"] or monitor.PLATFORMS,
        "directions": parsed["directions"] or monitor.DIRECTIONS,
        "amount_tiers": parsed["amounts"] or monitor.AMOUNT_TIERS,
        "quotes": [monitor.asdict(q) for q in quotes],
        "analysis": analysis,
    }


# ----------------------------------------------------------------------------
# 监控指标 / 告警规则（静态文档）
# ----------------------------------------------------------------------------

METRICS_DOC = {
    "metrics": [
        {"key": "net_received_usd", "name": "净到手 USD",
         "definition": "扣 gas 后用户实际到手金额，排名与对比核心口径"},
        {"key": "effective_spread_bps", "name": "有效价差 bps",
         "definition": "fee + impact - surplus，用于横向对比竞争力"},
        {"key": "gap_vs_best_bps", "name": "相对最优价差 bps",
         "definition": "本平台与同分组最优的净到手差距"},
        {"key": "price_impact_bps", "name": "价格影响 bps",
         "definition": "衡量流动性深度，大单区间放大"},
        {"key": "gas_cost_usd", "name": "gas 成本 USD",
         "definition": "小额区间稀释收益的关键因子"},
        {"key": "best_frequency_pct", "name": "最优频率 %",
         "definition": "周期内成为最优的占比，整体竞争力"},
        {"key": "cowswap_surplus_bps", "name": "CowSwap surplus bps",
         "definition": "批量拍卖相对即时报价的额外回报"},
        {"key": "agg_vs_uni_bps", "name": "聚合器 vs Uniswap bps",
         "definition": "聚合器能否覆盖其更高 gas 的核心指标"},
    ],
    "collection_frequency": [
        "实时报价快照：每 1 分钟一次（覆盖短时套利窗口）",
        "中价/gas 基准：每 15 秒一次（影响所有报价基准）",
        "历史趋势入库：每 5 分钟落库一次，供日报/周报",
        "大单（≥100K USD）区间：触发即时重采（MEV/深度波动大）",
    ],
    "alert_rules": [
        {"rule": "单一平台连续 N 次成为最优（N>=5）",
         "action": "通知做市团队检查价差"},
        {"rule": "gap_best_vs_second_bps < 1bps 持续 10 分钟",
         "action": "高度竞争，触发套利扫描"},
        {"rule": "gap_best_vs_worst_bps > 50bps",
         "action": "单一平台深度异常，疑似流动性撤出"},
        {"rule": "CowSwap surplus < 0bps 持续 5 分钟",
         "action": "solver 不返 surplus，市场可能混乱"},
        {"rule": "单 DEX 净到手超过所有聚合器",
         "action": "聚合器路由失效或 gas 过高，告警"},
        {"rule": "gas_cost_usd / amount_usd > 1%（小额区间）",
         "action": "提示用户走聚合器或合并订单"},
    ],
    "chart_suggestions": [
        {"name": "排名热力图", "desc": "x=金额区间，y=平台，cell=排名，快速看每个区间最优"},
        {"name": "最优报价频率柱状图", "desc": "x=平台，y=成为最优的百分比"},
        {"name": "价差随金额变化曲线", "desc": "x=金额(log)，y=相对最优 bps，看大单拉大趋势"},
        {"name": "双向报价对比图", "desc": "ETH->USDT 与 USDT->ETH 并排，看做市商对称性"},
        {"name": "价格影响放大曲线", "desc": "x=金额，y=impact(bps)，看流动性临界点"},
        {"name": "gas 占比曲线", "desc": "x=金额，y=gas/净到手%，看小额稀释"},
    ],
}


# ----------------------------------------------------------------------------
# 示例数据（页面初始化时展示）
# ----------------------------------------------------------------------------

EXAMPLE_KEYWORD = "ETH->USDT 50000 Uniswap 1inch CowSwap Tokenlon"

EXAMPLE_RESULT = run_query(EXAMPLE_KEYWORD)


# ----------------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静音默认日志，改用自定义
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    # ---------- 通用 helpers ----------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, content_type="text/plain; charset=utf-8", status=200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        if not os.path.isfile(path):
            self._send_text("404 Not Found", status=404)
            return
        ext = os.path.splitext(path)[1].lower()
        ct = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._send_file(os.path.join(STATIC_DIR, "index.html"))
            return
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            # 防目录穿越
            rel = rel.replace("..", "").lstrip("/")
            self._send_file(os.path.join(STATIC_DIR, rel))
            return
        if path == "/api/example":
            self._send_json({
                "keyword": EXAMPLE_KEYWORD,
                "explanation": (
                    "示例关键词解析：方向=ETH->USDT、金额=50000 USD、"
                    "平台=Uniswap/1inch/CowSwap/Tokenlon"
                ),
                "result": EXAMPLE_RESULT,
            })
            return
        if path == "/api/metrics":
            self._send_json(METRICS_DOC)
            return
        if path == "/api/health":
            # 真实提供者健康检查 + ETH 中价
            if not (monitor._REAL_AVAILABLE and monitor.real_providers is not None):
                self._send_json({
                    "real_available": False,
                    "message": "real_providers 模块未加载",
                })
                return
            try:
                status = monitor.real_providers.health_check()
                mp = monitor.real_providers.get_eth_usd_midprice()
                self._send_json({
                    "real_available": True,
                    "providers": status,
                    "eth_usd_midprice": mp,
                })
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return
        if path in ("/api/quotes", "/api/run-script"):
            kw = (qs.get("kw", [""])[0] or "").strip()
            # real=1 启用真实 API 报价（GET 参数优先于服务器默认）
            real_flag = SERVER_DEFAULT_REAL or (qs.get("real", ["0"])[0] in ("1", "true", "yes"))
            try:
                result = run_query(kw, use_real=real_flag)
                self._send_json(result)
            except Exception as e:
                self._send_json({"error": str(e), "keyword": kw}, status=400)
            return

        self._send_text("404 Not Found", status=404)

    def do_OPTIONS(self):  # CORS 预检
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def main(argv=None):
    parser = argparse.ArgumentParser(description="ETH/USDT DEX 报价监控 Web 服务器")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=8080, help="监听端口，默认 8080")
    parser.add_argument("--real", action="store_true",
                        help="启动即默认启用真实 API 报价（仍可被 ?real=0 覆盖；默认未开启时可用 ?real=1 临时启用）")
    args = parser.parse_args(argv)

    global SERVER_DEFAULT_REAL, EXAMPLE_RESULT
    SERVER_DEFAULT_REAL = bool(args.real)

    # 启动前预热示例数据（按 --real 决定数据源）
    EXAMPLE_RESULT = run_query(EXAMPLE_KEYWORD, use_real=SERVER_DEFAULT_REAL)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    real_tag = " [REAL 已启用]" if SERVER_DEFAULT_REAL else " [模拟模式，?real=1 可临时启用真实]"
    print(f"[web] ETH/USDT DEX 报价监控服务已启动{real_tag}")
    print(f"[web] 监听: http://{args.host}:{args.port}")
    print(f"[web] real_providers 可用: {monitor._REAL_AVAILABLE}")
    print(f"[web] 示例关键词: {EXAMPLE_KEYWORD}")
    print(f"[web] 端点:")
    print(f"[web]   GET /                  -> 首页")
    print(f"[web]   GET /api/example       -> 初始化示例数据")
    print(f"[web]   GET /api/metrics       -> 监控指标/告警规则")
    print(f"[web]   GET /api/health        -> 真实提供者健康检查 + ETH 中价")
    print(f"[web]   GET /api/quotes?kw=... &real=1  -> 现场运行（关键词采集）")
    print(f"[web]   GET /api/run-script?kw=...     -> 同上（语义化别名）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[web] 收到中断信号，正在关闭...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
