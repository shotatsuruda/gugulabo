#!/bin/bash
echo "🚀 デプロイ開始..."
git add .
git commit -m "${1:-update}"
git push origin main
echo "📡 VPSに反映中..."
ssh root@162.43.76.18 "cd /var/www/gugulabo && git pull origin main && systemctl restart gugulabo"
echo "✅ デプロイ完了!"
