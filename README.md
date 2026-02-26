# マッサージ店向け Googleレビュー管理システム

マッサージ店のオーナー・スタッフが使う、Googleレビューの獲得・返答をかんたんに管理できるWebアプリです。

## 機能

| 機能 | 説明 |
|------|------|
| **QRコード生成** | 店舗ごとにデザイン付きQRコードを生成・印刷 |
| **満足度アンケート** | QRを読み取ったお客様が★1〜5で満足度を回答 |
| **Googleレビュー誘導** | ★4〜5のお客様をGoogleレビュー投稿ページへ誘導 |
| **ご意見フォーム** | ★1〜3のお客様から社内向けのご意見を収集 |
| **クーポン自動送信** | アンケート回答者にクーポンをメールで自動送信 |
| **AI返答生成** | Googleレビューに対する返答文をAIが自動生成 |

### お客様の導線

```
QRコードをスキャン
    ↓
満足度を★1〜5で選択
    ├─ ★4〜5（高評価）→ Googleレビュー投稿ページへ誘導 ＋ クーポンプレゼント
    └─ ★1〜3（低評価）→ ご意見フォームを表示 ＋ クーポンプレゼント（Googleレビューへの誘導なし）
```

### QRコードのデザインオプション

- カラー・グラデーション（放射状・横・縦）を自由に設定
- 背景色を変更
- モジュールの形を選択（四角・丸角・ドット）
- 中央に絵文字ロゴを配置
- 高解像度PNG（1024×1024px）でダウンロード・印刷

---

## 必要なもの

- Python 3.11 以上
- [OpenRouter API キー](https://openrouter.ai/keys)（AI返答生成に必要）
- Gmail アカウント + アプリパスワード（クーポンメール送信に必要）

---

## セットアップ手順

### 1. リポジトリをダウンロード・移動

```bash
cd massage-review-system
```

### 2. 仮想環境を作成して有効化

```bash
# 仮想環境を作成
python -m venv venv

# 有効化（Mac / Linux）
source venv/bin/activate

# 有効化（Windows）
venv\Scripts\activate
```

### 3. 必要なパッケージをインストール

```bash
pip install -r requirements.txt
```

### 4. 環境変数ファイルを設定

`.env.example` をコピーして `.env` ファイルを作成します。

```bash
cp .env.example .env
```

`.env` をテキストエディタで開き、各項目を設定してください。

```env
# Flask設定（ランダムな文字列に変更してください）
FLASK_SECRET_KEY=your-very-secret-random-key-here

# お店の名前（AI返答文の署名に使用されます）
SHOP_NAME=あなたのマッサージ店名

# メール送信設定（クーポン自動送信に必要）
MAIL_SMTP_HOST=smtp.gmail.com
MAIL_SMTP_PORT=587
MAIL_USERNAME=your@gmail.com
MAIL_PASSWORD=your-app-password
MAIL_FROM=your@gmail.com

# OpenRouter API（AI返答生成）
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxx...
```

#### 各設定の取得方法

**Gmail アプリパスワード（メール送信）**
1. Google アカウントの [セキュリティ設定](https://myaccount.google.com/security) で2段階認証を有効にする
2. [アプリパスワード](https://myaccount.google.com/apppasswords) のページでアプリパスワードを発行
3. 発行された16文字のパスワードを `MAIL_PASSWORD` に設定

**OpenRouter API（AI返答生成）**
1. [OpenRouter](https://openrouter.ai/) にアカウント登録・ログイン
2. [APIキー管理ページ](https://openrouter.ai/keys) から新しいキーを発行
3. `.env` の `OPENROUTER_API_KEY` に設定

**Googleレビュー URL（店舗ごとに管理画面から設定）**
1. [Googleビジネスプロフィール](https://business.google.com/) にログイン
2. 「レビューを増やす」からレビュー用リンクを取得
3. 管理画面の「店舗管理」に入力

### 5. アプリを起動

```bash
python app.py
```

ブラウザで [http://127.0.0.1:5000](http://127.0.0.1:5000) を開くと管理画面が表示されます。

> **注意：macOS の AirPlay Receiver もポート5000を使用しています。**
> 競合する場合は `app.py` 末尾を `app.run(debug=True, port=5001)` に変更してください。

---

## 使い方

### QRコード生成

1. ナビゲーションの「QRコード生成」をクリック
2. 左パネルの「店舗を追加」で店舗名とGoogleレビューURLを登録
3. 右パネルで対象店舗を選択
4. カラー・形状・絵文字などデザインを設定
5. 「QRコードを生成する」ボタンをクリック
6. プレビューを確認し「PNG ダウンロード」または「印刷」

> QRコードを読み取るとアンケートページ（`/survey/<shop_id>`）に誘導されます。

### クーポン設定

1. 「QRコード生成」画面で対象店舗を選択
2. 下部の「クーポン設定」パネルが展開される
3. クーポン名・割引内容・有効日数を入力して「保存」
4. 「クーポンを有効にする」をオンにするとアンケートページに表示される

### AI返答生成

1. ナビゲーションの「AI返答生成」をクリック
2. GoogleレビューをコピーしてLeft側のテキストエリアに貼り付け
3. 「AI返答を生成する」ボタンをクリック（または `Ctrl+Enter`）
4. 生成された返答文を確認・編集
5. 「クリップボードにコピー」でコピーしてGoogleに貼り付け

---

## ファイル構成

```
massage-review-system/
├── app.py                  # メインアプリ（Flask）
├── requirements.txt        # Pythonパッケージ一覧
├── .env.example            # 環境変数サンプル
├── .env                    # 環境変数（自分で作成・gitignore済み）
├── .gitignore              # Git除外設定
├── review_system.db        # SQLiteデータベース（自動生成）
├── templates/
│   ├── base.html           # 共通レイアウト（管理画面用）
│   ├── index.html          # ホーム画面
│   ├── qr.html             # QRコード生成画面
│   ├── survey.html         # 顧客向けアンケートページ（管理画面レイアウト不使用）
│   └── review.html         # AI返答生成画面
└── static/
    └── style.css           # カスタムCSS
```

### データベースのテーブル構成

| テーブル | 内容 |
|----------|------|
| `shops` | 店舗情報（名前・GoogleレビューURL） |
| `coupons` | クーポン設定（店舗ごと1件） |
| `feedbacks` | お客様のご意見（★1〜3の回答） |
| `coupon_deliveries` | クーポン送信履歴 |

---

## 注意事項

- `.env` ファイルにはAPIキーが含まれるため、**絶対にGitHubなどにアップロードしないでください**（`.gitignore` で除外済み）
- 本番環境では `app.py` の `app.run(debug=True)` を `app.run(debug=False)` に変更してください

---

## 引き継ぎメモ（2026-02-26）

### 今日やったこと

1. **SMS機能を廃止してQRコード生成機能に刷新**
   - `senders.py`（SMS送信バックエンド）を削除
   - `templates/sms.html`（SMS送信画面）を削除
   - `.env` のSMS関連設定（`SMS_BACKEND`, `MAIL_TO` など）を削除
   - `qrcode[pil]` と `Pillow` を `requirements.txt` に追加・インストール
   - QRコード生成機能を実装（グラデーション・モジュール形状・絵文字ロゴ・PNG出力）

2. **顧客向けアンケート機能を新規実装**
   - QRコードの読み取り先を Googleレビューページから自社アンケートページ（`/survey/<shop_id>`）に変更
   - ★1〜5の満足度アンケートをモバイルファーストで実装
   - ★4〜5：Googleレビュー投稿ページへの誘導ボタンを表示
   - ★1〜3：ご意見テキストフォームを表示（Googleレビューへの誘導なし）
   - ご意見は `feedbacks` テーブルに保存

3. **クーポン自動送信機能を新規実装**
   - 管理画面からクーポン名・割引内容・有効日数を店舗ごとに設定可能
   - アンケートページのクーポンセクションでメールアドレスを入力すると自動送信
   - クーポンコードをサーバー側で自動生成（例: `ABCD-EF23-GH45`）
   - 送信履歴を `coupon_deliveries` テーブルに保存
   - テキスト・HTML両形式のメールを送信

4. **動作確認**
   - サーバー起動：正常
   - QRコード生成（グラデーション・ドット・絵文字）：正常
   - クーポンメール送信テスト：**成功** → `shotatsuruda0819@gmail.com` にメール到達を確認

### 現在の状況

| 項目 | 状況 |
|------|------|
| サーバー | 稼働中（`http://127.0.0.1:5000`） |
| QRコード生成 | 動作確認済み ✅ |
| アンケートページ | 動作確認済み ✅ |
| クーポンメール送信 | Gmailアプリパスワード設定済み・動作確認済み ✅ |
| AI返答生成 | OpenRouter APIキー設定済み・動作可能 ✅ |
| SHOP_NAME | **未設定**（デフォルト名のまま） |

### 次にやること

- [ ] `.env` の `SHOP_NAME` を実際の店舗名に設定する
- [ ] 管理画面から店舗とGoogleレビューURLを登録する
- [ ] クーポン内容（クーポン名・割引内容・有効日数）を設定して有効にする
- [ ] QRコードを生成・印刷して店頭に設置する
- [ ] 実際にスマホでQRを読み取り、アンケート〜クーポン受取の一連の流れを確認する
- [ ] 本番運用前に `app.py` の `app.run(debug=True)` を `app.run(debug=False)` に変更する
