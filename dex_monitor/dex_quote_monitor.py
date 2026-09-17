#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETH/USDT DEX 报价监控与分析工具
================================================

本工具用于监控以太坊主网上以下 DEX/聚合器对 ETH/USDT 双向交易的报价竞争力：
  - Tokenlon (RFQ 报价)
  - Uniswap (V3 AMM)
  - 1inch (聚合器)
  - CowSwap (批量拍卖)

数据来源说明
------------
当前实现以"模拟报价"为主：基于每个协议的真实参数（费率、流动性深度、gas、
MEV 风险特征）建立可解释的报价模型。模块对外暴露统一的 get_quote() 接口，
真实接入时只需把内部模拟函数替换为对应 RPC/SDK/REST 调用，分析层无需改动。

运行
----
CLI:
    python3 dex_quote_monitor.py --report          # 输出结构化报告
    python3 dex_quote_monitor.py --json           # 输出 JSON 给前端消费
    python3 dex_quote_monitor.py --amount 50000   # 只看某个金额区间
    python3 dex_quote_monitor.py --direction ETH->USDT
    python3 dex_quote_monitor.py --eth-price 3000 --gas-gwei 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple

# 真实 API 提供者（可选导入，失败时回退到模拟模式）
try:
    import real_providers
    _REAL_AVAILABLE = True
except Exception:
    _REAL_AVAILABLE = False
    real_providers = None


# ----------------------------------------------------------------------------
# 全局常量 / 默认参数
# ----------------------------------------------------------------------------

DEFAULT_ETH_PRICE_USD = 3000.0       # 默认 ETH/USDT 中价 (USD per ETH)
DEFAULT_GAS_PRICE_GWEI = 30.0        # 默认主网 gas 价格 (gwei)
DEFAULT_BLOCK_HEIGHT = 18_500_000    # 用于标注数据快照（占位高度）

PLATFORMS = ["Uniswap", "1inch", "CowSwap", "Tokenlon"]
DIRECTIONS = ["ETH->USDT", "USDT->ETH"]
AMOUNT_TIERS = [
    1_000, 5_000, 10_000, 50_000, 100_000,
    500_000, 1_000_000, 5_000_000, 10_000_000,
]


# ----------------------------------------------------------------------------
# 各 DEX/聚合器参数模型
# 字段说明：
#   fee_bps            : 协议费率（basis points，1bp = 0.01%）
#   liquidity_usd      : 等效流动性深度（USD），用于价格影响计算
#   gas_units          : 典型 gas 消耗（单笔 swap）
#   routing_complexity : 路由复杂度评分（1=单跳，2=多跳，3=聚合多路径）
#   mev_risk           : MEV 风险等级 (very_low / low / medium / high)
#   surplus_bps        : 协议给用户的"额外回报"（负的费率，bps）
#                        CowSwap 批量拍卖 surplus、Tokenlon 大单返点
#   size_bonus         : 大单折扣系数（金额越大折扣越大，bps per $1M）
# ----------------------------------------------------------------------------

DEX_PROFILES: Dict[str, Dict] = {
    "Uniswap": {
        "fee_bps": 5,                 # V3 0.05% 池（ETH/USDT 主流低费率池）
        "liquidity_usd": 50_000_000, # 集中流动性（活跃区间内深度）
        "gas_units": 180_000,
        "routing_complexity": 1,
        "mev_risk": "low",
        "surplus_bps": 0,
        "size_bonus": 0,
        "description": "Uniswap V3 0.05% 池，集中流动性，深度大但大单滑点明显",
    },
    "1inch": {
        "fee_bps": 0,                # 聚合器本身不收费（取底层池费）
        "liquidity_usd": 120_000_000,# 聚合后等效深度（多路径+跨池）
        "gas_units": 300_000,
        "routing_complexity": 3,
        "mev_risk": "medium",
        "surplus_bps": 0,
        "size_bonus": 0,
        "description": "1inch 聚合器，多路径寻路，gas 较高但深度聚合",
    },
    "CowSwap": {
        "fee_bps": 0,                # 协议无显式费用
        "liquidity_usd": 90_000_000,
        "gas_units": 320_000,
        "routing_complexity": 2,
        "mev_risk": "very_low",     # 批量拍卖 + solver 竞价，MEV 防护最强
        "surplus_bps": 6,            # 批量拍卖 surplus 约 6bps（solver 返还）
        "size_bonus": 0,
        "description": "CowSwap 批量拍卖，solver 竞价含 MEV 防护，大单 surplus 明显",
    },
    "Tokenlon": {
        "fee_bps": 10,               # RFQ 做市商价差约 10bps
        "liquidity_usd": 100_000_000,# 做市商资金池总深度
        "gas_units": 200_000,
        "routing_complexity": 1,
        "mev_risk": "very_low",     # RFQ 链下撮合，无三明治风险
        "surplus_bps": 0,
        "size_bonus": 1.2,           # 大单返点（机构量级折扣）
        "description": "Tokenlon RFQ，专业做市商链下报价，大单返点，无 MEV",
    },
}


# ----------------------------------------------------------------------------
# 报价数据结构
# ----------------------------------------------------------------------------

@dataclass
class Quote:
    platform: str
    direction: str
    amount_usd: float
    base_price: float           # 基础中价（ETH->USDT: USDT/ETH；USDT->ETH: ETH/USDT）
    exec_price: float           # 可执行价格（含 fee + impact - surplus）
    gross_out_usd: float         # 扣 fee/impact/surplus 后毛到手 USD（不含 gas）
    gas_cost_usd: float         # 预计 gas 成本 USD
    net_received_usd: float     # 净到手 USD = gross_out - gas
    price_impact_bps: float     # 价格影响（bps）
    fee_bps: float               # 协议费（bps，含 surplus 抵扣）
    effective_spread_bps: float # 相对中价的"全部价差"（fee + impact - surplus）
    gas_units: int
    routing_complexity: int
    mev_risk: str
    notes: str = ""


# ----------------------------------------------------------------------------
# 报价引擎
# ----------------------------------------------------------------------------

def price_impact(amount_usd: float, liquidity_usd: float) -> float:
    """
    简化 AMM 价格影响模型：
        impact = amount / (liquidity + amount)
    当 amount << liquidity 时近似线性；当 amount 接近 liquidity 时趋于 50%。
    真实 Uniswap V3 因集中流动性，活跃区间内 impact 比此模型更小，
    大单区间则急剧放大。本模型已做合理近似用于竞争力比较。
    """
    if liquidity_usd <= 0:
        return 1.0
    return amount_usd / (liquidity_usd + amount_usd)


def gas_cost_usd(gas_units: int, gas_gwei: float, eth_price: float) -> float:
    """单笔 swap 的 gas 成本 USD = gas_units * gas_gwei * 1e-9 * eth_price"""
    return gas_units * gas_gwei * 1e-9 * eth_price


def _build_quote_from_real(
    platform: str,
    direction: str,
    amount_usd: float,
    eth_price: float,
    gas_gwei: float,
    real_q: dict,
) -> Quote:
    """从 real_providers 返回的 dict 组装 Quote 对象。"""
    p = DEX_PROFILES[platform]
    gross_out_usd = real_q["gross_out_usd"]
    gas_units = real_q.get("gas_units", p["gas_units"])
    g_cost = gas_cost_usd(gas_units, gas_gwei, eth_price)
    net_received = gross_out_usd - g_cost
    exec_price = real_q["exec_price"]
    impact_bps = real_q.get("price_impact_bps", 0)
    fee_bps = real_q.get("fee_bps", p["fee_bps"])
    effective_spread_bps = fee_bps + impact_bps
    notes = real_q.get("notes", "")
    data_source = real_q.get("data_source", "real_api")

    if direction == "ETH->USDT":
        base_price = eth_price
        notes_full = notes + f" | 输入 {amount_usd/eth_price:.6f} ETH → 输出 {gross_out_usd:.2f} USDT"
    else:
        base_price = 1.0 / eth_price
        notes_full = notes + f" | 输入 {amount_usd:.2f} USDT → 输出 {gross_out_usd/eth_price:.6f} ETH"

    return Quote(
        platform=platform,
        direction=direction,
        amount_usd=amount_usd,
        base_price=base_price,
        exec_price=exec_price,
        gross_out_usd=gross_out_usd,
        gas_cost_usd=g_cost,
        net_received_usd=net_received,
        price_impact_bps=impact_bps,
        fee_bps=fee_bps,
        effective_spread_bps=effective_spread_bps,
        gas_units=gas_units,
        routing_complexity=real_q.get("routing_complexity", p["routing_complexity"]),
        mev_risk=p["mev_risk"],
        notes=notes_full,
    )


def get_quote(
    platform: str,
    direction: str,
    amount_usd: float,
    eth_price: float = DEFAULT_ETH_PRICE_USD,
    gas_gwei: float = DEFAULT_GAS_PRICE_GWEI,
    use_real: bool = False,
) -> Quote:
    """
    获取单个平台在指定方向、金额下的报价。
    返回 Quote 对象，含可执行价格、毛到手、净到手、gas、价格影响等字段。

    use_real=True 时优先调用真实 API (CowSwap/ParaSwap/Uniswap 池子)；
    失败自动回退到模拟模型。
    """
    # 真实报价模式：优先调用 real_providers
    if use_real and _REAL_AVAILABLE and real_providers is not None:
        real_q = real_providers.get_real_quote(platform, direction, amount_usd, eth_price, gas_gwei)
        if real_q is not None:
            return _build_quote_from_real(platform, direction, amount_usd, eth_price, gas_gwei, real_q)

    # 模拟报价（默认/回退）
    p = DEX_PROFILES[platform]
    impact = price_impact(amount_usd, p["liquidity_usd"])
    impact_bps = impact * 10000

    # 大单折扣（Tokenlon 等做市商随金额上升返点）
    size_bonus_bps = p.get("size_bonus", 0) * (amount_usd / 1_000_000)

    # 综合费率（bps）：协议费 + 价格影响 - surplus - 大单返点
    fee_bps = p["fee_bps"]
    surplus_bps = p.get("surplus_bps", 0)
    effective_spread_bps = fee_bps + impact_bps - surplus_bps - size_bonus_bps
    if effective_spread_bps < 0:
        effective_spread_bps = 0  # 不能负到给用户倒贴

    net_factor = 1 - effective_spread_bps / 10000

    # 名义毛到手 USD（按中价换算）
    gross_out_usd = amount_usd * net_factor

    # gas 成本
    g_cost = gas_cost_usd(p["gas_units"], gas_gwei, eth_price)

    # 净到手
    net_received = gross_out_usd - g_cost

    # 可执行价格（按输出/输入）
    if direction == "ETH->USDT":
        # 输入 ETH = amount_usd / eth_price，输出 USDT = gross_out_usd
        eth_in = amount_usd / eth_price
        exec_price = gross_out_usd / eth_in if eth_in > 0 else 0
        base_price = eth_price
        notes = f"输入 {eth_in:.6f} ETH → 输出 {gross_out_usd:.2f} USDT"
    else:  # USDT->ETH
        usdt_in = amount_usd
        eth_out = gross_out_usd / eth_price
        exec_price = eth_out / usdt_in if usdt_in > 0 else 0
        base_price = 1.0 / eth_price
        notes = f"输入 {usdt_in:.2f} USDT → 输出 {eth_out:.6f} ETH"

    return Quote(
        platform=platform,
        direction=direction,
        amount_usd=amount_usd,
        base_price=base_price,
        exec_price=exec_price,
        gross_out_usd=gross_out_usd,
        gas_cost_usd=g_cost,
        net_received_usd=net_received,
        price_impact_bps=impact_bps,
        fee_bps=fee_bps - surplus_bps - size_bonus_bps,  # 净费率
        effective_spread_bps=effective_spread_bps,
        gas_units=p["gas_units"],
        routing_complexity=p["routing_complexity"],
        mev_risk=p["mev_risk"],
        notes=notes,
    )


def get_quotes_matrix(
    eth_price: float = DEFAULT_ETH_PRICE_USD,
    gas_gwei: float = DEFAULT_GAS_PRICE_GWEI,
    platforms: Optional[List[str]] = None,
    directions: Optional[List[str]] = None,
    amounts: Optional[List[float]] = None,
    use_real: bool = False,
) -> List[Quote]:
    """采集全量报价矩阵：所有 (platform, direction, amount) 组合。"""
    ps = platforms or PLATFORMS
    ds = directions or DIRECTIONS
    amts = amounts or AMOUNT_TIERS
    out: List[Quote] = []
    for d in ds:
        for amt in amts:
            for p in ps:
                out.append(get_quote(p, d, amt, eth_price, gas_gwei, use_real=use_real))
    return out


# ----------------------------------------------------------------------------
# Async 版本（aiohttp + asyncio.gather 并发，供 web/批量场景使用）
# 真实模式下用 async 并发拉取链上/API 报价，总耗时≈单次最慢请求而非累加
# ----------------------------------------------------------------------------

async def get_quote_async(
    session,
    platform: str,
    direction: str,
    amount_usd: float,
    eth_price: float = DEFAULT_ETH_PRICE_USD,
    gas_gwei: float = DEFAULT_GAS_PRICE_GWEI,
    use_real: bool = False,
) -> Quote:
    """async 版 get_quote：真实模式走 real_providers.get_real_quote_async，
    失败回退到同步模拟逻辑（模拟为纯计算，直接复用 get_quote）。"""
    if use_real and _REAL_AVAILABLE and real_providers is not None:
        real_q = await real_providers.get_real_quote_async(
            session, platform, direction, amount_usd, eth_price, gas_gwei)
        if real_q is not None:
            return _build_quote_from_real(
                platform, direction, amount_usd, eth_price, gas_gwei, real_q)
    # 回退到模拟（纯计算，直接调用同步 get_quote）
    return get_quote(platform, direction, amount_usd, eth_price, gas_gwei, use_real=False)


async def get_quotes_matrix_async(
    eth_price: float = DEFAULT_ETH_PRICE_USD,
    gas_gwei: float = DEFAULT_GAS_PRICE_GWEI,
    platforms: Optional[List[str]] = None,
    directions: Optional[List[str]] = None,
    amounts: Optional[List[float]] = None,
    use_real: bool = False,
) -> List[Quote]:
    """async 版全量报价矩阵：用 asyncio.gather 并发采集所有组合。

    真实模式下所有 (platform, direction, amount) 组合并发请求，
    总耗时≈单次最慢 API（~3s）而非 72 次累加（~30s）。
    内部创建并复用同一个 aiohttp.ClientSession 以减少连接开销。
    """
    ps = platforms or PLATFORMS
    ds = directions or DIRECTIONS
    amts = amounts or AMOUNT_TIERS

    if not use_real or not _REAL_AVAILABLE or real_providers is None:
        # 非真实模式：直接走同步（纯计算，async 无收益）
        return get_quotes_matrix(eth_price, gas_gwei, ps, ds, amts, use_real=False)

    # 真实模式：创建共享 session，并发采集
    # trust_env=True 让 aiohttp 读取 HTTP_PROXY/HTTPS_PROXY（沙箱/容器环境必需）
    import aiohttp
    connector = aiohttp.TCPConnector(limit=20, limit_per_host=10)
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout,
                                     trust_env=True) as session:
        tasks = [
            get_quote_async(session, p, d, amt, eth_price, gas_gwei, use_real=True)
            for d in ds for amt in amts for p in ps
        ]
        results = await asyncio.gather(*tasks)
    return list(results)


# ----------------------------------------------------------------------------
# 分析层
# ----------------------------------------------------------------------------

@dataclass
class GroupStat:
    direction: str
    amount_usd: float
    rankings: List[Dict]    # 按 net_received_usd 降序排列的各平台明细
    best_platform: str
    worst_platform: str
    best_quote: Dict
    second_best_quote: Dict
    median_quote: Dict
    worst_quote: Dict
    gap_best_vs_second_bps: float
    gap_best_vs_median_bps: float
    gap_best_vs_worst_bps: float


def _quote_to_dict(q: Quote) -> Dict:
    d = asdict(q)
    return d


def analyze(quotes: List[Quote]) -> Dict:
    """
    对采集到的报价矩阵进行完整分析：
      - 按 (direction, amount) 分组
      - 每组按 net_received_usd 排名
      - 计算最优/次优/中位/最差 + 各类价差（bps）
      - 平台排名分布、成为最优频率、相对最优滑点
      - 聚合器 vs 单 DEX 差异
      - CowSwap 批量拍卖 vs 即时报价差异
    """
    # 分组
    groups: Dict[Tuple[str, float], List[Quote]] = {}
    for q in quotes:
        groups.setdefault((q.direction, q.amount_usd), []).append(q)

    group_stats: List[GroupStat] = []
    for (direction, amount), qs in groups.items():
        # 按 net_received 排序（降序）
        qs_sorted = sorted(qs, key=lambda x: x.net_received_usd, reverse=True)
        best = qs_sorted[0]
        second = qs_sorted[1] if len(qs_sorted) > 1 else qs_sorted[0]
        worst = qs_sorted[-1]
        nets = [x.net_received_usd for x in qs_sorted]
        median_val = statistics.median(nets)
        # 找中位报价对应对象
        median_q = min(qs_sorted, key=lambda x: abs(x.net_received_usd - median_val))

        def bps_gap(better, worse):
            return (better.net_received_usd - worse.net_received_usd) / worse.net_received_usd * 10000

        rankings = []
        for rank, q in enumerate(qs_sorted, start=1):
            rankings.append({
                "rank": rank,
                "platform": q.platform,
                "exec_price": q.exec_price,
                "gross_out_usd": q.gross_out_usd,
                "gas_cost_usd": q.gas_cost_usd,
                "net_received_usd": q.net_received_usd,
                "price_impact_bps": q.price_impact_bps,
                "effective_spread_bps": q.effective_spread_bps,
                "gas_units": q.gas_units,
                "routing_complexity": q.routing_complexity,
                "mev_risk": q.mev_risk,
                "notes": q.notes,
                "gap_vs_best_bps": bps_gap(best, q) if q.platform != best.platform else 0,
            })

        gs = GroupStat(
            direction=direction,
            amount_usd=amount,
            rankings=rankings,
            best_platform=best.platform,
            worst_platform=worst.platform,
            best_quote=_quote_to_dict(best),
            second_best_quote=_quote_to_dict(second),
            median_quote=_quote_to_dict(median_q),
            worst_quote=_quote_to_dict(worst),
            gap_best_vs_second_bps=bps_gap(best, second),
            gap_best_vs_median_bps=bps_gap(best, median_q),
            gap_best_vs_worst_bps=bps_gap(best, worst),
        )
        group_stats.append(gs)

    # 平台排名分布 + 成为最优频率
    rank_distribution: Dict[str, Dict[int, int]] = {p: {1: 0, 2: 0, 3: 0, 4: 0} for p in PLATFORMS}
    best_freq: Dict[str, int] = {p: 0 for p in PLATFORMS}
    for gs in group_stats:
        for r in gs.rankings:
            rank_distribution[r["platform"]][r["rank"]] = rank_distribution[r["platform"]].get(r["rank"], 0) + 1
        best_freq[gs.best_platform] += 1

    total_groups = len(group_stats)
    best_freq_pct = {p: round(v / total_groups * 100, 2) for p, v in best_freq.items()} if total_groups else {}

    # 平台相对最优报价的平均滑点（bps）
    avg_gap_vs_best: Dict[str, float] = {}
    for p in PLATFORMS:
        gaps = []
        for gs in group_stats:
            for r in gs.rankings:
                if r["platform"] == p and r["rank"] != 1:
                    gaps.append(r["gap_vs_best_bps"])
        avg_gap_vs_best[p] = round(statistics.mean(gaps), 2) if gaps else 0.0

    # 聚合器 vs 单 DEX 差异（同金额同方向）
    aggregator_vs_dex = _aggregator_vs_dex_diff(group_stats)

    # CowSwap 批量拍卖 vs 即时报价（Uniswap / 1inch / Tokenlon）差异
    cowswap_vs_instant = _cowswap_vs_instant_diff(group_stats)

    # 价格影响 / gas / 路由 / MEV 对竞争力的影响（结论由 report 层文字化）
    impact_by_amount = _impact_by_amount(group_stats)

    return {
        "group_stats": [asdict(gs) for gs in group_stats],
        "rank_distribution": rank_distribution,
        "best_frequency": best_freq,
        "best_frequency_pct": best_freq_pct,
        "avg_gap_vs_best_bps": avg_gap_vs_best,
        "aggregator_vs_dex": aggregator_vs_dex,
        "cowswap_vs_instant": cowswap_vs_instant,
        "impact_by_amount": impact_by_amount,
        "summary": {
            "platforms": PLATFORMS,
            "directions": DIRECTIONS,
            "amount_tiers": AMOUNT_TIERS,
            "eth_price": DEFAULT_ETH_PRICE_USD,
            "gas_gwei": DEFAULT_GAS_PRICE_GWEI,
        },
    }


def _aggregator_vs_dex_diff(group_stats: List[GroupStat]) -> List[Dict]:
    """
    计算每个分组中聚合器 (1inch, CowSwap) 相对单 DEX (Uniswap) 的净到手差异（bps）。
    """
    out = []
    for gs in group_stats:
        by_p = {r["platform"]: r for r in gs.rankings}
        uni = by_p.get("Uniswap")
        if not uni:
            continue
        for agg in ("1inch", "CowSwap"):
            a = by_p.get(agg)
            if not a:
                continue
            diff_bps = (a["net_received_usd"] - uni["net_received_usd"]) / uni["net_received_usd"] * 10000
            out.append({
                "direction": gs.direction,
                "amount_usd": gs.amount_usd,
                "aggregator": agg,
                "vs_uniswap_bps": round(diff_bps, 2),
                "interpretation": (
                    f"{agg} 比 Uniswap 净到手 {'高' if diff_bps > 0 else '低'} {abs(diff_bps):.2f} bps"
                ),
            })
    return out


def _cowswap_vs_instant_diff(group_stats: List[GroupStat]) -> List[Dict]:
    """
    CowSwap 批量拍卖成交价 vs Uniswap/1inch/Tokenlon 即时报价（净到手 bps）。
    """
    out = []
    for gs in group_stats:
        by_p = {r["platform"]: r for r in gs.rankings}
        cow = by_p.get("CowSwap")
        if not cow:
            continue
        for inst in ("Uniswap", "1inch", "Tokenlon"):
            o = by_p.get(inst)
            if not o:
                continue
            diff_bps = (cow["net_received_usd"] - o["net_received_usd"]) / o["net_received_usd"] * 10000
            out.append({
                "direction": gs.direction,
                "amount_usd": gs.amount_usd,
                "vs": inst,
                "diff_bps": round(diff_bps, 2),
                "note": (
                    f"CowSwap 批量拍卖成交价比 {inst} 即时报价 {'高' if diff_bps > 0 else '低'} "
                    f"{abs(diff_bps):.2f} bps"
                ),
            })
    return out


def _impact_by_amount(group_stats: List[GroupStat]) -> List[Dict]:
    """
    各金额区间下各平台的价格影响（bps），用于评估大单区间的滑点放大。
    """
    out = []
    for gs in group_stats:
        for r in gs.rankings:
            out.append({
                "direction": gs.direction,
                "amount_usd": gs.amount_usd,
                "platform": r["platform"],
                "price_impact_bps": r["price_impact_bps"],
                "effective_spread_bps": r["effective_spread_bps"],
                "gas_cost_usd": r["gas_cost_usd"],
            })
    return out


# ----------------------------------------------------------------------------
# 报告层
# ----------------------------------------------------------------------------

def fmt_usd(v: float) -> str:
    if v >= 1_000_000:
        return f"{v/1_000_000:.3f}M"
    if v >= 1_000:
        return f"{v/1_000:.2f}K"
    return f"{v:.2f}"


def fmt_bps(v: float) -> str:
    return f"{v:+.2f}bps"


def build_report(quotes: List[Quote], analysis: Dict, eth_price: float, gas_gwei: float) -> str:
    """构造文本结构化报告（CLI / 终端可读）。"""
    lines: List[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines.append("=" * 90)
    lines.append("ETH/USDT DEX 报价竞争力监控报告")
    lines.append("=" * 90)
    lines.append(f"快照时间        : {now}")
    lines.append(f"ETH 中价         : {eth_price:.2f} USDT")
    lines.append(f"Gas 价格         : {gas_gwei:.2f} gwei")
    lines.append(f"数据来源        : 模拟报价（基于协议真实参数模型）")
    lines.append(f"监控平台        : {', '.join(PLATFORMS)}")
    lines.append(f"方向            : {', '.join(DIRECTIONS)}")
    lines.append(f"金额区间        : {', '.join(fmt_usd(a) for a in AMOUNT_TIERS)} USD")
    lines.append("")

    # 1) 主表
    lines.append("-" * 90)
    lines.append("【1】报价主表（按方向 + 金额分组，按净到手 USD 排序）")
    lines.append("-" * 90)
    header = f"{'方向':<10}{'金额(USD)':<12}{'平台':<10}{'排名':<6}{'净到手USD':<14}{'可执行价':<14}{'价差vs最优':<14}{'gas(USD)':<10}{'MEV':<10}{'备注'}"
    lines.append(header)
    lines.append("-" * 90)
    for gs in analysis["group_stats"]:
        for r in gs["rankings"]:
            line = (
                f"{gs['direction']:<10}"
                f"{fmt_usd(gs['amount_usd']):<12}"
                f"{r['platform']:<10}"
                f"#{r['rank']:<5}"
                f"{r['net_received_usd']:<14.2f}"
                f"{r['exec_price']:<14.6f}"
                f"{fmt_bps(r['gap_vs_best_bps']):<14}"
                f"{r['gas_cost_usd']:<10.2f}"
                f"{r['mev_risk']:<10}"
                f"{r['notes']}"
            )
            lines.append(line)
        lines.append("")
    lines.append("")

    # 2) 各分组差距汇总
    lines.append("-" * 90)
    lines.append("【2】各金额区间最优/次优/中位/最差差距（bps，净到手口径）")
    lines.append("-" * 90)
    header = f"{'方向':<10}{'金额(USD)':<12}{'最优':<12}{'次优':<12}{'最差':<12}{'最优-次优':<14}{'最优-中位':<14}{'最优-最差':<14}"
    lines.append(header)
    lines.append("-" * 90)
    for gs in analysis["group_stats"]:
        line = (
            f"{gs['direction']:<10}"
            f"{fmt_usd(gs['amount_usd']):<12}"
            f"{gs['best_platform']:<12}"
            f"{gs['second_best_quote']['platform']:<12}"
            f"{gs['worst_platform']:<12}"
            f"{gs['gap_best_vs_second_bps']:<14.2f}"
            f"{gs['gap_best_vs_median_bps']:<14.2f}"
            f"{gs['gap_best_vs_worst_bps']:<14.2f}"
        )
        lines.append(line)
    lines.append("")

    # 3) 平台排名分布 + 最优频率
    lines.append("-" * 90)
    lines.append("【3】平台排名分布与最优报价频率")
    lines.append("-" * 90)
    header = f"{'平台':<12}{'#1':<6}{'#2':<6}{'#3':<6}{'#4':<6}{'最优频率%':<12}{'相对最优均滑点(bps)':<22}"
    lines.append(header)
    lines.append("-" * 90)
    for p in PLATFORMS:
        dist = analysis["rank_distribution"][p]
        freq = analysis["best_frequency_pct"].get(p, 0)
        avg_gap = analysis["avg_gap_vs_best_bps"].get(p, 0)
        line = (
            f"{p:<12}"
            f"{dist.get(1,0):<6}"
            f"{dist.get(2,0):<6}"
            f"{dist.get(3,0):<6}"
            f"{dist.get(4,0):<6}"
            f"{freq:<12}"
            f"{avg_gap:<22}"
        )
        lines.append(line)
    lines.append("")

    # 4) 聚合器 vs 单 DEX
    lines.append("-" * 90)
    lines.append("【4】聚合器 (1inch / CowSwap) vs Uniswap 单 DEX 净到手差异")
    lines.append("-" * 90)
    header = f"{'方向':<10}{'金额(USD)':<12}{'聚合器':<12}{'vs Uniswap(bps)':<20}{'解读'}"
    lines.append(header)
    lines.append("-" * 90)
    for r in analysis["aggregator_vs_dex"]:
        line = (
            f"{r['direction']:<10}"
            f"{fmt_usd(r['amount_usd']):<12}"
            f"{r['aggregator']:<12}"
            f"{r['vs_uniswap_bps']:<+20.2f}"
            f"{r['interpretation']}"
        )
        lines.append(line)
    lines.append("")

    # 5) CowSwap 批量拍卖 vs 即时报价
    lines.append("-" * 90)
    lines.append("【5】CowSwap 批量拍卖成交价 vs 即时报价差异")
    lines.append("-" * 90)
    header = f"{'方向':<10}{'金额(USD)':<12}{'对比':<12}{'差异(bps)':<14}{'说明'}"
    lines.append(header)
    lines.append("-" * 90)
    for r in analysis["cowswap_vs_instant"]:
        line = (
            f"{r['direction']:<10}"
            f"{fmt_usd(r['amount_usd']):<12}"
            f"{r['vs']:<12}"
            f"{r['diff_bps']:<+14.2f}"
            f"{r['note']}"
        )
        lines.append(line)
    lines.append("")

    # 6) 图表建议
    lines.append("-" * 90)
    lines.append("【6】图表建议")
    lines.append("-" * 90)
    charts = [
        "排名热力图        : x=金额区间，y=平台，cell=排名（1 最优），快速看每个区间的最优平台",
        "最优报价频率柱状图 : x=平台，y=成为最优的百分比，识别整体竞争力",
        "价差随金额变化曲线 : x=金额区间（log），y=相对最优的 bps 差距，看大单区间拉大趋势",
        "双向报价对比图     : ETH->USDT 与 USDT->ETH 并排，看做市商对称性",
        "价格影响放大曲线   : x=金额，y=impact(bps)，看流动性深度临界点",
        "gas 占比曲线       : x=金额，y=gas/净到手%，看小额区间 gas 稀释收益",
    ]
    for c in charts:
        lines.append(f"  - {c}")
    lines.append("")

    # 7) 结论
    lines.append("-" * 90)
    lines.append("【7】结论与套利/路由优化机会")
    lines.append("-" * 90)
    conclusions = _build_conclusions(analysis)
    for i, c in enumerate(conclusions, 1):
        lines.append(f"  {i}. {c}")
    lines.append("")

    # 8) 监控指标定义 / 采集频率 / 告警规则
    lines.append("-" * 90)
    lines.append("【8】可复用监控指标 / 采集频率 / 告警规则")
    lines.append("-" * 90)
    metrics = [
        ("net_received_usd", "净到手 USD", "扣 gas 后用户实际到手金额，排名与对比核心口径"),
        ("effective_spread_bps", "有效价差 bps", "fee + impact - surplus，用于横向对比竞争力"),
        ("gap_vs_best_bps", "相对最优价差 bps", "本平台与同分组最优的净到手差距"),
        ("price_impact_bps", "价格影响 bps", "衡量流动性深度，大单区间放大"),
        ("gas_cost_usd", "gas 成本 USD", "小额区间稀释收益"),
        ("best_frequency_pct", "最优频率 %", "周期内成为最优的占比，整体竞争力"),
        ("cowswap_surplus_bps", "CowSwap surplus bps", "批量拍卖相对即时报价的额外回报"),
        ("agg_vs_uni_bps", "聚合器 vs Uniswap bps", "聚合器能否覆盖其更高 gas"),
    ]
    header = f"{'指标 key':<28}{'名称':<22}{'定义'}"
    lines.append(header)
    lines.append("-" * 90)
    for k, name, definition in metrics:
        lines.append(f"{k:<28}{name:<22}{definition}")
    lines.append("")
    lines.append("采集频率建议：")
    lines.append("  - 实时报价快照：每 1 分钟一次（覆盖短时套利窗口）")
    lines.append("  - 中价/gas 基准：每 15 秒一次（影响所有报价基准）")
    lines.append("  - 历史趋势入库：每 5 分钟落库一次，供日报/周报")
    lines.append("  - 大单（≥100K USD）区间：触发即时重采（MEV/深度波动大）")
    lines.append("")
    lines.append("告警规则（建议阈值，可按业务调整）：")
    lines.append("  - 单一平台连续 N 次成为最优（N>=5）→ 通知做市团队检查价差")
    lines.append("  - gap_best_vs_second_bps < 1bps 持续 10 分钟 → 高度竞争，触发套利扫描")
    lines.append("  - gap_best_vs_worst_bps > 50bps → 单一平台深度异常，疑似流动性撤出")
    lines.append("  - CowSwap surplus < 0bps 持续 5 分钟 → solver 不返 surplus，可能市场混乱")
    lines.append("  - 单 DEX 净到手超过所有聚合器 → 聚合器路由失效或 gas 过高，告警")
    lines.append("  - gas_cost_usd / amount_usd > 1%（小额区间）→ 提示用户走聚合器或合并订单")
    lines.append("")
    lines.append("=" * 90)
    return "\n".join(lines)


def _build_conclusions(analysis: Dict) -> List[str]:
    """根据分析结果生成结论性文字。"""
    out: List[str] = []
    # 每个金额区间最有竞争力的平台
    best_by_amount: Dict[Tuple[str, float], str] = {}
    for gs in analysis["group_stats"]:
        best_by_amount[(gs["direction"], gs["amount_usd"])] = gs["best_platform"]

    # 按 direction 分组打印"区间→最优平台"
    for direction in DIRECTIONS:
        items = [(amt, best_by_amount[(direction, amt)]) for (d, amt) in best_by_amount if d == direction]
        items.sort(key=lambda x: x[0])
        seg = "；".join(f"{fmt_usd(amt)}→{plat}" for amt, plat in items)
        out.append(f"[{direction}] 各金额区间最优平台：{seg}")

    # 整体最优频率
    freq = analysis["best_frequency_pct"]
    best_platform_overall = max(freq, key=freq.get) if freq else "N/A"
    out.append(
        f"整体最优频率最高平台：{best_platform_overall} "
        f"({freq.get(best_platform_overall, 0)}%)，建议作为默认路由"
    )

    # 套利机会：最优-次优 < 1bps 的分组
    tight = [
        (gs["direction"], gs["amount_usd"], gs["gap_best_vs_second_bps"])
        for gs in analysis["group_stats"]
        if gs["gap_best_vs_second_bps"] < 1.0
    ]
    if tight:
        seg = "；".join(f"{d}@{fmt_usd(a)}={g:.2f}bps" for d, a, g in tight)
        out.append(f"高度竞争区间（最优-次优<1bps，路由切换敏感）：{seg}")
    else:
        out.append("当前无最优-次优<1bps 的高度竞争区间")

    # 套利/路由机会：最优-最差 > 30bps
    arb = [
        (gs["direction"], gs["amount_usd"], gs["gap_best_vs_worst_bps"],
         gs["best_platform"], gs["worst_platform"])
        for gs in analysis["group_stats"]
        if gs["gap_best_vs_worst_bps"] > 30
    ]
    if arb:
        seg = "；".join(
            f"{d}@{fmt_usd(a)}:{best}比{worst}好{g:.1f}bps"
            for d, a, g, best, worst in arb
        )
        out.append(f"价差套利/路由优化机会（最优-最差>30bps）：{seg}")

    # 大单区间流动性压力
    big = [gs for gs in analysis["group_stats"] if gs["amount_usd"] >= 1_000_000]
    if big:
        # 找大单区间价格影响最大的平台
        worst_impact = max(
            (r for gs in big for r in gs["rankings"]),
            key=lambda r: r["price_impact_bps"],
        )
        out.append(
            f"大单区间(≥1M USD)价格影响最显著：{worst_impact['platform']} "
            f"({worst_impact['price_impact_bps']:.1f}bps)，"
            f"建议走 RFQ/批量拍卖路径降低滑点"
        )

    # 小额区间 gas 稀释
    small = [gs for gs in analysis["group_stats"] if gs["amount_usd"] <= 5_000]
    if small:
        worst_gas = max(
            (r for gs in small for r in gs["rankings"]),
            key=lambda r: r["gas_cost_usd"] / max(r["net_received_usd"], 1) * 100,
        )
        pct = worst_gas["gas_cost_usd"] / max(worst_gas["net_received_usd"], 1) * 100
        out.append(
            f"小额区间(≤5K USD) gas 占比最高：{worst_gas['platform']} "
            f"(gas 占净到手 {pct:.3f}%)，建议合并订单或走聚合器"
        )

    return out


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def cli_main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="ETH/USDT DEX 报价监控与分析工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--report", action="store_true", help="输出结构化文本报告")
    parser.add_argument("--json", action="store_true", help="输出 JSON（供前端/下游消费）")
    parser.add_argument("--amount", type=float, help="只看某个金额区间 USD（默认全量）")
    parser.add_argument("--direction", choices=DIRECTIONS, help="只看某个方向（默认双向）")
    parser.add_argument("--platform", choices=PLATFORMS, action="append", help="只看某些平台（可多次）")
    parser.add_argument("--eth-price", type=float, default=DEFAULT_ETH_PRICE_USD, help="ETH 中价 USD")
    parser.add_argument("--gas-gwei", type=float, default=DEFAULT_GAS_PRICE_GWEI, help="Gas 价格 gwei")
    parser.add_argument("--real", action="store_true", help="启用真实 API 报价 (CowSwap/ParaSwap/Uniswap 链上)")
    args = parser.parse_args(argv)

    # 默认行为：无 flag 时输出 report
    if not args.report and not args.json:
        args.report = True

    amounts = [args.amount] if args.amount else AMOUNT_TIERS
    directions = [args.direction] if args.direction else DIRECTIONS
    platforms = args.platform or PLATFORMS

    # 真实模式：自动从链上获取 ETH 中价（除非用户显式指定）
    eth_price = args.eth_price
    data_source_label = "simulated_quote (基于协议真实参数模型)"
    if args.real:
        if _REAL_AVAILABLE and real_providers is not None:
            # 尝试从链上获取 ETH 中价
            mp = real_providers.get_eth_usd_midprice()
            if mp and abs(mp - DEFAULT_ETH_PRICE_USD) > 1:
                eth_price = mp
            data_source_label = "real_api (CowSwap solver + ParaSwap + Uniswap V3 pool slot0; Tokenlon 估算)"
        else:
            print("[警告] real_providers 模块不可用，回退到模拟模式")

    quotes = get_quotes_matrix(
        eth_price=eth_price,
        gas_gwei=args.gas_gwei,
        platforms=platforms,
        directions=directions,
        amounts=amounts,
        use_real=args.real,
    )
    analysis = analyze(quotes)

    if args.json:
        payload = {
            "snapshot_time": datetime.now(timezone.utc).isoformat(),
            "eth_price": eth_price,
            "gas_gwei": args.gas_gwei,
            "data_source": data_source_label,
            "real_mode": args.real,
            "platforms": platforms,
            "directions": directions,
            "amount_tiers": amounts,
            "quotes": [asdict(q) for q in quotes],
            "analysis": analysis,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(build_report(quotes, analysis, eth_price, args.gas_gwei))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
