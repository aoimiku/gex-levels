#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gex_levels.py — NAS100 / XAUUSD / BTCUSD 用の GEX レベルを無料データから自動計算し、
               TradingView インジケーター「GEX_Levels.pine」に貼る文字列を出力する。

■ データ元（APIキー不要）
    NAS100 : QQQ（--nas-source ndx で NDX 指数オプション）… CBOE 遅延チェーン
    XAUUSD : GLD … CBOE 遅延チェーン
    BTCUSD : Deribit の BTC オプション … public/get_book_summary_by_currency

■ 計算
    1%変動あたりの GEX = ガンマ × 建玉 × 乗数 × 原資産価格² × 0.01
    コールは＋、プットは−（ディーラーはコール買い・プット売りの仮定）
    ガンマは IV から Black-Scholes で自前計算する。CBOE の gamma 列は小数4桁で
    丸められており（NDX では 0.0001 などほぼ情報がない）、ガンマ・フリップ探索でも
    価格を動かして再計算するため、IV からの計算に統一している。
    Deribit は先物（満期別 underlying_price）に対する Black-76（r=0）。

■ 出力
    output/tv_input.txt      … Pine の「GEXデータ」に貼る文字列（3銘柄分を1行に連結）
    output/latest_<銘柄>.txt … 銘柄別の最新ブロック（別々の頻度で回しても連結できる）
    history/levels.csv        … 新しいデータを取得するたびのレベル履歴（引け後・週末の重複は保存しない）
    history/raw/<元>/<日付>.csv.gz … 全オプションの生スナップショット（将来のバックテスト用）

使い方:
    python3 gex_levels.py                    # 3銘柄すべて
    python3 gex_levels.py BTCUSD --copy      # BTC だけ更新し、貼り付け文字列をクリップボードへ
    python3 gex_levels.py NAS100 --nas-source ndx
"""

import argparse
import csv
import gzip
import json
import math
import os
import re
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timezone
from datetime import time as dtime
from zoneinfo import ZoneInfo

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
# 出力先の親フォルダ。GitHub Actions では GEX_DATA_DIR=data にして、手元の output/・history/ と分ける
DATA_DIR = os.path.join(BASE, os.environ.get("GEX_DATA_DIR", ""))
OUT_DIR = os.path.join(DATA_DIR, "output")
HIST_DIR = os.path.join(DATA_DIR, "history")
RAW_FILTER_RANGE = 0.25                # --raw filtered のとき保存するストライク範囲（現値±25%）

ET = ZoneInfo("America/New_York")
YEAR_SEC = 365.0 * 24 * 3600
MIN_T = 1.0 / (365.0 * 24)          # 残存1時間未満は1時間として扱う（満期直前のガンマ発散を抑える）
EXPIRED_T = 10.0 / (365.0 * 24 * 60)  # 残存10分未満は満期済みとみなす（引け値時点の当日満期など）
SQRT_2PI = math.sqrt(2.0 * math.pi)

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"
DERIBIT_URL = "https://www.deribit.com/api/v2/public/"

# range = 現物価格から±何%のストライクを使うか。r/q は IV→ガンマ計算用の金利・配当利回り。
TARGETS = {
    "NAS100": {"source": "cboe", "ticker": "QQQ", "mult": 100, "range": 0.10, "r": 0.04, "q": 0.005},
    "XAUUSD": {"source": "cboe", "ticker": "GLD", "mult": 100, "range": 0.12, "r": 0.04, "q": 0.0},
    "BTCUSD": {"source": "deribit", "ticker": "BTC", "mult": 1, "range": 0.15, "r": 0.0, "q": 0.0},
}
NDX_CONF = {"ticker": "_NDX", "mult": 100, "q": 0.006}

# 午前（寄り付き）決済の指数オプション。週次・日次（NDXP 等）は16:00決済。
AM_SETTLED_ROOTS = {"NDX", "SPX", "RUT"}
OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")

FLIP_STEPS = 240
PROFILE_STRIKES = 40


# ───────────────────────── 共通 ─────────────────────────

def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def norm_pdf(x):
    return math.exp(-0.5 * x * x) / SQRT_2PI


def bs_gamma(S, K, T, sigma, r=0.0, q=0.0):
    """Black-Scholes ガンマ（原資産1単位あたり）。r=q=0 で Black-76 の先物ガンマと同じ。"""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / sq
    return math.exp(-q * T) * norm_pdf(d1) / (S * sq)


def to_f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ───────────────────────── 取得・整形 ─────────────────────────

def load_cboe(ticker, now):
    """CBOE 遅延チェーン → (spot, 価格時刻UTC, 行リスト)。"""
    d = http_json(CBOE_URL.format(ticker))["data"]
    spot = float(d["current_price"])

    # 遅延データなので、原資産価格が実際についた時刻（ET）を CFD 換算と残存期間の基準時刻にする
    # （引け後に実行しても、引け時点の価格と残存期間の組で計算され、結果がぶれない）
    price_time = now
    if d.get("last_trade_time"):
        try:
            price_time = datetime.fromisoformat(d["last_trade_time"]).replace(tzinfo=ET).astimezone(timezone.utc)
        except ValueError:
            pass

    rows = []
    for o in d["options"]:
        m = OCC_RE.match(o.get("option", ""))
        if not m:
            continue
        root, ymd, cp, k = m.groups()
        exp = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
        settle = dtime(9, 30) if root in AM_SETTLED_ROOTS else dtime(16, 0)
        exp_dt = datetime.combine(exp, settle, ET)
        rows.append({
            "expiry": exp.isoformat(),
            "T": (exp_dt - price_time).total_seconds() / YEAR_SEC,
            "type": cp,
            "K": int(k) / 1000.0,
            "oi": to_f(o.get("open_interest")),
            "iv": to_f(o.get("iv")),
            "u0": spot,
            # 以下は履歴保存用
            "sym": o["option"],
            "gamma_src": o.get("gamma"),
            "bid": o.get("bid"), "ask": o.get("ask"), "volume": o.get("volume"),
        })
    return spot, price_time, rows


def load_deribit(currency, now):
    """Deribit 公開API → (index価格, 時刻UTC, 行リスト)。OI は BTC 枚数（1枚=1BTC）。"""
    res = http_json(DERIBIT_URL + f"get_book_summary_by_currency?currency={currency}&kind=option")["result"]
    idx = http_json(DERIBIT_URL + f"get_index_price?index_name={currency.lower()}_usd")["result"]["index_price"]

    rows = []
    for o in res:
        parts = o["instrument_name"].split("-")      # BTC-2OCT26-81000-P
        if len(parts) != 4:
            continue
        try:
            exp = datetime.strptime(parts[1], "%d%b%y").date()
            K = float(parts[2].replace("d", "."))
        except ValueError:
            continue
        exp_dt = datetime.combine(exp, dtime(8, 0), timezone.utc)
        rows.append({
            "expiry": exp.isoformat(),
            "T": (exp_dt - now).total_seconds() / YEAR_SEC,
            "type": parts[3],
            "K": K,
            "oi": to_f(o.get("open_interest")),
            "iv": to_f(o.get("mark_iv")) / 100.0,
            "u0": to_f(o.get("underlying_price")) or idx,   # 満期別の先物価格
            "sym": o["instrument_name"],
            "gamma_src": "",
            "bid": o.get("bid_price"), "ask": o.get("ask_price"), "volume": o.get("volume"),
        })
    return float(idx), now, rows


def clean(rows, spot, rng):
    """満期切れ・IV<=0（古い気配）・建玉0・範囲外ストライクを除外。"""
    lo, hi = spot * (1 - rng), spot * (1 + rng)
    out = []
    for r in rows:
        if r["T"] < EXPIRED_T or r["iv"] <= 0 or r["oi"] <= 0 or not (lo <= r["K"] <= hi):
            continue
        r = dict(r)
        r["T"] = max(r["T"], MIN_T)
        out.append(r)
    return out


# ───────────────────────── GEX ─────────────────────────

def option_gex(r, s, spot, mult, rate, q):
    """原資産が s のときの1オプション行の GEX（$／1%変動、符号なし）。"""
    u = r["u0"] * s / spot          # Deribit は満期別の先物を同率で動かす
    g = bs_gamma(u, r["K"], r["T"], r["iv"], rate, q)
    return g * r["oi"] * mult * u * u * 0.01


def total_gex(rows, s, spot, mult, rate, q):
    t = 0.0
    for r in rows:
        v = option_gex(r, s, spot, mult, rate, q)
        t += v if r["type"] == "C" else -v
    return t


def find_flip(rows, spot, rng, mult, rate, q):
    """現物価格を ±rng の範囲で動かし、合計 GEX が 0 を跨ぐ価格（現値に最も近いもの）。"""
    lo, hi = spot * (1 - rng), spot * (1 + rng)
    grid = [lo + (hi - lo) * i / FLIP_STEPS for i in range(FLIP_STEPS + 1)]
    vals = [total_gex(rows, s, spot, mult, rate, q) for s in grid]
    cands = []
    for i in range(1, len(grid)):
        a, b = vals[i - 1], vals[i]
        if a == 0:
            cands.append(grid[i - 1])
        elif (a < 0) != (b < 0):
            cands.append(grid[i - 1] + (grid[i] - grid[i - 1]) * (-a) / (b - a))
    return min(cands, key=lambda x: abs(x - spot)) if cands else None


def levels(rows, spot, rng, mult, rate, q, with_profile=False):
    by_k = {}
    for r in rows:
        v = option_gex(r, spot, spot, mult, rate, q)
        d = by_k.setdefault(r["K"], [0.0, 0.0])
        d[0 if r["type"] == "C" else 1] += v
    if not by_k:
        return None
    net = {k: c - p for k, (c, p) in by_k.items()}
    out = {
        "net": sum(net.values()),
        "cw": max(by_k, key=lambda k: by_k[k][0]),
        "pw": max(by_k, key=lambda k: by_k[k][1]),
        "pin": max(net, key=lambda k: abs(net[k])),
        "flip": find_flip(rows, spot, rng, mult, rate, q),
        "n": len(rows),
    }
    if with_profile:
        top = sorted(net, key=lambda k: abs(net[k]), reverse=True)[:PROFILE_STRIKES]
        out["prof"] = [(k, net[k]) for k in sorted(top)]
    return out


# ───────────────────────── 出力 ─────────────────────────

def fnum(x, nd=2):
    if x is None:
        return "na"
    s = f"{x:.{nd}f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def tv_block(name, src, spot, price_time, lv_all, lv_near, near_exp):
    """Pine が読む1銘柄分のブロック。GEX は $M 単位。"""
    f = [f"GEX:{name}", f"src={src}",
         f"ts={int(price_time.timestamp() * 1000)}", f"spot={fnum(spot, 4)}"]
    for suffix, lv in (("", lv_all), ("0", lv_near)):
        if lv is None:
            continue
        f += [f"net{suffix}={fnum(lv['net'] / 1e6)}", f"flip{suffix}={fnum(lv['flip'], 3)}",
              f"cw{suffix}={fnum(lv['cw'], 3)}", f"pw{suffix}={fnum(lv['pw'], 3)}",
              f"pin{suffix}={fnum(lv['pin'], 3)}"]
    if lv_near is not None:
        f.append(f"exp0={near_exp}")
    if lv_all and lv_all.get("prof"):
        f.append("prof=" + ",".join(f"{fnum(k, 3)}:{fnum(v / 1e6)}" for k, v in lv_all["prof"]))
    return "|".join(f)


def save_raw(src, now, price_time, spot, rows, min_gap_h=0.0):
    """生チェーンを日別 gzip に追記し、新しいデータだったかを返す。
    原資産の価格時刻が前回と同じ（引け後・週末の CBOE）なら新しくないので何も保存しない。
    min_gap_h > 0 なら、生チェーンの保存は前回保存から min_gap_h 時間以上空いたときだけにする（容量対策）。"""
    d = os.path.join(HIST_DIR, "raw", src.lstrip("_"))
    os.makedirs(d, exist_ok=True)
    state_path = os.path.join(d, ".state.json")
    state = {}
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    pt = price_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    if state.get("price_time") == pt:
        return False
    state["price_time"] = pt

    last_raw = state.get("raw_saved")
    if not last_raw or (now - datetime.fromisoformat(last_raw)).total_seconds() >= min_gap_h * 3600 - 300:
        day = now.strftime("%Y-%m-%d")
        path = os.path.join(d, f"{day}.csv.gz")
        new = not os.path.exists(path)
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        with gzip.open(path, "at", newline="", encoding="utf-8") as f:   # 実行ごとに gzip メンバーを追記
            w = csv.writer(f)
            if new:
                w.writerow(["snapshot_utc", "spot", "symbol", "expiry", "type", "strike", "open_interest",
                            "iv", "underlying", "gamma_src", "bid", "ask", "volume"])
            for r in rows:
                w.writerow([stamp, spot, r["sym"], r["expiry"], r["type"], r["K"], r["oi"], r["iv"],
                            r["u0"], r["gamma_src"], r["bid"], r["ask"], r["volume"]])
        state["raw_saved"] = now.isoformat()

    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    return True


def save_levels(row):
    os.makedirs(HIST_DIR, exist_ok=True)
    path = os.path.join(HIST_DIR, "levels.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new:
            w.writeheader()
        w.writerow(row)


def run_target(name, conf, now, args):
    src = conf["ticker"]
    if conf["source"] == "cboe":
        spot, price_time, raw = load_cboe(src, now)
    else:
        spot, price_time, raw = load_deribit(src, now)

    to_save = raw
    if args.raw == "filtered":          # リポジトリが肥大しないよう、建玉あり・現値±25%・未満期だけ残す
        to_save = [r for r in raw if r["oi"] > 0 and r["T"] > 0 and abs(r["K"] / spot - 1) <= RAW_FILTER_RANGE]
    fresh = False if args.no_history else save_raw(src, now, price_time, spot, to_save, args.raw_every)

    rows = clean(raw, spot, conf["range"])
    if not rows:
        print(f"[{name}] 有効なオプションがありません（市場データを確認してください）")
        return None
    args_m = (spot, conf["range"], conf["mult"], conf["r"], conf["q"])

    lv_all = levels(rows, *args_m, with_profile=True)
    near_exp = min(r["expiry"] for r in rows)
    lv_near = levels([r for r in rows if r["expiry"] == near_exp], *args_m)

    block = tv_block(name, src.lstrip("_"), spot, price_time, lv_all, lv_near, near_exp)

    if fresh:
        save_levels({
            "run_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "price_utc": price_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "target": name, "source": src.lstrip("_"), "spot": spot,
            "net_musd": round(lv_all["net"] / 1e6, 3), "flip": lv_all["flip"],
            "call_wall": lv_all["cw"], "put_wall": lv_all["pw"], "pin": lv_all["pin"],
            "near_expiry": near_exp, "net0_musd": round(lv_near["net"] / 1e6, 3), "flip0": lv_near["flip"],
            "call_wall0": lv_near["cw"], "put_wall0": lv_near["pw"], "pin0": lv_near["pin"],
        })

    def p(x):
        return "—" if x is None else f"{x:,.2f}"
    print(f"[{name}] 元={src.lstrip('_')}  現値={spot:,.2f}  価格時刻={price_time:%Y-%m-%d %H:%M}Z  "
          f"使用オプション={len(rows)}本")
    print(f"   全満期 : 純GEX {lv_all['net'] / 1e6:+,.1f}M$  フリップ {p(lv_all['flip'])}  "
          f"コールW {p(lv_all['cw'])}  プットW {p(lv_all['pw'])}  ピン {p(lv_all['pin'])}")
    print(f"   直近 {near_exp}: 純GEX {lv_near['net'] / 1e6:+,.1f}M$  フリップ {p(lv_near['flip'])}  "
          f"コールW {p(lv_near['cw'])}  プットW {p(lv_near['pw'])}  ピン {p(lv_near['pin'])}")
    return block


def main():
    ap = argparse.ArgumentParser(description="GEX レベルを計算して TradingView 貼付文字列を出力")
    ap.add_argument("targets", nargs="*", default=list(TARGETS), help="NAS100 XAUUSD BTCUSD（既定: すべて）")
    ap.add_argument("--nas-source", choices=["qqq", "ndx"], default="qqq", help="NAS100 の元データ")
    ap.add_argument("--copy", action="store_true", help="貼り付け文字列をクリップボードへコピー（macOS）")
    ap.add_argument("--no-history", action="store_true", help="履歴（生チェーン・レベル）を保存しない")
    ap.add_argument("--raw", choices=["all", "filtered"], default="all",
                    help="生チェーンの保存範囲（filtered: 建玉あり・現値±25%%のみ）")
    ap.add_argument("--raw-every", type=float, default=0.0,
                    help="生チェーンを保存する最短間隔（時間）。レベル履歴は毎回保存（既定0=毎回）")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    os.makedirs(OUT_DIR, exist_ok=True)

    failed = False
    for name in args.targets:
        name = name.upper()
        if name not in TARGETS:
            sys.exit(f"未対応の銘柄: {name}（{', '.join(TARGETS)}）")
        conf = dict(TARGETS[name])
        if name == "NAS100" and args.nas_source == "ndx":
            conf.update(NDX_CONF)
        try:
            block = run_target(name, conf, now, args)
        except Exception as e:                      # 1銘柄の失敗で他を止めない
            print(f"[{name}] 取得/計算に失敗: {e}")
            failed = True
            continue
        if block:
            with open(os.path.join(OUT_DIR, f"latest_{name}.txt"), "w", encoding="utf-8") as f:
                f.write(block)

    # 銘柄ごとに更新頻度が違っても、各銘柄の最新ブロックを連結して1本にする
    blocks = []
    for name in TARGETS:
        p = os.path.join(OUT_DIR, f"latest_{name}.txt")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                blocks.append(f.read().strip())
    tv = ";".join(blocks)
    with open(os.path.join(OUT_DIR, "tv_input.txt"), "w", encoding="utf-8") as f:
        f.write(tv)

    print(f"\n▼ TradingView「GEXデータ」に貼る文字列（{len(tv)}文字）: {os.path.relpath(os.path.join(OUT_DIR, 'tv_input.txt'), BASE)}")
    if args.copy and tv:
        subprocess.run(["pbcopy"], input=tv.encode("utf-8"), check=False)
        print("  → クリップボードにコピーしました")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
