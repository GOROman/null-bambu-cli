# null-bambu-cli

English | [日本語](README.ja.md)

`nbc` is a Python CLI for Bambu Lab printers with LAN Only Mode disabled. It discovers printers on your network, checks printer status, and lists or uploads sliced model files.

`nbc` は、LAN Only Mode をオフにしたまま Bambu Lab のプリンタを使うための Python CLI です。プリンタの検出、印刷状況とフィラメント情報の確認、スライス済みファイルの一覧取得・転送・削除ができます。

## Install

Use Python 3.10 or later. From the repository root, run:

```sh
python -m pip install .
nbc --help
```

On macOS or Linux, you can instead link the executable from your checkout. Keep the checkout in place and make sure `~/.local/bin` is on your `PATH`.

```sh
mkdir -p ~/.local/bin
ln -s "$PWD/nbc.py" ~/.local/bin/nbc
```

## Set up a printer

Put your computer and printer on the same local network, then run:

```sh
nbc setup
```

Setup discovers printers over SSDP and prompts you to choose one if it finds several. Compare the displayed SHA-256 fingerprint with one from a trusted copy of the printer certificate, then type `yes` to save it. You can also specify a display name or use the printer's IP address to skip discovery.

```sh
nbc discover
nbc setup H3C
nbc setup 192.168.1.50
```

Replace the sample IP address with your printer's address. If you already have a trusted copy of the certificate in PEM format, you can calculate its fingerprint with `openssl x509 -in printer.pem -noout -sha256 -fingerprint`.

## List model files

```sh
nbc list
nbc list --storage internal
nbc list --storage external
```

With one saved printer, `nbc list` selects it automatically. With several, specify the printer's IP address or set `NBC_HOST`.

The default listing uses the printer's file browser on TCP 6000. To list the FTPS root or a specific FTPS directory, use:

```sh
nbc list --storage ftps
nbc list 192.168.1.50 /cache
```

You can also supply a trusted fingerprint through `--fingerprint` or the `PRINTER_CERT_SHA256` environment variable.

## Check printer status

```sh
nbc status
nbc status --json
```

Status uses the saved printer setup and reads a snapshot over MQTT (TCP 8883). The summary shows the state, job, progress, remaining time, and temperatures when reported. The command needs `openssl` on your `PATH` to read the serial number from the verified printer certificate.

## Check filament

```sh
nbc filament
nbc filament --json
```

Filament uses the same connection as status. It shows material, color, and estimated remaining percentage for the AMS and spool slots reported by the printer. Slot IDs are shown as reported by the firmware.

## Upload a sliced file

Export a sliced plate as a `.gcode.3mf` file. After `nbc setup`, use:

```sh
nbc upload "example.gcode.3mf"
nbc list --storage internal
```

Uploads go to internal storage by default. Use `nbc upload --storage external "example.gcode.3mf"` for external storage on compatible printers. With several saved printers, select one with `--host` or `NBC_HOST`. Uploading stores the file for later printing.

On macOS or Windows you can also use `nbc upload --connect "example.gcode.3mf"` to open the file in Bambu Connect and confirm the transfer there. [Bambu Connect integration guide](https://wiki.bambulab.com/en/software/bambu-connect)

### Slice headlessly with Bambu Studio on macOS

Save a Bambu Studio project as `project.3mf` with the printer, process, and filament profiles you want to use. Run the Bambu Studio executable directly to slice plate 1 using those saved settings. If slicing succeeds, `nbc` uploads the result to the printer's internal storage.

```sh
studio="/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"
"$studio" --slice 1 --export-3mf "plate-1.gcode.3mf" "project.3mf" &&
  nbc upload "plate-1.gcode.3mf"
```

Run `nbc setup` first if you have yet to configure the printer. Use a project saved for your target printer and nozzle. [Bambu Studio command-line usage](https://github.com/bambulab/BambuStudio/wiki/Command-Line-Usage)

## FAQ

### How do I delete a file from the printer?

Use the exact file name shown by `nbc list` and specify its storage volume.

```sh
nbc delete --storage internal "example.gcode.3mf"
```

Review the printer, volume, and path shown, then type `yes` to delete one file.

### Why does `nbc list` ask for a printer access code?

The CLI first checks `NBC_ACCESS_CODE`. On macOS, it then looks up the access code that Bambu Studio saved for the serial number in the verified printer certificate. If neither source provides a code, enter the **Access Code** shown in the printer's network settings. The terminal hides the characters as you type. Setup saves the certificate fingerprint.

### Why did `nbc discover` find nothing or report a busy UDP port?

Discovery uses SSDP over the local network. Bambu Studio can hold UDP port 2021. The CLI also listens on UDP 1990 and sends search requests. If it still finds nothing, close Bambu Studio and retry or run `nbc setup <printer-ip>` to connect directly.

### What does `storage unavailable` mean?

The printer reported that the requested volume is unavailable. An accessible volume with no model files shows `0 file(s)`. For external storage, check that the printer recognizes the inserted drive. An empty FTPS root can coexist with files visible through `nbc list --storage internal`.

### What if setup reports that the certificate changed?

Check that the IP still belongs to your printer and verify the new certificate fingerprint through a trusted source. The CLI declines to replace a previously saved fingerprint and prints the path to the trust file.

### Can I control a print from the CLI?

Starting, pausing, and stopping a print are planned.

### Why could `nbc upload` not open the file in Bambu Connect?

The operating system needs an installed app registered to handle `bambu-connect://` links. Open Bambu Connect once after installing it, then retry the upload.

## License

[MIT](LICENSE)
