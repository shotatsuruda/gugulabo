#!/bin/bash
# Threads OAuth URL生成 + アクセストークン取得スクリプト
# Usage: bash threads_get_token.sh

set -e

# .envからAPP_SECRETを読み込む
ENV_FILE="$(dirname "$0")/.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "エラー: .envが見つかりません: $ENV_FILE"
  exit 1
fi
source "$ENV_FILE"

CLIENT_ID="${THREADS_APP_ID}"
CLIENT_SECRET="${THREADS_APP_SECRET}"
REDIRECT_URI="https://gugulabo.com/threads/callback"
SCOPE="threads_basic,threads_content_publish"

if [ -z "$CLIENT_ID" ] || [ -z "$CLIENT_SECRET" ]; then
  echo "エラー: .envにTHREADS_APP_IDとTHREADS_APP_SECRETを設定してください"
  exit 1
fi

# Step 1: OAuth URL表示
AUTH_URL="https://www.threads.net/oauth/authorize?client_id=${CLIENT_ID}&redirect_uri=${REDIRECT_URI}&scope=${SCOPE}&response_type=code"

echo "================================================"
echo "Step 1: ブラウザで以下のURLを開いてください"
echo "================================================"
echo "$AUTH_URL"
echo ""

# Step 2: codeを入力してもらう
echo "認証後に https://gugulabo.com/threads/callback?code=... に遷移します"
echo "ページに表示されたcodeを貼り付けてください:"
read -r CODE

if [ -z "$CODE" ]; then
  echo "エラー: codeが入力されませんでした"
  exit 1
fi

# Step 3: 短期アクセストークン取得
echo ""
echo "→ 短期アクセストークン取得中..."
SHORT_RESPONSE=$(curl -s -X POST "https://graph.threads.net/oauth/access_token" \
  -F "client_id=${CLIENT_ID}" \
  -F "client_secret=${CLIENT_SECRET}" \
  -F "grant_type=authorization_code" \
  -F "redirect_uri=${REDIRECT_URI}" \
  -F "code=${CODE}")

SHORT_TOKEN=$(echo "$SHORT_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('access_token',''))" 2>/dev/null)
USER_ID=$(echo "$SHORT_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('user_id',''))" 2>/dev/null)

if [ -z "$SHORT_TOKEN" ]; then
  echo "エラー: トークン取得失敗"
  echo "$SHORT_RESPONSE"
  exit 1
fi

echo "✅ 短期トークン取得: USER_ID=$USER_ID"

# Step 4: 長期トークンに変換（1年有効）
echo "→ 長期トークンに変換中..."
LONG_RESPONSE=$(curl -s "https://graph.threads.net/access_token?grant_type=th_exchange_token&client_secret=${CLIENT_SECRET}&access_token=${SHORT_TOKEN}")

LONG_TOKEN=$(echo "$LONG_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('access_token',''))" 2>/dev/null)
EXPIRES_IN=$(echo "$LONG_RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('expires_in',''))" 2>/dev/null)

if [ -z "$LONG_TOKEN" ]; then
  echo "エラー: 長期トークン変換失敗"
  echo "$LONG_RESPONSE"
  exit 1
fi

EXPIRES_DAYS=$((EXPIRES_IN / 86400))
echo "✅ 長期トークン取得（有効期限: 約${EXPIRES_DAYS}日）"

# Step 5: 結果表示
echo ""
echo "================================================"
echo "Mac の ~/.config/ai-media/threads_credentials に以下を設定してください："
echo "================================================"
echo "THREADS_USER_ID=\"${USER_ID}\""
echo "THREADS_ACCESS_TOKEN=\"${LONG_TOKEN}\""
