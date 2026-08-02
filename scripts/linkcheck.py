#!/usr/bin/env python3
"""docs/ 配下の全外部リンクを実際に取得して生存を確認する。

このリンク集は「掲載リンクは実在を確認している」と謳っている。
リンクは黙って腐るので、その約束を機械に見張らせるためのスクリプト。

設計上の要点は二つ。

1. 相手のサーバーに迷惑をかけないこと。同一ホストへのリクエストは必ず直列化し、
   間隔を空ける。掲載先には全国文化財総覧のように100本以上のリンクが集中する
   ホストがあり、並列に叩くと相手にとっては小規模な攻撃と変わらない。

2. 「読めなくなったこと」だけを問題として扱うこと。URLが変わっただけ、
   ボット避けで弾かれただけ、レート制限に当たっただけのものを
   リンク切れと呼ぶと、毎週オオカミ少年になり誰も見なくなる。

要対応とするもの:
  - 404 / 410 / 5xx / 接続できない
  - ソフト404（200を返すが、下層URLがドメイン直下に飛ばされている）

参考として出すだけのもの:
  - 403（ボット避けの可能性が高い。ブラウザでは開けることが多い）
  - 429（こちらの叩きすぎ。相手の問題ではない）
  - 別ホストへのリダイレクト（ホスティング移転など。ページは生きている）

判定できないもの:
  - リンク先の内容が説明文と合っているかどうか

使い方:
    python3 scripts/linkcheck.py
    python3 scripts/linkcheck.py --out report.md
要対応が1件でもあれば終了コード 1。
"""

from __future__ import annotations

import argparse
import collections
import pathlib
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126 Safari/537.36"
)
TIMEOUT = 30
HOST_WORKERS = 8      # 同時に相手にするホストの数
PER_HOST_DELAY = 1.2  # 同一ホストへの連続アクセスの間隔（秒）
RETRY_429 = 2         # レート制限に当たったときの再試行回数

LINK_RE = re.compile(r"\]\((https?://[^)\s]+)\)")

FATAL = {"404", "410"}


def collect_links() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(DOCS.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(DOCS.parent))
        for url in LINK_RE.findall(text):
            found.setdefault(url, []).append(rel)
    return found


def is_root(url: str) -> bool:
    parts = urlsplit(url)
    return parts.path in ("", "/") and not parts.query


def host_of(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host.split("@")[-1].split(":")[0].removeprefix("www.")


def load_ignores() -> dict[str, str]:
    """既知の例外。URL<TAB>理由。要対応から外すが記録は残す。"""
    path = pathlib.Path(__file__).resolve().parent / "linkcheck-ignore.tsv"
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            url, _, reason = line.partition("\t")
            out[url.strip()] = reason.strip() or "理由未記載"
    return out


def fetch(url: str) -> tuple[str, str]:
    proc = subprocess.run(
        ["curl", "-sS", "-L", "--max-time", str(TIMEOUT), "-o", "/dev/null",
         "-A", UA, "-w", "%{http_code}\t%{url_effective}", url],
        capture_output=True, text=True,
    )
    parts = (proc.stdout or "\t").split("\t")
    return (parts[0] or "000"), (parts[1] if len(parts) > 1 else "")


def classify(url: str, code: str, final: str) -> tuple[str | None, str | None]:
    """(要対応の理由, 参考情報) を返す。"""
    if code.startswith("2"):
        if final and not is_root(url) and is_root(final):
            return "ソフト404（トップページに飛ばされた）", None
        before, after = host_of(url), host_of(final)
        if before and after and before != after:
            return None, f"別ホストへリダイレクト（{before} → {after}）"
        return None, None
    if code == "000":
        return "接続できない", None
    if code in FATAL or code.startswith("5"):
        return f"HTTP {code}", None
    if code == "403":
        return None, "HTTP 403（ボット避けの可能性。ブラウザでの確認が必要）"
    if code == "429":
        return None, "HTTP 429（レート制限。相手の問題ではない）"
    return None, f"HTTP {code}（判定保留）"


def check_host(urls: list[str]) -> list[dict]:
    """1ホストぶんのURLを直列に、間隔を空けて確認する。"""
    out = []
    for i, url in enumerate(urls):
        if i:
            time.sleep(PER_HOST_DELAY)
        code, final = fetch(url)
        for attempt in range(RETRY_429):
            if code != "429":
                break
            time.sleep(PER_HOST_DELAY * 5 * (attempt + 1))
            code, final = fetch(url)
        problem, note = classify(url, code, final)
        out.append({"url": url, "code": code, "final": final,
                    "problem": problem, "note": note})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="レポートの出力先")
    args = ap.parse_args()

    links = collect_links()
    by_host: dict[str, list[str]] = collections.defaultdict(list)
    for url in sorted(links):
        by_host[host_of(url)].append(url)

    busiest = sorted(by_host.items(), key=lambda kv: -len(kv[1]))[:3]
    print(f"検査対象: {len(links)} 本 / {len(by_host)} ホスト", file=sys.stderr)
    print("  最多: " + "、".join(f"{h}({len(u)})" for h, u in busiest), file=sys.stderr)

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=HOST_WORKERS) as pool:
        for chunk in pool.map(check_host, by_host.values()):
            results.extend(chunk)

    ignores = load_ignores()
    problems = [r for r in results if r["problem"] and r["url"] not in ignores]
    known = [r for r in results if r["problem"] and r["url"] in ignores]
    notes = [r for r in results if r["note"]]

    lines = [
        f"外部リンク **{len(links)} 本**（{len(by_host)} ホスト）を実際に取得して確認した。",
        "",
        f"- 要対応: **{len(problems)}**",
        f"- 参考（ページは生きている可能性が高い）: {len(notes)}",
        "",
    ]
    if problems:
        lines += ["## 要対応", "", "| 問題 | URL | 掲載ページ |", "|---|---|---|"]
        for r in sorted(problems, key=lambda x: x["problem"]):
            where = "、".join(sorted(set(links[r["url"]])))
            lines.append(f"| {r['problem']} | {r['url']} | {where} |")
        lines += [
            "",
            "ソフト404は、ステータスコードだけを見る確認では正常と判定されてしまう。"
            "下層ページを指していたはずのリンクがドメイン直下に着地している場合に検出している。",
        ]
    else:
        lines.append("要対応のリンクは見つからなかった。")

    if known:
        lines += ["", "## 既知の例外（要対応に数えない）", ""]
        for r in known:
            lines.append(f"- {r['url']} — {r['problem']}（{ignores[r['url']]}）")

    if notes:
        by_note: dict[str, list[str]] = collections.defaultdict(list)
        for r in notes:
            by_note[r["note"].split("（")[0]].append(r["url"])
        lines += ["", "## 参考", "",
                  "いずれもページ自体は生きている可能性が高く、要対応には数えていない。", ""]
        for kind, urls in sorted(by_note.items()):
            lines.append(f"<details><summary>{kind} — {len(urls)} 件</summary>\n")
            for u in sorted(urls):
                lines.append(f"- {u}")
            lines.append("\n</details>")

    report = "\n".join(lines) + "\n"
    if args.out:
        pathlib.Path(args.out).write_text(report, encoding="utf-8")
    else:
        print(report)

    print(f"要対応 {len(problems)} / 既知 {len(known)} / 参考 {len(notes)}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
