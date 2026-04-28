# CC 競馬予想 AI

netkeibaから出走表を自動取得し、Claude AIが◎○▲・敗因シナリオ・買い目を生成する競馬予想アプリ。

## ローカル起動

```bash
export ANTHROPIC_API_KEY=sk-...
python3 server.py
# http://localhost:11000
```

## 構成

- `server.py` — Pythonサーバー（標準ライブラリのみ）
- `index.html` — フロントエンド
- `requirements.txt` — 空（外部依存なし）
- `render.yaml` — Render用デプロイ設定

## 対象競馬場

JRA10場：札幌(01), 函館(02), 福島(03), 新潟(04), 中山(05), 東京(06), 中京(07), 京都(08), 阪神(09), 小倉(10)

## 券種

単勝・複勝・馬連・馬単・ワイド・3連複・3連単
