# -*- coding: utf-8 -*-
"""xufs_probe.py v3 — 결정적 자기검증 + 광역 변화 관찰.

순서:
  1) LBA 0 부트섹터 읽어 BPB 파싱 → 우리 LBA 계산을 장치의 실제 기하와 대조
     (elen1 위치, 메일박스 LBA 독립 검증)
  2) 루트 디렉터리에서 ELEN1 엔트리 찾아 시작 클러스터 확인
  3) 전체 시퀀스 전송: 19 → 12,13,14,33(프리셋) → 9,29,0xF0(상태) → 1(시작)
  4) 10초간 58 루프 + elen1 전체(600섹터) 해시 비교 → 첫 변화 섹터 헥스덤프
  5) zlog/elen1/루트 타임스탬프 전후 비교
"""
import ctypes
import hashlib
import os
import struct
import sys
import time

import i18n
from i18n import T
import xufs_lib as X


def hexdump(data, limit=256, indent="    "):
    for off in range(0, min(len(data), limit), 16):
        chunk = data[off:off + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"{indent}{off:04x}: {hexs:<47}  {asc}")


def parse_fat32_boot(sector):
    """BPB 파싱 → (bytes_per_sec, sec_per_clus, rsvd, nfats, fat_sz32, root_clus, label)"""
    assert sector[510:512] == b"\x55\xaa", T("No boot signature — SPTD is reading the wrong location!")
    bps, = struct.unpack("<H", sector[11:13])
    spc = sector[13]
    rsvd, = struct.unpack("<H", sector[14:16])
    nfats = sector[16]
    fat_sz32, = struct.unpack("<I", sector[36:40])
    root_clus, = struct.unpack("<I", sector[44:48])
    label = sector[71:82].decode("ascii", "replace").strip()
    return bps, spc, rsvd, nfats, fat_sz32, root_clus, label


def read_sectors(reader, lba, n):
    """연속 섹터 읽기 (최대 64섹터=32KB 단위 — SPTD 제한)."""
    out = b""
    left = n
    cur = lba
    while left > 0:
        chunk = min(left, 64)
        out += reader.read(cur * 512, chunk * 512)
        cur += chunk
        left -= chunk
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_scsi = "--scsi" in sys.argv
    cands = X.find_elen1_drives()
    if not cands:
        print(T("[!] No drive with an elen1 file was found."))
        return 1
    letter = args[0].strip(":") if args else cands[0][0]
    print(T("[i] Read backend: {m}").format(
        m=T("SCSI passthrough") if use_scsi else T("Volume ReadFile")))
    cam = X.XufsCamera(letter, use_scsi=use_scsi)
    rd = cam.vol  # backend
    try:
        # ===== 1) 부트섹터 자기검증 =====
        print(T("\n[1] LBA 0 boot sector check:"))
        boot = rd.read(0, 512)
        hexdump(boot, 96)
        bps, spc, rsvd, nfats, fat_sz32, root_clus, label = parse_fat32_boot(boot)
        cluster_bytes = bps * spc
        data_start_lba = rsvd + nfats * fat_sz32
        print(T("[i] BPB: sector={bps}B sec/cluster={spc} reserved={rsvd} FATs={nfats} "
                "FAT size={fat_sz32} sectors root cluster={root_clus} label='{label}'").format(
                bps=bps, spc=spc, rsvd=rsvd, nfats=nfats, fat_sz32=fat_sz32,
                root_clus=root_clus, label=label))
        print(T("    → Data area start LBA = {x}, cluster={c}B").format(
         x=data_start_lba, c=cluster_bytes))

        # ===== 2) 루트 디렉터리에서 ELEN1 찾기 =====
        print(T("\n[2] Root directory:"))
        root_lba = data_start_lba + (root_clus - 2) * spc
        rootdir = read_sectors(rd, root_lba, spc)
        elen1_lba = None
        for off in range(0, len(rootdir), 32):
            e = rootdir[off:off + 32]
            if e[0] == 0:
                break
            if e[0] in (0xE5, 0x2E):
                continue
            name = (e[0:8].decode("ascii", "replace").strip() + "." +
                    e[8:11].decode("ascii", "replace").strip()).rstrip(".")
            attr = e[11]
            fclus, = struct.unpack("<H", e[26:28])
            fsize, = struct.unpack("<I", e[28:32])
            print(T("    Entry: {name:<12} attr={attr:#04x} cluster={fclus} size={fsize}").format(
         name=name, attr=attr, fclus=fclus, fsize=fsize))
            if name.upper().startswith("ELEN1"):
                elen1_lba = data_start_lba + (fclus - 2) * spc
                print(T("    → ELEN1 data start LBA = {x} (Windows LCN1 calc = {c})").format(
                     x=elen1_lba, c=cluster_bytes // 512))
        if elen1_lba is None:
            print(T("    [!] ELEN1 entry not found — the root may span multiple clusters"))

        # ===== 3) 전체 시퀀스 =====
        print(T("\n[3] Sending full command sequence:"))
        # 파일시스템 수준 전후 스냅샷
        def fs_snap():
            snaps = {}
            for fn in os.listdir(f"{cam.letter}:\\"):
                p = os.path.join(f"{cam.letter}:\\", fn)
                try:
                    st = os.stat(p)
                    with open(p, "rb") as f:
                        h = hashlib.md5(f.read()).hexdigest()
                    snaps[fn] = (st.st_size, st.st_mtime, h)
                except OSError:
                    pass
            return snaps
        snap_before = fs_snap()

        print(T("    cmd19 (datetime)"))
        cam.sync_datetime()
        for cmd in (12, 13, 14, 33):
            print(T("    cmd{cmd} (preset)").format(cmd=cmd))
            cam.send_command(cmd, 1)
            time.sleep(0.05)
        for cmd in (9, 29, 0xF0):
            cam.send_command(cmd)
            time.sleep(0.05)
        print("    cmd1(startrecord)")
        cam.send_command(1)

        # ===== 4) 광역 변화 관찰 =====
        print(T("\n[4] 10s loop: cmd58 + elen1 full (600 sectors) watch"))
        def elen1_hash():
            blob = read_sectors(rd, elen1_lba if elen1_lba is not None else 8, 600)
            return hashlib.md5(blob).hexdigest(), blob
        h_prev, blob_prev = elen1_hash()
        t0 = time.time()
        frames = resps = 0
        reported = 0
        while time.time() - t0 < 10.0:
            cam.request_frame()
            r = cam.poll()
            if r and r[0] == "resp":
                resps += 1
                _, cmd, arg, payload = r
                print(T("    [{t:5.1f}s] resp! cmd={cmd}({cmd_hx}) arg={arg_hx} {pl!r}").format(
                      t=time.time() - t0, cmd=cmd, cmd_hx=f"{cmd:#x}",
                      arg_hx=f"{arg:#x}", pl=repr(payload[:60])))
            elif r and r[0] == "frame":
                frames += 1
                print(T("    [{t:5.1f}s] frame! marker={mk!r} {n}B").format(
          t=time.time() - t0, mk=repr(r[2]), n=len(r[1])))
            h_now, blob_now = elen1_hash()
            if h_now != h_prev and reported < 3:
                # 변화 영역 찾기
                diffs = [i for i in range(0, min(len(blob_now), len(blob_prev)), 512)
                         if blob_now[i:i+512] != blob_prev[i:i+512]]
                print(T("    [{t:5.1f}s] elen1 changed! sectors: {secs}").format(
                      t=time.time() - t0, secs=[d // 512 for d in diffs[:10]]))
                for d in diffs[:2]:
                    hexdump(blob_now[d:d + 128])
                reported += 1
                h_prev, blob_prev = h_now, blob_now
            if frames >= 2:  # 프레임 확보 시에만 조기 종료 (응답만으로는 계속)
                break
        print(T("[Result] frames={f} resps={r} area changes={c}").format(
          f=frames, r=resps, c=reported))

        snap_after = fs_snap()
        changed = {k: (snap_before.get(k), v) for k, v in snap_after.items()
                   if snap_before.get(k) != v}
        new = set(snap_after) - set(snap_before)
        gone = set(snap_before) - set(snap_after)
        if changed or new or gone:
            print(T("[FS changes] changed={c} new={n} removed={g}").format(
          c=list(changed), n=list(new), g=list(gone)))
        else:
            print(T("[FS changes] none (incl. zlog)"))

        if not (frames or resps or reported or changed or new or gone):
            print(T("\n→ No response at all. Next: insert an SD card and retest."))
        return 0
    finally:
        try:
            cam.send_command(2)
        except OSError:
            pass
        cam.close()


if __name__ == "__main__":
    sys.exit(main())
