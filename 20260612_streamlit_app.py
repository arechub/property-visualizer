"""PropertyVisualizer - リフォームシミュレーター（Streamlit版）v1.7"""

APP_VERSION = "v1.9"

import base64
import csv
import io
import json
import os
from datetime import date
from pathlib import Path

import anthropic
import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
from huggingface_hub import InferenceClient
from PIL import Image as PILImage
from streamlit_paste_button import paste_image_button as pbutton

# ── 使用制限（ここを変更するだけで上限を調整可） ───────────
GUEST_SESSION_LIMIT = 5

# ── 定数 ────────────────────────────────────────────────────────
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

MADORI_LIST   = ['1R', '1K', '1DK', '1LDK', '2K', '2DK', '2LDK', '3DK', '3LDK', '4LDK']
AGE_OPTIONS   = [
    '築5年未満', '築5〜10年', '築10〜15年', '築15〜20年',
    '築20〜25年', '築25〜30年', '築30〜40年', '築40年以上',
]
STRUCT_OPTIONS = ['RC造（鉄筋コンクリート）', '木造', '軽量鉄骨造', '重量鉄骨造']

IMAGE_STYLES = {
    'シック': {
        'prompt': (
            'replace all cabinet fronts with matte black cabinet doors, '
            'replace flooring with dark charcoal wood planks, '
            'paint walls dark slate gray, replace backsplash with black stone tiles, '
            'add under-cabinet LED lighting, moody dramatic atmosphere'
        ),
        'bg': '#3d3d3d', 'fg': 'white', 'desc': 'ダーク&ラグジュアリー',
        'photo': 'photos/20260612_style_chic.png',
    },
    '明るく': {
        'prompt': (
            'replace all cabinet fronts with glossy white cabinet doors, '
            'replace flooring with pale birch wood planks, '
            'paint walls pure white, replace backsplash with white subway tiles, '
            'add bright ceiling lights, cheerful open airy atmosphere'
        ),
        'bg': '#fff3cd', 'fg': '#555', 'desc': '白基調・開放感',
        'photo': 'photos/20260612_style_bright.png',
    },
    'ナチュラル': {
        'prompt': (
            'replace all cabinet fronts with light oak wood cabinet doors, '
            'replace flooring with warm honey-toned wood planks, '
            'paint walls warm cream beige, replace backsplash with beige textured tiles, '
            'add small potted plants on counter, warm soft lighting, Japandi minimalist'
        ),
        'bg': '#c8e6c9', 'fg': '#2e4a2e', 'desc': '木材・植物・温もり',
        'photo': 'photos/20260612_style_natural.png',
    },
    'モダン': {
        'prompt': (
            'replace all cabinet fronts with dark brown wood-grain cabinet doors, '
            'replace flooring with gray concrete-look tiles, '
            'paint walls light gray, replace backsplash with large format gray tiles, '
            'add stainless steel fixtures, sleek minimalist contemporary'
        ),
        'bg': '#9e9e9e', 'fg': 'white', 'desc': 'コンクリート調・スタイリッシュ',
        'photo': 'photos/20260612_style_modern.png',
    },
}

SCRIPT_DIR = Path(__file__).parent
CSV_PATH   = SCRIPT_DIR / '20260612_master_prices.csv'
LOG_PATH   = SCRIPT_DIR / 'renovation_log.csv'  # ローカル開発フォールバック用
LOG_FIELDS = ['date', 'area', 'madori', 'age', 'structure', 'pattern',
              'estimated', 'actual', 'ratio', 'note']

load_dotenv(SCRIPT_DIR / '.env')


@st.cache_resource
def get_gsheet():
    """Google Sheetsワークシートを返す。Secrets未設定時はNone（CSVフォールバック）。"""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds_info = st.secrets.get("gcp_service_account")
        sheet_id   = st.secrets.get("SPREADSHEET_ID")
        if not creds_info or not sheet_id:
            return None
        creds = Credentials.from_service_account_info(
            dict(creds_info),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        client = gspread.authorize(creds)
        return client.open_by_key(sheet_id).sheet1
    except Exception:
        return None


# ── データ読み込み ────────────────────────────────────────────
@st.cache_data
def load_items():
    with open(CSV_PATH, encoding='utf-8') as f:
        return list(csv.DictReader(f))


# ── Vision API 解析 ───────────────────────────────────────────
def analyze_floor_plan(image):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("APIキーが設定されていません。")

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
            {"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf",
                "data": base64.standard_b64encode(pdf_bytes).decode('utf-8'),
            }},
            {"type": "text", "text": prompt},
        ]
    else:
        if hasattr(image, 'read'):
            image_bytes = image.read()
            image.seek(0)
            media_type = getattr(image, 'type', 'image/jpeg')
            if media_type not in ('image/png', 'image/jpeg', 'image/gif', 'image/webp'):
                media_type = 'image/jpeg'
        else:
            buf = io.BytesIO()
            image.save(buf, format='PNG')
            image_bytes = buf.getvalue()
            media_type = 'image/png'
        content = [
            {"type": "image", "source": {
                "type": "base64", "media_type": media_type,
                "data": base64.standard_b64encode(image_bytes).decode('utf-8'),
            }},
            {"type": "text", "text": prompt},
        ]

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model, max_tokens=512,
        messages=[{"role": "user", "content": content}],
    )
    text = response.content[0].text.strip()
    if '```' in text:
        text = text.split('```')[1]
        if text.startswith('json'):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


# ── After画像生成 ────────────────────────────────────────────
# HUGGING_FACE_PRO=true のとき: FLUX.1-Kontext（img2img・構造保持）
# それ以外              : Claude Vision解析 → FLUX.1-schnell（無料）
def generate_after_image(photo_file, style_key):
    token   = os.environ.get("HUGGING_FACE_TOKEN")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    is_pro  = os.environ.get("HUGGING_FACE_PRO", "").lower() == "true"

    if not token:
        raise ValueError("HUGGING_FACE_TOKEN が設定されていません。")

    photo_bytes = photo_file.read()
    photo_file.seek(0)
    style_prompt = IMAGE_STYLES[style_key]['prompt']
    client = InferenceClient(token=token)

    if is_pro:
        # ── Pro: FLUX.1-Kontext（同じ部屋の仕上げだけ変換） ──
        pil_image = PILImage.open(io.BytesIO(photo_bytes)).convert("RGB")
        instruction = (
            f"Complete renovation of this room: {style_prompt}. "
            "Keep the exact same camera angle, room layout, ceiling height, "
            "window positions, and door positions. "
            "Do NOT change the architecture or room structure. "
            "The result must look clearly different from the original."
        )
        result = client.image_to_image(
            pil_image,
            prompt=instruction,
            model="black-forest-labs/FLUX.1-Kontext-dev",
        )
        buf = io.BytesIO()
        result.save(buf, format="PNG")
        return buf.getvalue()

    else:
        # ── Free: Claude Vision で部屋構造を分析 → FLUX.1-schnell ──
        media_type = getattr(photo_file, 'type', 'image/jpeg')
        if media_type not in ('image/png', 'image/jpeg', 'image/gif', 'image/webp'):
            media_type = 'image/jpeg'
        vision_model = os.environ.get("MADORI_MODEL", "claude-haiku-4-5-20251001")
        ai_client = anthropic.Anthropic(api_key=api_key)
        vision_resp = ai_client.messages.create(
            model=vision_model, max_tokens=80,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": media_type,
                    "data": base64.standard_b64encode(photo_bytes).decode('utf-8'),
                }},
                {"type": "text", "text": (
                    "Describe the fixed architecture of this Japanese apartment room in under 40 words. "
                    "Include: kitchen layout type, ceiling height, window/door positions, room size. "
                    "English only. Focus on structural elements that stay after renovation."
                )},
            ]}],
        )
        room_structure = vision_resp.content[0].text.strip()
        prompt = (
            f"photorealistic interior photo, Japanese apartment after renovation, "
            f"room structure: {room_structure}, "
            f"renovation: {style_prompt}, "
            "professional architectural photography, 8k, sharp focus, "
            "no people, no text, no watermark"
        )
        image = client.text_to_image(prompt, model="black-forest-labs/FLUX.1-schnell")
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()


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

    th    = 'style="padding:8px 12px; text-align:{align}; background:#444; color:white;"'
    td    = 'style="padding:7px 12px; text-align:{align}; border-bottom:1px solid #e0e0e0;"'
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
                '</tr>'
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


def save_log(area, madori, age, structure, pattern, estimated, actual=None, note='', log_date=None):
    ratio = round(actual / estimated, 4) if actual else ''
    row = {
        'date':      log_date or date.today().isoformat(),
        'area':      area,
        'madori':    madori,
        'age':       age,
        'structure': structure,
        'pattern':   pattern,
        'estimated': int(estimated),
        'actual':    int(actual) if actual else '',
        'ratio':     ratio,
        'note':      note,
    }
    ws = get_gsheet()
    if ws is not None:
        ws.append_row([row[f] for f in LOG_FIELDS])
    else:
        # ローカル開発フォールバック（Secrets未設定時）
        is_new = not LOG_PATH.exists()
        with open(LOG_PATH, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            if is_new:
                writer.writeheader()
            writer.writerow(row)


def load_log_df():
    ws = get_gsheet()
    if ws is not None:
        records = ws.get_all_records()
        if not records:
            return pd.DataFrame(columns=LOG_FIELDS)
        df = pd.DataFrame(records)
    elif LOG_PATH.exists():
        df = pd.read_csv(LOG_PATH, dtype=str)
    else:
        return pd.DataFrame(columns=LOG_FIELDS)
    df['estimated'] = pd.to_numeric(df['estimated'], errors='coerce')
    df['actual']    = pd.to_numeric(df['actual'], errors='coerce')
    df['ratio']     = df['actual'] / df['estimated']
    return df


def apply_price_adjustment(factors: dict):
    """factors = {'A': 1.10, 'B': 1.05, 'C': 1.0} のように渡す"""
    rows = []
    with open(CSV_PATH, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            cat = row['category']
            if cat in factors and factors[cat] != 1.0:
                new_price = round(float(row['unit_price']) * factors[cat])
                row['unit_price'] = str(new_price)
            rows.append(row)
    with open(CSV_PATH, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    load_items.clear()  # キャッシュをクリア


# ── メイン ────────────────────────────────────────────────────
def main():
    st.set_page_config(page_title="リフォームシミュレーター", layout="centered")

    if 'floor_plan' not in st.session_state:
        st.session_state.floor_plan      = None
        st.session_state.floor_plan_name = None
        st.session_state.analysis_result = None
        st.session_state.is_master       = False
        st.session_state.session_analyses = 0
        st.session_state.comparison_quote     = None
        st.session_state.room_image          = None
        st.session_state.room_image_style    = None
        st.session_state.feedback_sent       = False
        st.session_state.simulation_triggered = False
        st.session_state.property_photo      = None
        st.session_state.property_photo_name = None
        st.session_state.style_choice        = list(IMAGE_STYLES.keys())[0]

    # ── サイドバー：マスター認証 ──────────────────────────────
    with st.sidebar:
        st.write("**モード**")
        if st.session_state.is_master:
            st.success("マスターモード（解析無制限）")
            if st.button("ログアウト"):
                st.session_state.is_master = False
                st.rerun()

            # ── 過去案件の登録 ────────────────────────────────
            with st.expander("過去案件の登録"):
                with st.form("past_case_form"):
                    past_date = st.date_input("見積り日 または 発注日", value=None)
                    st.caption("※ 実際の見積り日・発注日を入力してください（必須）")
                    past_area = st.number_input("専有面積（㎡）", min_value=10.0,
                                                max_value=200.0, value=30.0, step=0.5)
                    past_madori   = st.selectbox("間取り", MADORI_LIST)
                    past_age      = st.selectbox("築年数", AGE_OPTIONS)
                    past_structure = st.selectbox("構造", STRUCT_OPTIONS)
                    past_pattern  = st.selectbox("パターン", ['A', 'B', 'C'])

                    # 概算を自動計算
                    _items = load_items()
                    _sel   = [r for r in _items if int(r['tier']) <= PATTERN_TIERS[past_pattern]]
                    _res   = calculate(past_area, past_madori, _sel)
                    _, past_estimated = build_html_table(_res)
                    st.metric("概算額（現マスター）", f"¥{past_estimated:,}")

                    past_actual = st.number_input("実発注額（円）", min_value=0,
                                                  value=0, step=10000)
                    submitted = st.form_submit_button("登録する", type="primary")
                    if submitted:
                        if past_date is None:
                            st.error("見積り日 または 発注日を入力してください。")
                        elif past_actual <= 0:
                            st.error("実発注額を入力してください。")
                        else:
                            save_log(past_area, past_madori, past_age, past_structure,
                                     past_pattern, past_estimated, past_actual,
                                     log_date=past_date.isoformat())
                            st.success("登録しました。")

            # ── 分析レポート ──────────────────────────────────
            with st.expander("分析レポート"):
                df_log = load_log_df()
                df_valid = df_log.dropna(subset=['actual', 'estimated'])
                df_valid = df_valid[df_valid['actual'] > 0].copy()

                if df_valid.empty:
                    st.caption("比較データがまだありません。")
                else:
                    # パターン別集計
                    stats = (
                        df_valid.groupby('pattern')['ratio']
                        .agg(件数='count', 平均概算比='mean')
                        .reset_index()
                    )
                    stats['平均概算比'] = (stats['平均概算比'] * 100).round(1).astype(str) + '%'
                    stats.columns = ['パターン', '件数', '平均概算比']
                    st.dataframe(stats, hide_index=True, use_container_width=True)

                    # テキストFB
                    notes = df_log[
                        df_log['note'].notna() & (df_log['note'].astype(str).str.strip() != '')
                    ][['date', 'pattern', 'note']].tail(5)
                    if not notes.empty:
                        st.write("**テキストFB（最新5件）**")
                        for _, r in notes.iterrows():
                            st.caption(f"{r['date']} [{r['pattern']}] {r['note']}")

                    # 単価調整
                    st.divider()
                    st.write("**単価調整（カテゴリ別）**")
                    st.caption("調整率を入力して承認するとmaster_prices.csvを更新します。")
                    adj_a = st.number_input("カテゴリA（原状回復）%", value=0, step=5,
                                            min_value=-50, max_value=100, key="adj_a")
                    adj_b = st.number_input("カテゴリB（競争力向上）%", value=0, step=5,
                                            min_value=-50, max_value=100, key="adj_b")
                    adj_c = st.number_input("カテゴリC（商品化）%", value=0, step=5,
                                            min_value=-50, max_value=100, key="adj_c")

                    if any(v != 0 for v in [adj_a, adj_b, adj_c]):
                        if st.button(f"A:{adj_a:+d}%　B:{adj_b:+d}%　C:{adj_c:+d}%　で反映する",
                                     type="primary"):
                            apply_price_adjustment({
                                'A': 1 + adj_a / 100,
                                'B': 1 + adj_b / 100,
                                'C': 1 + adj_c / 100,
                            })
                            st.success("単価を更新しました。")
                            st.rerun()
                    else:
                        st.caption("調整率を入力するとボタンが表示されます。")

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
    # フッターを常にビューポート最下部に固定
    st.markdown(
        f"""
        <style>
        .pv-footer {{
            position: fixed; bottom: 0; left: 0; right: 0;
            background: white; border-top: 1px solid #e0e0e0;
            padding: 6px 16px; text-align: center;
            font-size: 12px; color: #aaa; z-index: 9999;
        }}
        /* フッター分のスペースを確保 */
        .main .block-container {{ padding-bottom: 48px; }}
        </style>
        <div class="pv-footer">
            &copy; 2026 AReC LLC. All rights reserved. &nbsp;|&nbsp; {APP_VERSION}
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        f'<h1 style="margin-bottom:0;">Property Visualizer '
        f'<span style="font-size:14px;font-weight:normal;color:#888;">{APP_VERSION}</span></h1>',
        unsafe_allow_html=True,
    )
    st.caption("リフォームシミュレーター — 業者見積りの概算チェックツール")
    st.divider()

    # ── STEP 1: 間取り図のアップロード ───────────────────────
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
            st.session_state.floor_plan      = uploaded
            st.session_state.floor_plan_name = uploaded.name
            st.rerun()
        if paste_result.image_data is not None:
            st.session_state.floor_plan      = paste_result.image_data
            st.session_state.floor_plan_name = "クリップボードから貼り付け"
            st.rerun()
        st.caption("間取り図をアップロードするとSTEP 2の解析ボタンが表示されます。")
    else:
        col_msg, col_btn = st.columns([5, 1])
        with col_msg:
            st.success(f"アップロード完了：{st.session_state.floor_plan_name}")
        with col_btn:
            if st.button("削除", use_container_width=True):
                st.session_state.floor_plan      = None
                st.session_state.floor_plan_name = None
                st.session_state.analysis_result = None
                st.rerun()

        is_pdf = hasattr(st.session_state.floor_plan, 'type') and \
                 st.session_state.floor_plan.type == 'application/pdf'
        if is_pdf:
            st.info("PDF：STEP 2の解析ボタンで直接読み取ります。")
        else:
            st.image(st.session_state.floor_plan, use_column_width=True)

    st.divider()

    # ── STEP 2: 間取り解析 ───────────────────────────────────
    st.subheader("STEP 2　間取りを解析する")

    if st.session_state.floor_plan is None:
        st.caption("STEP 1 で間取り図をアップロードしてください。")
    elif st.session_state.analysis_result is None:
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
            st.warning(f"ゲストの解析は1セッション {GUEST_SESSION_LIMIT} 回までです。")
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

    # ── STEP 3: 物件情報の入力 ───────────────────────────────
    st.subheader("STEP 3　物件情報の入力")

    detected = (st.session_state.analysis_result or {}).get('madori', '').upper()
    default_idx = MADORI_LIST.index(detected) if detected in MADORI_LIST else None

    col1, col2 = st.columns(2)
    with col1:
        age = st.selectbox("築年数", AGE_OPTIONS, index=None, placeholder="選択してください")
    with col2:
        structure = st.selectbox("構造", STRUCT_OPTIONS, index=None, placeholder="選択してください")

    col3, col4 = st.columns(2)
    with col3:
        area = st.number_input("専有面積（㎡）", min_value=10.0, max_value=200.0,
                               value=None, step=0.5, placeholder="例：35.0")
    with col4:
        label = "間取り（解析結果から自動入力）" if detected in MADORI_LIST else "間取り"
        madori = st.selectbox(label, MADORI_LIST, index=default_idx, placeholder="選択してください")

    if area is not None:
        st.caption(f"壁面積概算：{area * WALL_AREA_FACTOR:.1f}㎡　/　床面積：{area}㎡")

    if any(v is None for v in [age, structure, area, madori]):
        st.caption("すべての物件情報を入力するとSTEP 4が表示されます。")
        return

    st.divider()

    # ── STEP 4: リフォームパターン＋スタイルの選択 ────────────
    st.subheader("STEP 4　リフォームパターン＋スタイルの選択")

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

    if not selected:
        st.info("項目を選択してください。")
        return

    results = calculate(area, madori, selected)
    html_table, total = build_html_table(results)

    # スタイル選択
    st.divider()
    st.write("**リフォームスタイルを選ぶ**")
    swatch_cols = st.columns(4)
    for col, (name, info) in zip(swatch_cols, IMAGE_STYLES.items()):
        with col:
            photo_path = SCRIPT_DIR / info['photo']
            if photo_path.exists():
                st.image(str(photo_path), use_column_width=True)
            is_selected = st.session_state.style_choice == name
            outline = "outline: 3px solid #1f77b4;" if is_selected else ""
            st.markdown(
                f'<div style="background:{info["bg"]};padding:6px 4px;'
                f'border-radius:0 0 6px 6px;text-align:center;'
                f'color:{info["fg"]};font-weight:bold;font-size:13px;line-height:1.4;{outline}">'
                f'{name}<br>'
                f'<span style="font-size:10px;font-weight:normal;">{info["desc"]}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
            btn_label = "✓ 選択中" if is_selected else "選択する"
            btn_type  = "primary" if is_selected else "secondary"
            if st.button(btn_label, key=f"style_btn_{name}",
                         use_container_width=True, type=btn_type):
                st.session_state.style_choice  = name
                st.session_state.room_image    = None
                st.session_state.room_image_style = None
                st.rerun()
    style_choice = st.session_state.style_choice

    # 業者見積り入力（必須）
    st.divider()
    col_q, col_d = st.columns([3, 2])
    with col_q:
        st.write("**業者見積り金額（円）**")
        quote = st.number_input("業者見積り", min_value=0, value=0, step=10000,
                                label_visibility="collapsed")
    with col_d:
        st.write("**見積り日 / 発注日**")
        quote_date = st.date_input("見積り日", value=None, label_visibility="collapsed")
        st.caption("任意")

    # シミュレートボタン（見積り入力後に出現）
    st.divider()
    if quote == 0:
        st.info("業者見積り金額を入力すると「シミュレートする」ボタンが表示されます。")
    else:
        if st.button("この条件でシミュレートする", type="primary"):
            st.session_state.feedback_sent = False
            st.session_state.room_image       = None
            st.session_state.room_image_style = None
            st.session_state.property_photo      = None
            st.session_state.property_photo_name = None
            if not st.session_state.is_master:  # マスターモードはログ非蓄積
                log_date = quote_date.isoformat() if quote_date else None
                save_log(area, madori, age, structure, pattern_choice, total, quote,
                         log_date=log_date)
            st.session_state.simulation_triggered = True
            st.session_state.comparison_quote = quote

        # ── 結果出力 ─────────────────────────────────────────
        if st.session_state.simulation_triggered and \
                st.session_state.comparison_quote == quote:

            # 概算テーブル
            st.divider()
            st.markdown(html_table, unsafe_allow_html=True)

            # 比較結果
            st.divider()
            ratio = quote / total * 100
            diff  = abs(ratio - 100)
            if ratio >= 130:
                st.error(f"要確認（概算より{diff:.0f}%高い）　概算比：{ratio:.0f}%")
            elif ratio >= 110:
                st.warning(f"やや高め　概算比：{ratio:.0f}%")
            elif ratio <= 70:
                st.success(f"割安（概算より{diff:.0f}%低い）　概算比：{ratio:.0f}%")
            else:
                st.success(f"概ね適正　概算比：{ratio:.0f}%")

            # 3D間取りビュー（プレースホルダー）
            if st.session_state.floor_plan is not None:
                st.divider()
                st.write("**3D間取りビュー**")
                st.info("🔧 近日実装予定 — 間取り図から3Dビューを自動生成する機能を開発中です。")

            # フィードバック
            st.divider()
            st.write("**フィードバック**")
            st.caption("欲しい機能・追加してほしい項目・気になった点など、何でもどうぞ。今後の改善に活用します。")
            if not st.session_state.feedback_sent:
                fb_text = st.text_area(
                    "コメント（任意）",
                    placeholder="例：〇〇の項目も欲しい / △△が使いにくかった など",
                    height=80,
                )
                if st.button("フィードバックを送る"):
                    if fb_text and not st.session_state.is_master:  # マスターモードはログ非蓄積
                        save_log(area, madori, age, structure, pattern_choice,
                                 total, quote, note=fb_text)
                    st.session_state.feedback_sent = True
                    st.rerun()
            else:
                st.caption("✓ フィードバックを送信しました。ありがとうございます。")

            # ── After イメージ（任意） ────────────────────────
            st.divider()
            st.subheader("After イメージ（任意）")
            st.caption("物件写真をアップロードすると、選択したスタイルでリフォーム後のイメージ画像を作成します。")

            if st.session_state.property_photo is None:
                prop_photo = st.file_uploader(
                    "物件写真をアップロード（PNG / JPG）",
                    type=['png', 'jpg', 'jpeg'],
                    key="prop_photo_uploader",
                )
                if prop_photo is not None:
                    st.session_state.property_photo      = prop_photo
                    st.session_state.property_photo_name = prop_photo.name
                    st.rerun()
            else:
                col_pm, col_pb = st.columns([5, 1])
                with col_pm:
                    st.success(f"写真アップロード完了：{st.session_state.property_photo_name}")
                with col_pb:
                    if st.button("写真を削除", key="del_prop_photo", use_container_width=True):
                        st.session_state.property_photo      = None
                        st.session_state.property_photo_name = None
                        st.session_state.room_image          = None
                        st.session_state.room_image_style    = None
                        st.rerun()
                st.image(st.session_state.property_photo, use_column_width=True)

                if st.session_state.room_image is None:
                    st.caption(
                        "💡 現在は無料APIで生成しているため、画像の精度に限りがあります。"
                        "有料APIへの切り替えにより、Before写真の構造を保ったより高精度な"
                        "Afterイメージの生成が可能になります（実装予定）。"
                    )
                    if st.button("この写真でAfterイメージを生成する", type="primary",
                                 key="gen_after"):
                        with st.spinner("Afterイメージを生成中...（1〜2分かかる場合があります）"):
                            try:
                                img_bytes = generate_after_image(
                                    st.session_state.property_photo, style_choice)
                                st.session_state.room_image       = img_bytes
                                st.session_state.room_image_style = style_choice
                                st.rerun()
                            except Exception as e:
                                st.error(f"画像生成エラー：{type(e).__name__}: {e}")
                else:
                    st.divider()
                    st.write("**Before / After**")
                    col_b, col_a = st.columns(2)
                    with col_b:
                        st.write("**Before**")
                        st.image(st.session_state.property_photo, use_column_width=True)
                    with col_a:
                        st.write(f"**After（{st.session_state.room_image_style}）**")
                        st.image(st.session_state.room_image, use_column_width=True)
                    st.caption("※Afterはイメージ画像です。実際の仕上がりとは異なります。")
                    if st.button("生成結果を削除", key="del_after"):
                        st.session_state.room_image       = None
                        st.session_state.room_image_style = None
                        st.rerun()




if __name__ == '__main__':
    main()
