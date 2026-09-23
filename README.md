# Motor ECU Bug Hunter

C製モータ制御ECUをPythonでSIL実行し、要求仕様と100周期の実行トレースをclassifier.dev / Jevに判定させる実験です。正常版30ケースと、1ケースにつき1つだけ有効にした既知変異30ケースを比較します。

日本語の[要件仕様書 v0.1](docs/MOTOR_ECU_REQUIREMENTS.md)に要件ID、状態遷移、優先順位、境界条件、受入テスト例を定義しています。専用の「要件仕様書レビュー」画面で、本書の51要件をJevへ送信し、記述の品質を8観点で判定できます。SILトレースの評価用仕様は引き続き`requirements.json`です。

## 要件仕様書をJevでチェック

`start.bat`で起動し、[要件レビュー画面](http://127.0.0.1:8767/requirements)を開きます。自由入力では「異常が発生した場合、装置は速やかに停止する。」を初期表示します。「仕様書」では51要件から選択、または全件を実行できます。判定は実行ボタンを押したときに行われます。

| 観点 | Jevへ指定するラベル |
|---|---|
| 曖昧性 | `ambiguous` / `clear` |
| 検証可能性 | `testable` / `not_testable` |
| 単一要件性 | `atomic` / `compound` |
| 主体 | `specified` / `missing` |
| トリガ条件 | `specified` / `missing` |
| 完了条件 | `specified` / `missing` |
| 定量条件 | `sufficient` / `insufficient` |
| 安全関連 | `safety_related` / `ordinary` |

8観点は別々に判定します。主体・トリガ・完了条件の`specified`は記述の存在を表し、その記述が十分に明確であることまでは意味しません。条件節と1つの結果からなる要件は、それだけで`compound`とはしません。数値を必要としない要求に、不必要な数値条件を求めません。`safety_related`は重要度の分類であり、記述上の欠陥として数えません。

「一文のみ」はその要件本文だけを送ります。「仕様書の文脈付き」は定義・単位・優先順位等も渡し、文書の中で解釈した結果を得ます。入力条件はレポートに記録します。仕様書に作者が付けたSC/F/Mの重要度や検証方法、既存のバグ正解・テスト結果は判定入力から除きます。

「要確認」は最初の7観点に`ambiguous / not_testable / compound / missing / insufficient`のいずれかがある場合です。通信失敗や不正な応答は保留として残します。表示される確率・confidenceはAPIの値をそのまま保存し、実測正解率とは扱いません。ラベルを掛け合わせた「総合正確率」は計算しません。

```powershell
# 提示された例文を、その一文だけで8観点評価
python requirement_review.py --sample

# 要件仕様書51件を、文脈付きで8観点評価
python requirement_review.py --spec

# 自由な要件文
python requirement_review.py --text "非常停止入力が有効な周期には、モータPWM指令を0%にすること。"

# TypeSafe APIに直接送信する場合
python requirement_review.py --sample --provider typesafe
```

classifier.devはfastを使い、1要件につき8分類を消費します。結果は`artifacts/requirement_reviews/<run_id>/`へ保存します。SILの比較レポートとは別に、入力、8問の定義、モデル、確率、HTTP応答時間、エラーを記録します。画面から最新レビューのJSONを保存できます。

2026-09-23に実行した[サンプル文と51要件の実測結果](docs/REQUIREMENT_REVIEW_RESULTS.md)も参照できます。

## 起動

Windowsでは **`start.bat` をダブルクリック**してください。ブラウザで `http://127.0.0.1:8767` が開きます。既存のゲームランチャーにも「Motor ECU Bug Hunter」が表示されます。

```powershell
python launch.py
```

画面でプロバイダとseedを選び、実験を開始します。外部サービスへの送信は実行ボタンを押したときだけです。`classifier.dev` は要求仕様・初期条件・合成トレースを匿名のfast APIに送信します。`off` はネットワークを使わずC実行と従来ルールの評価だけを行います。

必要環境はPython 3.10以降とCコンパイラです。Python追加パッケージは不要です。GCC/Clang、またはWindowsのVisual Studio C++ Build Toolsを検出します。このPCでは既存MSVCを使います。初回だけC共有ライブラリをビルドし、ソースのハッシュが変わると再ビルドします。

## CLI

```powershell
# オフライン実験
python run.py --provider off --seed 20260920

# 60ウィンドウ × 5問 = 300分類を実APIで実行
python run.py --provider classifier --seed 20260920

# 小さな通信確認。評価されないケースは判定保留になる
python run.py --provider classifier --max-windows 4

# TypeSafe直接接続。環境変数 TYPESAFE_API_KEY が必要
python run.py --provider typesafe

# ダッシュボードのポート変更
python launch.py --port 8768

# 自動テスト。外部APIへの推論リクエストは送らない
python -m unittest discover -s tests -v

# Chrome/Edgeで画面・フィルター・オフライン実行を確認
python tests/browser_smoke.py
```

TypeSafe用キーはclassifier.devに転送しません。classifier.devへの接続が失敗しても別プロバイダへ自動で切り替えません。ただしclassifier.dev自身が代替モデルを返す場合があるため、応答の実モデル名を各判定に記録します。[API仕様](https://classifier.dev/developers)、[TypeSafe API](https://docs.typesafe.ai/api)

## 制御とSIL

状態は `OFF → STARTING → RUNNING → STOPPING → OFF` と `FAULT / EMERGENCY_STOP`。1周期10msです。E-STOP、電流・温度閾値、通信断、リミット、センサ有効性、起動・停止時間、PWM変化量、エンコーダwrap、故障解除、STARTのエッジを扱います。

- [controller.c](controller.c)：実際にコンパイル・実行するCコントローラ。変異0は正常版、1〜30は個別の変異です。
- [requirements.json](requirements.json)：要求、閾値、単位、評価上の制約。
- [mutations.json](mutations.json)：30種類の変異、発火シナリオ、期待される違反。
- [simulation.py](simulation.py)：慣性を持つPythonプラント、刺激の生成、Cの実行、従来検査。

正常CでPythonプラントを駆動して入力テープを記録し、正常版と変異版に**同じ入力列を再生**します。変異版を閉ループで回した安定性試験ではありません。温度、電流、停止操作などを強制する境界試験も含みます。各ケースは最初から決めた連続100周期で、異常箇所を正解から選び直しません。

16bit境界と1000回目のSTARTは、初期カウンタを境界付近に設定して短時間で到達させます。65536周期・1000回の完全な操作を実行したという意味ではありません。ISR競合と`volatile`欠落は、C内で再現する古い入力・割り込みタイミングの**振る舞いモデル**です。実ハードウェアの競合やコンパイラ最適化による不具合の再現は対象外です。

正解ラベルは単なる「変異ON」では決めません。正常トレースが要求由来の検査に合格し、変異版の状態・PWM・ブレーキ・エンコーダ差分に正常版との違いが実際に出たことを確認します。変異が発火しない実験はエラーにします。正解の根拠と差分周期をローカルに保存します。この正常実装と人手で作った仕様の妥当性は形式証明ではありません。

## Jevへの入力と判定

送信するのは要求の本文、単位、初期条件、入力と出力のCSVだけです。Cソース、変異ID・名称、正解、従来検査の結果、正常版との比較は送信しません。100周期が入力上限を超えた場合は切り捨てず停止します。送信順序もseedでシャッフルします。

| 問い | ラベル |
|---|---|
| Q1 異常 | NORMAL / SUSPICIOUS / FAULT |
| Q2 推定カテゴリ | TIMING / STATE_TRANSITION / INTERLOCK / INITIALIZATION / OVERFLOW / CONCURRENCY / SENSOR / OTHER |
| Q3 重大度 | SAFETY_CRITICAL / FUNCTIONAL / MINOR |
| Q4 SILを継続するか | YES / NO |
| Q5 詳細解析が必要か | YES / NO |

Q4は次のSIL試験を進めるかの助言で、実験エンジンや実機の制御には使いません。Q2の原因推定も正しい原因を確定したものではありません。明示的な違反は従来の決定的ルールで検査します。

Q1の `P(SUSPICIOUS) + P(FAULT) >= 0.5` を検出と数えます。閾値はこの実測を見る前に固定しています。Q1〜Q5は独立と仮定して乗算しません。未応答・不正応答・確率なしは定量評価で保留となり、正常や誤検出ゼロには置き換えません。

`confidence`はプロバイダが返す確信度です。実測の正解率や安全性の確率ではありません。元の確率分布と確信度を保存し、Q1についてBrier score、混同行列、誤検出率とWilson 95%区間も集計します。プロバイダ間ではconfidenceの定義が異なる場合があります。[TypeSafeのconfidence説明](https://docs.typesafe.ai/confidence)

## レポートと再現性

`artifacts/<run_id>/` に以下が保存され、ダッシュボードは最後の `artifacts/latest.json` を読みます。

| ファイル | 内容 |
|---|---|
| `report.json` / `report.md` | 実測の集計とケース別判定 |
| `suite.json` | 正解を含むローカル評価データ、入力・出力、正常版、差分周期 |
| `windows.jsonl` | プロバイダに渡す入力文字列とローカル照合用ID |
| `oracle.json` | 各問の確率・モデル、HTTP試行の時間とusage、エラー |
| `manifest.json` / `sources/` | seed、環境、ソースのSHA-256と当時のスナップショット |

ソースを変えなければ同じseedのSIL結果は再現できます。外部APIの応答はモデル更新等により変わり得ます。入力ハッシュをケースごとに保存します。検出数は**バグ種類単位**、誤検出率は**採点済み正常ウィンドウ単位**です。部分評価では「検出済み数 / 全変異数」は下限であり、未評価の変異を見逃しと断定しません。

これは発火条件を既知とした探索用コーパスで、未知不具合への一般性能・本番誤検出率を測ったものではありません。正常例も30件なので、小さな誤検出率を精密には推定できません。次の評価では仕様・変異・seedを増やし、閾値を決める開発セットと最終評価セットを分けてください。

費用が返らないときは不明と表示します。classifier.devの内部推論コストを利用者への課金額とは扱いません。100万件の性能・費用は未実測です。無料fastには現在1日20,000分類/IPの上限があり、5問は5分類を消費します。100万ケースをこの無料枠で即時処理できるとは限りません。[公開料金・制限](https://classifier.dev/pricing)

静的解析は実験ごとにMSVC `/analyze`、GCC `-fanalyzer`、Clang `--analyze`の利用できるものを実行し、`static-analysis.json`に診断を保存します。CLIの`--skip-static-analysis`で省略できます。単体では`python static_analysis.py`で実行します。全変異を含む1つの翻訳単位を解析するため、警告数を「30変異のうち何個検出」に読み替えません。カバレッジ測定とCBMC/Lean等の形式検証はこの初版では未実装です。

## 最初の実測結果

2026-09-20、seed `20260920`、classifier.dev fastの実応答モデル`jev-1.13.0`で60ウィンドウ・300分類を実行しました。プロンプト調整や閾値変更でこの結果を上書きしていません。

| 項目 | 実測 |
|---|---|
| 変異の活性化 | 30/30 |
| 従来ルール | 24/30 |
| Jev | 21/30 |
| 従来 + Jev | 28/30 |
| Jev誤検出 | 11/30正常窓（36.7%、Wilson 95%区間21.9〜54.5%） |
| Q1平均confidence | 0.4025 |
| 平均HTTP要求時間 | 1,417.7ms（10窓×1問 / 要求） |
| MSVC静的解析 | 完了、診断0件。変異別の検出数は未測定 |
| 料金・100万件換算 | 未報告・未測定 |

Jevが従来ルールに追加したのは、tickカウンタのwrap、故障解除後の内部禁止フラグ、1000回目のSTART、連続していない低速サンプルの誤蓄積の4種類です。両方が見逃したのは、START押しっぱなしでの再起動と、故障解除時のSTARTエッジ履歴消失の2種類でした。正常ケースの誤検出が多いため、このまま自動の合否ゲートにする成績ではありません。

保存先：`artifacts/20260920T060354Z-95901e/report.json`。ダッシュボードで正常／不具合／AIのみ検出／誤検出を絞り込み、結果を調べられます。
