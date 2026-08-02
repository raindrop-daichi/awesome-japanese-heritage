#!/usr/bin/env python3
"""docs/ 配下の全外部リンクを実際に取得して生存を確認する。

このリンク集は「掲載リンクは実在を確認している」と謳っている。
リンクは黙って腐るので、その約束を機械に見張らせるためのスクリプト。

要対応として扱うもの:
  - 死んだリンク（4xx / 5xx / 接続不可）
  - ソフト404（200を返すが、下層URLがドメイン直下に飛ばされている）

参考情報として出すだけのもの（ページは生きているため要対応にしない）:
  - 別ホストへのリダイレクト（ホスティング移転など）

判定できないもの:
  - リンク先の内容が説明文と合っているかどうか

使い方:
    python3 scripts/linkcheck.py            # 検査してレポートを標準出力へ
    python3 scripts/linkcheck.py --out r.md # レポートをファイルへ
問題が1件でもあれば終了コード 1。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126 Safari/537.36"
)
TIMEOUT = 30
WORKERS = 6
# Markdown のリンク記法だけを拾う。地の文に出てくる `http://` の説明などは対象外
LINK_RE = re.compile(r"\]\((https?://[^)\s]+)\)")


def collect_links() -> dict[str, list[str]]:
    """URL -> それが載っているファイルの一覧"""
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
    """比較用にホスト名を正規化する。ポート番号と www. は無視する。"""
    host = urlsplit(url).netloc.lower()
    host = host.split("@")[-1].split(":")[0]
    return host.removeprefix("www.")


def load_ignores() -> dict[str, str]:
    """既知の例外。URL<TAB>理由 の形式。要対応から外すが記録は残す。"""
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


def check(url: str) -> dict:
    proc = subprocess.run(
        ["curl", "-sS", "-L", "--max-time", str(TIMEOUT), "-o", "/dev/null",
         "-A", UA, "-w", "%{http_code}\t%{url_effective}", url],
        capture_output=True, text=True,
    )
    out = (proc.stdout or "\t").split("\t")
    code = out[0] or "000"
    final = out[1] if len(out) > 1 else ""

    problem = None
    note = None
    if code != "200":
        problem = f"HTTP {code}" if code != "000" else "接続できない"
    elif final and not is_root(url) and is_root(final):
        problem = "ソフト404（トップページに飛ばされた）"
    else:
        before, after = host_of(url), host_of(final)
        if before and after and before != after:
            note = f"{before} → {after}"

    return {"url": url, "code": code, "final": final, "problem": problem, "note": note}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="レポートの出力先")
    args = ap.parse_args()

    links = collect_links()
    urls = sorted(links)
    print(f"検査対象: {len(urls)} 本", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(check, urls))

    ignores = load_ignores()
    problems = [r for r in results if r["problem"] and r["url"] not in ignores]
    known = [r for r in results if r["problem"] and r["url"] in ignores]
    moved = [r for r in results if r["note"]]

    lines = [
        f"外部リンク **{len(urls)} 本**を実際に取得して確認した。",
        "",
        f"- 正常: **{len(results) - len(problems)}**",
        f"- 要対応: **{len(problems)}**",
        "",
    ]
    if problems:
        lines += ["| 問題 | URL | 掲載ページ |", "|---|---|---|"]
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
        lines += ["", "### 既知の例外（対応済み・要対応に数えない）", ""]
        for r in known:
            lines.append(f"- {r['url']} — {r['problem']}（{ignores[r['url']]}）")

    if moved:
        lines += ["", "### 別ホストへリダイレクトされたもの（参考）", "",
                  "ページ自体は生きている。ホスティング移転などで起きるため要対応にはしていない。", ""]
        for r in moved:
            lines.append(f"- {r['url']} — {r['note']}")

    report = "\n".join(lines) + "\n"
    if args.out:
        pathlib.Path(args.out).write_text(report, encoding="utf-8")
    else:
        print(report)

    print(f"要対応: {len(problems)} 件 / 既知の例外 {len(known)} 件 / リダイレクト {len(moved)} 件", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
