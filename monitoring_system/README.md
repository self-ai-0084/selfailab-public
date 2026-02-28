# ローカルネットワークPC監視システム

研究室内の PC 使用状況を監視するための最小構成です。  
各 PC に `client_agent.py` を常駐させ、監視サーバー `server.py` に定期送信します。

## 監視できる項目

- PC 動作状況
  - エージェントが定期送信している間は `online`
  - 一定時間受信が止まるとサーバー側で `offline` に自動判定
- 使用者
  - `getpass.getuser()` で取得したログインユーザー名
  - `SESSIONNAME` から `console` / `remote_desktop` を判定
- GPU 使用率
  - `nvidia-smi` が利用できる場合に GPU 名、GPU 使用率、使用メモリを取得
  - NVIDIA GPU でない場合や `nvidia-smi` がない場合は `null`

## 構成

- `server.py`
  - TCP で各 PC から状態を受信
  - メモリ上で最新状態を保持
  - 一定時間更新がなければ OFF 扱い
  - HTTP ダッシュボードと JSON API を提供
- `client_agent.py`
  - 各 PC 上で動かす送信エージェント
  - 10 秒ごとに状態をサーバーへ送信

## 使い方

### 1. 監視サーバーを起動

```powershell
python monitoring_system\server.py --tcp-port 8888 --http-port 8080 --offline-after 30
```

- ダッシュボード: `http://<サーバーIP>:8080/`
- JSON API: `http://<サーバーIP>:8080/api/clients`

### 2. 各 PC でクライアントを起動

```powershell
python monitoring_system\client_agent.py --server-host <サーバーIP> --server-port 8888 --interval 10
```

### 3. 起動時に自動実行したい場合

Windows ならタスクスケジューラに以下を登録してください。

- トリガー: ログオン時
- 操作: `python`
- 引数: `monitoring_system\client_agent.py --server-host <サーバーIP> --server-port 8888 --interval 10`

## API 例

`/api/clients` は以下の形式で返します。

```json
{
  "generated_at": "2026-02-28T04:00:00+00:00",
  "offline_after_seconds": 30,
  "clients": [
    {
      "client_id": "lab-pc-01",
      "pc_name": "LAB-PC-01",
      "ip_address": "192.168.1.10",
      "port": 51234,
      "status": "online",
      "current_user": "takahashi",
      "session_type": "console",
      "gpu_usage_percent": 42.0,
      "gpu_memory_used_mb": 2048,
      "gpu_name": "NVIDIA RTX 4070",
      "last_reported_at": "2026-02-28T04:00:00+00:00",
      "seconds_since_last_seen": 2.7
    }
  ]
}
```

## 注意点

- この実装は認証なしです。研究室 LAN 内だけで使う前提です。
- PC の「電源 OFF」は直接取得していません。一定時間通信が止まったら OFF とみなします。
- 複数 GPU がある場合、現在は 1 行目の GPU のみ表示します。
