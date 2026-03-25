---
name: deploy
description: gugulabo のデプロイ手順。「デプロイして」「本番に反映して」「サーバーに上げて」などのキーワードで自動起動。VPSへのgit push→pull→restart手順を案内する。
allowed tools: Bash, Read
---

# Deploy Skill - gugulabo

## デプロイ手順

### 前提確認
```bash
# SSH鍵が読み込まれているか確認（Mac再起動後は必須）
ssh-add -l
# 表示されない場合:
ssh-add ~/.ssh/id_ed25519
```

### 標準デプロイ
```bash
git add .
git commit -m "変更内容を日本語で記述"
git push origin main
ssh gugulabo "cd ~/gugulabo && git pull && sudo systemctl restart gugulabo"
```

### git競合が出た場合（VPS側）
```bash
ssh gugulabo "cd ~/gugulabo && git stash && git pull && sudo systemctl restart gugulabo"
```

### デプロイ確認
```bash
ssh gugulabo "sudo systemctl status gugulabo --no-pager"
ssh gugulabo "sudo journalctl -u gugulabo -n 30 --no-pager"
```

## 注意
- VPS上でファイルを直接編集しないこと
- .envはgit管理外のためVPS側は手動で更新が必要
