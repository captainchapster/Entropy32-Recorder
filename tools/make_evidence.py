#!/usr/bin/env python3
"""
make_evidence.py

One command from a finished capture to a verified evidence package -
everything between capture_serial_to_csv.py and `git commit`:

  1. check_edges.py     structural + Poisson sanity check of the raw edges
  2. derive             Section 5 reconstruction (derive_evidence.py),
                        pinned to the firmware checkout it claims to match
  3. NIST ea_non_iid    run here, with its stdout/stderr/exit status saved
                        verbatim; the tool's git commit is read from its
                        checkout (and must be the protocol's pinned v1.1.8,
                        unmodified) and the compiler from the binary's own
                        .comment section, instead of typed in by hand
  4. package            package_evidence.py, with setup.json filling in the
                        hardware/source/environment manifest fields
  5. verify             verify_evidence.py over the finished package

On Windows, ea_non_iid (a Linux build) runs inside WSL automatically.
Intermediate files live in a temporary directory - nothing is left in
tools/ except the raw capture you started from.

Settings come from setup.json in the repo root (see its "manifest",
"check_edges" and "tools" sections); flags override it.

Usage:
    python tools/make_evidence.py
    python tools/make_evidence.py --raw tools/raw_edges.csv --capture-id 2026-11-20a
"""
import argparse
import json
import os
import shlex
import string
import subprocess
import sys
import tempfile

TOOLS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)
from derive_evidence import detect_provenance, edge_times, write_derived  # noqa: E402
from package_evidence import PINNED_NIST_COMMIT, PINNED_NIST_VERSION  # noqa: E402


def step(n, title):
    print(f"\n[{n}/5] {title}", flush=True)


def die(msg):
    print(f"\n!! {msg}", file=sys.stderr)
    sys.exit(1)


def run_py(script, *args):
    """Run one of the sibling tools/ scripts, streaming its output."""
    return subprocess.run([sys.executable, os.path.join(TOOLS, script), *args]).returncode


# --- POSIX side (ea_non_iid is a Linux build) --------------------------------

def sh_path(path):
    """Quote a POSIX path for sh, keeping a leading ~/ expandable."""
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def run_sh(script, capture_text=True):
    cmd = ["sh", "-c", script]
    if sys.platform == "win32":
        cmd = ["wsl", "-e"] + cmd
    return subprocess.run(cmd, capture_output=True, text=capture_text)


def posix_view(path):
    """This machine's path as ea_non_iid's shell sees it."""
    path = os.path.abspath(path)
    if sys.platform != "win32":
        return path
    out = subprocess.run(["wsl", "-e", "wslpath", "-a", path],
                         capture_output=True, text=True)
    if out.returncode != 0:
        die(f"couldn't translate {path} for WSL (is WSL installed? `wsl --install`)")
    return out.stdout.strip()


def nist_identity(ea_non_iid):
    ea = sh_path(ea_non_iid)
    proc = run_sh(f"""
        EA={ea}
        [ -x "$EA" ] || {{ echo "missing=1"; exit 0; }}
        d=$(dirname "$EA")
        echo "commit=$(git -C "$d" rev-parse HEAD 2>/dev/null)"
        echo "modified=$(git -C "$d" status --porcelain --untracked-files=no 2>/dev/null | wc -l)"
        echo "compiler=$(tr -c '[:print:]' '\\n' < "$EA" | grep -m1 '^GCC: (')"
    """)
    info = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    if proc.returncode != 0 or info.get("missing"):
        die(f"no executable ea_non_iid at {ea_non_iid} - build it per the README "
            "(step 6) and point setup.json's tools.ea_non_iid, or --ea-non-iid, at it")
    return info


# --- main ---------------------------------------------------------------------

def next_capture_id(status):
    """<capture start date, UTC>, then the first free letter: 2026-10-03a."""
    date = (status.get("capture_start_utc") or "")[:10]
    if not date:
        die("capture_status has no capture_start_utc - pass --capture-id")
    for letter in string.ascii_lowercase:
        if not os.path.exists(os.path.join(REPO, f"entropy32_sp80090b_{date}{letter}")):
            return f"{date}{letter}"
    die(f"no free capture id for {date} - pass --capture-id")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[1],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--raw", default=os.path.join(TOOLS, "raw_edges.csv"),
                   help="raw capture (default tools/raw_edges.csv)")
    p.add_argument("--setup", default=os.path.join(REPO, "setup.json"),
                   help="rig description and tool paths (default setup.json)")
    p.add_argument("--capture-id",
                   help="default: capture start date (UTC) + first free letter")
    p.add_argument("--firmware-dir", help="overrides setup.json tools.firmware_dir")
    p.add_argument("--ea-non-iid", help="overrides setup.json tools.ea_non_iid")
    p.add_argument("--dead-time-ns", type=float,
                   help="overrides setup.json check_edges.dead_time_ns")
    p.add_argument("--allow-unpinned-nist", action="store_true",
                   help="package even if ea_non_iid isn't the protocol's pinned, "
                        "unmodified v1.1.8 checkout")
    args = p.parse_args()

    setup = {}
    if os.path.exists(args.setup):
        with open(args.setup) as f:
            setup = json.load(f)
    setup_dir = os.path.dirname(os.path.abspath(args.setup))
    tools_cfg = setup.get("tools", {})
    firmware_dir = args.firmware_dir or tools_cfg.get("firmware_dir")
    if firmware_dir and not args.firmware_dir:
        firmware_dir = os.path.normpath(os.path.join(setup_dir, firmware_dir))
    ea_non_iid = args.ea_non_iid or tools_cfg.get("ea_non_iid")
    dead_time_ns = (args.dead_time_ns if args.dead_time_ns is not None
                    else setup.get("check_edges", {}).get("dead_time_ns"))

    if not os.path.isfile(args.raw):
        die(f"no raw capture at {args.raw}")
    status_path = os.path.splitext(args.raw)[0] + ".status.json"
    if not os.path.isfile(status_path):
        die(f"no {status_path} - capture_serial_to_csv.py only writes it when a "
            "capture completes, so this capture didn't finish and isn't evidence")
    with open(status_path) as f:
        status = json.load(f)
    if status.get("capture_status") != "COMPLETE":
        die(f"{status_path} says {status.get('capture_status')!r}, not COMPLETE")
    if not firmware_dir or not os.path.isdir(firmware_dir):
        die("need the Entropy32 firmware checkout the derivation is pinned to - "
            "set setup.json tools.firmware_dir or pass --firmware-dir")
    if not ea_non_iid:
        die("need ea_non_iid - set setup.json tools.ea_non_iid or pass --ea-non-iid")

    capture_id = args.capture_id or next_capture_id(status)
    out_dir = os.path.join(REPO, f"entropy32_sp80090b_{capture_id}")
    if os.path.exists(out_dir):
        die(f"{out_dir} already exists - packages are immutable; pick another --capture-id")

    print(f"Building entropy32_sp80090b_{capture_id}/ from {args.raw} "
          f"({status.get('raw_edge_count'):,} edges, "
          f"{status.get('capture_start_utc')} to {status.get('capture_end_utc')})")

    step(1, "check_edges.py")
    check_args = [args.raw] + (["--dead-time-ns", str(dead_time_ns)] if dead_time_ns else [])
    if run_py("check_edges.py", *check_args) != 0:
        die("check_edges.py found structural problems - not packaging this capture")

    with tempfile.TemporaryDirectory(prefix="make_evidence_") as work:
        derived = os.path.join(work, "derived")

        step(2, f"derive (firmware: {firmware_dir})")
        provenance = detect_provenance(firmware_dir, None, None, None)
        if "unknown" in provenance:
            die(f"couldn't read repo/commit/.ino hash from {firmware_dir}: {provenance}")
        _paths, report = write_derived(derived, edge_times(args.raw), *provenance)
        print(f"  {report['raw_edges']:,} edges -> {report['accepted_intervals']:,} "
              f"accepted intervals -> {report['comparison_bits']:,} comparison bits "
              f"({report['ties']} ties, {report['short_interval_rejections']} "
              f"short-interval rejections)")
        print(f"  firmware {provenance[0]} @ {provenance[1][:12]}")

        step(3, f"NIST ea_non_iid ({ea_non_iid})")
        ident = nist_identity(ea_non_iid)
        commit, modified = ident.get("commit", ""), ident.get("modified", "").strip()
        compiler = ident.get("compiler") or "unknown"
        pinned = commit == PINNED_NIST_COMMIT and modified == "0"
        print(f"  tool commit {commit or 'unknown'}"
              f"{'' if modified == '0' else f' with {modified} modified file(s)'}"
              f" - {'pinned ' + PINNED_NIST_VERSION if pinned else 'NOT the pinned build'}")
        print(f"  compiler    {compiler}")
        if not pinned and not args.allow_unpinned_nist:
            die(f"ea_non_iid must be built from an unmodified {PINNED_NIST_VERSION} "
                f"checkout ({PINNED_NIST_COMMIT}) per protocol Section 8 "
                "(or pass --allow-unpinned-nist)")
        # Run from inside derived/ on the bare filename, so the recorded
        # command and NIST's "Opening file" line carry no machine paths.
        command = f"{ea_non_iid} -i -v comparison_bits.bin 1"
        print(f"  running {command} ...", flush=True)
        nist = run_sh(f"cd {shlex.quote(posix_view(derived))} && "
                      f"exec {sh_path(ea_non_iid)} -i -v comparison_bits.bin 1",
                      capture_text=False)
        files = {"stdout": nist.stdout, "stderr": nist.stderr}
        for name, data in files.items():
            with open(os.path.join(work, f"nist_{name}.txt"), "wb") as f:
                f.write(data)  # verbatim bytes, per Section 8
        h = [line for line in nist.stdout.decode(errors="replace").splitlines()
             if line.startswith("H_original")]
        print(f"  exit {nist.returncode}; {h[0] if h else 'no H_original line'}")
        if nist.returncode != 0:
            die("ea_non_iid failed - stderr:\n" + nist.stderr.decode(errors="replace"))

        step(4, "package_evidence.py")
        package_args = [
            "--raw", args.raw, "--capture-status", status_path,
            "--derived-dir", derived, "--capture-id", capture_id,
            "--nist-command", command,
            "--nist-stdout", os.path.join(work, "nist_stdout.txt"),
            "--nist-stderr", os.path.join(work, "nist_stderr.txt"),
            "--nist-exit-status", str(nist.returncode),
            "--nist-tool-commit", commit or "unknown",
            "--nist-compiler-version", f"{compiler} (from the ea_non_iid binary's .comment section)",
            "--out-dir", out_dir,
        ]
        if setup.get("manifest"):
            package_args += ["--setup", args.setup]
        if run_py("package_evidence.py", *package_args) != 0:
            die("package_evidence.py failed")

    step(5, "verify_evidence.py")
    if run_py("verify_evidence.py", out_dir, "--firmware-dir", firmware_dir) != 0:
        die(f"{out_dir} did NOT verify - don't commit it; see the FAIL lines above")

    rel = os.path.relpath(out_dir, REPO)
    print(f"\nDone. To record it:\n  git add {rel}\n  git commit -m \"Add capture {capture_id}\"")


if __name__ == "__main__":
    main()
