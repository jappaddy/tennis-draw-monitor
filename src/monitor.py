"""
複数ウェブページの更新をチェックし、変化があればLINEに通知する。
GitHub Actionsのcronから一回だけ実行される想定（常駐はしない）。
"""
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

ERROR_NOTIFY_THRESHOLD = 3
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


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


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


def get_target_element_info(html, container_selector, item_selector, text_keywords):
    """container_selector配下のitem_selectorをtext_keywordsで検索し、対象リンクのhref情報を返す。"""
    soup = BeautifulSoup(html, "html.parser")

    container = soup.select_one(container_selector)
    if not container:
        return {"found": False, "has_link": False, "link_url": None}

    items = container.select(item_selector)
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
        return {"found": False, "has_link": False, "link_url": None}

    href = target_link.get("href", "")
    has_link = bool(href)
    return {"found": True, "has_link": has_link, "link_url": href if has_link else None}


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


def check_one(monitor, entry):
    """1つの監視対象をチェックし、新しいstate entryを返す。通知が必要ならここで送信する。"""
    name = monitor["name"]
    url = monitor["url"]

    try:
        html = fetch_page(url)
    except requests.RequestException as e:
        logger.error(f"[{name}] ページ取得に失敗しました: {e}")
        return handle_error(entry, f"fetch failed: {e}", name)

    info = get_target_element_info(
        html, monitor["container_selector"], monitor["item_selector"], monitor.get("text_keywords", [])
    )
    if not info["found"]:
        logger.error(f"[{name}] 監視対象の要素が見つかりません（セレクタ/キーワードを確認してください）")
        return handle_error(entry, "target element not found", name)

    # 初回はbaselineとして保存するのみで通知しない
    if entry is None or not entry.get("initialized"):
        logger.info(f"[{name}] 初回チェック: baselineを保存します (has_link={info['has_link']})")
        return {
            "initialized": True,
            "last_checked_at": now_iso(),
            "has_link": info["has_link"],
            "link_url": info["link_url"],
            "consecutive_errors": 0,
            "last_error": None,
            "error_notified": False,
        }

    link_activated = (not entry.get("has_link")) and info["has_link"]
    if link_activated:
        logger.info(f"[{name}] 更新を検知しました: {info['link_url']}")
        text = (
            f"{monitor.get('notification_title', '📋 更新が検出されました！')}\n\n"
            f"🔗 {info['link_url']}\n\n"
            f"📅 確認日時: {datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}"
        )
        send_line_notification(text)
    else:
        logger.info(f"[{name}] 変化なし (has_link={info['has_link']})")

    return {
        "initialized": True,
        "last_checked_at": now_iso(),
        "has_link": info["has_link"],
        "link_url": info["link_url"],
        "consecutive_errors": 0,
        "last_error": None,
        "error_notified": False,
    }


def handle_error(entry, error_message, name):
    """フェッチ/抽出エラー時: has_link・link_urlは変更せず、last_checked_atも更新しない。
    連続エラー回数だけ増やし、閾値到達時に1度だけ通知する。"""
    base = entry or {
        "initialized": False,
        "last_checked_at": None,
        "has_link": False,
        "link_url": None,
        "consecutive_errors": 0,
        "last_error": None,
        "error_notified": False,
    }
    consecutive_errors = base.get("consecutive_errors", 0) + 1
    new_entry = {
        **base,
        "consecutive_errors": consecutive_errors,
        "last_error": error_message,
    }

    if consecutive_errors >= ERROR_NOTIFY_THRESHOLD and not base.get("error_notified"):
        try:
            send_line_notification(
                f"⚠️ 監視エラー: {name}\n{consecutive_errors}回連続でチェックに失敗しています。\n{error_message}"
            )
            new_entry["error_notified"] = True
        except Exception as e:
            logger.error(f"[{name}] エラー通知の送信にも失敗しました: {e}")

    return new_entry


def main():
    config = load_config()
    state = load_state()

    force_run_all = os.environ.get("FORCE_RUN_ALL") == "true"
    target_name_filter = os.environ.get("TARGET_NAME")
    dry_run = os.environ.get("DRY_RUN") == "true"

    changed = False
    for monitor in config.get("monitors", []):
        name = monitor["name"]
        if not monitor.get("enabled", True):
            continue
        if target_name_filter and name != target_name_filter:
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
