# null-bambu-cli

[English](README.md) | 日本語

`nbc` は、LAN Only Mode をオフにしたまま Bambu Lab のプリンタを操作する Python CLI です。同じネットワーク上のプリンタを検出し、印刷状況やフィラメント情報の確認、スライス済みファイルの一覧取得・転送・削除ができます。

## インストール

Python 3.10 以降が必要です。リポジトリのルートで実行します。

```sh
python -m pip install .
nbc --help
```

macOS または Linux では、チェックアウトした実行ファイルにシンボリックリンクを張る方法もあります。チェックアウトは移動せず、`~/.local/bin` が `PATH` に含まれていることを確認してください。

```sh
mkdir -p ~/.local/bin
ln -s "$PWD/nbc.py" ~/.local/bin/nbc
```

## プリンタのセットアップ

コンピュータとプリンタを同じローカルネットワークに接続して実行します。

```sh
nbc setup
```

SSDP でプリンタを検出します。複数台見つかった場合は対象を選びます。表示された SHA-256 指紋を、信頼できる経路で入手したプリンタ証明書の指紋と照合し、`yes` を入力して保存します。表示名での指定や、IP アドレスを指定して検出を省略することもできます。

```sh
nbc discover
nbc setup H3C
nbc setup 192.168.1.50
```

例の IP アドレスは実際のプリンタのアドレスに置き換えてください。証明書が PEM 形式なら、`openssl x509 -in printer.pem -noout -sha256 -fingerprint` で指紋を確認できます。

## モデルファイルの一覧

```sh
nbc list
nbc list --storage internal
nbc list --storage external
```

保存済みのプリンタが1台なら自動で選ばれます。複数台ある場合は、対象の IP アドレスを引数で指定するか、`NBC_HOST` を設定してください。

通常はプリンタのファイルブラウザ（TCP 6000）を使います。FTPS のルートや特定のディレクトリを確認するには、次のように実行します。

```sh
nbc list --storage ftps
nbc list 192.168.1.50 /cache
```

信頼済みの指紋を直接指定するには、`--fingerprint` または環境変数 `PRINTER_CERT_SHA256` を使います。

## プリンタの状態

```sh
nbc status
nbc status --json
```

保存済みのプリンタ設定を使い、MQTT（TCP 8883）で現在の状態を取得します。プリンタが報告した印刷状態、ジョブ名、進捗、残り時間、温度を表示します。検証済みの証明書からシリアル番号を読み取るため、`openssl` が `PATH` に必要です。

## フィラメント情報

```sh
nbc filament
nbc filament --json
```

`status` と同じ接続を使います。プリンタが報告する AMS や外部スプールのスロットについて、材質、色、推定残量を表示します。スロット ID はファームウェアの報告値です。

## スライス済みファイルの転送

スライス済みのプレートを `.gcode.3mf` として書き出し、`nbc setup` のあとに実行します。

```sh
nbc upload "example.gcode.3mf"
nbc list --storage internal
```

標準の転送先は内蔵ストレージです。対応するプリンタの外部ストレージに送るには `nbc upload --storage external "example.gcode.3mf"` を使います。保存済みのプリンタが複数台ある場合は、`--host` または `NBC_HOST` で選べます。転送したファイルは、後から印刷するために保存されます。

macOS または Windows では、`nbc upload --connect "example.gcode.3mf"` で Bambu Connect を開き、アプリ内で転送を確定することもできます。 [Bambu Connect の連携ガイド](https://wiki.bambulab.com/en/software/bambu-connect)

### macOS で Bambu Studio をヘッドレス実行する

目的のプリンタ、ノズル、印刷設定、フィラメント設定を選んだプロジェクトを `project.3mf` として保存します。次の例では、保存済みの設定でプレート1をスライスし、成功した場合にだけ内蔵ストレージへ転送します。プリンタの設定が済んでいなければ、先に `nbc setup` を実行してください。 [Bambu Studio のコマンドラインガイド](https://github.com/bambulab/BambuStudio/wiki/Command-Line-Usage)

```sh
studio="/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"
"$studio" --slice 1 --export-3mf "plate-1.gcode.3mf" "project.3mf" &&
  nbc upload "plate-1.gcode.3mf"
```

## FAQ

### プリンタ上のファイルを削除するには？

`nbc list` に表示されたファイル名と保存先を指定します。表示されたプリンタ、保存先、パスを確認し、`yes` を入力すると対象の1件が削除されます。

```sh
nbc delete --storage internal "example.gcode.3mf"
```

### アクセスコードの入力を求められるのはなぜ？

まず `NBC_ACCESS_CODE` を参照します。macOS では、次に検証済みの証明書のシリアル番号を使って、Bambu Studio がそのプリンタ用に保存したコードを探します。どちらからも取得できない場合は、プリンタのネットワーク設定にある **Access Code** を入力してください。入力した文字は端末に表示されません。`nbc setup` が保存するのは証明書の指紋です。

### プリンタが見つからない、または UDP ポートが使用中と表示される

Bambu Studio が UDP 2021 を使用している場合があります。`nbc discover` は UDP 1990 でも通知を待ち受け、探索リクエストも送ります。それでも見つからない場合は、Bambu Studio を終了して再試行するか、`nbc setup <プリンタのIPアドレス>` で直接指定してください。

### `storage unavailable` はどういう意味？

プリンタが指定されたストレージを利用できないと応答しました。ストレージが利用できてモデルファイルが空の場合は `0 file(s)` と表示されます。外部ストレージの場合は、USB ドライブなどがプリンタに認識されているか確認してください。FTPS のルートが空でも、`nbc list --storage internal` で内蔵ストレージのファイルが見える場合があります。

### 証明書が変わったと表示されたら？

IP アドレスが対象のプリンタのものか確かめ、新しい証明書の指紋を信頼できる経路で確認してください。CLI は保存済みの指紋の上書きを拒否し、設定ファイルのパスを表示します。

### CLI で印刷を操作できる？

印刷の開始、一時停止、停止は今後の対応予定です。

### Bambu Connect でファイルを開けない

OS に `bambu-connect://` リンクを開けるアプリが登録されている必要があります。Bambu Connect のインストール後、アプリを一度起動してから再試行してください。

## ライセンス

[MIT](LICENSE)
