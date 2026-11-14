#!/usr/bin/env python3
"""
DynamicEnabler

Enables the Dynamic Island of macOS 27.3 beta (build 26C5035r) on MacBook Pro models with a
display notch, and integrates it with the physical notch.

Overview
  1. Activation
     MenuBarAgent reads two preferences that control the Dynamic Island, EmulateCenterHUD and
     ShowSoftwareNotch. Each one is guarded by an IsAppleInternalBuild() check, so both are
     ignored on customer builds. The script sets both preferences and replaces the two guarding
     branches (cbz) with nop instructions.

  2. Notch integration (default layout "notch")
     The stock implementation draws the island around a virtual 59 x 34 pt sensor area 7 pt
     below the menu bar. The script replaces that sensor area with the real notch rectangle:
       - Compact and Minimal presentations sit on either side of the notch.
       - Expanded presentations start at the top edge of the screen and wrap around the notch.
       - iPhone-mirrored Live Activities are given a wider fixed width.
       - The square "software notch" window that ShowSoftwareNotch draws over the notch is
         reduced to zero size, so its corners no longer show around the rounded notch.
     Layout "below" keeps the stock 59 pt sensor hidden inside the notch and moves expanded
     presentations below the notch instead. Layout "classic" only performs the activation.
     With the island at the top edge, a small module loaded into MenuBarAgent also makes the
     topmost row of pixels clickable (see "Top edge clicks" below).

  3. Expanded widths in client processes (layout "notch" only)
     The width of each expanded presentation is fixed in the process that renders it (for
     example 260 pt for timers and 286 pt for Now Playing), which leaves too little room once
     the notch occupies the middle. The script hooks the SwiftUI frame initializers in those
     processes and replaces widths in the expanded range:
       - single-row presentations (height 60 pt): notch width + 2 x 100.5 pt
       - taller presentations:                    notch width + 2 x 72 pt
     A few clients size their panels without .frame() and receive dedicated patches (AirDrop,
     and the battery banners shown by ControlCenter). A background helper patches clients
     that start later, such as the timer process when a timer is first started.

  4. Top edge clicks (layouts "notch" and "below")
     WindowServer routes clicks on the transparent island window by the shape of its layers. It
     hit-tests only fully opaque content exactly; for all other content the shape loses its top
     1 pt, so a click with the pointer pushed against the top of the screen went to the menu bar.
     A dylib is compiled into the App Group container that MenuBarAgent may read and loaded with
     lldb. It places an opaque black layer at the bottom of every island hit-test layer, clipped
     by the island's own rounded container, so the clickable shape equals the visible black
     island. Because a filter on an ancestor also disqualifies opaque content, it removes the
     container's "colorInvertDisplayAware" filter while Invert Colors is off, where that filter
     has no effect; with Invert Colors on, the stock layers are left untouched. WindowServer
     sometimes keeps a stale click shape after the island's layers change, so every 0.5 s the
     module asks WindowServer which window owns the top row of each island and, only when that
     check fails, sets the container's filters again, which makes WindowServer recompute it.

Requirements
  - macOS 27.3 beta, build 26C5035r, with the exact MenuBarAgent binary this script was
    verified against. Every patch site is compared with its original bytes before writing.
  - A MacBook Pro with a notch (the built-in display must be active).
  - SIP's debugging restriction lifted ("csrutil enable --without debug" in Recovery), or SIP
    fully disabled. lldb cannot attach to system processes otherwise.
  - The latest Xcode beta, or Command Line Tools that match macOS 27 (for lldb, clang and
    swift). Older tools crash when lldb attaches.
  - Password-free debugging: "sudo DevToolsSecurity -enable" and membership in the _developer
    group. The script tests an lldb attach before it changes anything.

Notes
  - All changes are in-memory patches. They are lost when MenuBarAgent or the Mac restarts.
    Enable "apply automatically at login" to reapply them at every login.
  - The script never modifies SIP or files on the system volume. The only file it leaves behind
    is the top edge module in ~/Library/Group Containers/com.apple.MenuBar, removed by --disable.

Usage
  python3 enable_dynamic_island.py                      interactive setup
  python3 enable_dynamic_island.py --enable [--autostart | --no-autostart] [--layout notch|below|classic]
  python3 enable_dynamic_island.py --disable
"""

import argparse
import json
import os
import plistlib
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time

# ---------------------------------------------------------------------------------------------
# General configuration
# ---------------------------------------------------------------------------------------------

SUPPORTED_BUILD = "26C5035r"
MENUBARAGENT = "/System/Library/CoreServices/MenuBarAgent.app/Contents/MacOS/MenuBarAgent"
MENUBARAGENT_UUID = "DE472913-A81D-35BE-9584-EC597987E015"
DOMAIN = "com.apple.MenuBarAgent"
DEFAULTS = ("EmulateCenterHUD", "ShowSoftwareNotch")

SCRIPT_NAME = "enable_dynamic_island.py"
SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/DynamicEnabler")
LOG_DIR = os.path.expanduser("~/Library/Logs")
WATCHER_LOG = os.path.join(LOG_DIR, "dynamic_island_watcher.log")
AUTOSTART_LOG = os.path.join(LOG_DIR, "dynamic_island_autostart.log")
WATCHER_LABEL = "local.dynamic-island.watcher"
AUTOSTART_LABEL = "local.dynamic-island.autostart"
AUTOSTART_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{AUTOSTART_LABEL}.plist")
LAYOUTS = ("notch", "below", "classic")
# launchd jobs use the Apple-signed interpreter from the Command Line Tools. macOS lists background
# items under the signer of their executable; a python.org build would appear under its developer.
PYTHON = "/usr/bin/python3"


def run(*cmd):
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def fail(message):
    sys.exit("Error: " + message)


def gui_domain():
    return f"gui/{os.getuid()}"


def menubaragent_pid():
    job = run("launchctl", "print", f"{gui_domain()}/{DOMAIN}")
    match = re.search(r"^\s*pid = (\d+)$", job.stdout, re.M)
    return int(match.group(1)) if job.returncode == 0 and match else 0


# ---------------------------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------------------------

def check_sip():
    """Only SIP's debugging restriction blocks lldb; the rest of SIP can stay on."""
    status = run("csrutil", "status").stdout
    if re.search(r"status:\s*disabled", status, re.I) or \
            re.search(r"Debugging Restrictions:\s*disabled", status, re.I):
        return
    sys.exit(
        "System Integrity Protection (SIP) is enabled.\n"
        "This script attaches to MenuBarAgent and other system processes with lldb, which SIP's\n"
        "debugging restriction does not allow. No further steps can be performed until it is lifted.\n\n"
        "To lift only the debugging restriction (recommended; the rest of SIP stays on):\n"
        "  1. Shut down the Mac.\n"
        "  2. Press and hold the power button until \"Loading startup options\" appears.\n"
        "  3. Select Options, then choose Utilities > Terminal.\n"
        "  4. Run: csrutil enable --without debug\n"
        "  5. Restart the Mac and run this script again.\n\n"
        "Fully disabled SIP (csrutil disable) also works. Run \"csrutil enable\" from\n"
        "Recovery to restore full protection.")


def check_environment():
    if run("sw_vers", "-buildVersion").stdout.strip() != SUPPORTED_BUILD:
        fail(f"This script requires macOS 27.3 beta (build {SUPPORTED_BUILD}). "
             "Patch addresses differ between builds.")
    if MENUBARAGENT_UUID not in run("dwarfdump", "--uuid", MENUBARAGENT).stdout.upper():
        fail("MenuBarAgent does not match the verified binary. No changes were made.")
    if run("xcode-select", "-p").returncode or run("xcrun", "--find", "lldb").returncode:
        fail("Xcode is required. Install the latest Xcode beta from https://developer.apple.com/download/all/\n"
             "and select it with: sudo xcode-select -s /Applications/Xcode-beta.app/Contents/Developer\n"
             "(Command Line Tools that match macOS 27 also work; older versions crash when lldb attaches.)")


def check_debug_access():
    """lldb needs debugging rights without a password prompt; otherwise every attach waits for
    the "Developer Tools Access" dialog (and, at login, for nobody) until it times out."""
    in_group = "_developer" in run("id", "-Gn").stdout.split()
    dev_mode = "currently enabled" in run("DevToolsSecurity", "-status").stdout
    if in_group and dev_mode:
        return
    fail("lldb does not have permission to debug other processes without asking for a password.\n"
         "Without it, attaching to MenuBarAgent waits for the \"Developer Tools Access\" dialog\n"
         "and times out, and the dialog appears again after every restart.\n\n"
         "Run these commands, then log out and back in (or restart) and run this script again:\n"
         + ("" if dev_mode else "  sudo DevToolsSecurity -enable\n")
         + ("" if in_group else '  sudo dseditgroup -o edit -a "$USER" -t user _developer\n')
         + "\nNo changes were made.")
    check_lldb_attach()


def check_lldb_attach():
    """Attaches lldb to a short-lived child process. An outdated lldb crashes on attach under
    macOS 27 (SIGILL), and a missing debug right makes it wait for a dialog; both would otherwise
    only surface later as an unconfirmed MenuBarAgent patch."""
    target = subprocess.Popen(["/bin/sleep", "60"])
    try:
        try:
            probe = subprocess.run(
                ["xcrun", "lldb", "--batch", "-o", f"process attach --pid {target.pid}", "-o", "detach"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
                start_new_session=True)
        except subprocess.TimeoutExpired:
            fail("lldb did not attach to a test process within 30 seconds. It may be waiting for the\n"
                 "\"Developer Tools Access\" dialog. Check that \"DevToolsSecurity -status\" reports\n"
                 "enabled and that your user is in the _developer group. No changes were made.")
        if probe.returncode == 0 and "detached" in probe.stdout:
            return
        version = run("xcrun", "lldb", "--version").stdout.strip().splitlines()
        details = "\n".join(line for line in probe.stdout.strip().splitlines()[-5:] if line)
        if probe.returncode < 0:
            details += f"\n(lldb was terminated by signal {-probe.returncode})"
        fail("lldb could not attach to a test process:\n"
             f"{details}\n\n"
             f"Developer directory: {run('xcode-select', '-p').stdout.strip()}\n"
             f"lldb version: {version[0] if version else 'unknown'}\n\n"
             "This usually means that the developer tools are older than macOS 27. Install the\n"
             "latest Xcode beta from https://developer.apple.com/download/all/ and select it with:\n"
             "  sudo xcode-select -s /Applications/Xcode-beta.app/Contents/Developer\n"
             "Then check that \"xcrun lldb --version\" reports a newer version and run this script\n"
             "again. No changes were made.")
    finally:
        target.kill()
        target.wait()


# ---------------------------------------------------------------------------------------------
# Notch geometry
# ---------------------------------------------------------------------------------------------

def notch_geometry():
    """Returns (notch width, notch height) of the built-in display in points, or None."""
    swift = r'''
import AppKit
for s in NSScreen.screens where s.safeAreaInsets.top > 0 {
    if let l = s.auxiliaryTopLeftArea, let r = s.auxiliaryTopRightArea {
        print(r.minX - l.maxX, s.safeAreaInsets.top); break
    }
}
'''
    out = run("xcrun", "swift", "-e", swift).stdout.split()
    if len(out) != 2:
        return None
    # Rounded to 0.5 pt so that every derived constant can be loaded exactly with movz/movk.
    return tuple(round(float(v) * 2) / 2 for v in out)


# ---------------------------------------------------------------------------------------------
# ARM64 encoding helpers
# ---------------------------------------------------------------------------------------------

def enc(word):
    return struct.pack("<I", word)


def bl(pc, target):
    return 0x94000000 | (((target - pc) >> 2) & 0x3FFFFFF)


def b(pc, target):
    return 0x14000000 | (((target - pc) >> 2) & 0x3FFFFFF)


def load_double(xreg, value):
    """movz/movk sequence that loads the bit pattern of a double into an x register."""
    bits = struct.unpack("<Q", struct.pack("<d", value))[0]
    words = [0xD2800000 | (3 << 21) | (((bits >> 48) & 0xFFFF) << 5) | xreg]
    for hw in (2, 1, 0):
        words.append(0xF2800000 | (hw << 21) | (((bits >> (16 * hw)) & 0xFFFF) << 5) | xreg)
    return words


def mov_double(reg, value):
    """movz reg,#x,lsl#32 ; movk reg,#y,lsl#48 (valid when the low 32 bits are zero)."""
    bits = struct.unpack("<Q", struct.pack("<d", value))[0]
    assert bits & 0xFFFFFFFF == 0
    return (enc(0xD2C00000 | (((bits >> 32) & 0xFFFF) << 5) | reg).hex(),
            enc(0xF2E00000 | (((bits >> 48) & 0xFFFF) << 5) | reg).hex())


def fmov_imm(d, value):
    """fmov dD, #imm for values representable as (16..31)/16 x 2^(-3..4)."""
    for exp in range(-3, 5):
        frac = value / 2.0 ** exp * 16 - 16
        if 0 <= frac < 16 and frac == int(frac):
            e = exp + 3
            imm8 = ((0 if e & 4 else 1) << 6) | ((e & 3) << 4) | int(frac)
            return 0x1E601000 | (imm8 << 13) | d
    raise ValueError(value)


def fmov_d_x(d, x): return 0x9E670000 | (x << 5) | d
def fabs(d, n): return 0x1E60C000 | (n << 5) | d
def fcmp(n, m): return 0x1E602000 | (m << 16) | (n << 5)
def fadd(d, n, m): return 0x1E602800 | (m << 16) | (n << 5) | d
def fsub(d, n, m): return 0x1E603800 | (m << 16) | (n << 5) | d
def fmul(d, n, m): return 0x1E600800 | (m << 16) | (n << 5) | d
def dup_hi(d, n): return 0x5E180400 | (n << 5) | d          # mov dD, vN.d[1]
def ins_hi_v(d, n): return 0x6E180400 | (n << 5) | d        # mov vD.d[1], vN.d[0]
def ins_hi_x(d, n): return 0x4E181C00 | (n << 5) | d        # mov vD.d[1], xN
def ins_lo_x(d, n): return 0x4E081C00 | (n << 5) | d        # mov vD.d[0], xN


NOP = 0xD503201F
FMOV_D3_HALF = 0x1E6C1003            # fmov d3, #0.5
MOVI_V1_ZERO = 0x6F00E401            # movi.2d v1, #0
PROLOGUE = [0xA9BF7BFD, 0x910003FD]  # stp x29, x30, [sp, #-16]! ; mov x29, sp
EPILOGUE = [0xA8C17BFD, 0xD65F03C0]  # ldp x29, x30, [sp], #16 ; ret

# ---------------------------------------------------------------------------------------------
# MenuBarAgent patch sites (file virtual addresses in build 26C5035r)
# ---------------------------------------------------------------------------------------------

# IsAppleInternalBuild() guards in front of EmulateCenterHUD and ShowSoftwareNotch (cbz w0, ...).
GATES = [(0x10057FD98, "00060034"), (0x10057A138, "80030034")]

LEAF = 0x1001F7A98              # builds the virtual sensor rectangle (stock: 59 x 34, 7 pt below the menu bar)
RECT_INIT = 0x1005A3E08         # MacSystemUI.Rect.init(rawValue:) stub
GETTER_A = 0x1001F5644          # sensor rectangle getter (60 call sites)
GETTER_B = 0x1001F567C          # second sensor rectangle getter (10 call sites)
EXPANDED_SITES = (0x1001EE4C4, 0x1001EE4F4, 0x1001EE560, 0x1001EE7F4)   # getter calls in the expanded layout
NO_OBSTRUCTION = 0x1003C9650    # b.ne -> nop: expanded scenes report "no obstruction" (layout "below")
TEXT_RANGE = (0x100002A20, 0x100002A20 + 0x5A08D8)
STOCK_SENSOR_W = 59.0
# The gap that the system reports between the two menu bar areas is slightly wider than the
# visible notch. Island geometry is narrowed by this margin on each side so that the idle pill
# does not show a 1 px edge next to the notch.
NOTCH_MARGIN = 1.5

# The square "software notch" window: its frame passes through TO_SCREEN at two call sites
# (creation and update). The cave zeroes the size of the rectangle that equals the notch.
TO_SCREEN = 0x10044B200
NOTCH_WINDOW_SITES = (0x1002F1B48, 0x1002AE284)

# iPhone-mirrored ("remote") Live Activities are rendered at a fixed width of 374 pt and scaled
# by 0.8. The four instructions at REMOTE_WIDTH_SITE return that fixed width for both the
# expanded and the list item metrics; the cave returns a width that leaves REMOTE_SIDE pt on
# each side of the notch. Clients request new content after the change, which takes a few
# seconds.
REMOTE_WIDTH_SITE = 0x1003CFB30
REMOTE_WIDTH_ORIG = ("010080d2", "02008052", "0000ccd2", "e00ee8f2")
REMOTE_SCALE, REMOTE_SIDE = 0.8, 72.0

# Code caves in the zero padding at the end of the __TEXT segment.
CAVE0, CAVE1, CAVE2, CAVE3, CAVE4, CAVE5 = (0x1006A2700, 0x1006A2740, 0x1006A2780, 0x1006A27C0,
                                            0x1006A2800, 0x1006A28C0)
CAVE_SLOTS = {CAVE0: 16, CAVE1: 16, CAVE2: 16, CAVE3: 16, CAVE4: 48, CAVE5: 16}


def arm64e_slice(fat):
    nfat = struct.unpack(">I", fat[4:8])[0]
    for i in range(nfat):
        cpu, sub, off, size, _ = struct.unpack(">iiIII", fat[8 + 20 * i: 28 + 20 * i])
        if cpu == 0x0100000C and (sub & 0xFF) == 2:
            return fat[off: off + size]
    fail("MenuBarAgent has no arm64e slice.")


def build_patches(layout):
    """Returns ([(address, original bytes, new bytes)], (notch width, notch height) or None)."""
    with open(MENUBARAGENT, "rb") as f:
        image = arm64e_slice(f.read())

    def orig_word(addr):
        o = addr - 0x100000000
        return image[o: o + 4]

    patches = [(a, bytes.fromhex(h), enc(NOP)) for a, h in GATES]

    geometry = notch_geometry()
    if layout == "classic":
        # Classic layout still needs to hide the software notch window
        if geometry:
            real_notch_w, notch_h = geometry
            # CAVE4: calls TO_SCREEN (v0 = origin, v1 = size) and zeroes the size when it equals the
            # notch within 1 pt. Other windows are returned unchanged.
            c4 = PROLOGUE + [bl(CAVE4 + 8, TO_SCREEN), fmov_imm(18, 1.0)]
            skips = []
            c4 += load_double(8, real_notch_w) + [fmov_d_x(17, 8), fsub(16, 1, 17), fabs(16, 16), fcmp(16, 18)]
            skips.append(len(c4)); c4.append(None)
            c4 += load_double(8, notch_h) + [fmov_d_x(17, 8), dup_hi(16, 1), fsub(16, 16, 17), fabs(16, 16), fcmp(16, 18)]
            skips.append(len(c4)); c4.append(None)
            c4 += [MOVI_V1_ZERO]
            for at in skips:
                c4[at] = 0x54000000 | ((len(c4) - at) << 5) | 0xA      # b.ge -> epilogue
            c4 += EPILOGUE
            for a in NOTCH_WINDOW_SITES:
                if struct.unpack("<I", orig_word(a))[0] != bl(a, TO_SCREEN):
                    fail("The software notch window call sites do not match the verified binary.")

            # Apply CAVE4
            assert len(c4) <= CAVE_SLOTS[CAVE4], "code cave overflow"
            n = 4 * CAVE_SLOTS[CAVE4]
            if image[CAVE4 - 0x100000000: CAVE4 - 0x100000000 + n] != b"\0" * n:
                fail("The code cave area is not empty. No changes were made.")
            for i, w in enumerate(c4):
                patches.append((CAVE4 + 4 * i, b"\0\0\0\0", enc(w)))
            for a in NOTCH_WINDOW_SITES:
                patches.append((a, orig_word(a), enc(bl(a, CAVE4))))
        return patches, None

    if not geometry:
        fail("No built-in display with a notch was found. Open the lid, or use --layout classic.")
    real_notch_w, notch_h = geometry
    notch_w = real_notch_w - 2 * NOTCH_MARGIN
    below = layout == "below"
    sensor_w = STOCK_SENSOR_W if below else notch_w

    sites_a, sites_b = [], []
    for addr in range(TEXT_RANGE[0], TEXT_RANGE[1], 4):
        w = struct.unpack("<I", orig_word(addr))[0]
        if w & 0xFC000000 != 0x94000000:
            continue
        off = (w & 0x3FFFFFF) << 2
        if off & 0x8000000:
            off -= 1 << 28
        if addr + off == GETTER_A:
            sites_a.append(addr)
        elif addr + off == GETTER_B:
            sites_b.append(addr)
    if len(sites_a) != 60 or len(sites_b) != 10 or not set(EXPANDED_SITES) <= set(sites_a):
        fail("The sensor getter call sites do not match the verified binary. No changes were made.")

    # CAVE0 replaces the leaf: (x + (W - 59) / 2, 0, W, notch height), then Rect.init.
    c0 = (load_double(8, sensor_w) + [fmov_d_x(2, 8), fsub(1, 1, 2), FMOV_D3_HALF, fmul(1, 1, 3),
                                      fadd(0, 0, 1)]
          + load_double(9, notch_h) + [fmov_d_x(1, 8), ins_hi_x(1, 9)])
    c0.append(b(CAVE0 + 4 * len(c0), RECT_INIT))

    # CAVE1 (layout "below"): expanded presentations use the sensor moved down by its height.
    c1 = PROLOGUE + [bl(CAVE1 + 8, GETTER_A), dup_hi(16, 0), dup_hi(4, 1), fadd(16, 16, 4),
                     ins_hi_v(0, 16)] + EPILOGUE

    # CAVE2 / CAVE3: all other callers receive the notch rectangle.
    def notch_cave(base, getter):
        c = PROLOGUE + [bl(base + 8, getter)]
        c += load_double(8, (notch_w - sensor_w) / 2) + [fmov_d_x(5, 8), fsub(0, 0, 5)]
        c += load_double(9, notch_w) + [ins_lo_x(1, 9)] + EPILOGUE
        return c

    # CAVE4: calls TO_SCREEN (v0 = origin, v1 = size) and zeroes the size when it equals the
    # notch within 1 pt. Other windows are returned unchanged.
    c4 = PROLOGUE + [bl(CAVE4 + 8, TO_SCREEN), fmov_imm(18, 1.0)]
    skips = []
    c4 += load_double(8, real_notch_w) + [fmov_d_x(17, 8), fsub(16, 1, 17), fabs(16, 16), fcmp(16, 18)]
    skips.append(len(c4)); c4.append(None)
    c4 += load_double(8, notch_h) + [fmov_d_x(17, 8), dup_hi(16, 1), fsub(16, 16, 17), fabs(16, 16), fcmp(16, 18)]
    skips.append(len(c4)); c4.append(None)
    c4 += [MOVI_V1_ZERO]
    for at in skips:
        c4[at] = 0x54000000 | ((len(c4) - at) << 5) | 0xA      # b.ge -> epilogue
    c4 += EPILOGUE
    for a in NOTCH_WINDOW_SITES:
        if struct.unpack("<I", orig_word(a))[0] != bl(a, TO_SCREEN):
            fail("The software notch window call sites do not match the verified binary.")

    # CAVE5: fixed width for remote Live Activities (x1 = 0, w2 = 0, x0 = width bit pattern).
    remote_w = round((real_notch_w + 2 * REMOTE_SIDE) / REMOTE_SCALE * 2) / 2
    c5 = [0xD2800001, 0x52800002] + load_double(0, remote_w) + [0xD65F03C0]
    if tuple(orig_word(REMOTE_WIDTH_SITE + 4 * i).hex() for i in range(4)) != REMOTE_WIDTH_ORIG:
        fail("The remote Live Activity width instructions do not match the verified binary.")

    caves = {CAVE0: c0, CAVE1: c1, CAVE2: notch_cave(CAVE2, GETTER_A),
             CAVE3: notch_cave(CAVE3, GETTER_B), CAVE4: c4}
    if not below:
        caves[CAVE5] = c5
    for base, words in caves.items():
        assert len(words) <= CAVE_SLOTS[base], "code cave overflow"
        n = 4 * CAVE_SLOTS[base]
        if image[base - 0x100000000: base - 0x100000000 + n] != b"\0" * n:
            fail("The code cave area is not empty. No changes were made.")
        for i, w in enumerate(words):
            patches.append((base + 4 * i, b"\0\0\0\0", enc(w)))

    patches.append((LEAF, orig_word(LEAF), enc(b(LEAF, CAVE0))))
    for a in sites_a:
        patches.append((a, orig_word(a), enc(bl(a, CAVE1 if below and a in EXPANDED_SITES else CAVE2))))
    for a in sites_b:
        patches.append((a, orig_word(a), enc(bl(a, CAVE3))))
    if below:
        patches.append((NO_OBSTRUCTION, orig_word(NO_OBSTRUCTION), enc(NOP)))
    for a in NOTCH_WINDOW_SITES:
        patches.append((a, orig_word(a), enc(bl(a, CAVE4))))
    if not below:
        patches.append((REMOTE_WIDTH_SITE, orig_word(REMOTE_WIDTH_SITE), enc(b(REMOTE_WIDTH_SITE, CAVE5))))
    return patches, geometry


MENUBAR_LLDB = r'''
import json, lldb
def apply(debugger, command, result, internal_dict):
    t = debugger.GetSelectedTarget(); p = t.GetProcess(); m = t.GetModuleAtIndex(0); e = lldb.SBError()
    table = json.load(open(command.strip()))
    todo = []
    for a, o, n in table:
        la = m.ResolveFileAddress(a).GetLoadAddress(t)
        cur = p.ReadMemory(la, 4, e)
        if cur != bytes.fromhex(o) and cur != bytes.fromhex(n):
            print("MISMATCH", hex(a), cur.hex() if cur else None); return
        todo.append((la, bytes.fromhex(n)))
    for la, n in todo:          # the process is stopped at the attach point
        p.WriteMemory(la, n, e)
    if all(p.ReadMemory(la, 4, e) == n for la, n in todo):
        print("PATCH_OK", len(todo))
def __lldb_init_module(debugger, d):
    debugger.HandleCommand("command script add -f island_patch.apply island_apply")
'''


def patch_menubar(patches):
    """Restarts MenuBarAgent and patches the new process before it reads its preferences."""
    pid = menubaragent_pid()
    if not pid:
        return False, "MenuBarAgent is not running."
    if run("ps", "-p", str(pid), "-o", "comm=").stdout.strip() != MENUBARAGENT:
        return False, "The running process does not match MenuBarAgent."

    workdir = tempfile.mkdtemp(prefix="dynamic_island_")
    table = os.path.join(workdir, "patches.json")
    module = os.path.join(workdir, "island_patch.py")
    with open(table, "w") as f:
        json.dump([[a, o.hex(), n.hex()] for a, o, n in patches], f)
    with open(module, "w") as f:
        f.write(MENUBAR_LLDB)
    cmd = ["xcrun", "lldb", "--batch", "-o", f"command script import {module}",
           "-o", "process attach --name MenuBarAgent --waitfor", "-o", f"island_apply {table}", "-o", "detach"]
    timed_out = False
    try:
        debugger = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, start_new_session=True)

        def restart():          # launchd relaunches MenuBarAgent; lldb catches the new process
            time.sleep(3)
            if debugger.poll() is None:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

        threading.Thread(target=restart, daemon=True).start()
        try:
            output, _ = debugger.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(debugger.pid, signal.SIGINT)
            try:
                output, _ = debugger.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(debugger.pid, signal.SIGKILL)
                output, _ = debugger.communicate()
        success = debugger.returncode == 0 and "PATCH_OK" in output and "detached" in output
        if timed_out and not success:
            output += ("\nlldb did not finish within 30 seconds. It may have been waiting for the "
                       "\"Developer Tools Access\" dialog; check that \"DevToolsSecurity -status\" "
                       "reports enabled and that your user is in the _developer group.")
    except OSError as exc:
        output, success = str(exc), False
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return success, output


# ---------------------------------------------------------------------------------------------
# Top edge clicks (module loaded into MenuBarAgent, see "Top edge clicks" at the top)
# ---------------------------------------------------------------------------------------------

TOP_HIT_DIR = os.path.expanduser("~/Library/Group Containers/com.apple.MenuBar")   # readable in MenuBarAgent's sandbox
TOP_HIT_PREFIX = "DynamicEnablerTopHit"
TOP_HIT_SOURCE = r'''
#import <AppKit/AppKit.h>
#import <QuartzCore/QuartzCore.h>
#import <objc/runtime.h>
#import <objc/message.h>
#import <dlfcn.h>

static NSString *const kName = @"DynamicEnablerTopHit";
static char kSavedFilters;              // container -> its filters before the invert filter was removed
static BOOL gEnabled, gScanQueued, gWriting;
static IMP gOrigSetHit, gOrigSetFilters;
static int gCount, gRefreshes;
static NSMutableArray *gCovered;        // [window, container, hit layer] of every covered island

typedef int (*FindWindowFn)(int, int, int, int, const CGPoint *, CGPoint *, uint32_t *, int *);
static int (*gMainConnection)(void);
static FindWindowFn gFindWindow;

static BOOL isHitLayer(CALayer *l) {
    static SEL sel; if (!sel) sel = NSSelectorFromString(@"hitTestsAsOpaque");
    return [l respondsToSelector:sel] && ((BOOL (*)(id, SEL))objc_msgSend)(l, sel);
}

static BOOL isInvertFilter(id f) {
    for (NSString *key in @[@"type", @"name"]) {
        @try {
            id v = [f valueForKey:key];
            if ([v isKindOfClass:[NSString class]] && [v isEqualToString:@"colorInvertDisplayAware"]) return YES;
        } @catch (__unused id e) {}
    }
    return NO;
}

static void setFiltersRaw(CALayer *l, NSArray *filters) {
    gWriting = YES;
    ((void (*)(id, SEL, NSArray *))gOrigSetFilters)(l, @selector(setFilters:), filters);
    gWriting = NO;
}

static void stripFilter(CALayer *container) {
    NSArray *filters = container.filters;
    if (!filters.count) return;
    NSIndexSet *found = [filters indexesOfObjectsPassingTest:^BOOL(id f, NSUInteger i, BOOL *stop) { return isInvertFilter(f); }];
    if (!found.count) return;
    objc_setAssociatedObject(container, &kSavedFilters, filters, OBJC_ASSOCIATION_RETAIN);
    NSMutableArray *rest = [filters mutableCopy];
    [rest removeObjectsAtIndexes:found];
    setFiltersRaw(container, rest.count ? rest : nil);
}

static void restoreFilter(CALayer *container) {
    NSArray *saved = objc_getAssociatedObject(container, &kSavedFilters);
    if (!saved) return;
    objc_setAssociatedObject(container, &kSavedFilters, nil, OBJC_ASSOCIATION_RETAIN);
    setFiltersRaw(container, saved);
}

static CALayer *underlayOf(CALayer *hit) {
    for (CALayer *c in hit.sublayers) if ([c.name isEqualToString:kName]) return c;
    return nil;
}

static void walk(NSWindow *w, CALayer *l, BOOL add) {
    for (CALayer *c in [l.sublayers copy]) {
        if ([c.name isEqualToString:kName]) continue;
        // Island hit-test layers sit directly inside the rounded, clipping island container.
        if (isHitLayer(c) && l.cornerRadius > 0 && l.masksToBounds) {
            CALayer *u = underlayOf(c);
            if (add) {
                stripFilter(l);
                if (!u) {
                    u = [CALayer layer];
                    u.name = kName;
                    u.backgroundColor = CGColorGetConstantColor(kCGColorBlack);
                    u.frame = c.bounds;
                    u.autoresizingMask = kCALayerWidthSizable | kCALayerHeightSizable;
                    u.actions = @{@"bounds": [NSNull null], @"position": [NSNull null], @"frame": [NSNull null],
                                  @"onOrderIn": [NSNull null], @"onOrderOut": [NSNull null]};
                    [c insertSublayer:u atIndex:0];
                }
                gCount++;
                [gCovered addObject:@[w, l, c]];
            } else {
                [u removeFromSuperlayer];
                restoreFilter(l);
            }
        }
        walk(w, c, add);
    }
}

static void scan(void) {
    BOOL add = gEnabled && !NSWorkspace.sharedWorkspace.accessibilityDisplayShouldInvertColors;
    gCount = 0;
    gCovered = [NSMutableArray array];
    [CATransaction begin];
    [CATransaction setDisableActions:YES];
    for (NSWindow *w in NSApp.windows) {
        if (w.level != 26 || w.frame.size.height < 200) continue;      // the full-screen island window
        CALayer *root = (w.contentView.superview ?: w.contentView).layer;
        if (root) walk(w, root, add);
    }
    [CATransaction commit];
}

// WindowServer sometimes keeps a stale click shape after the island's layer tree changes (the
// opaque underlay is then ignored again). It recomputes the shape when the container's filters
// are set, so each covered island that touches the top edge is checked by asking WindowServer
// which window owns the middle of its top row of pixels, and refreshed only when that fails.
static BOOL topRowReachable(NSWindow *w, CALayer *hit) {
    if (!gFindWindow || !gMainConnection) return YES;
    CALayer *root = (w.contentView.superview ?: w.contentView).layer;
    CGRect r = [hit convertRect:hit.bounds toLayer:root];
    if (fabs(CGRectGetMaxY(r) - root.bounds.size.height) > 0.5) return YES;   // not at the top edge
    if (r.size.width < r.size.height + 4) return YES;      // a circle touches the edge in one point only
    NSRect s = [w convertRectToScreen:NSRectFromCGRect(r)];
    CGFloat primary = NSScreen.screens.firstObject.frame.size.height;
    CGPoint p = CGPointMake(NSMidX(s), primary - NSMaxY(s)), out;
    uint32_t wid = 0; int owner = 0;
    if (gFindWindow(gMainConnection(), 0, 1, 0, &p, &out, &wid, &owner) != 0) return YES;
    return wid == (uint32_t)w.windowNumber;
}

static void checkTopRows(void) {
    for (NSArray *item in [gCovered copy]) {
        NSWindow *w = item[0]; CALayer *container = item[1], *hit = item[2];
        if (!hit.superlayer || topRowReachable(w, hit)) continue;
        gRefreshes++;
        [CATransaction begin];
        [CATransaction setDisableActions:YES];
        NSArray *current = container.filters;
        setFiltersRaw(container, @[]);
        setFiltersRaw(container, current);
        ((void (*)(id, SEL, BOOL))gOrigSetHit)(hit, NSSelectorFromString(@"setHitTestsAsOpaque:"), NO);
        ((void (*)(id, SEL, BOOL))gOrigSetHit)(hit, NSSelectorFromString(@"setHitTestsAsOpaque:"), YES);
        [CATransaction commit];
    }
}

__attribute__((visibility("default"))) int dynamic_enabler_top_hit_refreshes(void) { return gRefreshes; }

static void queueScan(void) {
    if (gScanQueued) return;
    gScanQueued = YES;
    dispatch_async(dispatch_get_main_queue(), ^{ gScanQueued = NO; scan(); });
}

// New island layers turn on hit testing through this setter: rescan once the tree is assembled.
static void hookedSetHit(id self, SEL _cmd, BOOL value) {
    ((void (*)(id, SEL, BOOL))gOrigSetHit)(self, _cmd, value);
    if (value && gEnabled) queueScan();
}

// A container whose filter was removed may get its filters assigned again.
static void hookedSetFilters(CALayer *self, SEL _cmd, NSArray *filters) {
    ((void (*)(id, SEL, NSArray *))gOrigSetFilters)(self, _cmd, filters);
    if (!gWriting && objc_getAssociatedObject(self, &kSavedFilters)) {
        objc_setAssociatedObject(self, &kSavedFilters, nil, OBJC_ASSOCIATION_RETAIN);
        if (gEnabled) queueScan();
    }
}

// Returns the number of island hit-test layers that are currently covered.
__attribute__((visibility("default"))) int dynamic_enabler_top_hit(int enable) {
    void (^work)(void) = ^{
        static dispatch_once_t once;
        dispatch_once(&once, ^{
            Method m = class_getInstanceMethod([CALayer class], NSSelectorFromString(@"setHitTestsAsOpaque:"));
            if (m) gOrigSetHit = method_setImplementation(m, (IMP)hookedSetHit);
            m = class_getInstanceMethod([CALayer class], @selector(setFilters:));
            gOrigSetFilters = method_setImplementation(m, (IMP)hookedSetFilters);
            [NSWorkspace.sharedWorkspace.notificationCenter
                addObserverForName:NSWorkspaceAccessibilityDisplayOptionsDidChangeNotification object:nil
                queue:NSOperationQueue.mainQueue usingBlock:^(NSNotification *n) { queueScan(); }];
            void *sky = dlopen("/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight", RTLD_LAZY);
            if (sky && gOrigSetHit) {
                gMainConnection = (int (*)(void))dlsym(sky, "SLSMainConnectionID");
                gFindWindow = (FindWindowFn)dlsym(sky, "SLSFindWindowAndOwner");
            }
            // Every 0.5 s: check the click shapes WindowServer has from the previous pass, then
            // cover layers that were rebuilt without going through the hooked setters.
            NSTimer *t = [NSTimer timerWithTimeInterval:0.5 repeats:YES block:^(NSTimer *timer) {
                if (gEnabled) { checkTopRows(); scan(); }
            }];
            t.tolerance = 0.2;
            [NSRunLoop.mainRunLoop addTimer:t forMode:NSRunLoopCommonModes];
        });
        gEnabled = enable != 0;
        scan();
    };
    if ([NSThread isMainThread]) work(); else dispatch_sync(dispatch_get_main_queue(), work);
    return gCount;
}
'''


def remove_top_hit_files():
    if os.path.isdir(TOP_HIT_DIR):
        for name in os.listdir(TOP_HIT_DIR):
            if name.startswith(TOP_HIT_PREFIX) and name.endswith(".dylib"):
                os.remove(os.path.join(TOP_HIT_DIR, name))


def build_top_hit():
    """Compiles the module once per source version; returns its path or None."""
    import hashlib
    digest = hashlib.sha1(TOP_HIT_SOURCE.encode()).hexdigest()[:10]
    dylib = os.path.join(TOP_HIT_DIR, f"{TOP_HIT_PREFIX}-{digest}.dylib")
    if os.path.exists(dylib):
        return dylib
    if not os.path.isdir(TOP_HIT_DIR):
        return None         # created by MenuBarAgent itself; never create it
    remove_top_hit_files()
    workdir = tempfile.mkdtemp(prefix="dynamic_island_")
    try:
        source = os.path.join(workdir, "TopHit.m")
        with open(source, "w") as f:
            f.write(TOP_HIT_SOURCE)
        built = run("xcrun", "clang", "-dynamiclib", "-arch", "arm64e", "-fobjc-arc", "-O2",
                    "-framework", "AppKit", "-framework", "QuartzCore", source, "-o", dylib)
        if built.returncode or run("codesign", "-s", "-", "-f", dylib).returncode:
            if os.path.exists(dylib):
                os.remove(dylib)
            return None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return dylib


def load_top_hit():
    """Loads the module into the running MenuBarAgent. Returns a message, or None on success."""
    dylib = build_top_hit()
    if not dylib:
        return "the module could not be compiled"
    pid = menubaragent_pid()
    if not pid:
        return "MenuBarAgent is not running"
    out = run("xcrun", "lldb", "--batch", "-o", f"process attach --pid {pid}",
              "-o", f'expr -l c -- (long)dlopen("{dylib}", 2)',
              "-o", 'expr -l c -- ((int(*)(int))dlsym((void*)-2, "dynamic_enabler_top_hit"))(1)',
              "-o", "detach").stdout
    loaded = re.search(r"\(long\) \$0 = (\d+)", out)
    called = re.search(r"\(int\) \$1 = (\d+)", out)
    if not loaded or loaded.group(1) == "0" or not called:
        return "lldb could not load the module"
    return None


# ---------------------------------------------------------------------------------------------
# Client processes (expanded widths)
# ---------------------------------------------------------------------------------------------

EXPANDED_W_RANGE = (230.0, 300.0)   # stock expanded widths: timer 260, AirDrop 234/274, Now Playing 286
MIN_HEIGHT = 60.0                   # height of single-row (Min Height) expanded presentations
MIN_SIDE = 100.5                    # single-row: stock side width (260 - 59) / 2, as for timers
TALL_SIDE = REMOTE_SIDE             # taller presentations: side width next to the notch, as for Now Playing
SKIP_CLIENTS = ("WidgetRenderer",)  # third-party WidgetKit content is sized by the app itself

# Per-client side width for single-row presentations with less content.
# Screen recording: the stop button and the elapsed time fit with 90 pt on each side.
CLIENT_MIN_SIDE = {
    "com.apple.screencaptureui.agent": 90.0,
}

# Clients that do not size their panel with .frame(): process name -> (module,
# [(movz address, movk address, register, original instructions)], side width).
# AirDrop: 0x100088d4c returns the panel width (274 for several items, 234 for one). Its
# thumbnail needs 94 pt on each side to clear the notch.
CLIENT_EXTRAS = {
    "airdropui": ("AirDropUI", [(0x100089058, 0x10008905C, 8, ("0800c4d2", "280ee8f2")),
                                (0x1000892E8, 0x1000892EC, 8, ("0800c8d2", "a80de8f2"))], 94.0),
}


def frame_params(notch_w, neutral=False):
    """Parameters of the frame hook. neutral=True selects an empty range (no change)."""
    lo, hi = (1e9, 0.0) if neutral else EXPANDED_W_RANGE
    return {"lo": lo, "hi": hi, "min_h": MIN_HEIGHT,
            "w_min": notch_w + 2 * MIN_SIDE, "w_tall": notch_w + 2 * TALL_SIDE}


def client_params(ident, params):
    side = CLIENT_MIN_SIDE.get(ident)
    if side is None or params["lo"] > params["hi"]:
        return params
    return dict(params, w_min=params["w_min"] - 2 * MIN_SIDE + 2 * side)


def client_extras(name, params, neutral=False):
    """-> [module, [[address, original, new], ...]] or None. neutral=True restores the originals."""
    key = name.rsplit(".", 1)[-1]
    if key not in CLIENT_EXTRAS:
        return None
    mod, sites, side = CLIENT_EXTRAS[key]
    width = params["w_tall"] - 2 * TALL_SIDE + 2 * side
    items = []
    for a_movz, a_movk, reg, origs in sites:
        new = origs if neutral else mov_double(reg, width)
        items += [[a_movz, origs[0], new[0]], [a_movk, origs[1], new[1]]]
    return [mod, items]


FRAMES_LLDB = r'''
import json, lldb, struct

FRAME = "function signature specialization <Arg[3] = Dead> of SwiftUI._FrameLayout.init(width:"
FLEX = "SwiftUI._FlexFrameLayout.init(minWidth:"
TRAMPOLINE = struct.pack("<II", 0x58000050, 0xD61F0200)        # ldr x16, #8 ; br x16 ; .quad target
HEADER = 32                                                    # displaced instructions + jump back
CAVE_SIZE = 256


def bits(v): return struct.unpack("<Q", struct.pack("<d", v))[0]


def assemble(items):
    # items: ("raw", word) / ("label", name) / ("quad", name, value) / (instruction, register, label)
    pos, labels = 0, {}
    for it in items:
        if it[0] == "label": labels[it[1]] = pos
        elif it[0] == "quad": labels[it[1]] = pos; pos += 8
        else: pos += 4
    out, pos = b"", 0
    for it in items:
        if it[0] == "label": continue
        if it[0] == "quad": out += struct.pack("<Q", it[2]); pos += 8; continue
        if it[0] == "raw": w = it[1]
        else:
            off = (labels[it[2]] - pos) >> 2
            if it[0] == "tbnz0": w = 0x37000000 | ((off & 0x3FFF) << 5) | it[1]
            elif it[0] == "ldr": w = 0x58000000 | ((off & 0x7FFFF) << 5) | it[1]
            elif it[0] in ("bne", "blo", "bhi"):
                w = 0x54000000 | ((off & 0x7FFFF) << 5) | {"bne": 1, "blo": 3, "bhi": 8}[it[0]]
            elif it[0] == "b": w = 0x14000000 | (off & 0x3FFFFFF)
        out += struct.pack("<I", w); pos += 4
    return out


def cmp(n, m): return ("raw", 0xEB00001F | (m << 16) | (n << 5))


def cave(kind, P, displaced, back):
    # kind "frame": width x0/w1, height x2/w3. kind "flex": maxWidth x4/w5, minHeight x6/w7,
    # maxHeight on the stack. Widths in [lo, hi] are replaced: height == min_h -> w_min,
    # otherwise w_tall. Positive doubles compare correctly as unsigned integers.
    wreg, wflag = (0, 1) if kind == "frame" else (4, 5)
    it = [("label", "start")] + [("raw", w) for w in displaced]
    it += [("ldr", 16, "back"), ("raw", 0xD61F0200), ("quad", "back", back)]
    it += [("label", "entry"), ("tbnz0", wflag, "orig"),
           ("ldr", 16, "lo"), cmp(wreg, 16), ("blo", 0, "orig"),
           ("ldr", 16, "hi"), cmp(wreg, 16), ("bhi", 0, "orig")]
    if kind == "frame":
        it += [("tbnz0", 3, "tall"), ("ldr", 17, "h"), cmp(2, 17), ("bne", 0, "tall")]
    else:
        it += [("tbnz0", 7, "maxh"), ("ldr", 17, "h"), cmp(6, 17), ("bne", 0, "maxh"), ("b", 0, "min"),
               ("label", "maxh"), ("raw", 0x394063F0), ("tbnz0", 16, "tall"),
               ("raw", 0xF9400BF0), ("ldr", 17, "h"), cmp(16, 17), ("bne", 0, "tall")]
    # With only a maximum width, panels with little content would not grow; pin the minimum too.
    pin = [("raw", 0xAA0403E0), ("raw", 0x52800001)] if kind == "flex" else []
    it += [("label", "min"), ("ldr", wreg, "minv")] + pin + [("b", 0, "orig"),
           ("label", "tall"), ("ldr", wreg, "tallv")] + pin + [
           ("label", "orig"), ("b", 0, "start"),
           ("quad", "h", bits(P["min_h"])), ("quad", "lo", bits(P["lo"])), ("quad", "hi", bits(P["hi"])),
           ("quad", "minv", bits(P["w_min"])), ("quad", "tallv", bits(P["w_tall"]))]
    code = assemble(it)
    assert len(code) <= CAVE_SIZE
    return code


def pc_relative(w):
    return ((w & 0x7C000000) == 0x14000000 or (w & 0x1F000000) == 0x10000000 or (w & 0x3B000000) == 0x18000000
            or (w & 0x7E000000) in (0x34000000, 0x36000000) or (w & 0xFF000010) == 0x54000000)


def find_addrs(t):
    # Both functions are in SwiftUICore (shared cache), so the addresses are the same in every process.
    found = {}
    for m in t.module_iter():
        if m.GetFileSpec().GetFilename() != "SwiftUICore": continue
        for s in m:
            n = s.GetName() or ""
            if n.startswith(FRAME): found["frame"] = s.GetStartAddress().GetLoadAddress(t)
            elif n.startswith(FLEX): found["flex"] = s.GetStartAddress().GetLoadAddress(t)
    return found if len(found) == 2 else None


def install(p, addrs, P):
    e = lldb.SBError()
    pcs = [th.GetFrameAtIndex(0).GetPC() for th in p]
    done = 0
    for kind in ("frame", "flex"):
        fn = addrs[kind]
        if any(fn <= pc < fn + 16 for pc in pcs): continue
        head = p.ReadMemory(fn, 16, e)
        if not head: continue
        if head[:8] == TRAMPOLINE:                  # already hooked: reuse the existing cave
            mem = struct.unpack("<Q", head[8:])[0] - HEADER
            displaced = struct.unpack("<4I", p.ReadMemory(mem, 16, e))
        else:
            displaced = struct.unpack("<4I", head)
            if any(pc_relative(w) for w in displaced): continue
            mem = p.AllocateMemory(CAVE_SIZE, lldb.ePermissionsReadable | lldb.ePermissionsExecutable, e)
            if not e.Success(): continue
        code = cave(kind, P, displaced, fn + 16)
        p.WriteMemory(mem, code, e)
        if not e.Success() or p.ReadMemory(mem, len(code), e) != code: continue
        p.WriteMemory(fn, TRAMPOLINE + struct.pack("<Q", mem + HEADER), e)
        done += 1
    return done


def write_extras(t, p, extras):
    # The immediates (bits 5-20) may have been changed by an earlier run; all other bits must match.
    e = lldb.SBError()
    mods = {m.GetFileSpec().GetFilename(): m for m in t.module_iter()}
    mod, items = extras
    if mod not in mods: return False
    todo = []
    for addr, orig, new in items:
        la = mods[mod].ResolveFileAddress(addr).GetLoadAddress(t)
        cur = p.ReadMemory(la, 4, e)
        if not cur or (int.from_bytes(cur, "little") ^ int.from_bytes(bytes.fromhex(orig), "little")) & 0xFFE0001F:
            return False
        todo.append((la, bytes.fromhex(new)))
    for la, new in todo:
        p.WriteMemory(la, new, e)
    return all(p.ReadMemory(la, 4, e) == new for la, new in todo)


def apply(debugger, command, result, d):
    arg = json.loads(command)
    t = debugger.GetSelectedTarget(); p = t.GetProcess()
    addrs = arg.get("addrs") or find_addrs(t)
    if not addrs:
        print("FRAMES_NOSYMBOL"); return
    print("FRAMES_ADDRS " + json.dumps(addrs))
    ok = install(p, addrs, arg["params"]) == 2
    if arg.get("extras"):
        ok = write_extras(t, p, arg["extras"]) and ok
    print("FRAMES_OK" if ok else "FRAMES_FAIL")


def __lldb_init_module(debugger, d):
    debugger.HandleCommand("command script add -f island_frames.apply island_frames")
'''

# Battery banners (system banners shown by ControlCenter, not Live Activities). The expanded size
# is fixed in a SystemBannerUI function used only by the battery banners (0x2b0969f3c): charging
# 220 x 50 and low battery 300 x 60. Both branches load the width with `adrp x8 ; ldr d0, [x8, #...]`,
# which is replaced by `movz x8, #hi, lsl #48 ; fmov d0, x8`. Only the top 16 bits can be set, so the
# width is the representable value (16 pt steps) closest to notch width + 2 x side width.
# The 220 and 300 constants themselves are shared with other banner layouts and are not changed.
BANNER_MODULE = "SystemBannerUI"
BANNER_SITES = [   # (adrp address, original instructions, side width)
    (0x2B0969F9C, ("480200f0", "006945fd"), 80.5),      # charging 220 x 50: stock side width (220 - 59) / 2
    (0x2B0969F74, ("480200b0", "005d43fd"), MIN_SIDE),  # low battery 300 x 60: single row, as for timers
]

BANNER_LLDB = r"""
import json, lldb
def apply(debugger, command, result, d):
    t = debugger.GetSelectedTarget(); p = t.GetProcess(); e = lldb.SBError()
    mods = [m for m in t.module_iter() if m.GetFileSpec().GetFilename() == "%s"]
    if not mods:
        print("BANNER_FAIL nomodule"); return
    todo = []
    for addr, orig_hex, new_hex in json.loads(command):
        la = mods[0].ResolveFileAddress(addr).GetLoadAddress(t)
        cur = p.ReadMemory(la, 8, e) or b""
        w0 = int.from_bytes(cur[:4], "little") if len(cur) == 8 else 0
        ours = (w0 & 0xFFE0001F) == 0xD2E00008 and cur[4:] == bytes.fromhex("0001679e")
        if cur != bytes.fromhex(orig_hex) and not ours:
            print("BANNER_FAIL unexpected %%x %%s" %% (addr, cur.hex())); return
        todo.append((la, bytes.fromhex(new_hex)))
    for la, new in todo:
        p.WriteMemory(la, new, e)
    print("BANNER_OK" if all(p.ReadMemory(la, 8, e) == new for la, new in todo) else "BANNER_FAIL write")
def __lldb_init_module(debugger, d):
    debugger.HandleCommand("command script add -f island_banner.apply island_banner")
""" % BANNER_MODULE


def banner_words(notch_w, side):
    target = notch_w + 2 * side
    hi = struct.unpack("<Q", struct.pack("<d", target))[0] >> 48
    hi = min((hi, hi + 1), key=lambda h: abs(struct.unpack("<d", struct.pack("<Q", h << 48))[0] - target))
    return (enc(0xD2E00000 | (hi << 5) | 8).hex(), enc(0x9E670100).hex())


def run_lldb_module(pid, module_name, source, command):
    """Attaches to pid, runs one command from an lldb Python module and detaches."""
    workdir = tempfile.mkdtemp(prefix="dynamic_island_")
    path = os.path.join(workdir, module_name + ".py")
    with open(path, "w") as f:
        f.write(source)
    try:
        return subprocess.run(
            ["xcrun", "lldb", "--batch", "-o", f"command script import {path}",
             "-o", f"process attach --pid {pid}", "-o", command, "-o", "detach"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120).stdout
    except subprocess.TimeoutExpired:
        return ""
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


FRAME_ADDRS = {}    # SwiftUI frame initializer addresses, reused after the first lookup


def patch_frames(pid, params, name="", neutral=False):
    arg = json.dumps({"params": client_params(name, params), "addrs": FRAME_ADDRS or None,
                      "extras": client_extras(name, params, neutral)})
    out = run_lldb_module(pid, "island_frames", FRAMES_LLDB, "island_frames " + arg)
    found = re.search(r"^FRAMES_ADDRS (.*)$", out, re.M)
    if found:
        FRAME_ADDRS.update(json.loads(found.group(1)))
    return "FRAMES_OK" in out


def control_center_pid():
    out = run("pgrep", "-x", "ControlCenter").stdout.split()
    return int(out[0]) if out else 0


def patch_banner(notch_w, neutral=False):
    pid = control_center_pid()
    if not pid:
        return False
    items = [[addr, "".join(orig), "".join(orig if neutral else banner_words(notch_w, side))]
             for addr, orig, side in BANNER_SITES]
    return "BANNER_OK" in run_lldb_module(pid, "island_banner", BANNER_LLDB, "island_banner " + json.dumps(items))


CLIENT_LOG_PREDICATE = ('process == "MenuBarAgent" AND subsystem == "com.apple.FrontBoard" AND '
                        'eventMessage CONTAINS "liveactivity-" AND eventMessage CONTAINS "Client process connected"')


def parse_clients(out):
    """Live Activity client processes in MenuBarAgent log output -> {pid: bundle identifier}."""
    found = {}
    pattern = r"Client process connected: \[\w+<(?:application\.)?([^>]+?)(?:\.\d+\.\d+)?\(\d+\)>:(\d+)\]"
    for name, pid in re.findall(pattern, out):
        pid = int(pid)
        if any(s in name for s in SKIP_CLIENTS):
            continue
        try:
            os.kill(pid, 0)
        except OSError:
            continue
        found[pid] = name
    return found


def live_activity_clients(last="12h"):
    return parse_clients(run("/usr/bin/log", "show", "--last", last, "--style", "compact",
                             "--predicate", CLIENT_LOG_PREDICATE).stdout)


def patch_clients(notch_w, neutral=False, report=print):
    params = frame_params(notch_w, neutral)
    done = "restored" if neutral else "patched"
    for pid, name in live_activity_clients().items():
        ok = patch_frames(pid, params, name, neutral)
        report(f"  {name}: {done if ok else 'failed'}")
    ok = patch_banner(notch_w, neutral)
    report(f"  Battery banners (ControlCenter): {done if ok else 'failed'}")


# ---------------------------------------------------------------------------------------------
# Background helper: patches client processes that start after the script has finished
# ---------------------------------------------------------------------------------------------

def log_line(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")


def run_watcher(mba_pid):
    import select

    def note(text):
        log_line(WATCHER_LOG, text)

    def on_signal(signum, _frame):
        note(f"Received {signal.Signals(signum).name}; exiting.")
        sys.exit(0)
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, on_signal)

    geometry = notch_geometry()
    if not geometry:
        note("No built-in display with a notch was found; exiting.")
        return
    params = frame_params(geometry[0])
    done = set()
    banner_pid = [0]

    def handle(clients):
        for pid, name in clients.items():
            if pid not in done:
                done.add(pid)
                ok = patch_frames(pid, params, name)
                note(f"{name} (pid {pid}): {'patched' if ok else 'failed'}")

    def check_banner():         # reapply when ControlCenter restarts
        pid = control_center_pid()
        if pid and pid != banner_pid[0]:
            banner_pid[0] = pid
            ok = patch_banner(geometry[0])
            note(f"Battery banners (ControlCenter, pid {pid}): {'patched' if ok else 'failed'}")

    note(f"Started for MenuBarAgent pid {mba_pid}.")
    stream = subprocess.Popen(
        ["/usr/bin/log", "stream", "--style", "compact", "--predicate", CLIENT_LOG_PREDICATE],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    try:
        handle(live_activity_clients())
        check_banner()
        while True:
            try:
                os.kill(mba_pid, 0)
            except OSError:
                note("MenuBarAgent has exited; exiting.")
                return
            check_banner()
            if select.select([stream.stdout], [], [], 5)[0]:
                line = stream.stdout.readline()
                if not line:
                    note("The log stream ended; exiting.")
                    return
                handle(parse_clients(line))
    finally:
        stream.kill()


def launchd_job(label, arguments, log):
    return {"Label": label, "ProgramArguments": arguments, "RunAtLoad": True, "KeepAlive": False,
            "LimitLoadToSessionType": "Aqua", "WorkingDirectory": os.path.dirname(arguments[1]),
            "StandardOutPath": log, "StandardErrorPath": log}


def start_watcher(mba_pid):
    """Runs the helper as a launchd job so that it does not end with the terminal session."""
    stop_watcher()
    os.makedirs(LOG_DIR, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="dynamic_island_")
    plist = os.path.join(workdir, WATCHER_LABEL + ".plist")
    with open(plist, "wb") as f:
        plistlib.dump(launchd_job(WATCHER_LABEL, [PYTHON, os.path.abspath(__file__),
                                                  "--watcher", str(mba_pid)], WATCHER_LOG), f)
    ok = run("launchctl", "bootstrap", gui_domain(), plist).returncode == 0
    shutil.rmtree(workdir, ignore_errors=True)
    return ok


def stop_watcher():
    run("launchctl", "bootout", f"{gui_domain()}/{WATCHER_LABEL}")
    me = os.getpid()
    for pid in run("pgrep", "-f", f"{SCRIPT_NAME} --watcher").stdout.split():
        if int(pid) != me:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass


# ---------------------------------------------------------------------------------------------
# Automatic activation at login
# ---------------------------------------------------------------------------------------------

def install_autostart(layout, watch):
    """Copies the script to Application Support and registers a login LaunchAgent."""
    os.makedirs(SUPPORT_DIR, exist_ok=True)
    installed = os.path.join(SUPPORT_DIR, SCRIPT_NAME)
    if os.path.abspath(__file__) != installed:
        shutil.copy2(os.path.abspath(__file__), installed)
    arguments = [PYTHON, installed, "--login", "--layout", layout] + ([] if watch else ["--no-watch"])
    os.makedirs(os.path.dirname(AUTOSTART_PLIST), exist_ok=True)
    with open(AUTOSTART_PLIST, "wb") as f:
        plistlib.dump(launchd_job(AUTOSTART_LABEL, arguments, AUTOSTART_LOG), f)


def remove_autostart():
    removed = os.path.exists(AUTOSTART_PLIST)
    run("launchctl", "bootout", f"{gui_domain()}/{AUTOSTART_LABEL}")
    for path in (AUTOSTART_PLIST, os.path.join(SUPPORT_DIR, SCRIPT_NAME)):
        if os.path.exists(path):
            os.remove(path)
    if os.path.isdir(SUPPORT_DIR) and not os.listdir(SUPPORT_DIR):
        os.rmdir(SUPPORT_DIR)
    return removed


# ---------------------------------------------------------------------------------------------
# Enable and disable
# ---------------------------------------------------------------------------------------------

def set_defaults():
    """Turns both preferences on and returns a function that restores the previous values."""
    previous = {}
    for key in DEFAULTS:
        prev = run("defaults", "read", DOMAIN, key)
        if prev.returncode == 0 and prev.stdout.strip() not in ("0", "1"):
            fail(f"The existing preference {key} has an unexpected value.")
        previous[key] = prev
        if run("defaults", "write", DOMAIN, key, "-bool", "true").returncode:
            fail(f"The preference {key} could not be written.")

    def restore():
        for key, prev in previous.items():
            if prev.returncode:
                run("defaults", "delete", DOMAIN, key)
            else:
                run("defaults", "write", DOMAIN, key, "-bool", "true" if prev.stdout.strip() == "1" else "false")
    return restore


def enable(layout, watch, report=print):
    patches, geometry = build_patches(layout)
    restore = set_defaults()

    # Clients are patched first: expanded views are rebuilt when MenuBarAgent recreates its scenes.
    if geometry:
        report("Patching Live Activity clients:" if layout == "notch" else "Restoring stock client widths:")
        patch_clients(geometry[0], neutral=layout != "notch", report=report)

    report("Restarting and patching MenuBarAgent...")
    success, output = patch_menubar(patches)
    if not success:
        restore()
        report(output[-2000:])
        fail("The MenuBarAgent patch could not be confirmed. The previous preferences were restored.")

    top_hit_note = None
    if layout != "classic":
        time.sleep(3)       # let the new MenuBarAgent finish launching before loading the module
        problem = load_top_hit()
        top_hit_note = ("Clicks at the top edge of the screen reach the island." if not problem else
                        f"Top edge clicks were not enabled ({problem}); everything else works.")

    stop_watcher()
    watcher_note = None
    if layout == "notch" and watch:
        pid = menubaragent_pid()
        watcher_note = (f"Background helper started (log: {WATCHER_LOG})." if pid and start_watcher(pid)
                        else "The background helper could not be started; clients that start later "
                             "keep their stock widths.")

    description = {"notch": "expanded presentations wrap around the notch",
                   "below": "expanded presentations are placed below the notch",
                   "classic": "stock position below the menu bar"}[layout]
    report(f"The Dynamic Island is enabled ({description}; {len(patches)} patches).")
    if geometry:
        report(f"Notch: {geometry[0]:g} x {geometry[1]:g} pt.")
    if top_hit_note:
        report(top_hit_note)
    if watcher_note:
        report(watcher_note)


def disable(report=print):
    stop_watcher()
    if remove_autostart():
        report("Automatic activation at login has been removed.")

    if menubaragent_pid():
        report("Restoring stock client widths:")
        patch_clients(0.0, neutral=True, report=report)

    for key in DEFAULTS:
        run("defaults", "delete", DOMAIN, key)
    run("killall", "MenuBarAgent")      # launchd relaunches it without patches
    remove_top_hit_files()
    report("The Dynamic Island is disabled. MenuBarAgent has been restarted with its stock behavior.")


# ---------------------------------------------------------------------------------------------
# Command line interface
# ---------------------------------------------------------------------------------------------

def ask(question, options):
    """Presents numbered options and returns the key of the selected one."""
    print(question)
    for i, (_, label) in enumerate(options, 1):
        print(f"  {i}. {label}")
    while True:
        try:
            answer = input(f"Select 1-{len(options)}: ").strip()
        except EOFError:
            fail("No selection was made.")
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            print()
            return options[int(answer) - 1][0]
        print("Please enter the number of one of the options.")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Enables the macOS 27.3 beta Dynamic Island on MacBook Pro models with a notch.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--enable", action="store_true", help="enable the Dynamic Island")
    action.add_argument("--disable", action="store_true",
                        help="disable the Dynamic Island and remove automatic activation")
    startup = parser.add_mutually_exclusive_group()
    startup.add_argument("--autostart", dest="autostart", action="store_true", default=None,
                         help="apply the patches automatically at every login")
    startup.add_argument("--no-autostart", dest="autostart", action="store_false",
                         help="do not apply the patches at login")
    parser.add_argument("--layout", choices=LAYOUTS, default="notch",
                        help="notch: wrap around the notch (default); below: expanded presentations "
                             "below the notch; classic: activation only")
    parser.add_argument("--no-watch", action="store_true",
                        help="do not start the background helper for clients that start later")
    parser.add_argument("--watcher", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--login", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def login_run(args):
    """Entry point of the login LaunchAgent."""
    def report(text):
        log_line(AUTOSTART_LOG, text)
    report("Login activation started.")
    deadline = time.time() + 120
    while not menubaragent_pid() and time.time() < deadline:
        time.sleep(2)
    if not menubaragent_pid():
        report("MenuBarAgent did not start within two minutes.")
        sys.exit(1)
    time.sleep(5)       # let the login session settle before restarting MenuBarAgent
    try:
        enable(args.layout, not args.no_watch, report=report)
    except SystemExit as exc:
        report(str(exc))
        raise


def main():
    args = parse_args()

    if args.watcher:
        run_watcher(args.watcher)
        return

    check_sip()
    check_environment()
    try:
        check_debug_access()
    except SystemExit as exc:
        if args.login:
            log_line(AUTOSTART_LOG, str(exc))
        raise

    if args.login:
        login_run(args)
        return

    interactive = not (args.enable or args.disable)
    if interactive and not sys.stdin.isatty():
        fail("Run the script in a terminal, or pass --enable or --disable.")

    action = "enable" if args.enable else "disable" if args.disable else ask(
        "What would you like to do?", [("enable", "Enable the Dynamic Island"),
                                       ("disable", "Disable the Dynamic Island")])
    if action == "disable":
        disable()
        return

    autostart = args.autostart      # None: keep the current setting (non-interactive use only)
    if autostart is None and interactive:
        autostart = ask("Apply the Dynamic Island automatically at every login?",
                        [(True, "Yes, apply it at login"),
                         (False, "No, apply it only now (run this script again after a restart)")])

    enable(args.layout, not args.no_watch)
    if autostart is None:
        autostart = os.path.exists(AUTOSTART_PLIST)
        if autostart:
            install_autostart(args.layout, not args.no_watch)       # refresh the installed copy
    elif autostart:
        install_autostart(args.layout, not args.no_watch)
        print(f"Automatic activation at login is enabled (log: {AUTOSTART_LOG}).")
        print("macOS may show a \"Background Items Added\" notification for this login item.")
    elif remove_autostart():
        print("Automatic activation at login has been removed.")
    if not autostart:
        print("The patches are lost when the Mac or MenuBarAgent restarts. Run this script again to reapply them.")
    print("Play music or start a timer to see the Dynamic Island.")


if __name__ == "__main__":
    main()
