# PropertyVisualizer 設計履歴

## 概要

リフォーム費用シミュレーター + 間取り図解析 + Before/Afterイメージ生成ツール。  
Streamlit Cloud で公開: https://property-visualizer-sim.streamlit.app/  
GitHub: https://github.com/arechub/property-visualizer

---

## バージョン変遷

| Version | 主な変更内容 |
|---------|------------|
| v0.1〜0.3 | CLIシミュレーター → Streamlit化の初期実装 |
| v1.0 | Streamlitアプリとして初リリース |
| v1.1〜1.3 | 各種UX改善・バグ修正 |
| v1.4 | STEP 4全面刷新（パターン＋スタイル統合、シミュレートボタン前に見積り入力必須） |
| v1.5 | フロー全面刷新（物件写真→After生成を結果セクション後に移動、STEP3初期値ブランク化） |
| v1.6 | スタイル選択にリフォーム例画像4枚を追加（ChatGPT生成・4分割加工） |
| v1.7 | タイトルにバージョン表示、固定フッター著作権表示、After画像生成をFree/Proトグル構造に |
| v1.8 | 過去案件登録フォーム、分析レポート、築年数5年刻み、見積り日/発注日入力、スタイル選択をカラム内ボタンに変更 |
| v1.9 | マスターモード時ログ非蓄積（デバッグ用）、ログデータ全削除（本番リリース前リセット） |

---

## UXフロー設計（v1.8〜）

```
STEP 1: 基本情報入力
  └─ 専有面積（必須）/ 間取り / 築年数 / 建物構造
     └─ 間取り図（任意）→ 解析すると間取り欄に転記、参考情報として表示

STEP 2: リフォームパターン選択
  └─ A（必須）/ B（競争力向上）/ C（商品化）/ カスタム
  └─ スタイル選択: シック / 明るく / ナチュラル / モダン（例画像4枚表示）

STEP 3: 業者見積り入力
  └─ 業者見積り金額（0円 = 未入力扱い）
  └─ 見積り日 または 発注日（横並び配置）
  └─ 入力あり → 「シミュレートする」ボタン表示
  └─ 入力なし → 「見積り金額を入力してください」メッセージ

シミュレート結果
  └─ 概算合計 / 業者見積りとの比較（4段階判定）
  └─ 内訳テーブル
  └─ 比較コメント表示

フィードバック入力
  └─ テキストエリア + 「フィードバックを送る」ボタン
  └─ ※ 送信時に renovation_log.csv へ保存（マスターモード時は非保存）

STEP 4（任意）: After イメージ生成
  └─ 物件写真アップロード → 「Afterイメージを生成」ボタン
  └─ 無料モード: Claude Vision特徴抽出 → FLUX.1-schnell（テキストから生成）
  └─ Proモード: FLUX.1-Kontext-dev（img2img・構造保持）
  └─ スタイル変更時はAfter画像を自動リセット
```

---

## 技術選定と経緯

### Claude Vision（間取り図解析）
- モデル：`claude-haiku-4-5-20251001`（コスト重視）
- 用途：間取り図解析（部屋数・面積・間取り種別を抽出）
- 理由：OCRより精度が高く、日本語住宅図面のフォーマット多様性に対応できる
- `.env` の `MADORI_MODEL` で切り替え可能（sonnet等への切替余地あり）

### HuggingFace（After画像生成）
- 無料モード：`FLUX.1-schnell`（InferenceClient.text_to_image）
  - Claude Visionで写真から「部屋の特徴テキスト」を抽出 → それをプロンプトに含めてFLUXで生成
  - 真のimg2imgではないため別の部屋になることがある（既知の限界）
- Proモード：`FLUX.1-Kontext-dev`（InferenceClient.image_to_image via fal-ai）
  - 構造保持型img2img。Before写真の間取りを維持したまま内装スタイルを変更できる
  - fal-aiプロバイダー経由。月次無料クレジット（$1相当）で5回程度で枯渇
  - **切替方法**：`.env` に `HUGGING_FACE_PRO=true` を追加（コメントアウト解除）
  - Streamlit CloudのSecretsにも `HUGGING_FACE_PRO = "true"` を追加

### FLUX.1-Kontextの画像スタイルプロンプト設計
- 汎用表現（「明るい雰囲気に」）では変化が乏しいため、**素材指定型**に変更
- 例：「replace all cabinet fronts with matte black cabinet doors, replace flooring with dark charcoal wood planks」
- 理由：ナチュラルスタイルで「すでに白壁・明るいフローリング」の部屋だとKontextが変更不要と判断してしまう問題への対処

### フィードバック学習の設計
- `renovation_log.csv`：date, area, madori, age, structure, pattern, estimated, actual, ratio, note
- `actual`（実発注額）が入っているレコードのみを学習対象とする
- `date`：見積り日または発注日（ユーザーが手動入力）→ フォームのデフォルトを `None`（未入力必須）にして誤登録を防止
- `note`欄：見積り日/発注日の区別やその他メモ
- 単価調整は `apply_price_adjustment(factors)` で `master_prices.csv` を直接書き換える方式
- 調整前に分析レポートを表示 → マスターが承認してから実行（半自動）

---

## マスターモードの設計

- サイドバーにパスワード入力欄（`.env` の `MASTER_PASSWORD`）
- 認証後に開放される機能：
  1. **過去案件の登録**（見積り日/発注日・実発注額を含む手動入力フォーム）
  2. **分析レポート**（乖離率の集計・パターン別分析）
  3. **単価マスター調整**（レポート確認後に承認実行）
- **マスターモード中はログ非蓄積**（デバッグ・テスト目的の操作がデータを汚染しないよう）
  - シミュレートボタン押下時の `save_log()` をスキップ
  - フィードバック送信時の `save_log()` をスキップ
  - ただし「過去案件登録フォーム」からの `save_log()` は常に保存（意図的な登録のため）

---

## セッション管理

```python
st.session_state.floor_plan          # 間取り図バイナリ
st.session_state.floor_plan_name     # ファイル名
st.session_state.analysis_result     # 間取り解析テキスト
st.session_state.is_master           # マスターモードフラグ
st.session_state.session_analyses    # セッション内解析回数（利用制限）
st.session_state.comparison_quote    # 業者見積り額（結果表示に使用）
st.session_state.room_image          # 生成Afterイメージ
st.session_state.room_image_style    # Afterイメージ生成時のスタイル
st.session_state.feedback_sent       # フィードバック送信済みフラグ
st.session_state.simulation_triggered # シミュレート実行済みフラグ
st.session_state.property_photo      # 物件写真バイナリ
st.session_state.property_photo_name # 物件写真ファイル名
st.session_state.style_choice        # 選択中スタイル名
```

---

## 既知の課題と対応方針

| 課題 | 現状 | 将来対応 |
|-----|------|---------|
| After画像が別の部屋になる | FLUX.1-schnellは擬似img2img。プロンプト精度に依存 | HuggingFace Pro契約後にFLUX.1-Kontext-devへ切替 |
| 回数制限のバイパス（リロード） | `session_analyses` はリロードでリセット | アクセス増加後にStreamlit Cloud認証を追加 |
| 間取り図から3D表示 | Phase 4として未着手 | Three.js / Blender等の検討（Phase 4） |
| 単価マスターの精度 | 2026年時点の概算・廉価グレード想定 | 実績データ蓄積後に `apply_price_adjustment()` で補正 |

---

## ファイル構成

```
PropertyVisualizer/
├── 20260612_streamlit_app.py      # Streamlitアプリ本体（メイン）
├── 20260612_master_prices.csv     # リフォーム単価マスター
├── 20260612_renovation_calc.py    # CLIシミュレーター（参考）
├── 20260612_design_history.md     # 本ドキュメント（設計履歴）
├── .env                           # APIキー（gitignore済み・非公開）
├── .env.example                   # 環境変数テンプレート
├── .gitignore                     # .env / renovation_log.csv を除外
├── requirements.txt               # 依存ライブラリ
├── CLAUDE.md                      # プロジェクトルール（Claudeへの指示）
├── photos/                        # スタイル選択用リフォーム例画像
│   ├── 20260612_style_chic.png    # シック（ダーク&ラグジュアリー）
│   ├── 20260612_style_bright.png  # 明るく（ホワイト&ブライト）
│   ├── 20260612_style_natural.png # ナチュラル（ウッド&グリーン）
│   └── 20260612_style_modern.png  # モダン（グレー&メタリック）
└── renovation_log.csv             # 実績ログ（自動生成・gitignore済み）
```

---

## 環境変数・シークレット

| キー | 用途 | 設定場所 |
|-----|------|---------|
| `ANTHROPIC_API_KEY` | Claude Vision（間取り解析・After写真特徴抽出） | .env / Streamlit Secrets |
| `MASTER_PASSWORD` | マスターモード認証 | .env / Streamlit Secrets |
| `HUGGING_FACE_TOKEN` | FLUX.1-schnell / FLUX.1-Kontext-dev | .env / Streamlit Secrets |
| `HUGGING_FACE_PRO` | `true` でKontextモード切替（省略=無料モード） | .env（コメントアウト） / Streamlit Secrets |
| `MADORI_MODEL` | 間取り解析モデルID | .env |

---

## APP_VERSION 定数

タイトルとフッターの両方で参照。更新はこの1行のみ。

```python
APP_VERSION = "v1.9"
```

---

_最終更新：2026-06-12_
