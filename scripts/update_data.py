#!/usr/bin/env python3
"""S&P 500 の月次データを取得して data/sp500-monthly.json を更新する。

サーバー側（GitHub Actions）で実行するのでCORSの制約を受けない。
取得元を上から順に試し、すべて失敗したら終了コード1で止まる
（既存のJSONは壊さない）。

取得元の順番には理由がある:
  1. datasets/s-and-p-500 (raw.githubusercontent.com)
     GitHub 上のCSVなので Actions のランナーから確実に到達できる。
     1871年からの月次データ。値は月中平均なので月末終値とは僅かに異なる。
  2. Stooq / 3. Yahoo
     月末終値そのものが取れるが、どちらもデータセンターIPからの
     アクセスを 403 で拒否することがあり、Actions 上では失敗しやすい。
     ローカル実行時のために残してある。

依存: 標準ライブラリのみ
"""

from __future__ import annotations

import csv
import io
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "sp500-monthly.json"
TIMEOUT = 30
UA = "Mozilla/5.0 (compatible; sp500-chart/1.0; +https://github.com/)"

# 実データとして受け入れる最低件数。これを下回るレスポンスは異常とみなす
MIN_ROWS = 300

DATAHUB_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500/main/data/data.csv"
)


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        return res.read()


def normalize(rows: list[tuple[str, float]]) -> tuple[list[str], list[float]]:
    """(YYYY-MM-DD, 価格) のリストを月単位に畳んで昇順に整える。"""
    by_month: dict[str, float] = {}
    for date_str, close in rows:
        if close is None or close <= 0:
            continue
        by_month[date_str[:7]] = close
    keys = sorted(by_month)
    return [k + "-01" for k in keys], [round(by_month[k], 2) for k in keys]


def from_datahub() -> dict:
    """GitHub 上の公開データセット。Actions から確実に到達できる。"""
    raw = http_get(DATAHUB_URL).decode("utf-8", "replace")
    reader = csv.DictReader(io.StringIO(raw))
    rows: list[tuple[str, float]] = []
    for rec in reader:
        date_str = (rec.get("Date") or "").strip()
        value_str = (rec.get("SP500") or "").strip()
        if len(date_str) != 10 or not value_str:
            continue
        try:
            rows.append((date_str, float(value_str)))
        except ValueError:
            continue
    if len(rows) < MIN_ROWS:
        raise RuntimeError(f"datahub: 件数が不足しています ({len(rows)}件)")
    dates, prices = normalize(rows)
    return {"source": "datasets/s-and-p-500 (月次)", "dates": dates, "prices": prices}


def from_stooq() -> dict:
    """Stooq の月足CSV。月末終値が取れるがCI環境では拒否されやすい。"""
    raw = http_get("https://stooq.com/q/d/l/?s=%5Espx&i=m").decode("utf-8", "replace")
    reader = csv.DictReader(io.StringIO(raw))
    rows: list[tuple[str, float]] = []
    for rec in reader:
        date_str = (rec.get("Date") or "").strip()
        close_str = (rec.get("Close") or "").strip()
        if len(date_str) != 10 or not close_str:
            continue
        try:
            rows.append((date_str, float(close_str)))
        except ValueError:
            continue
    if len(rows) < MIN_ROWS:
        raise RuntimeError(f"Stooq: 件数が不足しています ({len(rows)}件)")
    dates, prices = normalize(rows)
    return {"source": "Stooq (^SPX)", "dates": dates, "prices": prices}


def from_yahoo() -> dict:
    """Yahoo Finance の chart API。^GSPC は1927年12月から。"""
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        "%5EGSPC?range=max&interval=1mo"
    )
    payload = json.loads(http_get(url))
    result = payload["chart"]["result"][0]
    stamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    rows: list[tuple[str, float]] = []
    for stamp, close in zip(stamps, closes):
        if close is None:
            continue
        dt = datetime.fromtimestamp(stamp, tz=timezone.utc)
        rows.append((f"{dt.year:04d}-{dt.month:02d}-01", float(close)))
    if len(rows) < MIN_ROWS:
        raise RuntimeError(f"Yahoo: 件数が不足しています ({len(rows)}件)")
    dates, prices = normalize(rows)
    return {"source": "Yahoo Finance (^GSPC)", "dates": dates, "prices": prices}


def load_existing() -> dict | None:
    try:
        return json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main() -> int:
    errors: list[str] = []
    data: dict | None = None

    for fetch in (from_datahub, from_stooq, from_yahoo):
        try:
            data = fetch()
            print(f"取得成功: {data['source']} / {len(data['dates'])}件")
            break
        except (urllib.error.URLError, OSError, ValueError, KeyError, RuntimeError) as exc:
            errors.append(f"{fetch.__name__}: {exc}")
            print(f"取得失敗 {fetch.__name__}: {exc}", file=sys.stderr)

    if data is None:
        print("すべての取得元で失敗しました。既存データは変更しません。", file=sys.stderr)
        for err in errors:
            print("  - " + err, file=sys.stderr)
        return 1

    existing = load_existing()
    if existing and existing.get("dates") == data["dates"] and existing.get("prices") == data["prices"]:
        print("価格データに変更はありません。")
        return 0

    # 取得元によって開始年が違う（1871年 / 1928年）ので件数では比較しない。
    # 「最新月が既存より過去に戻る」ことだけを異常とみなす。
    old_dates = (existing or {}).get("dates") or []
    if old_dates and data["dates"][-1] < old_dates[-1]:
        print(
            f"新データの最終月が既存より過去のため中止しました "
            f"({data['dates'][-1]} < {old_dates[-1]})",
            file=sys.stderr,
        )
        return 1

    data["updated"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"書き込み完了: {OUT_PATH} ({data['dates'][0]} 〜 {data['dates'][-1]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
