"""Read the existing F103 firmware via ST-Link and upload local telemetry.

No flashing, resets, halts or automatic model calls. OpenOCD is a child owned by
this bridge, bound to localhost and terminated when the bridge stops.
"""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time

import httpx

from .models import DeviceStatus, Telemetry


class DeviceReadError(Exception):
    pass


@dataclass(frozen=True)
class Snapshot:
    counter: int
    heart_rate: float
    finger: bool
    hr_valid: bool
    weak: bool
    spo2: int
    spo2_valid: bool
    overflow: int


def decode_snapshot(text: str, address: int) -> Snapshot:
    """Reject partial/misaligned reads instead of filling them with old values."""
    words = []
    next_address = address
    for line in text.splitlines():
        match = re.match(r"\s*0x([0-9a-fA-F]+):\s+(.*)", line)
        if not match:
            continue
        tokens = match[2].split()
        if int(match[1], 16) != next_address or not tokens or any(
                re.fullmatch(r"[0-9a-fA-F]{8}", token) is None for token in tokens):
            raise DeviceReadError("invalid_memory_read")
        words.extend(int(token, 16) for token in tokens)
        next_address += len(tokens) * 4
    if len(words) != 14:
        raise DeviceReadError("incomplete_memory_read")
    finger, valid = (words[4] >> 16) & 255, (words[4] >> 24) & 255
    weak, spo2_valid = words[8] & 255, (words[8] >> 24) & 255
    hr, spo2 = (words[4] & 65535) / 10, (words[8] >> 16) & 255
    if any(flag not in (0, 1) for flag in (finger, valid, weak, spo2_valid)):
        raise DeviceReadError("invalid_firmware_flags")
    if (valid and not 0 < hr <= 250) or (spo2_valid and not 0 < spo2 <= 100):
        raise DeviceReadError("invalid_firmware_measurement")
    return Snapshot(words[0], hr, bool(finger), bool(valid), bool(weak), spo2, bool(spo2_valid), words[7])


class CounterTracker:
    def __init__(self, stale_after: float = 3):
        self.last = None
        self.last_change = None
        self.stale_after = stale_after

    def observe(self, counter: int, now: float):
        """First observation/reset is pending; a changing counter proves sampling."""
        if self.last is None:
            self.last, self.last_change = counter, now
            return "pending", True
        if counter != self.last:
            delta = (counter - self.last) & 0xffffffff
            status = "live" if 0 < delta < 0x80000000 else "pending"
            self.last, self.last_change = counter, now
            return status, True
        return ("stalled" if now - self.last_change >= self.stale_after else "pending"), False


def telemetry_from_snapshot(device_id: str, snapshot: Snapshot | None, sampling_status: str):
    good = snapshot is not None and sampling_status == "live" and snapshot.finger and not snapshot.weak
    hr = snapshot.heart_rate if good and snapshot.hr_valid else None
    spo2 = snapshot.spo2 if good and snapshot.spo2_valid else None
    quality = "good" if hr is not None or spo2 is not None else "unknown"
    if snapshot and sampling_status == "live" and (not snapshot.finger or snapshot.weak):
        quality = "poor"
    return Telemetry(
        device_id=device_id, timestamp=datetime.now(timezone.utc), source="stm32",
        heart_rate=hr, spo2=spo2, signal_quality=quality,
        reading_valid=hr is not None or spo2 is not None,
        device_status=DeviceStatus(
            transport="stlink", sampling_status=sampling_status,
            sample_counter=snapshot.counter if snapshot else None,
            finger_detected=snapshot.finger if snapshot else None,
            firmware_heart_rate_valid=snapshot.hr_valid if snapshot else None,
            firmware_spo2_valid=snapshot.spo2_valid if snapshot else None,
            signal_weak=snapshot.weak if snapshot else None,
            fifo_overflow_count=snapshot.overflow if snapshot else None,
        ),
    )


class DebugChannel:
    def __init__(self, sock):
        self.sock = sock
        self._receive()

    def _receive(self):
        data = b""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise DeviceReadError("debug_channel_closed")
            data += chunk
            if len(data) > 65536:
                raise DeviceReadError("debug_response_too_large")
            if data.endswith(b"> "):
                return data.decode(errors="ignore").replace("\x00", "")
        raise DeviceReadError("debug_channel_timeout")

    def command(self, text):
        self.sock.sendall((text + "\n").encode())
        return self._receive()


class OpenOCDReader:
    def __init__(self, project: Path, toolchain: Path, openocd: str, runtime: Path):
        self.project, self.toolchain = project, toolchain
        self.openocd, self.runtime = openocd, runtime
        self.process = self.sock = self.log = self.temp = None

    def __enter__(self):
        try:
            self._open()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _open(self):
        elf = self.project / "build/f103-blink.elf"
        binary = self.project / "build/f103-blink.bin"
        for path in (elf, binary, self.toolchain / "arm-none-eabi-nm", self.toolchain / "arm-none-eabi-objcopy"):
            if not path.is_file():
                raise DeviceReadError("missing_firmware_or_toolchain")
        symbols = subprocess.check_output([str(self.toolchain / "arm-none-eabi-nm"), "-S", "--defined-only", str(elf)], text=True)
        rows = [line.split() for line in symbols.splitlines() if line.split() and line.split()[-1] == "hr"]
        if len(rows) != 1 or int(rows[0][1], 16) != 56:
            raise DeviceReadError("firmware_layout_not_supported")
        self.address = int(rows[0][0], 16)
        if not 0x20000000 <= self.address <= 0x20005000 - 56:
            raise DeviceReadError("firmware_address_not_in_sram")
        self.temp = tempfile.TemporaryDirectory(prefix="guardian-stm32-", dir=self.runtime)
        compiled = Path(self.temp.name) / "compiled.bin"
        subprocess.run([str(self.toolchain / "arm-none-eabi-objcopy"), "-O", "binary", str(elf), str(compiled)],
                       check=True, capture_output=True)
        expected = binary.read_bytes()
        if compiled.read_bytes() != expected:
            raise DeviceReadError("elf_binary_mismatch")
        with socket.socket() as check:
            check.bind(("127.0.0.1", 0))
            port = check.getsockname()[1]
        self.log = (self.runtime / "stm32-openocd.log").open("w")
        self.process = subprocess.Popen([
            self.openocd, "-f", "interface/stlink.cfg", "-f", "target/stm32f1x.cfg",
            "-c", "bindto 127.0.0.1", "-c", "gdb_port disabled", "-c", "tcl_port disabled",
            "-c", f"telnet_port {port}", "-c", "init",
        ], stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise DeviceReadError("openocd_start_failed")
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=0.5)
                self.sock.settimeout(0.5)
                break
            except OSError:
                time.sleep(0.1)
        if self.sock is None:
            raise DeviceReadError("openocd_start_timeout")
        self.channel = DebugChannel(self.sock)
        state = self.channel.command("stm32f1x.cpu curstate")
        if re.search(r"\brunning\b", state) is None:
            raise DeviceReadError("firmware_not_running")
        board = Path(self.temp.name) / "board.bin"
        self.channel.command(f'dump_image "{board}" 0x08000000 {len(expected)}')
        if not board.exists() or board.read_bytes() != expected:
            raise DeviceReadError("board_firmware_mismatch")

    def read(self):
        return decode_snapshot(self.channel.command(f"mdw 0x{self.address:08x} 14"), self.address)

    def __exit__(self, *args):
        if self.sock:
            self.sock.close()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.log:
            self.log.close()
        if self.temp:
            self.temp.cleanup()


@contextmanager
def bridge_lock(runtime: Path):
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "stm32-bridge.lock").open("a+") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("STM32 连接程序已在运行，请使用已有连接。") from None
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def upload(client, url, packet):
    try:
        response = client.post(url.rstrip("/") + "/v1/telemetry", json=packet.model_dump(mode="json"))
        response.raise_for_status()
        return True
    except httpx.HTTPError:
        print("暂时无法上传，请检查 Agent 是否运行；继续尝试连接。", flush=True)
        return False


def run(args):
    runtime = Path(__file__).resolve().parent.parent / ".runtime"
    last = None
    with bridge_lock(runtime), httpx.Client(trust_env=False, timeout=3) as client:
        try:
            while True:
                try:
                    with OpenOCDReader(args.project, args.toolchain, args.openocd, runtime) as reader:
                        print("已核对板上固件，开始接收真实数据。", flush=True)
                        tracker = CounterTracker()
                        while True:
                            last = reader.read()
                            status, changed = tracker.observe(last.counter, time.monotonic())
                            if changed or status == "stalled":
                                packet = telemetry_from_snapshot(args.device, last, status)
                                if upload(client, args.url, packet):
                                    print(f"{args.device}: 心率={packet.heart_rate} 血氧={packet.spo2} "
                                          f"采样={status} 有效={packet.reading_valid}", flush=True)
                            time.sleep(args.interval)
                except (DeviceReadError, OSError, subprocess.SubprocessError) as error:
                    upload(client, args.url, telemetry_from_snapshot(args.device, last, "disconnected"))
                    print(f"设备连接暂不可用 ({error})，3 秒后重试。", flush=True)
                    time.sleep(3)
        except KeyboardInterrupt:
            upload(client, args.url, telemetry_from_snapshot(args.device, last, "disconnected"))
            print("STM32 数据连接已停止。", flush=True)


def main():
    parser = argparse.ArgumentParser(description="将现有 STM32 心率固件的只读数据接入 Agent")
    parser.add_argument("--project", type=Path, default=Path.home() / "stm32-projects/f103-blink")
    parser.add_argument("--toolchain", type=Path, default=Path.home() / "stm32-tools/gnu-arm-15.3/bin")
    parser.add_argument("--openocd", default=shutil.which("openocd") or "openocd")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--device", default="stm32_f103_001")
    parser.add_argument("--interval", type=float, default=1)
    args = parser.parse_args()
    if not 0.1 <= args.interval <= 10:
        parser.error("interval must be between 0.1 and 10 seconds")
    # Validate device identity before opening a hardware connection.
    Telemetry(device_id=args.device)
    def stop(signum, frame):
        raise KeyboardInterrupt
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)
    run(args)


if __name__ == "__main__":
    main()
