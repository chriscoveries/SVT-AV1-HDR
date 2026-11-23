#!/usr/bin/env python3
"""Optional native SVT-AV1 Dolby Vision fail-closed smoke test.

Run with a libdovi-enabled SvtAv1EncApp on a disposable CI/test host.
Framefold's real Av1an/VMAF tests remain separate acceptance gates.

A one-record RPU fixture can be supplied locally or downloaded explicitly
from quietvoid/dovi_tool's pinned, public test fixture. No production files
or credentials are read or modified.
"""
import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import tempfile
import urllib.request
from pathlib import Path

RPU_URL = (
    "https://raw.githubusercontent.com/quietvoid/dovi_tool/"
    "41dae646193c12de152333ff5dbc33579cb9ef4a/assets/tests/profile84.bin"
)
RPU_GIT_BLOB_SHA1 = "8e7f2c92a610993c609114ce3d6dbad372f892c2"
FRAMES = 2
WIDTH, HEIGHT = 64, 64


def make_y4m(path):
    # 10-bit 4:2:0, 64x64, two reproducible frames; writes < 40 KiB.
    y = struct.pack("<H", 512) * (WIDTH * HEIGHT)
    uv = struct.pack("<H", 512) * ((WIDTH // 2) * (HEIGHT // 2))
    header = f"YUV4MPEG2 W{WIDTH} H{HEIGHT} F24:1 Ip A1:1 C420p10\n".encode("ascii")
    with path.open("wb") as out:
        out.write(header)
        for frame in range(FRAMES):
            out.write(b"FRAME\n")
            out.write(y)
            out.write(uv)
            out.write(uv)
    assert path.stat().st_size == len(header) + FRAMES * (6 + len(y) + 2 * len(uv))


def one_rpu_record(data):
    # dovi_tool extract-rpu .bin uses Annex B start-code-delimited NALs.
    # Keep the first record so that the test supplies exactly 1 or 2 RPUs.
    starts = []
    for i in range(len(data) - 3):
        if data[i:i + 4] == b"\x00\x00\x00\x01":
            starts.append(i)
    if not starts or starts[0] != 0:
        raise ValueError("expected 4-byte Annex B start code at byte 0")
    record = data[:starts[1]] if len(starts) > 1 else data
    if len(record) < 8:
        raise ValueError("fixture RPU record is too short")
    return record


def checked_fixture(path, fetch):
    if fetch:
        with urllib.request.urlopen(RPU_URL, timeout=20) as response:
            data = response.read(4096)
        header = f"blob {len(data)}\0".encode("ascii")
        assert hashlib.sha1(header + data).hexdigest() == RPU_GIT_BLOB_SHA1, (
            "upstream RPU fixture identity differs from pinned Git blob")
        return one_rpu_record(data)
    if path is None:
        raise ValueError("provide --rpu-bin FILE or --fetch-pinned-rpu")
    return one_rpu_record(path.read_bytes())


def run_command(argv, timeout):
    try:
        return subprocess.run(argv, text=True, capture_output=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"encoder timed out after {timeout}s") from exc


def test_encoder(binary, ffprobe, rpu, folder, timeout, require_decoded_rpu):
    help_result = run_command([str(binary), "--help"], timeout)
    if "--dolby-vision-rpu" not in help_result.stdout + help_result.stderr:
        raise RuntimeError("encoder lacks --dolby-vision-rpu; build with libdovi")
    video = folder / "source.y4m"
    make_y4m(video)
    cases = {"valid_two": rpu * 2, "truncated_one": rpu, "empty_zero": b""}
    results = {}
    for name, blob in cases.items():
        meta, output = folder / f"{name}.rpu", folder / f"{name}.ivf"
        meta.write_bytes(blob)
        proc = run_command([str(binary), "-i", str(video), "-b", str(output),
                            "--preset", "12", "--lp", "1", "--crf", "35",
                            "--input-depth", "10", "--dolby-vision-rpu", str(meta)], timeout)
        results[name] = {"exit_code": proc.returncode,
                         "stderr_tail": proc.stderr[-500:]}
        if name != "valid_two":
            if proc.returncode == 0:
                raise AssertionError(f"{name}: encoder silently accepted incomplete RPU proof")
            continue
        if proc.returncode != 0:
            raise AssertionError("valid RPU fixture rejected: " + proc.stderr[-1500:])
        probe = run_command([str(ffprobe), "-v", "error", "-select_streams", "v:0",
                             "-show_frames", "-show_entries", "frame=side_data_list",
                             "-of", "json", str(output)], timeout)
        if probe.returncode != 0:
            raise AssertionError("ffprobe could not decode the complete positive control")
        frames = json.loads(probe.stdout).get("frames", [])
        if len(frames) != FRAMES:
            raise AssertionError(f"positive control decoded {len(frames)} frames, expected {FRAMES}")
        dovi = sum(any("Dolby Vision" in str(side.get("side_data_type", ""))
                       for side in frame.get("side_data_list", [])) for frame in frames)
        results[name].update(decoded_frames=len(frames), decoded_dovi_frames=dovi)
        if require_decoded_rpu and dovi != FRAMES:
            raise AssertionError(f"decoded only {dovi}/{FRAMES} frame-level Dolby RPUs")
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--encoder", type=Path, help="SvtAv1EncApp built WITH libdovi")
    p.add_argument("--ffprobe", default=shutil.which("ffprobe"), help="decoder/metadata verifier")
    p.add_argument("--rpu-bin", type=Path, help="local Annex B Dolby Vision RPU sample")
    p.add_argument("--fetch-pinned-rpu", action="store_true", help="download & Git-hash verify public dovi_tool fixture")
    p.add_argument("--require-decoded-rpu", action="store_true", help="require ffprobe to expose Dolby metadata on both frames")
    p.add_argument("--self-test", action="store_true", help="verify only deterministic Y4M fixture creation")
    p.add_argument("--timeout", type=int, default=120)
    a = p.parse_args()
    with tempfile.TemporaryDirectory(prefix="dovi-rpu-cli-test-") as tmp:
        folder = Path(tmp)
        if a.self_test:
            video = folder / "source.y4m"
            make_y4m(video)
            print(json.dumps({"self_test": "pass", "y4m_bytes": video.stat().st_size,
                              "y4m_sha256": hashlib.sha256(video.read_bytes()).hexdigest()}, sort_keys=True))
            return
        if a.encoder is None or not a.ffprobe:
            p.error("--encoder and an available ffprobe are required for native tests")
        rpu = checked_fixture(a.rpu_bin, a.fetch_pinned_rpu)
        results = test_encoder(a.encoder, a.ffprobe, rpu, folder, a.timeout,
                               a.require_decoded_rpu)
        print(json.dumps({"native_results": results, "source": "synthetic-y4m",
                          "rpu_records": [2, 1, 0]}, sort_keys=True))


if __name__ == "__main__":
    main()
