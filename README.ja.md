# limpet

[English](README.md) | 日本語

エージェントが早く止まりすぎるのを止める、Claude Code の Stop hook。

`rules.md` に自然言語でルールを書く。Claude Code が止まろうとするたびに、limpet は直前のやり取りを [jev](https://typesafe.ai)（TypeSafe AI の評価モデル。[Vercel AI Gateway](https://vercel.com/ai-gateway) 経由）に送り、ルールごとに「いま違反した確率」を受け取る。閾を超えたルールがあれば、止まる代わりに作業を続けるようエージェントに差し戻す。

Python 1 ファイル、標準ライブラリのみ。正規表現の保守なし、ローカルモデルなし、学習なし。

## なぜ

コーディングエージェントは早く止まる。「CI を待ちます」「実行しますか？」「◯◯さんに聞いてください」。あなたは「やって」と打つ。また打つ。limpet はその停止を人が見る前に捕まえて作業に押し戻す。岩に張り付くカサガイ（limpet）のように、タスクから離れない。

実際には誤検知もある。その場合エージェントは「このルールは該当しない、理由は…」と 1 行書いて普通に止まるだけなので、誤検知は安い。本当の早すぎる停止を見逃すと人の往復が 1 回増えるので、再現率寄りに調整する。

## 他との違い

エージェントを作業に押し戻す Stop hook は、大きく 2 派ある。

- **正規表現派**: [checkpoint-guard](https://github.com/platcrest/checkpoint-guard)、[llm-dark-patterns](https://github.com/waitdeadai/llm-dark-patterns)、[cc-enforcer](https://github.com/skymanbp/cc-enforcer) など。無料で即時だが、英語の言い回しに一致させる方式なので、新しい止まり方が出るたびにパターンが増え、他の言語のルールは書けない。
- **Claude に判定させる派**: Claude Code 標準の `type: "prompt"` hook や [superpowers](https://github.com/obra/superpowers) の judge スクリプトなど。ルールの意味は分かるが、停止のたびにモデル 1 回分の遅延と費用がかかる。

limpet はその間にいる。ルールは任意の言語の自然言語で、判定は yes/no 質問専用の分類器がやる。1 停止あたり約 0.7 秒、0.01 セント。判決ではなく確率が返るので、閾は固定のプロンプトを信じるのではなく、自分のログからルールごとに決められる。

## インストール

1. Vercel AI Gateway の鍵を用意する。jev は入力 100 万トークンあたり $0.042。1 停止あたり 1,000〜2,000 トークンなので、ヘビーに使っても 1 日数セント。
2. clone する:

   ```sh
   git clone https://github.com/noplan-inc/limpet ~/limpet
   ```

3. `~/.claude/settings.json`（またはプロジェクトの `.claude/settings.json`）に hook を足す:

   ```json
   {
     "env": {
       "AI_GATEWAY_API_KEY": "vck_...",
       "LIMPET_BLOCK": "0.8"
     },
     "hooks": {
       "Stop": [{ "hooks": [{ "type": "command", "command": "python3 ~/limpet/jev_stop.py", "timeout": 30 }] }]
     }
   }
   ```

   鍵をファイルに置きたくなければ、`LIMPET_KEY_CMD` に鍵を出力するシェルコマンドを入れる。例: `op read op://vault/vercel-ai-gateway/password`

4. `rules.md` を編集する。`- ` で始まる行だけがルール。言語は問わない。

## 閾

`LIMPET_BLOCK` は全ルール共通の数値 1 つか、`ルール文の部分文字列=閾` のカンマ区切り:

```
LIMPET_BLOCK="shall I start=0.30,hand work=0.60,before stopping=0.35,time estimate=0.74"
```

閾の無いルールは止めない。`LIMPET_BLOCK` を未設定にすると影の運転になる。全停止を採点して `~/.limpet/log.jsonl` に記録するだけで、何も止めない。1〜2 日回したら:

```sh
python3 ~/limpet/jev_stop.py --stats
```

でルールごとのパーセンタイルが出る。p95 あたりに閾を置くと、そのルールで最悪 5% の停止を止める。作者はそこから始めた。

## エージェントに見えるもの

ヒットすると hook は exit 2 で stderr にこれを出し、Claude Code がエージェントに返す:

> limpet: this response may violate: "Fix problems you find before stopping…" (83%). If it does, follow the rule and keep working. If it does not, say why in one line, then stop.

同じ停止の 2 回目は `stop_hook_active` が立つので必ず通す。差し戻しは 1 停止につき最大 1 回。

## 失敗時

鍵が無い、API エラー、タイムアウト: 黙って exit 0。サービスが落ちているからといって hook が仕事を止めてはいけない。エラーはログに残る。

## 環境変数

| 変数 | 既定 | |
|---|---|---|
| `AI_GATEWAY_API_KEY` | | Vercel AI Gateway の鍵 |
| `LIMPET_KEY_CMD` | | 鍵を出力するシェルコマンド。上が未設定のとき使う |
| `LIMPET_BLOCK` | 未設定 | 閾。上記参照 |
| `LIMPET_RULES` | スクリプト隣の `rules.md` | ルールファイル |
| `LIMPET_LOG` | `~/.limpet/log.jsonl` | ログ。1 停止 1 行の JSON |
| `LIMPET_JEV_MODEL` | `typesafe-ai/jev` | Gateway に送るモデル ID |

## テスト

```sh
python3 test_jev_stop.py
```

API は呼ばない。

## ライセンス

MIT
