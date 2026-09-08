"""
複数ウェブページの更新をチェックし、変化があればLINEに通知する。
GitHub Actionsのcronから一回だけ実行される想定（常駐はしない）。
"""
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "..", "config", "targets.json")
STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "state", "state.json")

FETCH_ERROR_NOTIFY_THRESHOLD = 3       # ネットワーク不調は3回連続してから通知（一時的な障害の可能性があるため）
STRUCTURE_ERROR_NOTIFY_THRESHOLD = 1   # 構造変化は1回で即通知（サイトリニューアル等を早期に検知するため）
FETCH_RETRIES = 3
FETCH_TIMEOUT_SECONDS = 15
FETCH_BACKOFF_SECONDS = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def migrate_state_entry(entry):
    """旧スキーマ(has_link/link_url)を新スキーマ(signature)に変換する。
    既に新スキーマなら不足フィールドのみ補完する。"""
    if "signature" in entry:
        entry.setdefault("consecutive_structure_errors", 0)
        entry.setdefault("structure_error_notified", False)
        entry.setdefault("last_error_type", None)
        return entry

    has_link = entry.get("has_link", False)
    link_url = entry.get("link_url")
    return {
        "initialized": entry.get("initialized", False),
        "last_checked_at": entry.get("last_checked_at"),
        "signature": link_url if has_link else None,
        "consecutive_errors": entry.get("consecutive_errors", 0),
        "consecutive_structure_errors": 0,
        "last_error": entry.get("last_error"),
        "last_error_type": "fetch_error" if entry.get("last_error") else None,
        "error_notified": entry.get("error_notified", False),
        "structure_error_notified": False,
    }


def load_state():
    """state.jsonを読み込み、旧スキーマのentryは自動的に新スキーマへ変換する。
    戻り値: (state, migrated_any) — migrated_anyは1件でも変換が発生したか"""
    if not os.path.exists(STATE_FILE):
        return {}, False
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        raw_state = json.load(f)
    migrated_any = any("signature" not in v for v in raw_state.values())
    state = {name: migrate_state_entry(entry) for name, entry in raw_state.items()}
    return state, migrated_any


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_page(url):
    """リトライ・タイムアウト付きでページを取得する。失敗時は例外を投げる。"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    last_error = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            response = requests.get(url, headers=headers, timeout=FETCH_TIMEOUT_SECONDS)
            response.encoding = "utf-8"
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            last_error = e
            logger.warning(f"Fetch attempt {attempt}/{FETCH_RETRIES} failed for {url}: {e}")
            if attempt < FETCH_RETRIES:
                time.sleep(FETCH_BACKOFF_SECONDS * attempt)
    raise last_error


def compute_link_href_signature(html, container_selector, item_selector, text_keywords):
    """container_selector配下のitem_selectorをtext_keywordsで検索し、対象リンクのhrefをsignatureとする。
    signature: リンクが有効ならhref文字列、無効ならNone。"""
    soup = BeautifulSoup(html, "html.parser")

    container = soup.select_one(container_selector)
    if not container:
        return {
            "found": False,
            "signature": None,
            "debug_info": {"container_found": False, "item_count": 0, "has_link": False, "link_url": None},
        }

    items = container.select(item_selector)
    item_count = len(items)
    target_link = None
    for item in items:
        link = item.find("a")
        text = item.get_text(strip=True)
        if text_keywords and any(keyword in text for keyword in text_keywords):
            target_link = link
            break
        elif not text_keywords and link:
            target_link = link
            break

    if not target_link:
        return {
            "found": False,
            "signature": None,
            "debug_info": {"container_found": True, "item_count": item_count, "has_link": False, "link_url": None},
        }

    href = target_link.get("href", "")
    has_link = bool(href)
    return {
        "found": True,
        "signature": href if has_link else None,
        "debug_info": {"container_found": True, "item_count": item_count, "has_link": has_link, "link_url": href if has_link else None},
    }


def compute_selector_hash_signature(html, selector):
    """selector配下のHTML断片をsha256化してsignatureとする。"""
    soup = BeautifulSoup(html, "html.parser")
    element = soup.select_one(selector)
    if not element:
        return {"found": False, "signature": None, "debug_info": {"selector_matched": False}}
    content = str(element)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {"found": True, "signature": digest, "debug_info": {"selector_matched": True, "content_length": len(content)}}


def compute_full_text_hash_signature(html):
    """<body>全体のテキストをsha256化してsignatureとする。"""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("body")
    if not body:
        return {"found": False, "signature": None, "debug_info": {"body_found": False}}
    text = body.get_text(separator="\n", strip=True)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {"found": True, "signature": digest, "debug_info": {"body_found": True, "text_length": len(text)}}


def validate_monitor(monitor):
    """watch_typeごとに必須フィールドが揃っているか事前検証する。"""
    watch_type = monitor.get("watch_type", "link_href")
    if watch_type == "link_href":
        required = ["container_selector", "item_selector"]
    elif watch_type == "selector_hash":
        required = ["selector"]
    elif watch_type == "full_text_hash":
        required = []
    else:
        raise ValueError(f"未知のwatch_type: {watch_type}")
    missing = [key for key in required if key not in monitor]
    if missing:
        raise ValueError(f"watch_type={watch_type}に必要なフィールドが不足しています: {missing}")


def evaluate_watch(html, monitor):
    """monitorのwatch_typeに応じて適切な抽出関数にディスパッチする。"""
    watch_type = monitor.get("watch_type", "link_href")
    if watch_type == "link_href":
        return compute_link_href_signature(
            html, monitor["container_selector"], monitor["item_selector"], monitor.get("text_keywords", [])
        )
    elif watch_type == "selector_hash":
        return compute_selector_hash_signature(html, monitor["selector"])
    elif watch_type == "full_text_hash":
        return compute_full_text_hash_signature(html)
    raise ValueError(f"未知のwatch_type: {watch_type}")


def has_update(watch_type, old_signature, new_signature):
    """前回signatureと今回signatureを比較し、通知すべき変化かどうかを判定する。"""
    if watch_type == "link_href":
        # 「リンク無し→リンク有り」の遷移のみ通知する（既にリンクがある状態でURLだけ変わっても通知しない）
        return old_signature is None and new_signature is not None
    # selector_hash / full_text_hash: 値が変化したら通知
    return old_signature is not None and new_signature is not None and old_signature != new_signature


def format_debug_info(watch_type, debug_info):
    if watch_type == "link_href":
        container_state = "あり" if debug_info.get("container_found") else "なし"
        return (
            f"container_selectorに一致した要素: {container_state} / "
            f"item数: {debug_info.get('item_count', 0)}"
        )
    if watch_type == "selector_hash":
        return f"selectorに一致した要素: {'あり' if debug_info.get('selector_matched') else 'なし'}"
    if watch_type == "full_text_hash":
        return f"<body>要素: {'あり' if debug_info.get('body_found') else 'なし'} / 文字数: {debug_info.get('text_length', 0)}"
    return str(debug_info)


def send_line_notification(text):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.environ.get("LINE_USER_ID")
    if not token or not user_id:
        raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN または LINE_USER_ID が設定されていません")

    if os.environ.get("DRY_RUN") == "true":
        logger.info(f"[DRY_RUN] LINE通知（送信スキップ）:\n{text}")
        return

    response = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"to": user_id, "messages": [{"type": "text", "text": text}]},
        timeout=FETCH_TIMEOUT_SECONDS,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"LINE API error: {response.status_code} {response.text}")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def is_due(entry, interval_minutes, force_run_all):
    if force_run_all or entry is None or not entry.get("initialized"):
        return True
    last_checked_at = entry.get("last_checked_at")
    if not last_checked_at:
        return True
    elapsed_seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(last_checked_at)).total_seconds()
    return elapsed_seconds >= interval_minutes * 60


def build_ok_entry(signature):
    return {
        "initialized": True,
        "last_checked_at": now_iso(),
        "signature": signature,
        "consecutive_errors": 0,
        "consecutive_structure_errors": 0,
        "last_error": None,
        "last_error_type": None,
        "error_notified": False,
        "structure_error_notified": False,
    }


def build_notification_text(monitor, result):
    watch_type = monitor.get("watch_type", "link_href")
    title = monitor.get("notification_title", "📋 更新が検出されました！")
    timestamp = datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")
    if watch_type == "link_href":
        return f"{title}\n\n🔗 {result['debug_info'].get('link_url')}\n\n📅 確認日時: {timestamp}"
    return f"{title}\n\n🔄 コンテンツの変化を検知しました\n\n📅 確認日時: {timestamp}"


def check_one(monitor, entry):
    """1つの監視対象をチェックし、新しいstate entryを返す。通知が必要ならここで送信する。"""
    name = monitor["name"]
    url = monitor["url"]
    watch_type = monitor.get("watch_type", "link_href")

    try:
        html = fetch_page(url)
    except requests.RequestException as e:
        logger.error(f"[{name}] ページ取得に失敗しました: {e}")
        return handle_error(entry, f"fetch failed: {e}", name, error_type="fetch_error")

    result = evaluate_watch(html, monitor)
    if not result["found"]:
        logger.error(f"[{name}] 監視対象の要素が見つかりません: {format_debug_info(watch_type, result['debug_info'])}")
        return handle_error(
            entry, "target element not found", name,
            error_type="structure_error", debug_info=result["debug_info"], watch_type=watch_type,
        )

    new_signature = result["signature"]

    # 初回はbaselineとして保存するのみで通知しない
    if entry is None or not entry.get("initialized"):
        logger.info(f"[{name}] 初回チェック: baselineを保存します (signature={new_signature})")
        return build_ok_entry(new_signature)

    old_signature = entry.get("signature")
    if has_update(watch_type, old_signature, new_signature):
        logger.info(f"[{name}] 更新を検知しました (signature={new_signature})")
        send_line_notification(build_notification_text(monitor, result))
    else:
        logger.info(f"[{name}] 変化なし (signature={new_signature})")

    return build_ok_entry(new_signature)


def handle_error(entry, error_message, name, error_type="fetch_error", debug_info=None, watch_type=None):
    """フェッチ/抽出エラー時: signatureも last_checked_atも更新しない。
    エラー種別ごとに連続回数を分けてカウントし、閾値到達時に1度だけ通知する。"""
    base = entry or {
        "initialized": False,
        "last_checked_at": None,
        "signature": None,
        "consecutive_errors": 0,
        "consecutive_structure_errors": 0,
        "last_error": None,
        "last_error_type": None,
        "error_notified": False,
        "structure_error_notified": False,
    }
    base.setdefault("consecutive_structure_errors", 0)
    base.setdefault("structure_error_notified", False)

    new_entry = dict(base)
    new_entry["last_error"] = error_message
    new_entry["last_error_type"] = error_type

    if error_type == "structure_error":
        consecutive = base.get("consecutive_structure_errors", 0) + 1
        new_entry["consecutive_structure_errors"] = consecutive
        threshold, notified_key = STRUCTURE_ERROR_NOTIFY_THRESHOLD, "structure_error_notified"
    else:
        consecutive = base.get("consecutive_errors", 0) + 1
        new_entry["consecutive_errors"] = consecutive
        threshold, notified_key = FETCH_ERROR_NOTIFY_THRESHOLD, "error_notified"

    if consecutive >= threshold and not base.get(notified_key, False):
        try:
            if error_type == "structure_error":
                debug_str = format_debug_info(watch_type, debug_info or {})
                message = (
                    f"🛠 監視対象の構造変化を検知した可能性: {name}\n"
                    f"{consecutive}回連続で対象要素が見つかりません。サイトのリニューアル等でセレクタが合わなくなった可能性があります。\n\n"
                    f"[デバッグ情報]\n{debug_str}\n\n"
                    f"config/targets.jsonのセレクタ設定を見直してください。"
                )
            else:
                message = f"⚠️ 監視エラー: {name}\n{consecutive}回連続でチェックに失敗しています。\n{error_message}"
            send_line_notification(message)
            new_entry[notified_key] = True
        except Exception as e:
            logger.error(f"[{name}] エラー通知の送信にも失敗しました: {e}")

    return new_entry


def main():
    config = load_config()
    state, migrated_any = load_state()

    force_run_all = os.environ.get("FORCE_RUN_ALL") == "true"
    target_name_filter = os.environ.get("TARGET_NAME")
    dry_run = os.environ.get("DRY_RUN") == "true"

    changed = migrated_any  # 旧スキーマからの移行が発生していれば、対象のdue判定に関わらず必ず保存する
    for monitor in config.get("monitors", []):
        name = monitor["name"]
        if not monitor.get("enabled", True):
            continue
        if target_name_filter and name != target_name_filter:
            continue

        try:
            validate_monitor(monitor)
        except ValueError as e:
            logger.error(f"[{name}] 設定エラー: {e}")
            continue

        entry = state.get(name)
        if not is_due(entry, monitor["check_interval_minutes"], force_run_all):
            logger.info(f"[{name}] チェック間隔未経過のためスキップします")
            continue

        try:
            new_entry = check_one(monitor, entry)
        except Exception as e:
            logger.error(f"[{name}] 予期しないエラー: {e}")
            new_entry = handle_error(entry, str(e), name)

        state[name] = new_entry
        changed = True

    if changed and not dry_run:
        save_state(state)
        logger.info("state.jsonを更新しました")
    elif changed and dry_run:
        logger.info("[DRY_RUN] state.jsonへの書き込みはスキップしました")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"致命的なエラーが発生しました: {e}")
        sys.exit(1)
