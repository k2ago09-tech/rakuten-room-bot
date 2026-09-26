"""
毎日実行されるメインスクリプト。
1. 楽天ウェブサービスAPI(ランキング)でジャンルごとの売れ筋を取得
2. レビュー件数/評価でフィルタ + 過去投稿との重複除外 + 急上昇(順位変化)を優先
3. 選ばれた商品についてGemini APIで下書きコメントを生成
4. data/today.json に書き出し、data/history.json を更新
5. Discordにwebhookで通知

このスクリプトはGitHub Actionsから実行される想定(環境変数で認証情報を渡す)。
楽天ウェブサービスAPIは2026年5月のインフラ刷新でエンドポイント・認証方式が変わっており、
applicationId に加えて accessKey が必須になっている点に注意(このスクリプトは新方式に対応済み)。
"""

import json
import os
import random
import re
import time
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()  # ローカル実行時、リポジトリ直下の .env を読み込む(GitHub Actions上ではSecretsが直接環境変数として渡るため無害)

# ── 設定 ────────────────────────────────────────────────

RAKUTEN_APP_ID = os.environ["RAKUTEN_APP_ID"]
RAKUTEN_ACCESS_KEY = os.environ["RAKUTEN_ACCESS_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

# PWAの公開URL(GitHub Pagesのアドレス)
PWA_BASE_URL = os.environ.get("PWA_BASE_URL", "https://k2ago09-tech.github.io/rakuten-room-bot/")

# 楽天ウェブサービスのアプリ登録を「Web Application」タイプ(ドメイン制限)で行うため、
# バックエンドからのリクエストにも同じRefererヘッダーを付与する。
# IP制限(API/Backend Serviceタイプ)はGitHub Actionsのようにランナーの送信元IPが
# 毎回変わる環境では運用できないため、この方式を採用している。
RAKUTEN_REFERER = os.environ.get("RAKUTEN_REFERER", "https://k2ago09-tech.github.io/rakuten-room-bot/")
# Web Applicationタイプの登録では、Refererに加えてOriginヘッダーも必須(片方だけだと
# 403 REQUEST_CONTEXT_BODY_HTTP_REFERRER_MISSING になる)。Originは末尾スラッシュなしのオリジン形式。
RAKUTEN_HEADERS = {
    "Referer": RAKUTEN_REFERER,
    "Origin": RAKUTEN_REFERER.rstrip("/"),
}

RANKING_ENDPOINT = "https://openapi.rakuten.co.jp/ichibaranking/api/IchibaItem/Ranking/20220601"

GENRES = {
    "food": 100227,     # 食品・消耗品
    "zakka": 215783,    # 日用品雑貨・文房具・手芸
}

REVIEW_COUNT_MIN = 10
REVIEW_AVG_MIN = 3.5
POST_COUNT_PER_DAY = 2
CANDIDATES_PER_GENRE = 30  # ランキング取得件数(この中からフィルタ)

HISTORY_PATH = "data/history.json"        # 内部管理用(重複除外・順位履歴)。PWAからは読まない
TODAY_PATH = "docs/data/today.json"       # GitHub Pages(/docs)配下に置き、PWAから直接fetchする

JST = timezone(timedelta(hours=9))

DRAFT_PROMPT_TEMPLATE = """\
あなたは楽天ROOM(楽天市場のショッピングSNS)で商品紹介コメントを書く、毒舌だけど愛のあるツッコミキャラです。以下のルールを厳守してください。

■ キャラクター設定
- 基本は「毒舌ツッコミ系」。商品のお得さ・便利さに対して、ちょっと皮肉っぽく、でも最終的には好意的にツッコミを入れる口調
- 一人称は使わず、断定口調・体言止め・比喩を効果的に使う
- 語尾は「〜だ」「〜である」を基本としつつ、テンポよく崩す(丁寧語は使わない)

■ 絶対厳守のルール
1. 実際に商品を使った体験談を「装う」表現は禁止(嘘の一人称体験談を書かない)。あくまで商品情報・レビュー実績の整理として書くこと
2. 文字数は500字以内(厳守。超過は不可)
3. 最初の42文字に「価格」または「最大の売り」を必ず入れる(一覧表示で最初の42文字しか見えないため)。
   ただし、毎回同じ言い回し(「〇〇はバグ。」等の固定フレーズ)を使い回さないこと。
   商品や型に応じて、以下のようなフックの種類を毎回変えて使い分ける：
   - 誇張・断定型(例:「〇〇はもう、反則。」)
   - 疑問形で煽る型(例:「まだ〇〇で消耗してるの？」)
   - 数字の意外性を突く型(例:「〇〇円で〇〇が手に入る意味、わかる？」)
   - 直接語りかけ型(例:「〇〇で悩んでるなら、これ見て。」)
   - ネットスラング型(「バグ」「反則」「詐欺レベル」等)※多用しすぎない、目安6回に1回程度
4. 構成は「フック(掴み)→メリット→デメリット→デメリットのポジティブな言い換え→クロージング(締め)」の順
5. デメリット(気になる点の情報があれば)は必ず入れる。ただし直後に必ずポジティブな言い換えを添える
6. 絵文字は0〜3個程度、使いすぎない
7. 商品の説明文をそのままコピーしない。必ず自分の言葉に変換する
8. 以下の表現は禁止:「利益」「転売」「せどり」「買取」などの転売を連想させる言葉、および楽天ROOMの禁止ワード(例:「熱中症」など医療・健康に関する断定的表現)
9. 締めの一文は必ず「格付け:★の数(1〜5個、商品の魅力度に応じて調整)(〇〇部門・△△)」という1行を入れ、その直後に「以上、解散。」で終える。★の数と括弧内の部門名・順位表現は商品ごとに面白く即興で作ること

■ 型のローテーション
以下6パターンの中から、商品の性質に合わせて1つを選んで書いてください。0番(ベーストーン)を基本形として高頻度で選び、1〜5番は「たまに来る変化球」として低頻度で混ぜてください(目安:0番が半分程度、1〜5番合わせて残り半分)。

0. ベーストーン:上記キャラクター設定どおりのシンプルな毒舌ツッコミ文
1. 法廷コント風:商品を「被告」に見立て、無罪判決を下す寸劇形式
2. 格闘技実況風:商品と「悩み・不満」を対戦させる実況中継形式
3. 緊急ニュース速報風:商品の特徴を「速報」として報じるニュース形式
4. 取扱説明書パロディ風:「警告」「注意」「使用方法」など公文書調のフォーマット
5. RPGステータス画面風:商品を武器・アイテムのステータス画面に見立てる形式

※1〜5番の型を選んだ場合、冒頭のフックはその型の世界観の中で直接表現すること
(例:法廷コント風なら判事の宣言や罪状の読み上げそのものを冒頭フックにする)。
ベーストーン用のフックを型の前に別途つけ足さないこと。

■ 出力フォーマット
以下の形式で、他の説明文を一切加えずに出力してください。

型:[番号]
本文:[本文(500字以内)]

■ 商品情報
商品名:{item_name}
価格:{item_price}円
ジャンル:{genre_label}
商品の特徴:{caption}
レビュー実績:{review_count}件、評価{review_average}
気になりそうな点:{concern_note}
"""


def load_history():
    if not os.path.exists(HISTORY_PATH):
        return {"posted_item_codes": [], "rank_history": {}}
    with open(HISTORY_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_history(history):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def fetch_ranking(genre_id):
    params = {
        "applicationId": RAKUTEN_APP_ID,
        "accessKey": RAKUTEN_ACCESS_KEY,
        "genreId": genre_id,
        "format": "json",
        "formatVersion": 2,
    }
    resp = requests.get(RANKING_ENDPOINT, params=params, headers=RAKUTEN_HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    # レスポンスの配列キー名はAPIバージョンにより Items / items の揺れがあるため両対応
    items = data.get("Items") or data.get("items") or []
    # formatVersion=1相当のネスト({"Item": {...}})が返ってきた場合の保険
    return [it.get("Item", it) for it in items]


def guess_concern_note(item_name, caption):
    text = f"{item_name} {caption}"
    if "訳あり" in text or "わけあり" in text:
        return "訳あり品のため、サイズや形に個体差が出ることがある"
    if "在庫" in text or "数量限定" in text:
        return "数量限定のため早めに売り切れる可能性がある"
    return "特になし(価格・実績のわかりやすさで勝負する商品)"


def extract_image_url(item):
    urls = item.get("mediumImageUrls") or []
    if not urls:
        return ""
    first = urls[0]
    # レスポンスの形が {"imageUrl": "..."} の場合と、素の文字列の場合の両方に対応
    return first.get("imageUrl", "") if isinstance(first, dict) else first


def build_candidates(genre_key, genre_id, history):
    rank_history = history.setdefault("rank_history", {})
    posted = set(history.get("posted_item_codes", []))

    raw_items = fetch_ranking(genre_id)[:CANDIDATES_PER_GENRE]
    candidates = []

    for rank, item in enumerate(raw_items, start=1):
        item_code = item.get("itemCode")
        if not item_code or item_code in posted:
            continue

        # 楽天APIは数値項目も文字列で返してくることがあるため明示的にキャストする
        review_count = int(item.get("reviewCount") or 0)
        review_average = float(item.get("reviewAverage") or 0)
        item_price = int(item.get("itemPrice") or 0)
        if review_count < REVIEW_COUNT_MIN or review_average < REVIEW_AVG_MIN:
            continue

        prev_rank = rank_history.get(item_code)
        rank_delta = (prev_rank - rank) if prev_rank else 0  # 正の値=順位上昇(急上昇)

        candidates.append({
            "genre_key": genre_key,
            "genre_id": genre_id,
            "item_code": item_code,
            "item_name": item.get("itemName", ""),
            "item_price": item_price,
            "item_url": item.get("itemUrl", ""),
            "image_url": extract_image_url(item),
            "caption": (item.get("itemCaption") or "")[:200],
            "review_count": review_count,
            "review_average": review_average,
            "rank": rank,
            "rank_delta": rank_delta,
        })

        # 順位履歴を更新(次回の急上昇判定用)
        rank_history[item_code] = rank

    # 急上昇(rank_delta大)を優先しつつ、次点でレビュー実績を評価
    candidates.sort(key=lambda c: (c["rank_delta"], c["review_count"]), reverse=True)
    return candidates


def call_gemini(prompt_text, max_attempts=3):
    from google import genai
    from google.genai import errors as genai_errors

    client = genai.Client(api_key=GEMINI_API_KEY)

    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.models.generate_content(
                model="gemini-flash-latest",
                contents=prompt_text,
            )
            return response.text
        except genai_errors.ServerError as e:
            # 503(高負荷)等の一時的なサーバーエラーはリトライする
            last_error = e
            print(f"Gemini呼び出し失敗(試行{attempt}/{max_attempts}): {e}")
            if attempt < max_attempts:
                time.sleep(attempt * 10)  # 10s, 20s と間隔を空けて再試行
    raise last_error


def parse_gemini_output(raw_text):
    type_match = re.search(r"型[:：]\s*(\d+)", raw_text)
    body_match = re.search(r"本文[:：]\s*(.+)", raw_text, re.DOTALL)
    pattern_id = int(type_match.group(1)) if type_match else 0
    body = body_match.group(1).strip() if body_match else raw_text.strip()
    return pattern_id, body


def make_room_link(item_code):
    # item_code は "shopcode:itemnumber" 形式。room.rakuten.co.jp の投稿画面に直接ジャンプする。
    return f"https://room.rakuten.co.jp/mix?itemcode={item_code}"


def generate_draft_for(candidate):
    prompt = DRAFT_PROMPT_TEMPLATE.format(
        item_name=candidate["item_name"],
        item_price=candidate["item_price"],
        genre_label={"food": "食品・消耗品", "zakka": "日用品雑貨・掃除グッズ"}[candidate["genre_key"]],
        caption=candidate["caption"] or "(商品ページの説明を参照)",
        review_count=candidate["review_count"],
        review_average=candidate["review_average"],
        concern_note=guess_concern_note(candidate["item_name"], candidate["caption"]),
    )
    raw = call_gemini(prompt)
    pattern_id, body = parse_gemini_output(raw)
    return {
        "item_code": candidate["item_code"],
        "item_name": candidate["item_name"],
        "item_price": candidate["item_price"],
        "item_url": candidate["item_url"],
        "image_url": candidate["image_url"],
        "room_link": make_room_link(candidate["item_code"]),
        "pattern_id": pattern_id,
        "comment": body,
    }


def notify_discord(drafts, today_str):
    lines = [f"【本日の下書き {today_str}】"]
    for i, d in enumerate(drafts, start=1):
        lines.append(f"\n{i}. {d['item_name']}(¥{d['item_price']})")
    lines.append(f"\n確認・投稿はこちら → {PWA_BASE_URL}")
    payload = {"content": "\n".join(lines)}
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
    resp.raise_for_status()


def main():
    history = load_history()

    all_candidates = []
    for genre_key, genre_id in GENRES.items():
        all_candidates.extend(build_candidates(genre_key, genre_id, history))

    if not all_candidates:
        print("条件を満たす候補が見つかりませんでした。しきい値やジャンルを見直してください。")
        save_history(history)
        return

    # ジャンルが偏らないよう、上位から交互に選ぶ
    all_candidates.sort(key=lambda c: (c["rank_delta"], c["review_count"]), reverse=True)
    chosen = all_candidates[:POST_COUNT_PER_DAY]

    drafts = [generate_draft_for(c) for c in chosen]

    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    os.makedirs(os.path.dirname(TODAY_PATH), exist_ok=True)
    with open(TODAY_PATH, "w", encoding="utf-8") as f:
        json.dump({"date": today_str, "drafts": drafts}, f, ensure_ascii=False, indent=2)

    history.setdefault("posted_item_codes", [])
    history["posted_item_codes"].extend(d["item_code"] for d in drafts)
    save_history(history)

    notify_discord(drafts, today_str)


if __name__ == "__main__":
    main()
