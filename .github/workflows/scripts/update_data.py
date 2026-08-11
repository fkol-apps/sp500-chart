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

1 は確定した月しか持たないため、取得後に FRED（セントルイス連銀）の
日次終値から最新月を継ぎ足す。継ぎ足しに失敗しても処理は続行する
（最新月が無いだけで、チャート自体は問題なく描けるため）。

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

# FRED（セントルイス連銀）の S&P 500 日次終値。APIキー不要。
# 直近10年分しかないので歴史データには使えないが、
# 月次データセットが持たない「確定前の最新月」を埋めるのに使う。
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=SP500"


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
    return {"source": "datasets/s-and-p-500 ／ 月中平均", "dates": dates, "prices": prices}


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
    return {"source": "Stooq (^SPX) ／ 月末終値", "dates": dates, "prices": prices}


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
    return {"source": "Yahoo Finance (^GSPC) ／ 月末終値", "dates": dates, "prices": prices}


def fred_month_end() -> dict[str, float]:
    """FRED の日次終値を「月 -> その月の最終営業日の終値」に畳む。

    CSV の見出しは observation_date / DATE と揺れるので、
    日付は1列目、値は SP500 列として読む。休場日は "." で入っている。
    """
    raw = http_get(FRED_URL).decode("utf-8", "replace")
    reader = csv.reader(io.StringIO(raw))
    header = next(reader, None)
    if not header or len(header) < 2:
        raise RuntimeError("FRED: 見出し行が読めません")
    try:
        i_val = [h.strip().upper() for h in header].index("SP500")
    except ValueError:
        raise RuntimeError(f"FRED: SP500 列が見つかりません ({header})")

    by_month: dict[str, float] = {}
    for row in reader:
        if len(row) <= i_val:
            continue
        date_str = row[0].strip()
        value_str = row[i_val].strip()
        if len(date_str) != 10 or not value_str or value_str == ".":
            continue
        try:
            value = float(value_str)
        except ValueError:
            continue
        if value <= 0:
            continue
        # 日付昇順で入っているので、後から来たものが月内で最新
        by_month[date_str[:7]] = value
    if not by_month:
        raise RuntimeError("FRED: 有効な値がありません")
    return by_month


def fill_recent_months(data: dict) -> dict:
    """主取得元が持たない最新月を FRED で継ぎ足す。

    失敗しても致命傷にしない。継ぎ足せなければ元のデータをそのまま返す
    （最新月が無いだけで、チャート自体は問題なく描ける）。
    """
    try:
        recent = fred_month_end()
    except (urllib.error.URLError, OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"直近月の補完をスキップ: {exc}", file=sys.stderr)
        return data

    last_month = data["dates"][-1][:7]
    extra = sorted(k for k in recent if k > last_month)
    if not extra:
        print("補完すべき最新月はありません。")
        return data

    data["dates"] = data["dates"] + [k + "-01" for k in extra]
    data["prices"] = data["prices"] + [round(recent[k], 2) for k in extra]
    data["source"] = f"{data['source']}（{extra[0]}以降は FRED の月末終値）"
    print(f"FRED で補完: {', '.join(extra)}")
    return data


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

    # 主取得元が前月までしか持たない場合に備えて最新月を継ぎ足す
    data = fill_recent_months(data)

    existing = load_existing()
    # source も比較する。取得元ラベルだけ変わったときに
    # 書き換えを取りこぼさないため
    if (
        existing
        and existing.get("dates") == data["dates"]
        and existing.get("prices") == data["prices"]
        and existing.get("source") == data["source"]
    ):
        print("データに変更はありません。")
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
