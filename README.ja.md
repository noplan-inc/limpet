<p align="center">
  <img src="assets/logo.svg" width="160" alt="limpet">
</p>

<h1 align="center">limpet</h1>

<p align="center">
  <em>エージェントは止まる。limpet は止めさせない。</em>
</p>

<p align="center">
  <a href="https://github.com/noplan-inc/limpet/actions/workflows/test.yml"><img src="https://img.shields.io/github/actions/workflow/status/noplan-inc/limpet/test.yml?style=flat-square&label=tests&color=0b6e6e" alt="tests"></a>
  <img src="https://img.shields.io/badge/works%20with-Claude%20Code%20%C2%B7%20Codex-0b6e6e?style=flat-square" alt="Claude Code and Codex">
  <img src="https://img.shields.io/badge/deps-stdlib%20only-0b6e6e?style=flat-square" alt="stdlib only">
  <img src="https://img.shields.io/badge/per%20stop-0.7s%20%C2%B7%20%240.0001-0b6e6e?style=flat-square" alt="1 停止あたり 0.7 秒・0.01 セント">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-0b6e6e?style=flat-square" alt="MIT"></a>
</p>

<p align="center">
  <sub><a href="README.md">English</a> &middot; 日本語</sub>
</p>

---

コーディングエージェントは早く止まる。*「CI を待ちます」* *「実行しますか？」* *「チームに確認してください」*。あなたは **やって** と打つ。また打つ。また打つ。

limpet は Stop hook です。ルールを自然言語で書く。エージェントが止まろうとするたびに、[jev](https://typesafe.ai) が **0.7 秒**で全ルールに対して採点し、違反していれば止まる代わりに作業へ押し戻す。岩に張り付くカサガイ（limpet）のように、タスクから離れない。

Python 1 ファイル。標準ライブラリのみ。正規表現の保守なし、ローカルモデルなし、学習なし。

## Before / after

limpet なし:

```
● auth.py のバグを見つけました。修正はトークン検査の 1 行です。
  適用しますか？

> やって
```

limpet あり。同じ停止は、あなたの目に届かない:

```
● auth.py のバグを見つけました。修正はトークン検査の 1 行です。
  適用しますか？

  ⏹ limpet: this response may violate: "既に依頼された作業に
    「始めますか？」と許可を取らない" (91%). If it does, follow the
    rule and keep working. If it does not, say why in one line, then stop.

● 修正を適用します。
  ⎿ Edit auth.py
  ⎿ Bash pytest -q · 42 passed
  完了。期限切れトークンを弾くようになり、テストは通っています。
```

## 仕組み

```
エージェントが止まろうとする
        │
        ▼
limpet が直前 3 メッセージ＋このターンのツール呼び出し＋最後の発話を読む
        │
        ▼
jev がルールごとに yes/no を並列で答える。約 0.7 秒
   「テストを走らせずに完了と言わない」          →  0.08
   「依頼済みの作業に『始めますか？』と聞かない」 →  0.91  ◀ 閾超え
   「見つけた問題は直してから止まる」            →  0.31
        │
        ▼
exit 2 ＋ stderr に 1 行 → エージェントは作業を続ける
```

同じ停止の 2 回目は必ず通すので、差し戻しは 1 停止につき最大 1 回。納得できなければエージェントは理由を 1 行書いて止まる。誤検知の代償は 1 文。見逃しの代償はあなたの往復 1 回。再現率寄りに調整する。

## インストール

jev の鍵が要る。どちらか一方で動く:

- **TypeSafe**（直接）: https://console.typesafe.ai/keys
- **Vercel AI Gateway**: https://vercel.com/ai-gateway、モデル `typesafe-ai/jev`

1 停止あたり入力 1,000〜2,000 トークン。jev の価格なら約 0.01 セント。ヘビーに使っても 1 日数セント。

### Claude Code

```
/plugin marketplace add noplan-inc/limpet
```
```
/plugin install limpet@limpet
```

インストール時に Claude Code が鍵と閾を聞く（鍵はセキュアストレージに入り、`settings.json` には書かれない）。シェルからなら:

```sh
claude plugin marketplace add noplan-inc/limpet
claude plugin install limpet@limpet --config typesafe_api_key=... --config block=0.8
```

### Codex

```sh
codex plugin marketplace add noplan-inc/limpet
codex plugin add limpet@limpet
```

その後 `codex` を起動して `/hooks` を開き、limpet の Stop hook を信頼する。Codex のプラグインは秘密情報を持てないので、鍵は `~/.limpet/env` に置く:

```sh
mkdir -p ~/.limpet && echo 'TYPESAFE_API_KEY=...' >> ~/.limpet/env
```

### Claude 形式の hooks を持つ他のエージェント

```sh
git clone https://github.com/noplan-inc/limpet ~/limpet
```

`python3 ~/limpet/limpet.py` を走らせる `Stop` hook（timeout 30）を `~/.claude/settings.json` や `~/.codex/hooks.json` など、そのエージェントの hooks 設定に足し、鍵を `~/.limpet/env` に置く。

## ルール

初回起動時に limpet は既定のルールを `~/.limpet/rules.md` にコピーする。そのファイルを編集する。`- ` で始まる行だけがルール。言語は問わない。

```markdown
- テストを走らせずに「完了」と言わない
- 既に依頼された作業に「始めますか？」と許可を取らない。戻せない操作だけ聞く
- 人にしかできないこと以外を人に振らない
- 見つけた問題は直してから止まる。「CI が落ちています」で止まらない
- 待つときは見込み時間を書く
- Don't say "done" without running the tests
```

[既定のルール](rules.md)は、作者のエージェントが実際に破る 9 本。

## ルールを見つける

どのルールが要るかは、推測しなくていい。limpet は自分の transcript を読んで教えてくれる:

```sh
python3 ~/limpet/limpet.py suggest            # 直近 30 日。--days 90 --max 5000 で広げる
```

エージェントが止まってあなたが返した場面を全部拾い、jev に「あなたはどう反応したか（催促・訂正・質問・次の依頼）」と「エージェントは何を間違えたか」を聞いて、`~/.limpet/suggest.md` に書く:

```
## What went wrong (598 stops the human pushed back on)

### handoff: 259 (43%) — limpet can target this at stop time
- agent: コマンドを用意したので、次を貼って実行してください。
  human: さすがにファイルにかいてくれん？
### overreach: 87 (15%) — limpet can target this at stop time
- agent: 招待はまだできていません。他のプロジェクトも…
  human: ちょっと勝手に広げないでよ。なんでその他のやつもやろうとしてんの？
### unverified: 60 (10%) — not visible at stop time
- agent: PR #339 へ push しました。3 人の再レビュー、必須修正なし。
  human: ci failed

## Suggested rules
<!-- handoff: 43% of your bad stops -->
- Don't ask "shall I start?" for work that was already requested …
```

1,500 停止で約 100 秒・15 セント。Claude Code と Codex の transcript を読む。「停止時には見えない型」（後で CI が落ちる成功報告、単に間違った修正）は正直にそう書くので、limpet が何を取れて何を取れないかが分かる。

## 閾

まず**影の運転**から始める。閾は未設定。全停止が採点されて `~/.limpet/log.jsonl` に記録されるだけで、何も止めない。1〜2 日回したら:

```sh
python3 ~/limpet/limpet.py --stats
```

```
312 stops in /Users/you/.limpet/log.jsonl
p50=0.06 p90=0.21 p95=0.30 max=0.97 blocked=  0  既に依頼された作業に「始めますか？」…
p50=0.12 p90=0.44 p95=0.60 max=0.99 blocked=  0  人にしかできないこと以外を人に振らない…
p50=0.09 p90=0.25 p95=0.35 max=0.93 blocked=  0  見つけた問題は直してから止まる…
```

効かせたいルールについて p95 あたりに閾を置く。ルールごとでも全体でもよい:

```
LIMPET_BLOCK="始めますか=0.30,人に振らない=0.60,直してから止まる=0.35"
LIMPET_BLOCK="0.8"
```

閾の無いルールは止めない。キーはルール文の部分文字列なら何でもよい。

## 他との違い

エージェントを押し戻す Stop hook は、大きく 2 派ある。

- **正規表現派**: [checkpoint-guard](https://github.com/platcrest/checkpoint-guard)、[llm-dark-patterns](https://github.com/waitdeadai/llm-dark-patterns)、[cc-enforcer](https://github.com/skymanbp/cc-enforcer) など。無料で即時だが、英語の言い回しに一致させる方式。新しい止まり方が出るたびにパターンが増え、他の言語のルールは書けない。
- **Claude に判定させる派**: Claude Code 標準の `type: "prompt"` hook や [superpowers](https://github.com/obra/superpowers) の judge スクリプトなど。ルールの意味は分かるが、停止のたびにモデル 1 回分の遅延と費用がかかる。

limpet はその間にいる。ルールは任意の言語の自然言語で、判定は yes/no 質問専用のモデルがやる。判決ではなく確率が返るので、閾は固定のプロンプトを信じるのではなく、自分のログからルールごとに決められる。

## 設定

環境変数か、`~/.limpet/env` の `KEY=VALUE` 行。Claude Code プラグインの設定項目も同じ名前に対応する。

| 変数 | 既定 | |
|---|---|---|
| `TYPESAFE_API_KEY` | | TypeSafe の鍵。`api.typesafe.ai` を直接呼ぶ |
| `AI_GATEWAY_API_KEY` | | Vercel AI Gateway の鍵。どちらか一方でよい。両方あれば TypeSafe が優先 |
| `LIMPET_KEY_CMD` | | 鍵を出力するシェルコマンド。パスワードマネージャ用。`op read op://vault/item/password` |
| `LIMPET_PROVIDER` | `vercel` | `typesafe` か `vercel`。`LIMPET_KEY_CMD` を使うときだけ必要 |
| `LIMPET_BLOCK` | 未設定 | 閾。上記参照。未設定は影の運転 |
| `LIMPET_RULES` | `~/.limpet/rules.md` | ルールファイル |
| `LIMPET_LOG` | `~/.limpet/log.jsonl` | 1 停止 1 行の JSON。確率・遅延・トークン・止めたルール |
| `LIMPET_JEV_MODEL` | `jev-latest` / `typesafe-ai/jev` | プロバイダに送るモデル ID |

## 失敗時

鍵が無い、API エラー、タイムアウト: 黙って exit 0。サービスが落ちているからといって hook が仕事を止めてはいけない。エラーはログに残る。

## プライバシー

1 停止ごとに送るのは、直前 3 メッセージ、このターンのツール呼び出し 1 行ずつ（ツール名と主引数）、最後の発話。`suggest` はこれに加えてあなた自身の返しも送る。編集内容（diff）は送らない。選んだプロバイダにだけ送る。それ以外はマシンから出ない。ログと提案は `~/.limpet` に残る。

## テスト

```sh
python3 test_limpet.py
```

1 秒未満で終わり、API は呼ばない。CI は Linux と macOS、Python 3.9 と 3.13 で回す。

## ライセンス

MIT. Made by [no plan inc.](https://github.com/noplan-inc)
