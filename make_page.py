#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_page.py — gex_levels.py の最新ブロック（output/latest_*.txt）から、
               スマホで見るための1枚の HTML（レベル一覧 + TradingView 用データのコピーボタン）を作る。

    python3 make_page.py                    # docs/index.html を書き出す
    GEX_DATA_DIR=data python3 make_page.py  # GitHub Actions と同じ data/ を読む
"""

import html
import os
import sys
from datetime import datetime, timedelta, timezone

import gex_levels as g

JST = timezone(timedelta(hours=9))
NAMES = {"NAS100": "NASDAQ 100", "XAUUSD": "ゴールド", "BTCUSD": "ビットコイン"}
SRC_NOTE = {"QQQ": "QQQ オプション（CBOE）", "NDX": "NDX オプション（CBOE）",
            "GLD": "GLD オプション（CBOE）", "BTC": "BTC オプション（Deribit）"}


def parse_block(text):
    parts = text.strip().split("|")
    kv = {"name": parts[0].split(":", 1)[1]}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            kv[k] = v
    return kv


def num(kv, k):
    try:
        return float(kv[k])
    except (KeyError, ValueError):
        return None


def fmt_price(x, spot):
    if x is None:
        return "—"
    nd = 0 if spot >= 10000 else 2
    return f"{x:,.{nd}f}"


def pct(x, spot):
    if x is None or not spot:
        return ""
    return f"{(x / spot - 1) * 100:+.1f}%"


def card(kv):
    spot = num(kv, "spot") or 0.0
    flip = num(kv, "flip")
    net = num(kv, "net") or 0.0
    pos = spot >= flip if flip is not None else net >= 0
    ts = int(float(kv.get("ts", "0")))
    t_jst = datetime.fromtimestamp(ts / 1000, JST)
    e = html.escape

    rows = []
    for key, label in (("flip", "ガンマフリップ"), ("cw", "コールウォール"),
                       ("pw", "プットウォール"), ("pin", "ピン（最大ガンマ）")):
        cells = []
        for sfx in ("", "0"):
            v = num(kv, key + sfx)
            cells.append(f'<td><span class="v">{fmt_price(v, spot)}</span>'
                         f'<span class="d">{pct(v, spot)}</span></td>')
        rows.append(f'<tr class="k-{key}"><th>{label}</th>{"".join(cells)}</tr>')
    net0 = num(kv, "net0")
    rows.append('<tr><th>純GEX（1%あたり）</th>'
                + "".join(f'<td class="{"pos" if (v or 0) >= 0 else "neg"}">{"—" if v is None else f"{v:+,.1f}M$"}</td>'
                          for v in (net, net0)) + "</tr>")

    # ストライク別 純GEX（高いストライクを上に、現値の位置に線）
    prof = []
    for item in kv.get("prof", "").split(","):
        if ":" in item:
            k, v = item.split(":")
            prof.append((float(k), float(v)))
    prof.sort(reverse=True)
    mx = max((abs(v) for _, v in prof), default=1.0) or 1.0
    bars, spot_drawn = [], False
    for k, v in prof:
        if not spot_drawn and k < spot:
            bars.append(f'<div class="spot">現値 {fmt_price(spot, spot)}</div>')
            spot_drawn = True
        w = abs(v) / mx * 50
        side = "r" if v >= 0 else "l"
        bars.append(f'<div class="bar"><span class="kk">{fmt_price(k, spot)}</span>'
                    f'<span class="track"><i class="{side}" style="width:{w:.1f}%"></i></span>'
                    f'<span class="vv">{v:+,.0f}</span></div>')
    if not spot_drawn:
        bars.append(f'<div class="spot">現値 {fmt_price(spot, spot)}</div>')

    exp0 = kv.get("exp0", "")
    return f"""
<section class="card">
  <header>
    <div><h2>{e(kv['name'])}</h2><p class="sub">{e(NAMES.get(kv['name'], ''))} ・ {e(SRC_NOTE.get(kv.get('src', ''), kv.get('src', '')))}</p></div>
    <span class="chip {'pos' if pos else 'neg'}">{'正ガンマ' if pos else '負ガンマ'}</span>
  </header>
  <p class="meta">{e(kv.get('src', ''))} 現値 <b>{fmt_price(spot, spot)}</b> ・ 価格時刻 {t_jst:%m/%d %H:%M} JST
    <span class="age" data-ts="{ts}"></span></p>
  <p class="hint">{'フリップより上：値動きが抑えられやすい' if pos else 'フリップより下：値動きが拡大しやすい'}</p>
  <table>
    <thead><tr><th></th><th>全満期</th><th>直近 {e(exp0[5:] if len(exp0) >= 10 else exp0)}</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <details><summary>ストライク別の純GEX（$M、全満期）</summary><div class="prof">{''.join(bars)}</div></details>
</section>"""


PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>GEX Levels</title>
<style>
:root {{ --bg:#f6f7f9; --card:#fff; --fg:#14171c; --mut:#69707d; --line:#e3e6ea;
        --pos:#0f8a6c; --neg:#d0433f; --flip:#c98500; --pin:#8a4fb3; --acc:#2962ff; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#0f1115; --card:#181b21; --fg:#e8eaed; --mut:#9aa0aa; --line:#2a2f37;
          --pos:#2bbd94; --neg:#ef6461; --flip:#ffb300; --pin:#bb86e0; --acc:#5b8cff; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg);
       font:15px/1.5 -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Noto Sans JP", sans-serif; }}
main {{ max-width:640px; margin:0 auto; padding:16px 16px 48px; }}
h1 {{ font-size:20px; margin:4px 0 2px; }}
.top {{ color:var(--mut); font-size:13px; margin:0 0 14px; }}
.copy {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px; margin-bottom:16px; }}
.copy button {{ width:100%; padding:14px; border:0; border-radius:10px; background:var(--acc); color:#fff;
               font-size:16px; font-weight:600; }}
.copy button.done {{ background:var(--pos); }}
.copy p {{ margin:8px 0 0; font-size:12px; color:var(--mut); }}
.copy textarea {{ width:100%; height:70px; margin-top:8px; font:11px/1.4 ui-monospace, Menlo, monospace;
                  color:var(--mut); background:transparent; border:1px solid var(--line); border-radius:8px; padding:6px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px; margin-bottom:14px; }}
.card header {{ display:flex; justify-content:space-between; align-items:flex-start; gap:8px; }}
h2 {{ font-size:18px; margin:0; }}
.sub {{ margin:0; font-size:12px; color:var(--mut); }}
.chip {{ font-size:12px; font-weight:600; padding:3px 10px; border-radius:99px; white-space:nowrap; }}
.chip.pos {{ color:var(--pos); background:color-mix(in srgb, var(--pos) 14%, transparent); }}
.chip.neg {{ color:var(--neg); background:color-mix(in srgb, var(--neg) 14%, transparent); }}
.meta {{ margin:8px 0 0; font-size:13px; color:var(--mut); }}
.meta b {{ color:var(--fg); }}
.age.old {{ color:var(--neg); font-weight:600; }}
.hint {{ margin:2px 0 8px; font-size:12px; color:var(--mut); }}
table {{ width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }}
th, td {{ padding:7px 4px; border-top:1px solid var(--line); text-align:right; font-size:14px; }}
thead th {{ border-top:0; font-size:12px; color:var(--mut); font-weight:500; }}
tbody th {{ text-align:left; font-weight:500; font-size:13px; }}
td .v {{ display:block; font-weight:600; }}
td .d {{ display:block; font-size:11px; color:var(--mut); }}
.k-flip th {{ color:var(--flip); }} .k-cw th {{ color:var(--pos); }} .k-pw th {{ color:var(--neg); }} .k-pin th {{ color:var(--pin); }}
td.pos {{ color:var(--pos); font-weight:600; }} td.neg {{ color:var(--neg); font-weight:600; }}
details {{ margin-top:10px; }}
summary {{ font-size:13px; color:var(--acc); cursor:pointer; }}
.prof {{ margin-top:8px; font-size:11px; font-variant-numeric:tabular-nums; }}
.bar {{ display:grid; grid-template-columns:64px 1fr 52px; align-items:center; gap:6px; height:15px; }}
.kk {{ text-align:right; color:var(--mut); }} .vv {{ text-align:right; color:var(--mut); }}
.track {{ position:relative; height:9px; border-left:0; }}
.track::before {{ content:""; position:absolute; left:50%; top:-3px; bottom:-3px; border-left:1px solid var(--line); }}
.track i {{ position:absolute; top:0; height:9px; border-radius:2px; }}
.track i.r {{ left:50%; background:var(--pos); }}
.track i.l {{ right:50%; background:var(--neg); }}
.spot {{ margin:3px 0; padding:1px 0; border-top:1px dashed var(--acc); color:var(--acc); font-size:11px; text-align:center; }}
footer {{ color:var(--mut); font-size:12px; margin-top:20px; }}
</style>
</head>
<body>
<main>
  <h1>GEX Levels</h1>
  <p class="top">更新 {generated} JST ・ 毎時自動更新（GitHub Actions）</p>
  <div class="copy">
    <button id="cp">TradingView 用データをコピー</button>
    <p>インジケーター設定の「GEXデータ」欄に貼り付けてください（3銘柄分まとめて入っています）。</p>
    <textarea id="tv" readonly>{tv}</textarea>
  </div>
  {cards}
  <footer>価格は元データ（QQQ・GLD・BTC）建てです。CFD への換算は TradingView のインジケーターが行います。
  ディーラーはコール買い・プット売りの仮定で計算しているため、ゴールドと BTC は参考値です。</footer>
</main>
<script>
const tv = document.getElementById('tv'), btn = document.getElementById('cp');
btn.onclick = async () => {{
  try {{ await navigator.clipboard.writeText(tv.value); }}
  catch (e) {{ tv.focus(); tv.select(); document.execCommand('copy'); }}
  btn.textContent = 'コピーしました'; btn.classList.add('done');
  setTimeout(() => {{ btn.textContent = 'TradingView 用データをコピー'; btn.classList.remove('done'); }}, 2000);
}};
for (const el of document.querySelectorAll('.age')) {{
  const h = (Date.now() - Number(el.dataset.ts)) / 3600000;
  el.textContent = '（' + (h < 1 ? Math.round(h * 60) + '分前' : h.toFixed(1) + '時間前') + '）';
  if (h > 30) el.classList.add('old');
}}
</script>
</body>
</html>
"""


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(g.BASE, "docs", "index.html")
    blocks = []
    for name in g.TARGETS:
        p = os.path.join(g.OUT_DIR, f"latest_{name}.txt")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                blocks.append(f.read().strip())
    if not blocks:
        sys.exit("latest_*.txt がありません。先に gex_levels.py を実行してください。")

    page = PAGE.format(
        generated=datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        tv=html.escape(";".join(blocks)),
        cards="".join(card(parse_block(b)) for b in blocks),
    )
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"ページを書き出しました: {out}")


if __name__ == "__main__":
    main()
