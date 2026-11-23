#!/usr/bin/env python3
import argparse
from contextlib import ExitStack, contextmanager
import errno
import ftplib
import getpass
import hashlib
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import re
import select
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import quote, urlencode


def discover_printers():
    printers = {}
    group = "239.255.255.250"
    with ExitStack() as stack:
        probe = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        sockets = [probe]
        busy_ports = []
        for port in (2021, 1990):
            listener = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            try:
                listener.bind(("", port))
            except OSError as error:
                if error.errno != errno.EADDRINUSE:
                    raise
                busy_ports.append(port)
                continue
            listener.setsockopt(
                socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                socket.inet_aton(group) + socket.inet_aton("0.0.0.0"),
            )
            sockets.append(listener)

        probe.bind(("", 0))
        for port in (1900, 1990, 2021):
            query = (
                f'M-SEARCH * HTTP/1.1\r\nHOST: {group}:{port}\r\n'
                'MAN: "ssdp:discover"\r\nMX: 2\r\n'
                'ST: urn:bambulab-com:device:3dprinter:1\r\n\r\n'
            )
            probe.sendto(query.encode("ascii"), (group, port))

        deadline = time.monotonic() + 10
        while (remaining := deadline - time.monotonic()) > 0:
            readable, _, _ = select.select(sockets, [], [], remaining)
            for sock in readable:
                packet, (sender, _) = sock.recvfrom(8192)
                lines = packet.decode("utf-8", errors="replace").splitlines()
                if not lines or not lines[0].startswith(("NOTIFY ", "HTTP/1.1 200 ")):
                    continue
                headers = {}
                for line in lines[1:]:
                    name, separator, value = line.partition(":")
                    if separator:
                        headers[name.lower()] = value.strip()
                if headers.get("nt", headers.get("st", "")).startswith("urn:bambulab-com:device:"):
                    host = str(IPv4Address(sender))
                    name, serial = printers.get(host, ("", ""))
                    printers[host] = (
                        headers.get("devname.bambu.com") or name,
                        headers.get("usn") or serial,
                    )
    return printers, busy_ports


def discover():
    printers, busy_ports = discover_printers()
    for host, (name, serial) in sorted(printers.items()):
        print(f"{host}\t{name!r}\t{serial!r}")
    if not printers and busy_ports:
        ports = ", ".join(map(str, busy_ports))
        print(f"nbc: UDP {ports} in use. Close Bambu Studio to retry listening on those ports.", file=sys.stderr)
    return bool(printers)


def choose_printer(target):
    if target:
        try:
            return str(IPv4Address(target))
        except ValueError:
            pass

    print("Looking for Bambu printers on the local network...")
    discovered, _ = discover_printers()
    if target:
        matches = [host for host, (name, serial) in discovered.items()
                   if target.casefold() in (name.casefold(), serial.casefold())]
        if len(matches) > 1:
            raise ValueError(f"multiple printers match {target!r}; specify an IP address")
        return matches[0] if matches else target

    candidates = sorted((host, name) for host, (name, serial) in discovered.items() if name or serial)
    if not candidates:
        raise ValueError("no identified printers found; run nbc setup with the printer IP address")
    if len(candidates) == 1:
        host, name = candidates[0]
        print(f"Found {name!r} at {host}.")
        return host
    for index, (host, name) in enumerate(candidates, 1):
        print(f"{index}. {name!r} ({host})")
    choice = int(input("Printer number: "))
    if not 1 <= choice <= len(candidates):
        raise ValueError("invalid printer number")
    return candidates[choice - 1][0]


def trust_file():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "nbc" / "printers.json"


def read_trust():
    try:
        data = json.loads(trust_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict) or not all(isinstance(value, str) for value in data.values()):
        raise ValueError("invalid printer trust file")
    return data


def trust_printer(host):
    printers = read_trust()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, 990), timeout=15) as tcp:
        with context.wrap_socket(tcp, server_hostname=host) as tls:
            fingerprint = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
    existing = printers.get(host)
    if existing == fingerprint:
        print(f"Certificate already trusted for {host}.")
        return True
    if existing:
        raise ValueError(f"certificate changed for {host}. Check the printer before updating {trust_file()}")
    print(f"FTPS certificate for {host}\nSHA-256: {fingerprint}")
    if input("Verify this fingerprint, then type yes to trust it: ").strip() != "yes":
        return False
    printers[host] = fingerprint
    path = trust_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        temporary_path = Path(temp.name)
        try:
            json.dump(printers, temp, indent=2)
            temp.close()
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)
    return True


class PrinterFTPS(ftplib.FTP_TLS):
    def __init__(self, fingerprint: str):
        super().__init__(context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT), timeout=15)
        self.context.check_hostname = False
        self.context.verify_mode = ssl.CERT_NONE
        self.fingerprint = fingerprint

    def wrap_socket(self, sock, session=None):
        secured = self.context.wrap_socket(sock, server_hostname=self.host, session=session)
        if hashlib.sha256(secured.getpeercert(binary_form=True)).hexdigest() != self.fingerprint:
            secured.close()
            raise ssl.SSLError("printer certificate fingerprint mismatch")
        return secured

    def getresp(self):
        if self.file is not None and not isinstance(self.sock, ssl.SSLSocket):
            self.file.close()
            self.sock = self.wrap_socket(self.sock)
            self.file = self.sock.makefile("r", encoding=self.encoding)
        return super().getresp()

    def ntransfercmd(self, cmd, rest=None):
        sock, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        try:
            return self.wrap_socket(sock, self.sock.session), size
        except Exception:
            sock.close()
            raise


def printer_serial(certificate):
    try:
        subject = subprocess.run(
            ["openssl", "x509", "-inform", "DER", "-noout", "-subject", "-nameopt", "RFC2253"],
            input=certificate, capture_output=True, check=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.fullmatch(rb"subject=\s*CN=([A-Za-z0-9]+)", subject)
    return match[1].decode("ascii") if match else None


def studio_access_code(serial):
    if sys.platform != "darwin" or serial is None:
        return None
    config = Path.home() / "Library/Application Support/BambuStudio/BambuStudio.conf"
    try:
        codes = json.loads(config.read_text(encoding="utf-8")).get("access_code")
        if isinstance(codes, dict):
            code = codes.get(serial)
            if isinstance(code, str) and code:
                return code
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return None
    return None


def printer_access_code(serial):
    return (
        os.environ.get("NBC_ACCESS_CODE")
        or studio_access_code(serial)
        or getpass.getpass("Printer access code: ")
    )


@contextmanager
def file_browser(host, fingerprint):
    with PrinterFTPS(certificate_fingerprint(fingerprint)) as transport:
        transport.host = host
        with socket.create_connection((host, 6000), timeout=30) as tcp:
            with transport.wrap_socket(tcp) as tls, tls.makefile("rb") as stream:
                yield open_file_browser(tls, stream)


def open_file_browser(tls, stream):
    def send(channel, sequence, payload):
        tls.sendall(struct.pack("<IIII", len(payload), channel, sequence, 0) + payload)

    def receive():
        header = stream.read(16)
        if len(header) != 16:
            raise OSError("printer closed the file browser connection")
        size = struct.unpack("<IIII", header)[0]
        if size > 16 * 1024 * 1024:
            raise OSError("file browser response is too large")
        body = stream.read(size)
        if len(body) != size:
            raise OSError("incomplete file browser response")
        return body

    def send_message(frame, message, chunk=None):
        payload = json.dumps(message).encode()
        if chunk is not None:
            payload += b"\n\n" + chunk
        send(0x0102013F, frame, payload)

    def read_reply(sequence):
        reply = json.loads(receive())
        if not isinstance(reply, dict) or reply.get("sequence") != sequence:
            raise OSError("unexpected file browser response")
        return reply

    code = printer_access_code(printer_serial(tls.getpeercert(binary_form=True))).encode("ascii")
    if not 1 <= len(code) <= 8:
        raise ValueError("the file browser access code must be 1–8 ASCII characters")
    send(0x0101013F, 0, struct.pack("<8s8s", b"bblp", code))
    if receive() != bytes(4):
        raise OSError("printer rejected the file browser login")
    send_message(1, {
        "mtype": 12291, "sequence": 0,
        "req": {"t_av": 1, "mtype": 12289, "peer_t": 3, "pid": uuid.uuid4().hex, "ver": "0.1.0"},
    })
    setup = read_reply(0)
    if setup.get("result") != 0:
        raise OSError(f"file browser setup failed (result {setup.get('result')})")
    return send_message, read_reply


def storage_files(session, volume, sequence):
    send_message, read_reply = session
    options = {"type": "model", "api_version": 2, "notify": "DETAIL"}
    if volume == "internal":
        options["storage"] = "internal"
    send_message(sequence + 1, {"mtype": 12289, "cmdtype": 1, "sequence": sequence, "req": options})
    reply = read_reply(sequence)
    result = reply.get("result")
    if result == 17:
        return None
    if result != 0:
        raise OSError(f"{volume} listing failed (result {result})")
    body = reply.get("reply")
    files = body.get("file_lists") if isinstance(body, dict) else None
    if not isinstance(files, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("name"), str) for entry in files
    ):
        raise OSError(f"invalid {volume} file list")
    return files


def list_storage(session, storage):
    volumes = ("internal", "external") if storage == "all" else (storage,)
    for sequence, volume in enumerate(volumes, 1):
        files = storage_files(session, volume, sequence)
        if files is None:
            print(f"{volume}: storage unavailable")
            continue
        print(f"{volume}: {len(files)} file(s)")
        for entry in files:
            print(f"  {entry['name']}")


def certificate_fingerprint(value):
    fingerprint = value.replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("expected a SHA-256 certificate fingerprint")
    return fingerprint


def list_files(args):
    if args.storage != "ftps" and args.directory is None:
        with file_browser(args.host, args.fingerprint) as session:
            list_storage(session, args.storage or "all")
        return
    with PrinterFTPS(certificate_fingerprint(args.fingerprint)) as ftp:
        ftp.connect(args.host, 990)
        ftp.login("bblp", printer_access_code(printer_serial(ftp.sock.getpeercert(binary_form=True))))
        ftp.cwd(args.directory or "/")
        ftp.voidcmd("TYPE I")
        ftp.prot_p()
        ftp.dir()


def upload_file(args, path):
    with path.open("rb") as source, file_browser(args.host, args.fingerprint) as session:
        size = os.fstat(source.fileno()).st_size
        if not 0 < size < 2**32:
            raise ValueError("upload requires a nonempty file smaller than 4 GiB")
        send_message, read_reply = session
        message = {"mtype": 12289, "cmdtype": 5, "sequence": 1}
        storage = {"internal": "emmc", "external": "udisk"}[args.storage]
        send_message(2, {**message, "req": {
            "type": "model", "storage": storage, "path": path.name, "total": size,
        }})
        reply = read_reply(1)
        if reply.get("result") == 19:
            raise OSError("a file with that name already exists")
        if reply.get("result") != 1:
            raise OSError(f"printer refused the upload (result {reply.get('result')})")
        options = reply.get("reply") or {}
        chunk_kb = options.get("chunk_size")
        if not isinstance(chunk_kb, int) or not 1 <= chunk_kb <= 16384 or options.get("offset", 0) != 0:
            raise OSError("printer requested unsupported upload parameters")
        offset = 0
        fragment = 0
        checksum = hashlib.md5(usedforsecurity=False)
        while offset < size:
            chunk = source.read(min(chunk_kb * 1024, size - offset))
            if not chunk:
                raise OSError("local file became shorter during transfer")
            checksum.update(chunk)
            details = {"frag_id": fragment, "offset": offset, "size": len(chunk)}
            offset += len(chunk)
            if offset == size:
                details["file_md5"] = checksum.hexdigest()
            send_message(fragment + 3, {**message, "req": details}, chunk)
            fragment += 1
        result = read_reply(1).get("result")
        if result != 0:
            raise OSError(f"printer did not confirm the transfer (result {result})")
        print(f"Uploaded {path.name} to {args.storage} storage ({size} bytes).")


def delete_file(args):
    with file_browser(args.host, args.fingerprint) as session:
        files = storage_files(session, args.storage, 1)
        if files is None:
            raise OSError(f"{args.storage} storage unavailable")
        matches = [entry for entry in files if entry["name"] == args.name]
        if len(matches) != 1:
            raise ValueError(f"expected one exact match for {args.name!r}, found {len(matches)}")
        path = matches[0].get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("printer did not return a path for this file")
        print(f"Delete {args.name!r} from {args.storage} storage on {args.host} ({path!r})?")
        try:
            confirmed = input("Type yes to delete: ").strip() == "yes"
        except EOFError:
            confirmed = False
        if not confirmed:
            print("Deletion canceled.")
            return False

        send_message, read_reply = session
        send_message(3, {"mtype": 12289, "cmdtype": 3, "sequence": 2, "req": {"paths": [path]}})
        result = read_reply(2).get("result")
        if result != 0:
            raise OSError(f"printer did not confirm deletion (result {result})")
        print(f"Deleted {args.name!r} from {args.storage} storage.")
        return True


def mqtt_string(value):
    data = value.encode()
    return struct.pack(">H", len(data)) + data


def read_status(tls, stream, serial):
    def send(header, body):
        size = len(body)
        length = bytearray()
        while size >= 128:
            length.append(size % 128 + 128)
            size //= 128
        length.append(size)
        tls.sendall(bytes([header]) + length + body)

    def receive():
        def read(size):
            data = stream.read(size)
            if len(data) != size:
                raise OSError("MQTT connection closed")
            return data

        header = read(1)[0]
        size = 0
        for shift in (0, 7, 14, 21):
            digit = read(1)[0]
            size += (digit & 127) << shift
            if digit < 128 and size <= 4 * 1024 * 1024:
                return header, read(size)
        raise OSError("invalid MQTT packet length")

    client_id = "nbc-" + uuid.uuid4().hex[:16]
    credentials = mqtt_string("bblp") + mqtt_string(printer_access_code(serial))
    send(0x10, b"\x00\x04MQTT\x04\xc2\x00\x1e" + mqtt_string(client_id) + credentials)
    if receive() != (0x20, b"\x00\x00"):
        raise OSError("printer rejected MQTT login")
    report = mqtt_string(f"device/{serial}/report")
    send(0x82, b"\x00\x01" + report + b"\x00")
    if receive() != (0x90, b"\x00\x01\x00"):
        raise OSError("printer rejected the status subscription")
    request = mqtt_string(f"device/{serial}/request")
    send(0x30, request + b'{"pushing":{"sequence_id":"0","command":"pushall"}}')
    deadline = time.monotonic() + 10
    while (remaining := deadline - time.monotonic()) > 0:
        tls.settimeout(remaining)
        header, body = receive()
        if header >> 4 != 3 or not body.startswith(report):
            continue
        message = json.loads(body[len(report):])
        status = message.get("print") if isinstance(message, dict) else None
        if isinstance(status, dict) and "gcode_state" in status:
            send(0xE0, b"")
            return status
    raise OSError("printer did not report its print state")


def printer_status(args):
    with PrinterFTPS(certificate_fingerprint(args.fingerprint)) as transport:
        transport.host = args.host
        with socket.create_connection((args.host, 8883), timeout=10) as tcp:
            with transport.wrap_socket(tcp) as tls, tls.makefile("rb") as stream:
                serial = printer_serial(tls.getpeercert(binary_form=True))
                if not serial:
                    raise OSError("could not read the printer serial from its certificate; check that openssl is installed")
                return read_status(tls, stream, serial)


def show_status(args):
    status = printer_status(args)
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return
    for key, label, unit in (
        ("gcode_state", "State", ""),
        ("subtask_name", "Job", ""),
        ("mc_percent", "Progress", "%"),
        ("mc_remaining_time", "Remaining", " min"),
        ("nozzle_temper", "Nozzle", " °C"),
        ("bed_temper", "Bed", " °C"),
    ):
        if key in status and status[key] not in (None, ""):
            print(f"{label}: {status[key]}{unit}")


def show_filament(args):
    status = printer_status(args)
    slots = []

    def append_slot(source, unit, tray):
        if not isinstance(tray, dict) or tray.get("id") is None:
            return
        slots.append({
            "source": source,
            "unit": unit,
            "slot": str(tray["id"]),
            "material": tray.get("tray_type") or None,
            "name": tray.get("tray_sub_brands") or None,
            "color": tray.get("tray_color") if tray.get("tray_type") else None,
            "remaining_percent": tray["remain"] if isinstance(tray.get("remain"), int) and 0 <= tray["remain"] <= 100 else None,
        })

    ams = status.get("ams") or {}
    for unit in ams.get("ams") or []:
        for tray in unit.get("tray") or []:
            append_slot("ams", str(unit["id"]), tray)
    virtual = status.get("vir_slot") or []
    if virtual:
        for tray in virtual:
            append_slot("virtual", None, tray)
    elif status.get("vt_tray"):
        append_slot("external", None, status["vt_tray"])

    if args.json:
        print(json.dumps(slots, ensure_ascii=False, indent=2))
        return
    if not slots:
        print("No filament slots reported.")
    for slot in slots:
        source = f"AMS {slot['unit']}" if slot["source"] == "ams" else slot["source"].title()
        details = [slot["material"] or "No filament information"]
        if slot["name"] and slot["name"] != slot["material"]:
            details.append(slot["name"])
        if slot["color"]:
            details.append(f"#{slot['color']}")
        if slot["remaining_percent"] is not None:
            details.append(f"{slot['remaining_percent']}%")
        print(f"{source} / slot {slot['slot']}: {', '.join(details)}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="nbc", description="Work with Bambu Lab print files.")
    commands = parser.add_subparsers(dest="command")
    upload_command = commands.add_parser(
        "upload", help="Transfer a sliced file to printer storage."
    )
    upload_command.add_argument("file", type=Path)
    upload_command.add_argument("--storage", choices=("internal", "external"), default="internal")
    upload_command.add_argument("--connect", action="store_true", help="Open the file in Bambu Connect instead.")
    delete_command = commands.add_parser("delete", help="Delete one model file from printer storage.")
    delete_command.add_argument("name", help="Exact name shown by nbc list.")
    delete_command.add_argument("--storage", required=True, choices=("internal", "external"))
    commands.add_parser("open", help="Open a sliced file in Bambu Connect.").add_argument("file", type=Path)
    commands.add_parser("discover", help="Listen for local Bambu printers over SSDP.")
    setup_command = commands.add_parser(
        "setup", aliases=["trust"], help="Find a printer and save its verified FTPS certificate fingerprint."
    )
    setup_command.add_argument("printer", nargs="?", help="Printer IP address, display name, or serial number.")
    list_command = commands.add_parser("list", help="List files on the printer.")
    status_command = commands.add_parser("status", help="Read the printer's current state.")
    status_command.add_argument("--json", action="store_true", help="Print the raw status object as JSON.")
    filament_command = commands.add_parser("filament", help="Show the printer's reported filament slots.")
    filament_command.add_argument("--json", action="store_true", help="Print filament slots as JSON.")
    for printer_command in (list_command, status_command, filament_command):
        printer_command.add_argument("host", nargs="?", help="Printer IP address or hostname (default: NBC_HOST).")
        printer_command.add_argument("--fingerprint", help="Trusted SHA-256 fingerprint (default: PRINTER_CERT_SHA256).")
    for printer_command in (upload_command, delete_command):
        printer_command.add_argument("--host", help="Printer IP address or hostname (default: NBC_HOST).")
        printer_command.add_argument("--fingerprint", help="Trusted SHA-256 fingerprint (default: PRINTER_CERT_SHA256).")
    list_command.add_argument("directory", nargs="?", help="List this directory over FTPS.")
    list_command.add_argument("--storage", choices=("all", "internal", "external", "ftps"),
                              help="Select a storage volume (default: all).")
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "discover":
        try:
            if discover():
                return 0
        except OSError as error:
            parser.exit(1, f"nbc: discovery failed: {error}\n")
        parser.exit(1, "nbc: no Bambu SSDP announcements or replies received in 10 seconds\n")
    if args.command in ("setup", "trust"):
        try:
            host = choose_printer(args.printer)
            if trust_printer(host):
                print(f"Setup complete. Run nbc list {host} to list files.")
                return 0
            return 1
        except (OSError, ValueError, EOFError) as error:
            parser.exit(1, f"nbc: could not save printer trust: {error}\n")
    if args.command in ("upload", "open"):
        path = args.file.expanduser().absolute()
        if not path.is_file() or not path.name.lower().endswith(".gcode.3mf"):
            parser.error("provide an existing sliced .gcode.3mf file")

    if args.command in ("list", "status", "delete", "filament") or (args.command == "upload" and not args.connect):
        command = {
            "list": list_command, "status": status_command, "upload": upload_command,
            "delete": delete_command, "filament": filament_command,
        }[args.command]
        if args.command == "list" and args.directory is not None and args.storage not in (None, "ftps"):
            list_command.error("a directory can only be used with FTPS")
        try:
            args.host = args.host or os.environ.get("NBC_HOST")
            args.fingerprint = args.fingerprint or os.environ.get("PRINTER_CERT_SHA256")
            if not args.host or not args.fingerprint:
                printers = read_trust()
                if not args.host and len(printers) == 1:
                    args.host = next(iter(printers))
                if not args.host:
                    command.error("specify the printer IP address or set NBC_HOST")
                args.fingerprint = args.fingerprint or printers.get(args.host)
            if not args.fingerprint:
                command.error("run nbc setup or supply a trusted fingerprint with --fingerprint")
            if args.command == "list":
                list_files(args)
            elif args.command == "status":
                show_status(args)
            elif args.command == "filament":
                show_filament(args)
            elif args.command == "delete":
                return 0 if delete_file(args) else 1
            else:
                upload_file(args, path)
        except ftplib.all_errors + (ValueError,) as error:
            parser.exit(1, f"nbc: {args.command} failed: {error}\n")
        return 0

    try:
        query = urlencode(
            {"path": str(path), "name": path.name, "version": "1.0.0"},
            quote_via=quote,
        )
        url = f"bambu-connect://import-file?{query}"
        if sys.platform == "darwin":
            subprocess.run(["open", url], check=True, capture_output=True)
        elif sys.platform == "win32":
            os.startfile(url)
        else:
            parser.exit(1, "nbc: Bambu Connect handoff requires macOS or Windows\n")
    except (OSError, subprocess.CalledProcessError):
        parser.exit(1, "nbc: could not open the file in Bambu Connect. Check that the app is installed.\n")

    print("Import requested. Open Bambu Connect to select a printer and confirm printing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
