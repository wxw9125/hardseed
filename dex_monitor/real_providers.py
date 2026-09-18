#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真实链上/REST API 报价提供者
================================

为 dex_quote_monitor 提供真实报价数据，无需 API Key：
  - CowSwap  : https://api.cow.fi/mainnet/api/v1/quote  (批量拍卖 solver 报价)
  - ParaSwap : https://apiv5.paraswap.io/prices          (聚合器最优路径)
  - Uniswap  : Quoter V2 合约 via 公共 RPC                (链上模拟报价)

设计原则
--------
- 仅使用 Python 标准库 (urllib/json)
- 任一提供者失败自动降级返回 None，调用方回退到模拟报价
- 统一返回 (gross_out_usd, gas_units, notes) 三元组，由调用方组装 Quote

ETH 主网常量
------------
WETH   : 0xC02aaA39b223FE8D0A0e5C4F27eAD9083c756Cc2
USDT   : 0xdAC17F958D2ee523a2206206994597C13D831ec7
USDC   : 0xA0b86991c6218b36c1D19D4a2e9Eb0cE3606eB48  (ParaSwap 中转用)
Quoter : 0x61fF0ea22d4bE8a3F6B2eCa31B86Fe7bC6Fb1b00  (Uniswap V3 QuoterV2)
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from typing import Optional, Tuple, Dict

# 以太坊主网代币地址
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083c756Cc2"
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
USDC = "0xA0b86991c6218b36c1D19D4a2e9Eb0cE3606eB48"

# Uniswap V3 QuoterV2 合约（直接 eth_call 会 revert，改用池子 slot0）
UNISWAP_QUOTER_V2 = "0x61fF0ea22d4bE8a3F6B2eCa31B86Fe7bC6Fb1b00"

# Uniswap V3 ETH/USDT 0.05% 池子（token0=WETH, token1=USDT）
UNISWAP_POOL_ETH_USDT_005 = "0x4e68Ccd3E89f51C3074ca5072bbAC773960dFa36"
# Uniswap V3 ETH/USDT 0.3% 池子
UNISWAP_POOL_ETH_USDT_030 = "0x11b815aFB9dca7e0Ea9FC5a4c90B82454374E6D0"

# 公共 RPC（无需 API Key，按可用性排序）
PUBLIC_RPCS = [
    "https://ethereum-rpc.publicnode.com",
    "https://eth.drpc.org",
    "https://eth.merkle.io",
    "https://1rpc.io/eth",
]

# CowSwap API
COWSWAP_API = "https://api.cow.fi/mainnet/api/v1/quote"

# ParaSwap API
PARASWAP_API = "https://apiv5.paraswap.io/prices"

# 哨兵地址（CowSwap 需要 from 字段但不能用真实地址）
COWSWAP_FROM = "0x0000000000000000000000000000000000000001"

# 各 API 超时（秒）
TIMEOUT = 12


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------

def _http_get(url: str, headers: Optional[Dict] = None) -> Optional[dict]:
    """GET 请求，返回 JSON dict；失败返回 None"""
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"_error": str(e)}


def _http_post(url: str, body: dict, headers: Optional[Dict] = None) -> Optional[dict]:
    """POST 请求，返回 JSON dict；失败返回 None"""
    try:
        data = json.dumps(body).encode("utf-8")
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, data=data, headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"_error": str(e)}


def _rpc_call(rpc_url: str, method: str, params: list) -> Optional[dict]:
    """JSON-RPC 调用"""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return _http_post(rpc_url, body)


# ----------------------------------------------------------------------------
# ETH 中价获取（从 Uniswap V3 池子读最简价）
# ----------------------------------------------------------------------------

def _read_pool_slot0(pool_addr: str, max_attempts: int = 2) -> Optional[Tuple[int, int]]:
    """
    读取 Uniswap V3 池子的 slot0() 与 liquidity()。
    返回 (sqrtPriceX96, liquidity)；失败返回 None。
    slot0() selector: 0x3850c7bd
    liquidity() selector: 0x1a686502

    max_attempts: 对每个 RPC 重试次数（公共 RPC 偶发抖动，重试可显著提升成功率）。
    """
    for attempt in range(max_attempts):
        for rpc in PUBLIC_RPCS:
            # slot0()
            r1 = _rpc_call(rpc, "eth_call", [
                {"to": pool_addr, "data": "0x3850c7bd"},
                "latest"
            ])
            if not r1 or "result" not in r1 or r1["result"] in ("0x", None, ""):
                continue
            hex1 = r1["result"][2:]
            if len(hex1) < 128:
                continue
            sqrt_price_x96 = int(hex1[:64], 16)

            # liquidity()
            r2 = _rpc_call(rpc, "eth_call", [
                {"to": pool_addr, "data": "0x1a686502"},
                "latest"
            ])
            if not r2 or "result" not in r2 or r2["result"] in ("0x", None, ""):
                continue
            liquidity = int(r2["result"][2:].rjust(64, "0")[:64], 16)

            return (sqrt_price_x96, liquidity)
        # 一轮全失败 -> 短暂等待后重试
        if attempt < max_attempts - 1:
            time.sleep(0.4)
    return None


# ETH 中价短时缓存（公共 RPC 偶发抖动时回退到最近一次成功值）
_ETH_MIDPRICE_CACHE: Dict = {"value": None, "ts": 0.0}
_MIDPRICE_TTL = 300.0  # 缓存有效期（秒）— ETH 中价 5 分钟内变化有限，作为兜底参考足够


def get_eth_usd_midprice() -> Optional[float]:
    """
    从 Uniswap V3 ETH/USDT 0.05% 池子读取 slot0() 计算 ETH 中价。
    Price = (sqrtPriceX96 / 2^96)^2  （token0=WETH, token1=USDT 时为 USDT per ETH）

    带短时缓存（5 分钟）：RPC 抖动失败时回退到最近一次成功值，避免中价跳回默认 3000。
    """
    res = _read_pool_slot0(UNISWAP_POOL_ETH_USDT_005)
    price_usd_per_eth = None
    if res is not None:
        sqrt_price_x96, _ = res
        if sqrt_price_x96:
            price = (sqrt_price_x96 / (2**96)) ** 2
            price_usd_per_eth = price * 10**(18 - 6)  # decimals diff
            if price_usd_per_eth and price_usd_per_eth > 0:
                _ETH_MIDPRICE_CACHE["value"] = price_usd_per_eth
                _ETH_MIDPRICE_CACHE["ts"] = time.time()
                return price_usd_per_eth
    # 实时获取失败 -> 尝试用未过期缓存
    cached = _ETH_MIDPRICE_CACHE.get("value")
    age = time.time() - _ETH_MIDPRICE_CACHE.get("ts", 0)
    if cached and age < _MIDPRICE_TTL:
        return cached
    return price_usd_per_eth  # None or stale


# ----------------------------------------------------------------------------
# Uniswap V3 真实报价（基于池子 slot0 + liquidity 估算）
# ----------------------------------------------------------------------------

def uniswap_quote(direction: str, amount_usd: float, eth_price: float) -> Optional[Dict]:
    """
    从 Uniswap V3 ETH/USDT 池读取真实中价与流动性，结合交易量估算报价。
    对小单接近真实成交价；对大单使用简化价格影响模型。
    direction: 'ETH->USDT' or 'USDT->ETH'

    说明：V3 的 L（流动性）需结合 tick bitmap 才能精确换算可成交深度，
    这里采用与模拟模型一致的有效深度假设（0.05% 池 ≈$50M、0.3% 池 ≈$30M），
    仅中价来自链上 slot0，保证报价基准真实。
    """
    # 读 0.05% 池（主池）；失败回退 0.3% 池
    pool_addr = UNISWAP_POOL_ETH_USDT_005
    fee_bps = 5
    liquidity_usd = 50_000_000
    res = _read_pool_slot0(pool_addr)
    if res is None:
        pool_addr = UNISWAP_POOL_ETH_USDT_030
        fee_bps = 30
        liquidity_usd = 30_000_000
        res = _read_pool_slot0(pool_addr)

    # 真实中价 USDT per ETH（slot0 直接计算；RPC 抖动时用短时缓存回退）
    mid_price_usdt_per_eth = None
    if res is not None:
        sqrt_price_x96, _liquidity = res
        if sqrt_price_x96:
            mid_price_usdt_per_eth = (sqrt_price_x96 / (2**96)) ** 2 * 10**(18 - 6)
            # 同步更新短时缓存，供 get_eth_usd_midprice 复用
            if mid_price_usdt_per_eth > 0:
                _ETH_MIDPRICE_CACHE["value"] = mid_price_usdt_per_eth
                _ETH_MIDPRICE_CACHE["ts"] = time.time()
    if not (mid_price_usdt_per_eth and mid_price_usdt_per_eth > 0):
        # 回退到带缓存的 get_eth_usd_midprice（与 0.05% 池同源）
        cached = get_eth_usd_midprice()
        if not (cached and cached > 0):
            return None
        mid_price_usdt_per_eth = cached

    if direction == "ETH->USDT":
        eth_in = amount_usd / mid_price_usdt_per_eth
        # 价格影响（基于有效深度，与模拟模型一致）
        impact_ratio = amount_usd / (liquidity_usd + amount_usd)
        impact_bps = impact_ratio * 10000
        # 毛到手 = amount * (1 - fee - impact)
        net_factor = max(0.5, 1 - fee_bps / 10000 - impact_ratio)
        gross_out_usd = amount_usd * net_factor
        exec_price = gross_out_usd / eth_in if eth_in > 0 else 0
        notes = (f"Uniswap V3 fee=0.{fee_bps*10}% 池 | 链上 slot0 中价 "
                 f"${mid_price_usdt_per_eth:.2f} | 有效深度≈${liquidity_usd/1e6:.0f}M")
    else:  # USDT->ETH
        usdt_in = amount_usd
        impact_ratio = usdt_in / (liquidity_usd + usdt_in)
        impact_bps = impact_ratio * 10000
        net_factor = max(0.5, 1 - fee_bps / 10000 - impact_ratio)
        gross_out_usd = amount_usd * net_factor
        eth_out = gross_out_usd / mid_price_usdt_per_eth
        exec_price = eth_out / usdt_in if usdt_in > 0 else 0
        notes = (f"Uniswap V3 fee=0.{fee_bps*10}% 池 | 链上 slot0 中价 "
                 f"${mid_price_usdt_per_eth:.2f} | 有效深度≈${liquidity_usd/1e6:.0f}M")

    return {
        "gross_out_usd": gross_out_usd,
        "gas_units": 180000,
        "exec_price": exec_price,
        "price_impact_bps": impact_bps,
        "fee_bps": float(fee_bps),
        "notes": notes,
        "data_source": "uniswap_v3_pool_slot0_midprice + depth_model",
    }


# ----------------------------------------------------------------------------
# CowSwap 真实报价
# ----------------------------------------------------------------------------

def cowswap_quote(direction: str, amount_usd: float, eth_price: float) -> Optional[Dict]:
    """
    调用 CowSwap API 获取真实批量拍卖报价。
    使用 WETH（ETH 入口用 WETH 地址，CowSwap 不支持原生 ETH）
    """
    if direction == "ETH->USDT":
        sell_token = WETH
        buy_token = USDT
        eth_in = amount_usd / eth_price
        sell_amount = int(eth_in * 10**18)
        sell_decimals = 18
        buy_decimals = 6
    else:  # USDT->ETH
        sell_token = USDT
        buy_token = WETH
        sell_amount = int(amount_usd * 10**6)
        sell_decimals = 6
        buy_decimals = 18

    body = {
        "from": COWSWAP_FROM,
        "sellToken": sell_token,
        "buyToken": buy_token,
        "kind": "sell",
        "sellAmountBeforeFee": str(sell_amount),
    }

    resp = _http_post(COWSWAP_API, body)
    if not resp or "_error" in resp or "quote" not in resp:
        return None

    quote = resp["quote"]
    buy_amount = int(quote.get("buyAmount", "0"))
    gas_amount = int(quote.get("gasAmount", "320000"))
    fee_amount = int(quote.get("feeAmount", "0"))

    if buy_amount == 0:
        return None

    # 实际卖出量 = sellAmountBeforeFee - feeAmount
    actual_sell = sell_amount - fee_amount

    # 计算毛到手（USD）
    if direction == "ETH->USDT":
        gross_out_usd = buy_amount / 10**buy_decimals  # USDT 直接是 USD
        # 可执行价：USDT per ETH（基于实际卖出 ETH）
        actual_sell_eth = actual_sell / 10**sell_decimals
        exec_price = gross_out_usd / actual_sell_eth if actual_sell_eth > 0 else 0
        ref_price = eth_price
        price_impact_bps = max(0, (ref_price - exec_price) / ref_price * 10000) if ref_price > 0 else 0
        notes = f"CowSwap 批量拍卖 | solver 报价 | fee={fee_amount/10**18:.6f} ETH | {gross_out_usd:.2f} USDT"
    else:  # USDT->ETH
        eth_out = buy_amount / 10**buy_decimals
        gross_out_usd = eth_out * eth_price
        exec_price = eth_out / amount_usd  # ETH per USDT
        ref_price = 1.0 / eth_price
        price_impact_bps = max(0, (ref_price - exec_price) / ref_price * 10000) if ref_price > 0 else 0
        notes = f"CowSwap 批量拍卖 | solver 报价 | {eth_out:.6f} ETH"

    return {
        "gross_out_usd": gross_out_usd,
        "gas_units": gas_amount,
        "exec_price": exec_price,
        "price_impact_bps": price_impact_bps,
        "fee_bps": fee_amount / actual_sell * 10000 if actual_sell > 0 else 0,
        "notes": notes,
        "data_source": "cowswap_solver_api",
    }


# ----------------------------------------------------------------------------
# ParaSwap 真实报价（聚合器，作为 1inch 的免费替代）
# ----------------------------------------------------------------------------

def paraswap_quote(direction: str, amount_usd: float, eth_price: float) -> Optional[Dict]:
    """
    调用 ParaSwap API 获取聚合器最优路径报价。
    注：ParaSwap 在平台映射中替代 1inch（1inch 需要 API Key）。
    """
    if direction == "ETH->USDT":
        src_token = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"  # ParaSwap 用 0xe...e 代表 ETH
        dest_token = USDT
        amount = int((amount_usd / eth_price) * 10**18)
        src_decimals = 18
        dest_decimals = 6
    else:
        src_token = USDT
        dest_token = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        amount = int(amount_usd * 10**6)
        src_decimals = 6
        dest_decimals = 18

    params = {
        "srcToken": src_token.lower(),
        "destToken": dest_token.lower(),
        "amount": str(amount),
        "side": "SELL",
        "network": "1",
    }
    url = PARASWAP_API + "?" + urllib.parse.urlencode(params)
    resp = _http_get(url)
    if not resp or "_error" in resp or "priceRoute" not in resp:
        return None

    pr = resp["priceRoute"]
    dest_amount = int(pr.get("destAmount", "0"))
    if dest_amount == 0:
        return None

    # 从 bestRoute 提取 gas 估算与路由复杂度
    best_route = pr.get("bestRoute", [])
    gas_estimate = 300000  # 默认
    routing_complexity = max(1, len(best_route))
    exchanges_used = set()
    for leg in best_route:
        for swap in leg.get("swaps", []):
            for ex in swap.get("swapExchanges", []):
                exchanges_used.add(ex.get("exchange", ""))

    if direction == "ETH->USDT":
        gross_out_usd = dest_amount / 10**dest_decimals  # USDT
        eth_in = amount / 10**src_decimals
        exec_price = gross_out_usd / eth_in if eth_in > 0 else 0
        price_impact_bps = max(0, (eth_price - exec_price) / eth_price * 10000) if eth_price > 0 else 0
        notes = f"ParaSwap 聚合 | exchanges={','.join(sorted(exchanges_used))[:60]} | {gross_out_usd:.2f} USDT"
    else:
        eth_out = dest_amount / 10**dest_decimals
        gross_out_usd = eth_out * eth_price
        exec_price = eth_out / amount_usd
        ref_price = 1.0 / eth_price
        price_impact_bps = max(0, (ref_price - exec_price) / ref_price * 10000) if ref_price > 0 else 0
        notes = f"ParaSwap 聚合 | exchanges={','.join(sorted(exchanges_used))[:60]} | {eth_out:.6f} ETH"

    return {
        "gross_out_usd": gross_out_usd,
        "gas_units": gas_estimate,
        "exec_price": exec_price,
        "price_impact_bps": price_impact_bps,
        "fee_bps": 0,
        "routing_complexity": routing_complexity,
        "notes": notes,
        "data_source": "paraswap_api_v5",
    }


# ----------------------------------------------------------------------------
# Tokenlon 估算模型（基于官方费率表 + PMM 架构，无公开 API）
# 数据来源：https://support.tokenlon.im/hc/en-us/articles/360037260272-Fees
# ----------------------------------------------------------------------------

# 官方费率档位（10 档），基于 30 天累计交易量 OR LON 持仓量取更优档
# 此处以单笔金额作为 30 天量的代理（监控用途，展示不同规模用户的费率）
_TOKENLON_FEE_TIERS = [
    # (min_amount_usd, fee_bps, level)
    (0,          30, 0),    # 0.30%
    (30_000,     29, 1),    # 0.29%
    (50_000,     28, 2),    # 0.28%
    (100_000,    26, 3),    # 0.26%
    (200_000,    24, 4),    # 0.24%
    (500_000,    22, 5),    # 0.22%
    (1_000_000,  20, 6),    # 0.20%
    (2_000_000,  18, 7),    # 0.18%
    (5_000_000,  15, 8),    # 0.15%
    (10_000_000, 10, 9),    # 0.10%
]


def _tokenlon_fee_bps(amount_usd: float) -> tuple:
    """根据金额映射到官方费率档位，返回 (fee_bps, level)"""
    fee, level = 30, 0
    for min_amt, bps, lv in _TOKENLON_FEE_TIERS:
        if amount_usd >= min_amt:
            fee, level = bps, lv
    return fee, level


def tokenlon_quote(direction: str, amount_usd: float, eth_price: float, gas_gwei: float) -> Optional[Dict]:
    """
    Tokenlon RFQ 估算模型 v2（基于官方费率表 + PMM 架构）。
    无公开 REST API，需 RFQ WebSocket 接入；此处按官方公开费率规则建模。

    官方 Gas 规则（Scenario 2，Tokenlon 代付 gas）：
      - ETH→Token：用户自付 gas，交易费全额收取
      - Token→Token：trade_fee > gas → 实际费=trade_fee（gas 含在内）
                      trade_fee < gas → 不收交易费，只扣 gas
    """
    import math

    base_fee_bps, level = _tokenlon_fee_bps(amount_usd)
    gas_units = 200_000
    gas_cost_eth = gas_units * gas_gwei * 1e-9
    gas_cost_usd = gas_cost_eth * eth_price
    trade_fee_usd = amount_usd * base_fee_bps / 10000

    if direction == "ETH->USDT":
        # 卖 ETH：用户自付 gas（官方 Scenario 1），交易费全额收取
        gross_out_usd = amount_usd - trade_fee_usd
        effective_fee_bps = base_fee_bps
        # gas_units 保持 200k，外层 net_received 会扣 gas
    else:
        # USDT->ETH：Tokenlon 代付 gas（官方 Scenario 2）
        if trade_fee_usd >= gas_cost_usd:
            # 交易费覆盖 gas：用户净成本 = 交易费，gas 不额外扣
            gross_out_usd = amount_usd - trade_fee_usd
            effective_fee_bps = base_fee_bps
            gas_units = 0  # gas 已含在交易费内
        else:
            # gas > 交易费：不收交易费，只扣 gas 成本
            gross_out_usd = amount_usd - gas_cost_usd
            effective_fee_bps = gas_cost_usd / amount_usd * 10000 if amount_usd > 0 else 0
            gas_units = 0  # gas 已在 gross_out 扣除

    # RFQ 价格影响：做市商链下报价，小单几乎无影响
    # 大单做市商需对冲，影响随金额平方根增长（比 AMM 线性温和）
    # 参考：$1k≈2bps, $50k≈4.5bps, $1M≈20bps, $10M≈40bps(封顶)
    impact_bps = min(40, 2.0 * math.sqrt(amount_usd / 10_000))
    gross_out_usd *= (1 - impact_bps / 10000)

    # 计算 exec_price
    if direction == "ETH->USDT":
        eth_in = amount_usd / eth_price
        exec_price = gross_out_usd / eth_in if eth_in > 0 else 0
        ref_price = eth_price
    else:
        eth_out = gross_out_usd / eth_price
        exec_price = eth_out / amount_usd if amount_usd > 0 else 0
        ref_price = 1.0 / eth_price

    price_impact_bps = max(0, (ref_price - exec_price) / ref_price * 10000) if ref_price > 0 else 0
    return {
        "gross_out_usd": gross_out_usd,
        "gas_units": gas_units,
        "exec_price": exec_price,
        "price_impact_bps": price_impact_bps,
        "fee_bps": effective_fee_bps,
        "notes": f"Tokenlon RFQ 估算（官方费率 L{level}={base_fee_bps}bps，无公开 API）",
        "data_source": "tokenlon_estimated_model_v2",
    }


# ----------------------------------------------------------------------------
# 统一调度入口
# ----------------------------------------------------------------------------

# 平台 -> 提供者函数映射
# 注：1inch 需 API Key，免费方案中以 ParaSwap 替代聚合器槽位
PLATFORM_PROVIDERS = {
    "Uniswap": uniswap_quote,
    "1inch": paraswap_quote,  # 用 ParaSwap 作为聚合器（1inch 需 key）
    "CowSwap": cowswap_quote,
    "Tokenlon": tokenlon_quote,
}


def get_real_quote(platform: str, direction: str, amount_usd: float,
                   eth_price: float, gas_gwei: float) -> Optional[Dict]:
    """
    统一获取真实/混合报价。
    返回 dict (含 gross_out_usd, gas_units, exec_price, price_impact_bps, fee_bps, notes, data_source)
    失败返回 None（调用方应回退到模拟模型）。
    """
    provider = PLATFORM_PROVIDERS.get(platform)
    if provider is None:
        return None
    try:
        if platform == "Tokenlon":
            return provider(direction, amount_usd, eth_price, gas_gwei)
        return provider(direction, amount_usd, eth_price)
    except Exception:
        return None


def health_check() -> Dict[str, str]:
    """快速健康检查各提供者可用性"""
    status = {}
    # CowSwap
    try:
        r = cowswap_quote("ETH->USDT", 1000, 3000)
        status["CowSwap"] = "OK" if r else "FAIL"
    except Exception:
        status["CowSwap"] = "ERROR"
    # ParaSwap
    try:
        r = paraswap_quote("ETH->USDT", 1000, 3000)
        status["1inch(ParaSwap)"] = "OK" if r else "FAIL"
    except Exception:
        status["1inch(ParaSwap)"] = "ERROR"
    # Uniswap
    try:
        r = uniswap_quote("ETH->USDT", 1000, 3000)
        status["Uniswap"] = "OK" if r else "FAIL"
    except Exception:
        status["Uniswap"] = "ERROR"
    status["Tokenlon"] = "ESTIMATED (无公开 API)"
    return status


# ============================================================================
# Async 版本（aiohttp + asyncio.gather 并发，供 web/批量场景使用）
# 同步函数保持不变，async 版本共享中价缓存 _ETH_MIDPRICE_CACHE
# ============================================================================

try:
    import aiohttp
    _AIOHTTP_AVAILABLE = True
except ImportError:
    _AIOHTTP_AVAILABLE = False


async def _aio_get(session: "aiohttp.ClientSession", url: str,
                   headers: Optional[Dict] = None) -> Optional[dict]:
    """async GET，返回 JSON dict；失败返回 {"_error": ...}"""
    try:
        async with session.get(url, headers=headers or {},
                               timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            return await resp.json(content_type=None)
    except Exception as e:
        return {"_error": str(e)}


async def _aio_post(session: "aiohttp.ClientSession", url: str, body: dict,
                    headers: Optional[Dict] = None) -> Optional[dict]:
    """async POST，返回 JSON dict；失败返回 {"_error": ...}"""
    try:
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        async with session.post(url, json=body, headers=h,
                                timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            return await resp.json(content_type=None)
    except Exception as e:
        return {"_error": str(e)}


async def _aio_rpc_call(session: "aiohttp.ClientSession", rpc_url: str,
                        method: str, params: list) -> Optional[dict]:
    """async JSON-RPC 调用"""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return await _aio_post(session, rpc_url, body)


async def _aio_read_pool_slot0(session: "aiohttp.ClientSession", pool_addr: str,
                               max_attempts: int = 2) -> Optional[Tuple[int, int]]:
    """async 读取 Uniswap V3 池 slot0() + liquidity()，逻辑同同步版本"""
    for attempt in range(max_attempts):
        for rpc in PUBLIC_RPCS:
            r1 = await _aio_rpc_call(session, rpc, "eth_call",
                                     [{"to": pool_addr, "data": "0x3850c7bd"}, "latest"])
            if not r1 or "result" not in r1 or r1["result"] in ("0x", None, ""):
                continue
            hex1 = r1["result"][2:]
            if len(hex1) < 128:
                continue
            sqrt_price_x96 = int(hex1[:64], 16)
            r2 = await _aio_rpc_call(session, rpc, "eth_call",
                                     [{"to": pool_addr, "data": "0x1a686502"}, "latest"])
            if not r2 or "result" not in r2 or r2["result"] in ("0x", None, ""):
                continue
            liquidity = int(r2["result"][2:].rjust(64, "0")[:64], 16)
            return (sqrt_price_x96, liquidity)
        if attempt < max_attempts - 1:
            await asyncio.sleep(0.4)
    return None


async def get_eth_usd_midprice_async(session: "aiohttp.ClientSession") -> Optional[float]:
    """async 版获取 ETH 中价，带 5 分钟缓存（与同步版本共享缓存）"""
    res = await _aio_read_pool_slot0(session, UNISWAP_POOL_ETH_USDT_005)
    if res is not None:
        sqrt_price_x96, _ = res
        if sqrt_price_x96:
            price = (sqrt_price_x96 / (2**96)) ** 2
            price_usd_per_eth = price * 10 ** (18 - 6)
            if price_usd_per_eth and price_usd_per_eth > 0:
                _ETH_MIDPRICE_CACHE["value"] = price_usd_per_eth
                _ETH_MIDPRICE_CACHE["ts"] = time.time()
                return price_usd_per_eth
    cached = _ETH_MIDPRICE_CACHE.get("value")
    age = time.time() - _ETH_MIDPRICE_CACHE.get("ts", 0)
    if cached and age < _MIDPRICE_TTL:
        return cached
    return None


async def uniswap_quote_async(session, direction, amount_usd, eth_price):
    """async 版 Uniswap V3 报价，逻辑同 uniswap_quote()"""
    pool_addr = UNISWAP_POOL_ETH_USDT_005
    fee_bps = 5
    liquidity_usd = 50_000_000
    res = await _aio_read_pool_slot0(session, pool_addr)
    if res is None:
        pool_addr = UNISWAP_POOL_ETH_USDT_030
        fee_bps = 30
        liquidity_usd = 30_000_000
        res = await _aio_read_pool_slot0(session, pool_addr)

    mid_price_usdt_per_eth = None
    if res is not None:
        sqrt_price_x96, _liquidity = res
        if sqrt_price_x96:
            mid_price_usdt_per_eth = (sqrt_price_x96 / (2**96)) ** 2 * 10 ** (18 - 6)
            if mid_price_usdt_per_eth > 0:
                _ETH_MIDPRICE_CACHE["value"] = mid_price_usdt_per_eth
                _ETH_MIDPRICE_CACHE["ts"] = time.time()
    if not (mid_price_usdt_per_eth and mid_price_usdt_per_eth > 0):
        cached = _ETH_MIDPRICE_CACHE.get("value")
        age = time.time() - _ETH_MIDPRICE_CACHE.get("ts", 0)
        if cached and age < _MIDPRICE_TTL:
            mid_price_usdt_per_eth = cached
        else:
            return None

    if direction == "ETH->USDT":
        eth_in = amount_usd / mid_price_usdt_per_eth
        impact_ratio = amount_usd / (liquidity_usd + amount_usd)
        impact_bps = impact_ratio * 10000
        net_factor = max(0.5, 1 - fee_bps / 10000 - impact_ratio)
        gross_out_usd = amount_usd * net_factor
        exec_price = gross_out_usd / eth_in if eth_in > 0 else 0
        notes = (f"Uniswap V3 fee=0.{fee_bps*10}% 池 | 链上 slot0 中价 "
                 f"${mid_price_usdt_per_eth:.2f} | 有效深度≈${liquidity_usd/1e6:.0f}M")
    else:
        usdt_in = amount_usd
        impact_ratio = usdt_in / (liquidity_usd + usdt_in)
        impact_bps = impact_ratio * 10000
        net_factor = max(0.5, 1 - fee_bps / 10000 - impact_ratio)
        gross_out_usd = amount_usd * net_factor
        eth_out = gross_out_usd / mid_price_usdt_per_eth
        exec_price = eth_out / usdt_in if usdt_in > 0 else 0
        notes = (f"Uniswap V3 fee=0.{fee_bps*10}% 池 | 链上 slot0 中价 "
                 f"${mid_price_usdt_per_eth:.2f} | 有效深度≈${liquidity_usd/1e6:.0f}M")

    return {
        "gross_out_usd": gross_out_usd, "gas_units": 180000,
        "exec_price": exec_price, "price_impact_bps": impact_bps,
        "fee_bps": float(fee_bps), "notes": notes,
        "data_source": "uniswap_v3_pool_slot0_midprice + depth_model",
    }


async def cowswap_quote_async(session, direction, amount_usd, eth_price):
    """async 版 CowSwap 报价，逻辑同 cowswap_quote()"""
    if direction == "ETH->USDT":
        sell_token, buy_token = WETH, USDT
        eth_in = amount_usd / eth_price
        sell_amount = int(eth_in * 10**18)
        sell_decimals, buy_decimals = 18, 6
    else:
        sell_token, buy_token = USDT, WETH
        sell_amount = int(amount_usd * 10**6)
        sell_decimals, buy_decimals = 6, 18

    body = {
        "from": COWSWAP_FROM, "sellToken": sell_token, "buyToken": buy_token,
        "kind": "sell", "sellAmountBeforeFee": str(sell_amount),
    }
    resp = await _aio_post(session, COWSWAP_API, body)
    if not resp or "_error" in resp or "quote" not in resp:
        return None

    quote = resp["quote"]
    buy_amount = int(quote.get("buyAmount", "0"))
    gas_amount = int(quote.get("gasAmount", "320000"))
    fee_amount = int(quote.get("feeAmount", "0"))
    if buy_amount == 0:
        return None

    actual_sell = sell_amount - fee_amount
    if direction == "ETH->USDT":
        gross_out_usd = buy_amount / 10**buy_decimals
        actual_sell_eth = actual_sell / 10**sell_decimals
        exec_price = gross_out_usd / actual_sell_eth if actual_sell_eth > 0 else 0
        ref_price = eth_price
        price_impact_bps = max(0, (ref_price - exec_price) / ref_price * 10000) if ref_price > 0 else 0
        notes = f"CowSwap 批量拍卖 | solver 报价 | fee={fee_amount/10**18:.6f} ETH | {gross_out_usd:.2f} USDT"
    else:
        eth_out = buy_amount / 10**buy_decimals
        gross_out_usd = eth_out * eth_price
        exec_price = eth_out / amount_usd
        ref_price = 1.0 / eth_price
        price_impact_bps = max(0, (ref_price - exec_price) / ref_price * 10000) if ref_price > 0 else 0
        notes = f"CowSwap 批量拍卖 | solver 报价 | {eth_out:.6f} ETH"

    return {
        "gross_out_usd": gross_out_usd, "gas_units": gas_amount,
        "exec_price": exec_price, "price_impact_bps": price_impact_bps,
        "fee_bps": fee_amount / actual_sell * 10000 if actual_sell > 0 else 0,
        "notes": notes, "data_source": "cowswap_solver_api",
    }


async def paraswap_quote_async(session, direction, amount_usd, eth_price):
    """async 版 ParaSwap 报价，逻辑同 paraswap_quote()"""
    if direction == "ETH->USDT":
        src_token = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        dest_token = USDT
        amount = int((amount_usd / eth_price) * 10**18)
        src_decimals, dest_decimals = 18, 6
    else:
        src_token = USDT
        dest_token = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        amount = int(amount_usd * 10**6)
        src_decimals, dest_decimals = 6, 18

    params = {
        "srcToken": src_token.lower(), "destToken": dest_token.lower(),
        "amount": str(amount), "side": "SELL", "network": "1",
    }
    url = PARASWAP_API + "?" + urllib.parse.urlencode(params)
    resp = await _aio_get(session, url)
    if not resp or "_error" in resp or "priceRoute" not in resp:
        return None

    pr = resp["priceRoute"]
    dest_amount = int(pr.get("destAmount", "0"))
    if dest_amount == 0:
        return None

    best_route = pr.get("bestRoute", [])
    gas_estimate = 300000
    routing_complexity = max(1, len(best_route))
    exchanges_used = set()
    for leg in best_route:
        for swap in leg.get("swaps", []):
            for ex in swap.get("swapExchanges", []):
                exchanges_used.add(ex.get("exchange", ""))

    if direction == "ETH->USDT":
        gross_out_usd = dest_amount / 10**dest_decimals
        eth_in = amount / 10**src_decimals
        exec_price = gross_out_usd / eth_in if eth_in > 0 else 0
        price_impact_bps = max(0, (eth_price - exec_price) / eth_price * 10000) if eth_price > 0 else 0
        notes = f"ParaSwap 聚合 | exchanges={','.join(sorted(exchanges_used))[:60]} | {gross_out_usd:.2f} USDT"
    else:
        eth_out = dest_amount / 10**dest_decimals
        gross_out_usd = eth_out * eth_price
        exec_price = eth_out / amount_usd
        ref_price = 1.0 / eth_price
        price_impact_bps = max(0, (ref_price - exec_price) / ref_price * 10000) if ref_price > 0 else 0
        notes = f"ParaSwap 聚合 | exchanges={','.join(sorted(exchanges_used))[:60]} | {eth_out:.6f} ETH"

    return {
        "gross_out_usd": gross_out_usd, "gas_units": gas_estimate,
        "exec_price": exec_price, "price_impact_bps": price_impact_bps,
        "fee_bps": 0, "routing_complexity": routing_complexity,
        "notes": notes, "data_source": "paraswap_api_v5",
    }


async def tokenlon_quote_async(session, direction, amount_usd, eth_price, gas_gwei):
    """async 版 Tokenlon（纯计算无 IO，直接复用同步函数）"""
    return tokenlon_quote(direction, amount_usd, eth_price, gas_gwei)


# async 平台 -> 提供者映射
PLATFORM_PROVIDERS_ASYNC = {
    "Uniswap": uniswap_quote_async,
    "1inch": paraswap_quote_async,
    "CowSwap": cowswap_quote_async,
    "Tokenlon": tokenlon_quote_async,
}


async def get_real_quote_async(session, platform, direction, amount_usd,
                               eth_price, gas_gwei):
    """
    async 统一获取真实/混合报价。
    需要 aiohttp.ClientSession 作为第一个参数。
    失败返回 None（调用方应回退到模拟模型）。
    """
    if not _AIOHTTP_AVAILABLE:
        return None
    provider = PLATFORM_PROVIDERS_ASYNC.get(platform)
    if provider is None:
        return None
    try:
        if platform == "Tokenlon":
            return await provider(session, direction, amount_usd, eth_price, gas_gwei)
        return await provider(session, direction, amount_usd, eth_price)
    except Exception:
        return None


async def health_check_async(session):
    """async 健康检查：并发探测各提供者"""
    if not _AIOHTTP_AVAILABLE:
        return {"_error": "aiohttp 未安装"}
    tasks = {
        "CowSwap": cowswap_quote_async(session, "ETH->USDT", 1000, 3000),
        "1inch(ParaSwap)": paraswap_quote_async(session, "ETH->USDT", 1000, 3000),
        "Uniswap": uniswap_quote_async(session, "ETH->USDT", 1000, 3000),
    }
    keys = list(tasks.keys())
    results = await asyncio.gather(*[tasks[k] for k in keys])
    status = {}
    for k, r in zip(keys, results):
        status[k] = "OK" if r else "FAIL"
    status["Tokenlon"] = "ESTIMATED (无公开 API)"
    return status


if __name__ == "__main__":
    import sys
    print("=== 真实 API 提供者健康检查 ===")
    s = health_check()
    for k, v in s.items():
        print(f"  {k:25s} : {v}")
    print()
    print("=== ETH 中价 (链上) ===")
    mp = get_eth_usd_midprice()
    print(f"  ETH/USDT = ${mp:.2f}" if mp else "  获取失败")
