---
name: debug
description: gugulabo のデバッグ・ログ確認スキル。「エラーが出た」「ログを見たい」「動かない」「500エラー」などのキーワードで自動起動。VPSのログ確認コマンドを提供する。
allowed tools: Bash, Read
---

# Debug Skill - gugulabo

## ログ確認コマンド

### アプリログ（直近100行）
```bash
ssh gugulabo "sudo journalctl -u gugulabo -n 100 --no-pager"
```

### リアルタイム監視
```bash
ssh gugulabo "sudo journalctl -u gugulabo -f"
```

### Nginxエラーログ
```bash
ssh gugulabo "sudo tail -n 50 /var/log/nginx/error.log"
```

### サービス状態確認
```bash
ssh gugulabo "sudo systemctl status gugulabo --no-pager"
```

## よくあるエラーパターン
- `ModuleNotFoundError` → pip install 漏れ。VPS側で手動インストール必要
- `500 Internal Server Error` → journalctlでPythonトレースバックを確認
- `APIキーが取れない` → .envの記載を確認。関数内でos.getenv()しているか確認
- `git conflict` → git stash && git pull で解消
