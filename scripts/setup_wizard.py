"""
新しい監視対象をconfig/targets.jsonに追加するためのローカル開発者向けウィザード。

GitHub Actionsの実行フロー（src/monitor.py, .github/workflows/watch.yml）には
一切関与しない。LINEトークン等のSecretsもここでは扱わない（環境変数管理は変わらず）。

使い方:
    python scripts/setup_wizard.py
"""
import json
import os
import sys

import requests
from bs4 import BeautifulSoup

# Windowsのコンソール(cp932等)でも絵文字混じりの出力でクラッシュしないようにする
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "..", "config", "targets.json")
DEFAULT_INTERVAL_MINUTES = 30  # 現行のcron間隔(.github/workflows/watch.yml)に合わせたデフォルト


def fetch_html(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    response = requests.get(url, headers=headers, timeout=15)
    response.encoding = "utf-8"
    response.raise_for_status()
    return response.text


def build_css_selector(element):
    """要素からCSSセレクタを生成する（id優先、なければclass、最大3階層）。"""
    parts = []
    current = element
    while current and getattr(current, "name", None):
        class_attr = current.get("class")
        id_attr = current.get("id")
        if id_attr:
            parts.insert(0, f"{current.name}#{id_attr}")
            break
        elif class_attr:
            class_str = " ".join(class_attr) if isinstance(class_attr, list) else class_attr
            parts.insert(0, f"{current.name}.{class_str.replace(' ', '.')}")
        else:
            parts.insert(0, current.name)
        current = current.parent
        if len(parts) >= 3:
            break
    return " > ".join(parts) if parts else element.name


def find_keyword_candidates(html, keyword):
    """keywordを含むテキストを持つ要素を探し、各候補についてセレクタと親要素候補を返す。"""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for element in soup.find_all(["a", "li", "div", "article", "section", "button"]):
        text = element.get_text(strip=True)
        if keyword not in text:
            continue
        parents = []
        current = element
        for _ in range(5):
            current = current.parent
            if current and getattr(current, "name", None):
                parents.append({"selector": build_selector_for_parent(current)})
        results.append({
            "element": element,
            "text": text,
            "tag": element.name,
            "has_link": bool(element.find("a") or element.name == "a"),
            "selector": build_css_selector(element),
            "parents": parents[:3],
        })
    # テキストが短い（＝キーワードに近い、より具体的な）要素を優先する。
    # 大きな親要素（ヘッダー全体など）がたまたまキーワードを含むテキストを持つ場合、
    # それが候補の上位に出て紛らわしくなるのを避けるため。
    results.sort(key=lambda r: len(r["text"]))
    return results


def build_selector_for_parent(element):
    return build_css_selector(element)


def print_candidates(results):
    for i, result in enumerate(results[:3], 1):
        print(f"\n【候補 {i}】")
        print(f"  テキスト: {result['text'][:50]}")
        print(f"  要素: {result['tag']}")
        print(f"  セレクター: {result['selector']}")
        print(f"  リンク有: {'✓' if result['has_link'] else '✗'}")
        if result["parents"]:
            print("  親要素:")
            for parent in result["parents"][:2]:
                print(f"    - {parent['selector']}")


def wizard_link_href(html):
    """組合せリンクのようなhref変化を検知する設定を作る。"""
    keyword = input("\n監視対象要素のキーワード（例: 組合せ）: ").strip()
    if not keyword:
        container_selector = input("コンテナセレクター（例: div.entry_items）: ").strip()
        item_selector = input("アイテムセレクター（例: li）[デフォルト: li]: ").strip() or "li"
        return {"container_selector": container_selector, "item_selector": item_selector, "text_keywords": []}

    results = find_keyword_candidates(html, keyword)
    if not results:
        print(f"⚠️  '{keyword}' を含む要素が見つかりませんでした。手動で入力してください。")
        container_selector = input("コンテナセレクター（例: div.entry_items）: ").strip()
        item_selector = input("アイテムセレクター（例: li）[デフォルト: li]: ").strip() or "li"
        return {"container_selector": container_selector, "item_selector": item_selector, "text_keywords": [keyword]}

    print(f"\n✓ '{keyword}' を含む要素が {len(results)} 件見つかりました")
    print_candidates(results)

    choice = input("\nどの候補を使用しますか？（1-3）: ").strip()
    if choice in ("1", "2", "3") and int(choice) <= len(results):
        selected = results[int(choice) - 1]
        container_selector = selected["selector"]
        if selected["parents"]:
            print("\n親要素の候補（コンテナとして使う場合）:")
            for i, parent in enumerate(selected["parents"][:2], 1):
                print(f"  {i}. {parent['selector']}")
            parent_choice = input("コンテナとして使用する親要素（1-2、スキップ可）: ").strip()
            if parent_choice in ("1", "2") and int(parent_choice) <= len(selected["parents"]):
                container_selector = selected["parents"][int(parent_choice) - 1]["selector"]
    else:
        container_selector = input("コンテナセレクター（例: div.entry_items）: ").strip()

    item_selector = input("アイテムセレクター（例: li）[デフォルト: li]: ").strip() or "li"
    keywords_str = input(f"テキストキーワード（カンマ区切り）[デフォルト: {keyword}]: ").strip()
    text_keywords = [k.strip() for k in keywords_str.split(",") if k.strip()] if keywords_str else [keyword]

    return {"container_selector": container_selector, "item_selector": item_selector, "text_keywords": text_keywords}


def wizard_selector_hash(html):
    """特定要素のHTML変化（価格・在庫など）を検知する設定を作る。"""
    keyword = input("\n監視対象要素を見つけるためのキーワード（例: 価格）: ").strip()
    if keyword:
        results = find_keyword_candidates(html, keyword)
        if results:
            print(f"\n✓ '{keyword}' を含む要素が {len(results)} 件見つかりました")
            print_candidates(results)
            choice = input("\nどの候補のセレクターを使用しますか？（1-3、スキップして手動入力も可）: ").strip()
            if choice in ("1", "2", "3") and int(choice) <= len(results):
                return {"selector": results[int(choice) - 1]["selector"]}
        else:
            print(f"⚠️  '{keyword}' を含む要素が見つかりませんでした。手動で入力してください。")
    selector = input("監視したい要素のCSSセレクター（例: span.price, #stock-status）: ").strip()
    return {"selector": selector}


def wizard_full_text_hash():
    """ページ全体のテキスト変化を検知する。追加設定は不要。"""
    print("\nページ全体（<body>）のテキストのハッシュ比較で変化を検知します。追加の設定は不要です。")
    return {}


def load_targets():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"monitors": []}


def save_targets(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def main():
    print("=" * 60)
    print("tennis-draw-monitor セットアップウィザード")
    print("=" * 60)

    name = input("\n監視対象の名前（state.jsonのキーになるので重複不可）: ").strip()
    if not name:
        print("名前は必須です。中止しました。")
        return

    url = input("監視対象のURL: ").strip()
    if not url.startswith("http"):
        url = "https://" + url

    print("\nページを取得中...")
    try:
        html = fetch_html(url)
    except requests.RequestException as e:
        print(f"❌ ページ取得に失敗しました: {e}")
        return
    print("✓ ページを取得しました")

    print("\n監視方式を選んでください:")
    print("  1. link_href      : 特定要素がリンク化されたら通知（大会組み合わせ等）")
    print("  2. selector_hash  : 特定要素のHTML変化を検知（価格・在庫等）")
    print("  3. full_text_hash : ページ全体のテキスト変化を検知")
    choice = input("番号を選択 [デフォルト: 1]: ").strip() or "1"
    watch_type = {"1": "link_href", "2": "selector_hash", "3": "full_text_hash"}.get(choice, "link_href")

    if watch_type == "link_href":
        extra = wizard_link_href(html)
    elif watch_type == "selector_hash":
        extra = wizard_selector_hash(html)
    else:
        extra = wizard_full_text_hash()

    interval_str = input(f"\nチェック間隔（分）[デフォルト: {DEFAULT_INTERVAL_MINUTES}]: ").strip()
    try:
        check_interval = int(interval_str) if interval_str else DEFAULT_INTERVAL_MINUTES
    except ValueError:
        check_interval = DEFAULT_INTERVAL_MINUTES

    notification_title = input("通知メッセージ [デフォルト: 📋 更新が検出されました！]: ").strip() or "📋 更新が検出されました！"

    monitor_entry = {
        "name": name,
        "url": url,
        "enabled": True,
        "check_interval_minutes": check_interval,
        "watch_type": watch_type,
        "notification_title": notification_title,
        **extra,
    }

    print("\n--- 追加される監視設定 ---")
    print(json.dumps(monitor_entry, ensure_ascii=False, indent=2))

    config = load_targets()
    existing_names = [m["name"] for m in config.get("monitors", [])]
    if name in existing_names:
        overwrite = input(f"\n'{name}' は既に存在します。上書きしますか？(y/n): ").strip().lower()
        if overwrite != "y":
            print("中止しました。config/targets.jsonは変更していません。")
            return
        config["monitors"] = [m for m in config["monitors"] if m["name"] != name]

    confirm = input("\nこの内容でconfig/targets.jsonに追記しますか？(y/n) [デフォルト: n]: ").strip().lower()
    if confirm != "y":
        print("中止しました。config/targets.jsonは変更していません。")
        return

    config.setdefault("monitors", []).append(monitor_entry)
    save_targets(config)
    print("\n✓ config/targets.jsonに追記しました。")
    print("  git diffで内容を確認してからコミットしてください（自動コミットはしません）。")

    if check_interval < DEFAULT_INTERVAL_MINUTES:
        print(
            f"\n⚠️  注意: check_interval_minutes={check_interval}分は現在のcron間隔"
            f"（{DEFAULT_INTERVAL_MINUTES}分毎）より短いです。"
            ".github/workflows/watch.yml のcron式を見直すことを検討してください"
            "（このウィザードは自動編集していません）。"
        )


if __name__ == "__main__":
    main()
