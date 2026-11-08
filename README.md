# Landscape Translation System

既存のFastAPIアプリをRender上で実行し、同じ画面からGemini APIを呼び出します。画面の見た目や配置は維持し、ホーム画面用のPWA設定だけを追加しています。

## Renderへの公開

1. [Render](https://render.com/)にアカウントを作成します。
2. プロジェクトをGitHubの**Private**リポジトリに登録し、RenderからそのGitHubアカウントを接続します。`.env`はGitに登録しないでください。
3. Render Dashboardの **New → Web Service** を選び、GitHubリポジトリを接続します。**Runtime: Docker**、**Instance Type: Free**、**Region: Singapore**を選び、Health Check Pathに`/healthz`を設定します。永続ディスクは追加しません。`render.yaml`はBlueprintを使う場合の任意設定で、ここでは使いません。
4. 環境変数の設定欄で、次の値を登録します。値はRenderの画面にだけ入力してください。
   - `GEMINI_API_KEY`: Google AI Studioで作成したGemini APIキー
   - `APP_USERNAME`: このアプリ専用のログイン名
   - `APP_PASSWORD`: 長く推測されにくい、このアプリ専用のパスワード
5. Freeの選択と永続ディスクが未追加であることを確認してWeb Serviceを作成します。初回ビルドとデプロイが完了したら、Renderのサービス画面に表示される`https://...onrender.com`が公開URLです。
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

`rag_files`はDockerイメージ内の`/app/rag_files`から読み込みます。Freeプランには永続ディスクがなく、クロールで生成・更新されたファイルはスリープ、再起動、再デプロイ時に失われます。次回起動時に同梱された元ファイルから再びクロールします。元資料を更新した場合はPrivate GitHubリポジトリへpushし、再デプロイしてください。現在、データベースや利用者がアプリへ保存するファイルはありません。

Gemini Files API上のアップロードは48時間で自動削除されるため、アプリは稼働中に24時間ごとにRAGファイルを再アップロードします。Renderがスリープから復帰すると、まずイメージに同梱した資料を登録して翻訳を利用可能にし、その後URLリストを再クロールして最新資料へ差し替えます。最新資料への更新が終わるまでは同梱資料を使うため、起動直後の案内が早く表示される一方、最新クロール完了前の翻訳は同梱時点の情報に基づく場合があります。Freeサービスの再起動・スリープ復帰後は、この準備処理が再度実行されます。

GitHubの接続ブランチへ変更をpushするとRenderが再デプロイします。GitHubへpushする前に`git status`を確認し、`.env`や秘密情報が含まれていないことを確認してください。Geminiの利用料・利用上限はGoogle側で別途適用されます。

## ローカル開発

`.env.example`を`.env`にコピーして必要な値を記入し、`docker compose up --build`で起動します。`.env`は`.gitignore`で除外されています。

## セキュリティと費用

公開サイトはHTTP Basic認証で保護します。Renderでは`APP_USERNAME`と`APP_PASSWORD`を両方設定し、使い回しのない強いパスワードを使ってください。`/healthz`だけはRenderの死活確認用に認証なしで応答します。認証情報はHTTPS上でブラウザの標準ダイアログから送信されます。

Render FreeのWeb Serviceは15分間アクセスがないと停止し、次のアクセスで起動するまで約1分かかる場合があります。無料枠はワークスペースごとに月750実行時間で、超過すると当月は停止されます。Freeには永続ディスクがなく、Renderが任意に再起動する場合もあります。HTTPS/TLSは利用できます。RenderのWeb Service自体はFreeで利用できますが、Gemini API利用料は別途です。通信・ビルド枠の上限もあるため、課金方法が登録済みの場合はRenderのUsage画面とSpend Limitを確認してください。無料枠は常時即応や永続ファイル保存を保証しません。
