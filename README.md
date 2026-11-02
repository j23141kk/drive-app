# Landscape Translation System

既存のFastAPIアプリをRender上で実行し、同じ画面からGemini APIを呼び出します。画面の見た目や配置は維持し、ホーム画面用のPWA設定だけを追加しています。

## Renderへの公開

1. [Render](https://render.com/)にアカウントを作成します。
2. プロジェクトをGitHubの**Private**リポジトリに登録し、RenderからそのGitHubアカウントを接続します。`.env`はGitに登録しないでください。
3. Render Dashboardの **New → Blueprint** を選び、対象リポジトリを接続します。ルートの`render.yaml`がDocker Web Service、有料0.5 CPU/512 MBプラン、シンガポールリージョン、1GB永続ディスクを設定します。
4. Blueprint作成画面で、次の環境変数を入力します。値はRenderの画面にだけ入力してください。
   - `GEMINI_API_KEY`: Google AI Studioで作成したGemini APIキー
   - `APP_USERNAME`: このアプリ専用のログイン名
   - `APP_PASSWORD`: 長く推測されにくい、このアプリ専用のパスワード
5. 有料リソースの内容と金額を確認してBlueprintを作成します。初回ビルドとデプロイが完了したら、Renderのサービス画面に表示される`https://...onrender.com`が公開URLです。
6. URLを開き、ブラウザの認証ダイアログへ`APP_USERNAME`と`APP_PASSWORD`を入力します。画面が表示され、資料読み込みが終わったあとに位置情報を許可して動作を確認します。

### 環境変数の登録・変更

Render Dashboardで対象サービスを開き、左側の **Environment** → **Environment Variables** → **Add Environment Variable** を選びます。Keyに`GEMINI_API_KEY`、Valueにご自身のAPIキーを入力します。同じ画面で`APP_USERNAME`と`APP_PASSWORD`も登録します。**Save, rebuild, and deploy**を選ぶと再デプロイされます。APIキーはソースコード、HTML、GitHubには入力しません。

### 公開後の利用

- **Android**: Chromeで公開URLを開き、メニューから「アプリをインストール」または「ホーム画面に追加」を選びます。
- **iPhone**: Safariで公開URLを開き、共有ボタン →「ホーム画面に追加」を選びます。

Renderは`onrender.com`のHTTPS URLを発行するため、GPS利用に必要な安全なコンテキストを使えます。アプリのAPI通信と地図表示にはインターネット接続が必要です。

## 外部APIの利用

住所取得は公開Nominatimを利用します。座標を約100m単位でキャッシュし、サーバー全体で1秒に1回を超えないようにしています。公開Nominatimは容量に限りがあり、定期的な自動照会は強く推奨されていません。利用者や照会量が増えた場合は、商用ジオコーディングサービスまたは自前のNominatimへ切り替えてください。地図タイルにもOpenStreetMapの利用条件が適用されます。

## データと更新

`rag_files`の既存資料をRenderの`/var/data/rag_files`へ初回コピーし、以後は1GBの永続ディスクから読み込みます。Git上のシードファイルが更新され、ディスク側が前回のシードのままなら更新内容を反映します。ディスク上で直接変更されたファイルは上書きしません。サービスのディスクを削除・再作成する前に、Renderからデータを退避してください。データベースは現在使っていません。

Gemini Files API上のアップロードは48時間で自動削除されるため、アプリは24時間ごとにRAGファイルを再アップロードします。Render再起動後は最初に既存URLリストをクロールし、資料が準備できるまでAPIが画面に読込中の案内を返します。

GitHubの接続ブランチへ変更をpushするとRenderが再デプロイします。GitHubへpushする前に`git status`を確認し、`.env`や秘密情報が含まれていないことを確認してください。Geminiの利用料・利用上限はGoogle側で別途適用されます。

## ローカル開発

`.env.example`を`.env`にコピーして必要な値を記入し、`docker compose up --build`で起動します。`.env`は`.gitignore`で除外されています。

## セキュリティと費用

公開サイトはHTTP Basic認証で保護します。Renderでは`APP_USERNAME`と`APP_PASSWORD`を両方設定し、使い回しのない強いパスワードを使ってください。`/healthz`だけはRenderの死活確認用に認証なしで応答します。認証情報はHTTPS上でブラウザの標準ダイアログから送信されます。

Render Freeは15分アクセスがないと停止し、再アクセス時に起動待ちが発生します。またFree Web Serviceには永続ディスクがありません。このアプリの「いつでも利用」とデータ保持にはFreeではなく有料プランを選んでください。公開時のRender価格では0.5 CPU/512 MB Web Serviceが約$7/月、1GB永続ディスクが約$0.25/月です。通信量とGemini利用料は別途かかるため、料金はデプロイ画面で最終確認してください。
