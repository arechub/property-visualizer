"""PropertyVisualizer - リフォームシミュレーター（Streamlit版）v0.9"""

import base64
import csv
import io
import json
import os
from datetime import date
from pathlib import Path

import anthropic
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from streamlit_paste_button import paste_image_button as pbutton

# ── 使用制限（ここを変更するだけで上限を調整可） ───────────
GUEST_SESSION_LIMIT = 5  # ゲスト：1セッションあたりの解析上限

# ── その他定数 ───────────────────────────────────────────────
WALL_AREA_FACTOR = 2.5
PATTERN_TIERS = {'A': 1, 'B': 2, 'C': 3}

ROOM_COUNTS = {
    '1R': 1, '1K': 1, '1DK': 1, '1LDK': 2,
    '2K': 2, '2DK': 2, '2LDK': 3,
    '3DK': 3, '3LDK': 4, '4LDK': 5,
}
CLEANING_COSTS = {
    '1R': 30000, '1K': 33000, '1DK': 38000, '1LDK': 45000,
    '2K': 48000, '2DK': 52000, '2LDK': 60000,
    '3DK': 65000, '3LDK': 75000, '4LDK': 90000,
}

CAT_LABELS = {
    'A': 'A：必須（原状回復）',
    'B': 'B：競争力向上',
    'C': 'C：商品化',
    'D': 'Custom項目',
}

PATTERN_DESC = {
    'A': 'クロス・CF・クリーニングのみ。退去後の最低限リフォーム。',
    'B': 'AにLED・温水洗浄便座・モニターホン・鍵交換を追加。賃貸募集力アップ。',
    'C': 'BにキッチンUB・給湯器・洗面台を追加。売却・フルリフォーム想定。',
    'Custom': '全項目からチェックで自由に選択。',
}

MADORI_LIST = ['1R', '1K', '1DK', '1LDK', '2K', '2DK', '2LDK', '3DK', '3LDK', '4LDK']

SCRIPT_DIR = Path(__file__).parent
CSV_PATH = SCRIPT_DIR / '20260612_master_prices.csv'
LOG_PATH = SCRIPT_DIR / 'renovation_log.csv'
LOG_FIELDS = ['date', 'area', 'madori', 'pattern', 'estimated', 'actual', 'ratio', 'note']

load_dotenv(SCRIPT_DIR / '.env')


# ── データ読み込み ────────────────────────────────────────────
@st.cache_data
def load_items():
    with open(CSV_PATH, encoding='utf-8') as f:
        return list(csv.DictReader(f))


# ── Vision API 解析 ───────────────────────────────────────────
def analyze_floor_plan(image):
    """Claude Vision API で間取り図（画像・PDF）を解析し辞書を返す"""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("APIキーが設定されていません。.env ファイルを確認してください。")

    model = os.environ.get("MADORI_MODEL", "claude-haiku-4-5-20251001")

    prompt = (
        "この間取り図を分析し、以下のJSON形式のみで返してください（前後の説明不要）：\n"
        '{"madori":"間取りタイプ（例：1K, 1LDK, 2LDK, 3LDK）",'
        '"rooms":["部屋リスト（例：LDK, 洋室, 洋室, 浴室, トイレ, 洗面所）"],'
        '"notes":"特記事項（広さの特徴など、なければ空文字）"}'
    )

    is_pdf = hasattr(image, 'type') and image.type == 'application/pdf'

    if is_pdf:
        pdf_bytes = image.read()
        image.seek(0)
        content = [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(pdf_bytes).decode('utf-8'),
                },
            },
            {"type": "text", "text": prompt},
        ]
    else:
        if hasattr(image, 'read'):
            image_bytes = image.read()
            image.seek(0)
            media_type = getattr(image, 'type', 'image/jpeg')
            if media_type not in ('image/png', 'image/jpeg', 'image/gif', 'image/webp'):
                media_type = 'image/jpeg'
        else:  # PIL Image（スクショ貼り付け）
            buf = io.BytesIO()
            image.save(buf, format='PNG')
            image_bytes = buf.getvalue()
            media_type = 'image/png'
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(image_bytes).decode('utf-8'),
                },
            },
            {"type": "text", "text": prompt},
        ]

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": content}],
    )

    text = response.content[0].text.strip()
    if '```' in text:
        text = text.split('```')[1]
        if text.startswith('json'):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


# ── 計算 ──────────────────────────────────────────────────────
def calc_quantity(row, areas, room_count):
    basis = row['quantity_basis']
    if basis == 'wall_area':
        return areas['wall']
    elif basis in ('floor_area', 'ceiling_area'):
        return areas['floor']
    elif basis == 'room_count':
        return room_count
    elif basis == 'fixed':
        return float(row['fixed_qty'])
    return 0


def calculate(area, madori, items):
    areas = {'floor': area, 'wall': round(area * WALL_AREA_FACTOR, 1)}
    room_count = ROOM_COUNTS.get(madori.upper(), 2)
    cleaning_cost = CLEANING_COSTS.get(madori.upper(), 55000)

    results = []
    for row in items:
        if row['quantity_basis'] == 'cleaning':
            qty, unit_price = 1, cleaning_cost
        else:
            qty = calc_quantity(row, areas, room_count)
            unit_price = float(row['unit_price'])
        results.append({
            'category': row['category'],
            '項目': row['item_name'],
            '数量': round(qty, 1),
            '単位': row['unit'],
            '単価': int(unit_price),
            '金額': int(unit_price * qty),
        })
    return results


def build_html_table(results):
    df = pd.DataFrame(results)
    total = df['金額'].sum()

    th = 'style="padding:8px 12px; text-align:{align}; background:#444; color:white;"'
    td = 'style="padding:7px 12px; text-align:{align}; border-bottom:1px solid #e0e0e0;"'
    td_sub = 'style="padding:7px 12px; text-align:{align}; background:#eeeeee; font-weight:bold;"'
    td_cat = 'style="padding:7px 12px; background:#666666; color:white; font-weight:bold;"'
    td_tot = 'style="padding:10px 12px; text-align:{align}; background:#333333; color:white; font-weight:bold; font-size:15px;"'

    rows = ['<table style="width:100%; border-collapse:collapse; font-size:14px;">']
    rows.append(
        '<thead><tr>'
        f'<th {th.format(align="left")}>項目</th>'
        f'<th {th.format(align="right")}>数量</th>'
        f'<th {th.format(align="left")}>単位</th>'
        f'<th {th.format(align="right")}>単価</th>'
        f'<th {th.format(align="right")}>金額</th>'
        '</tr></thead><tbody>'
    )
    for cat in sorted(df['category'].unique()):
        cat_df = df[df['category'] == cat]
        subtotal = int(cat_df['金額'].sum())
        rows.append(f'<tr><td colspan="5" {td_cat}>{CAT_LABELS.get(cat, cat)}</td></tr>')
        for i, (_, row) in enumerate(cat_df.iterrows()):
            bg = '#fafafa' if i % 2 == 0 else 'white'
            rows.append(
                f'<tr style="background:{bg};">'
                f'<td {td.format(align="left")}>{row["項目"]}</td>'
                f'<td {td.format(align="right")}>{row["数量"]}</td>'
                f'<td {td.format(align="left")}>{row["単位"]}</td>'
                f'<td {td.format(align="right")}>¥{row["単価"]:,}</td>'
                f'<td {td.format(align="right")}>¥{row["金額"]:,}</td>'
                f'</tr>'
            )
        rows.append(
            f'<tr><td colspan="4" {td_sub.format(align="right")}>小計</td>'
            f'<td {td_sub.format(align="right")}>¥{subtotal:,}</td></tr>'
        )
    rows.append(
        f'<tr><td colspan="4" {td_tot.format(align="right")}>概算合計</td>'
        f'<td {td_tot.format(align="right")}>¥{total:,}</td></tr>'
    )
    rows.append('</tbody></table>')
    return '\n'.join(rows), int(total)


def save_log(area, madori, pattern, estimated, actual=None, note=''):
    is_new = not LOG_PATH.exists()
    ratio = round(actual / estimated, 4) if actual else ''
    row = {
        'date': date.today().isoformat(),
        'area': area, 'madori': madori, 'pattern': pattern,
        'estimated': estimated, 'actual': actual or '', 'ratio': ratio, 'note': note,
    }
    with open(LOG_PATH, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


# ── メイン ────────────────────────────────────────────────────
def main():
    st.set_page_config(page_title="リフォームシミュレーター", layout="centered")

    # セッション初期化
    if 'floor_plan' not in st.session_state:
        st.session_state.floor_plan = None
        st.session_state.floor_plan_name = None
        st.session_state.analysis_result = None
        st.session_state.is_master = False
        st.session_state.session_analyses = 0
        st.session_state.comparison_quote = None

    # ── サイドバー：マスター認証 ──────────────────────────────
    with st.sidebar:
        st.write("**モード**")
        if st.session_state.is_master:
            st.success("マスターモード（解析無制限）")
            if st.button("ログアウト"):
                st.session_state.is_master = False
                st.rerun()
        else:
            remaining = GUEST_SESSION_LIMIT - st.session_state.session_analyses
            st.info(f"ゲストモード（解析残り {remaining} / {GUEST_SESSION_LIMIT} 回）")
            pwd = st.text_input("マスターパスワード", type="password")
            if st.button("認証"):
                if pwd and pwd == os.environ.get("MASTER_PASSWORD", ""):
                    st.session_state.is_master = True
                    st.rerun()
                else:
                    st.error("パスワードが違います")

    # ── ヘッダー ──────────────────────────────────────────────
    st.title("PropertyVisualizer")
    st.caption("リフォームシミュレーター — 業者見積りの概算チェックツール")
    st.divider()

    # ── STEP 1: 間取り図アップロード ─────────────────────────
    st.subheader("STEP 1　間取り図のアップロード")

    st.markdown("""
    <style>
    [data-testid="stFileUploader"] section {
        border: 2px dashed #aaa; border-radius: 10px;
        padding: 24px; background: #fafafa; text-align: center;
    }
    [data-testid="stFileUploader"] section:hover {
        border-color: #1f77b4; background: #f0f8ff;
    }
    </style>
    """, unsafe_allow_html=True)

    if st.session_state.floor_plan is None:
        uploaded = st.file_uploader(
            "ここに間取り図をドロップ、またはクリックして選択（PNG / JPG / PDF）",
            type=['png', 'jpg', 'jpeg', 'pdf'],
        )
        st.write("または")
        paste_result = pbutton(
            "スクリーンショットを貼り付け",
            background_color="#f0f2f6",
            hover_background_color="#dde1ea",
            errors="raise",
        )
        if uploaded is not None:
            st.session_state.floor_plan = uploaded
            st.session_state.floor_plan_name = uploaded.name
            st.rerun()
        if paste_result.image_data is not None:
            st.session_state.floor_plan = paste_result.image_data
            st.session_state.floor_plan_name = "クリップボードから貼り付け"
            st.rerun()
        st.caption("間取り図をアップロードすると解析ボタンが表示されます。")

    else:
        # プレビュー＋削除
        col_msg, col_btn = st.columns([5, 1])
        with col_msg:
            st.success(f"アップロード完了：{st.session_state.floor_plan_name}")
        with col_btn:
            if st.button("削除", use_container_width=True):
                st.session_state.floor_plan = None
                st.session_state.floor_plan_name = None
                st.session_state.analysis_result = None
                st.rerun()

        is_pdf = hasattr(st.session_state.floor_plan, 'type') and \
                 st.session_state.floor_plan.type == 'application/pdf'
        if is_pdf:
            st.info("PDF：解析ボタンで直接読み取ります。")
        else:
            st.image(st.session_state.floor_plan, use_column_width=True)

        # 解析ボタン / 結果表示
        if st.session_state.analysis_result is None:
            can_analyze = st.session_state.is_master or \
                          st.session_state.session_analyses < GUEST_SESSION_LIMIT

            if can_analyze:
                if st.button("間取りを解析する", type="primary"):
                    with st.spinner("解析中..."):
                        try:
                            result = analyze_floor_plan(st.session_state.floor_plan)
                            st.session_state.analysis_result = result
                            if not st.session_state.is_master:
                                st.session_state.session_analyses += 1
                            st.rerun()
                        except Exception as e:
                            st.error(f"解析エラー：{e}")
            else:
                st.warning(
                    f"ゲストの解析は1セッション {GUEST_SESSION_LIMIT} 回までです。"
                )
        else:
            result = st.session_state.analysis_result
            rooms_str = "・".join(result.get('rooms', []))
            st.info(f"解析結果：**{result.get('madori', '不明')}**　（{rooms_str}）")
            if result.get('notes'):
                st.caption(result['notes'])
            if st.button("再解析する"):
                st.session_state.analysis_result = None
                st.rerun()

    st.divider()

    # ── STEP 2: 物件情報の入力 ────────────────────────────────
    st.subheader("STEP 2　物件情報の入力")

    detected = (st.session_state.analysis_result or {}).get('madori', '').upper()
    default_idx = MADORI_LIST.index(detected) if detected in MADORI_LIST else 6

    col1, col2 = st.columns(2)
    with col1:
        area = st.number_input("専有面積（㎡）", min_value=10.0, max_value=200.0,
                               value=25.0, step=0.5)
    with col2:
        label = "間取り（解析結果から自動入力）" if detected in MADORI_LIST else "間取り"
        madori = st.selectbox(label, MADORI_LIST, index=default_idx)

    st.caption(f"壁面積概算：{area * WALL_AREA_FACTOR:.1f}㎡　/　床面積：{area}㎡")
    st.divider()

    # ── STEP 3: パターン選択 ──────────────────────────────────
    st.subheader("STEP 3　リフォームパターンの選択")

    pattern_labels = {
        'A': 'A：必須（原状回復）',
        'B': 'B：競争力向上（賃貸）',
        'C': 'C：商品化（売却向け）',
        'Custom': 'Custom：個別選択',
    }
    pattern_choice = st.radio(
        "リフォームパターン", list(pattern_labels.keys()),
        format_func=lambda x: pattern_labels[x],
        horizontal=True, label_visibility="collapsed",
    )
    st.caption(PATTERN_DESC[pattern_choice])

    all_items = load_items()

    if pattern_choice == 'Custom':
        st.write("**含める項目を選んでください**")
        groups: dict = {}
        for row in all_items:
            groups.setdefault(row['group'], []).append(row)
        selected = []
        cols = st.columns(3)
        for i, (group, items) in enumerate(groups.items()):
            with cols[i % 3]:
                st.write(f"*{group}*")
                for row in items:
                    if st.checkbox(row['item_name'], key=f"chk_{row['id']}"):
                        selected.append(row)
    else:
        tier = PATTERN_TIERS[pattern_choice]
        selected = [r for r in all_items if int(r['tier']) <= tier]

    st.divider()

    # ── 結果 ──────────────────────────────────────────────────
    if not selected:
        st.info("項目を選択してください。")
        return

    results = calculate(area, madori, selected)
    html_table, total = build_html_table(results)

    st.subheader(f"概算合計：¥{total:,}")
    st.markdown(html_table, unsafe_allow_html=True)
    st.divider()

    # ── STEP 4: 業者見積りと比較する ─────────────────────────
    st.subheader("STEP 4　業者見積りと比較する")

    quote = st.number_input("業者見積り金額（円）", min_value=0, value=0, step=10000)
    feedback = st.text_area(
        "フィードバック（任意）",
        placeholder="欲しい機能・追加してほしい項目・気になった点など、何でもどうぞ",
        height=80,
    )

    if quote > 0:
        if st.button("比較する", type="primary"):
            save_log(area, madori, pattern_choice, total, quote, note=feedback)
            st.session_state.comparison_quote = quote

        if st.session_state.comparison_quote == quote:
            ratio = quote / total * 100
            diff = abs(ratio - 100)
            if ratio >= 130:
                st.error(f"要確認（概算より{diff:.0f}%高い）　概算比：{ratio:.0f}%")
            elif ratio >= 110:
                st.warning(f"やや高め　概算比：{ratio:.0f}%")
            elif ratio <= 70:
                st.success(f"割安（概算より{diff:.0f}%低い）　概算比：{ratio:.0f}%")
            else:
                st.success(f"概ね適正　概算比：{ratio:.0f}%")
    else:
        st.caption("業者から見積りを取ったら入力してください。")


if __name__ == '__main__':
    main()
