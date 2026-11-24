# -*- coding: utf-8 -*-
"""
xufs_lib.py — XUFS(Generalplus) 차량 USB 카메라 Windows 클라이언트 공통 라이브러리.

프로토콜 요약 (PROTOCOL.md 참고):
  * 카메라는 USB Mass Storage(VID 0x1B3F/PID 0x8301)로 마운트되며 루트에 elen1(307200B)이 있음.
  * 명령 전송: elen1 오프셋 0x3E800에서 "read 크기"로 니블 인코딩.
      read 0x2200 → [cmd:2][arg1:2][payload] 각 바이트를 lo/hi 니블 read → read 0x2400
      니블 n(1~15) → n*512 바이트 read, 0이면 16*512.
  * 수신: 카메라가 elen1 내용을 갱신 → 호스트가 섹터0(0x200B) 폴링.
      섹터0[0:2]==55 AA → 제어 응답 {cmd@2 u16, arg@4 u16, len@6 u16, data@0xA}
      아니면 프레임 헤더: {len@0 u32, type@4 u16, JPEG 데이터는 0x10부터}
"""
import ctypes
import ctypes.wintypes as wt
import datetime
import os
import struct
import sys
import time

from i18n import T

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_WRITE_THROUGH = 0x80000000
FSCTL_GET_RETRIEVAL_POINTERS = 0x00090073
INVALID_HANDLE_VALUE = wt.HANDLE(-1).value

ELEN1_SIZE = 307200  # 0x4B000
MAILBOX_OFF = 0x3E800  # 명령 신호 오프셋 (elen1 내부)

# 파일 카테고리 플래그 (cmd 5/6/20/21/22 arg1 공용 인코딩)
# 0x8000=사진(PICT), 0x4000=후방 카메라.
# LOCK은 플래그가 아니라 목록 응답의 날짜시간 비트0으로 전달된다.
CATEGORY_FLAG = {"MOVI": 0x0000, "LOCK": 0x0000, "PICT": 0x8000}


def file_arg(idx, kind="MOVI", camera=0):
    """파일 접근 명령(cmd 6/20/21/22)의 arg1 = 파일번호 | 카테고리플래그 | cameraId<<14."""
    return (idx & 0x3FFF) | CATEGORY_FLAG.get(kind, 0) | ((camera & 1) << 14)


class STARTING_VCN_INPUT_BUFFER(ctypes.Structure):
    _fields_ = [("StartingVcn", ctypes.c_int64)]


class EXTENT(ctypes.Structure):
    _fields_ = [("NextVcn", ctypes.c_int64), ("Lcn", ctypes.c_int64)]


class RETRIEVAL_POINTERS_BUFFER(ctypes.Structure):
    _fields_ = [("ExtentCount", wt.DWORD), ("StartingVcn", ctypes.c_int64),
                ("Extents", EXTENT * 1)]


class SCSI_PASS_THROUGH_DIRECT(ctypes.Structure):
    _fields_ = [("Length", wt.WORD),
                ("ScsiStatus", wt.BYTE), ("PathId", wt.BYTE),
                ("TargetId", wt.BYTE), ("Lun", wt.BYTE),
                ("CdbLength", wt.BYTE), ("SenseInfoLength", wt.BYTE),
                ("DataIn", wt.BYTE),
                ("DataTransferLength", wt.DWORD),
                ("TimeOutValue", wt.DWORD),
                ("DataBuffer", ctypes.c_void_p),
                ("SenseInfoOffset", wt.DWORD),
                ("Cdb", wt.BYTE * 16)]


IOCTL_SCSI_PASS_THROUGH_DIRECT = 0x4D014
IOCTL_DISK_GET_PARTITION_INFO_EX = 0x70030
IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x2D1080
IOCTL_DISK_GET_LENGTH_INFO = 0x7405C
SCSIOP_READ10 = 0x28


def _winerr(msg):
    e = ctypes.get_last_error()
    raise OSError(e, f"{msg} (Win32 error {e}: {ctypes.FormatError(e).strip()})")


class RawVolume:
    """NO_BUFFERING 볼륨 핸들 — 각 ReadFile이 1:1 SCSI READ가 되도록 한다."""

    def __init__(self, drive_letter):
        self.letter = drive_letter
        path = f"\\\\.\\{drive_letter}:"
        self.handle = kernel32.CreateFileW(
            path, GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE, None,
            OPEN_EXISTING, FILE_FLAG_NO_BUFFERING | FILE_FLAG_WRITE_THROUGH, None)
        if self.handle == INVALID_HANDLE_VALUE:
            _winerr(f"볼륨 열기 실패 {path} (관리자 권한 필요할 수 있음)")

    def read(self, volume_offset, size):
        """볼륨 절대 오프셋에서 정확히 size 바이트 읽기. 오프셋/크기는 512 배수여야 함."""
        assert volume_offset % 512 == 0 and size % 512 == 0
        buf = ctypes.create_string_buffer(size + 512)
        addr = ctypes.addressof(buf)
        aligned = ctypes.c_void_p((addr + 511) & ~511)
        pos = ctypes.c_int64(volume_offset)
        if not kernel32.SetFilePointerEx(self.handle, pos, None, 0):
            _winerr("SetFilePointerEx 실패")
        nread = wt.DWORD(0)
        ok = kernel32.ReadFile(self.handle, aligned, wt.DWORD(size),
                               ctypes.byref(nread), None)
        if not ok:
            _winerr(f"ReadFile({size}) 실패")
        if nread.value != size:
            raise OSError(f"ReadFile 크기 불일치: {nread.value}/{size}")
        return ctypes.string_at(aligned.value, size)

    def close(self):
        if getattr(self, "handle", None) not in (None, INVALID_HANDLE_VALUE):
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class ScsiVolumeReader:
    """볼륨 핸들에 IOCTL_SCSI_PASS_THROUGH_DIRECT로 READ(10)를 직접 발행.
    일반 ReadFile의 신호 전달이 안 될 때의 확진 백엔드 (관리자 권한 필요할 수 있음)."""

    def __init__(self, drive_letter, verbose=True):
        self.letter = drive_letter
        self.sector_size = 512
        self._bufpool = {}
        self.partition_start = None

        # 기준 데이터: 볼륨 백엔드가 보는 LBA 0 (진짜 부트섹터)
        ref = RawVolume(drive_letter).read(0, 512)
        guess = struct.unpack("<I", ref[0x1C:0x20])[0] if ref[510:512] == b"\x55\xaa" else 0

        # 후보 물리 디스크: 이 볼륨의 것 우선, 실패 시 전수 조사
        dev_num = self._volume_device_number(drive_letter)
        order = [dev_num] + [n for n in range(8) if n != dev_num]
        for n in order:
            try:
                if self._try_open_drive(n, ref, guess, verbose):
                    return  # 성공: 올바른 디스크 + 파티션 오프셋 확정
            except OSError as e:
                if verbose:
                    print(f"    PhysicalDrive{n}: {e}")
        raise RuntimeError(
            "어떤 물리 디스크에서도 볼륨 부트섹터 내용을 찾지 못함 — "
            "SPTD 응답이 실제 매체와 무관하게 합성되고 있을 가능성 (USBPcap 캡처 필요)")

    def _volume_device_number(self, drive_letter):
        vpath = f"\\\\.\\{drive_letter}:"
        vh = kernel32.CreateFileW(
            vpath, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
            OPEN_EXISTING, 0, None)
        if vh == INVALID_HANDLE_VALUE:
            _winerr(f"볼륨 열기 실패 {vpath}")
        buf = ctypes.create_string_buffer(64)
        ret = wt.DWORD(0)
        ok = kernel32.DeviceIoControl(
            vh, IOCTL_STORAGE_GET_DEVICE_NUMBER, None, 0,
            buf, ctypes.sizeof(buf), ctypes.byref(ret), None)
        kernel32.CloseHandle(vh)
        if not ok:
            _winerr("IOCTL_STORAGE_GET_DEVICE_NUMBER 실패")
        return struct.unpack("<III", buf.raw[:12])[1]

    def _disk_length(self, handle):
        buf = ctypes.create_string_buffer(8)
        ret = wt.DWORD(0)
        if kernel32.DeviceIoControl(
                handle, IOCTL_DISK_GET_LENGTH_INFO, None, 0,
                buf, ctypes.sizeof(buf), ctypes.byref(ret), None):
            return struct.unpack("<q", buf.raw)[0]
        return -1

    def _try_open_drive(self, n, ref, guess, verbose):
        ppath = f"\\\\.\\PhysicalDrive{n}"
        h = kernel32.CreateFileW(
            ppath, GENERIC_READ | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
            FILE_SHARE_READ | FILE_SHARE_WRITE, None,
            OPEN_EXISTING, FILE_FLAG_WRITE_THROUGH, None)
        if h == INVALID_HANDLE_VALUE:
            return False
        size = self._disk_length(h)
        saved_handle, saved_num = self.handle if hasattr(self, "handle") else None, \
            getattr(self, "device_number", None)
        self.handle, self.device_number = h, n
        try:
            # 후보 오프셋 빠른 시도
            for cand in dict.fromkeys([guess, 63, 128, 2048, 4096, 0]):
                if self.read_absolute(cand * 512, 512) == ref:
                    self.partition_start = cand
                    if verbose:
                        print(T("[i] SCSI target: PhysicalDrive{n} ({gb:.2f}GB), partition start LBA {lba}({hx})")
                              .format(n=n, gb=size / 1e9, lba=cand, hx=f"{cand:#x}"))
                    return True
            # 선형 스캔 (최대 64KB 영역)
            if size >= 0 and 64 * 1024 * 1024 < size < 8 * 1024**3:  # 카메라급 크기만
                for lba in range(0, 16384):
                    if self.read_absolute(lba * 512, 512) == ref:
                        self.partition_start = lba
                        if verbose:
                            print(T("[i] SCSI target (scan): PhysicalDrive{n}, partition start LBA {lba}({hx})")
                                  .format(n=n, lba=lba, hx=f"{lba:#x}"))
                        return True
            return False
        except OSError:
            return False
        finally:
            if self.partition_start is None:
                kernel32.CloseHandle(h)
                self.handle, self.device_number = saved_handle, saved_num

    def read_absolute(self, abs_offset, size):
        """디스크 절대 오프셋에서 READ(10) 1회 발행 (최대 32KB)."""
        assert abs_offset % 512 == 0 and size % 512 == 0 and size <= 0x8000
        return self._read10(abs_offset // 512, size // 512)

    def aligned_buf(self, size):
        size = (size + 511) & ~511
        if size not in self._bufpool:
            raw = ctypes.create_string_buffer(size + 4096)
            addr = ctypes.addressof(raw)
            self._bufpool[size] = (raw, ctypes.c_void_p((addr + 4095) & ~4095))
        return self._bufpool[size]

    def read(self, volume_offset, size):
        """일반 볼륨 오프셋 인터페이스 호환. 필요 시 32KB 청크로 분할 발행.
        (니블 신호용 읽기는 모두 0x2200 이하 → 항상 단일 CDB로 나감)"""
        assert volume_offset % 512 == 0 and size % 512 == 0
        out = []
        off = volume_offset
        left = size
        while left > 0:
            n = min(left, 0x8000)
            out.append(self._read10((self.partition_start * 512 + off) // 512,
                                    n // self.sector_size))
            off += n
            left -= n
        return b"".join(out)

    def _read10(self, lba, blocks):
        size = blocks * self.sector_size
        raw = ctypes.create_string_buffer(ctypes.sizeof(SCSI_PASS_THROUGH_DIRECT) + 64)
        sptd = ctypes.cast(raw, ctypes.POINTER(SCSI_PASS_THROUGH_DIRECT)).contents
        sptd.Length = ctypes.sizeof(SCSI_PASS_THROUGH_DIRECT)
        sptd.CdbLength = 10
        sptd.SenseInfoLength = 32
        sptd.DataIn = 1  # SCSI_IOCTL_DATA_IN
        sptd.DataTransferLength = size
        sptd.TimeOutValue = 4
        sptd.SenseInfoOffset = ctypes.sizeof(SCSI_PASS_THROUGH_DIRECT)
        cdb = bytes([SCSIOP_READ10, 0,
                     (lba >> 24) & 0xFF, (lba >> 16) & 0xFF,
                     (lba >> 8) & 0xFF, lba & 0xFF, 0,
                     (blocks >> 8) & 0xFF, blocks & 0xFF, 0])
        sptd.Cdb[:len(cdb)] = cdb  # 나머지는 0으로 유지
        keepalive, data_ptr = self.aligned_buf(size)
        sptd.DataBuffer = data_ptr
        ret = wt.DWORD(0)
        ok = kernel32.DeviceIoControl(
            self.handle, IOCTL_SCSI_PASS_THROUGH_DIRECT,
            raw, ctypes.sizeof(SCSI_PASS_THROUGH_DIRECT) + 64,
            raw, ctypes.sizeof(SCSI_PASS_THROUGH_DIRECT) + 64,
            ctypes.byref(ret), None)
        if not ok:
            _winerr("SPTD 실패 (관리자 권한 필요?)")
        if sptd.ScsiStatus != 0:
            sense = raw.raw[ctypes.sizeof(SCSI_PASS_THROUGH_DIRECT):
                            ctypes.sizeof(SCSI_PASS_THROUGH_DIRECT) + 18]
            raise OSError(f"SCSI 상태 {sptd.ScsiStatus}, sense={sense.hex()}")
        return ctypes.string_at(data_ptr.value, size)

    close = RawVolume.close
    __enter__ = RawVolume.__enter__
    __exit__ = RawVolume.__exit__


def find_elen1_drives():
    """루트에 elen1(307200B)이 있는 드라이브 문자 목록 → [(letter, path, size)]"""
    out = []
    bitmask = kernel32.GetLogicalDrives()
    for i, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        if not (bitmask >> i) & 1:
            continue
        root = f"{letter}:\\"
        if kernel32.GetDriveTypeW(root) not in (2, 3):  # REMOVABLE/FIXED만
            continue
        p = os.path.join(root, "elen1")
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_size == ELEN1_SIZE:
            out.append((letter, p, st.st_size))
    return out


def get_file_extents(path):
    """FSCTL_GET_RETRIEVAL_POINTERS로 파일의 (VCN→LCN) 확장 목록과 클러스터 크기 반환.
    반환: (cluster_size, [(vcn_start, vcn_end, lcn_start), ...])"""
    h = kernel32.CreateFileW(
        path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
        OPEN_EXISTING, 0, None)  # 버퍼링 무관(메타데이터만 조회)
    if h == INVALID_HANDLE_VALUE:
        _winerr(f"elen1 열기 실패: {path}")
    try:
        # 클러스터 크기
        spc = wt.DWORD(0); bps = wt.DWORD(0); fc = wt.DWORD(0); tc = wt.DWORD(0)
        drive_root = os.path.dirname(path)
        if not kernel32.GetDiskFreeSpaceW(drive_root, ctypes.byref(spc),
                                          ctypes.byref(bps), ctypes.byref(fc),
                                          ctypes.byref(tc)):
            _winerr("GetDiskFreeSpaceW 실패")
        cluster = spc.value * bps.value

        inbuf = STARTING_VCN_INPUT_BUFFER(0)
        outbuf = ctypes.create_string_buffer(4096)
        ret = wt.DWORD(0)
        ok = kernel32.DeviceIoControl(
            h, FSCTL_GET_RETRIEVAL_POINTERS, ctypes.byref(inbuf),
            ctypes.sizeof(inbuf), outbuf, ctypes.sizeof(outbuf),
            ctypes.byref(ret), None)
        if not ok:
            _winerr("FSCTL_GET_RETRIEVAL_POINTERS 실패")
        rp = ctypes.cast(outbuf, ctypes.POINTER(RETRIEVAL_POINTERS_BUFFER)).contents
        count = rp.ExtentCount
        extents = []
        prev_vcn = rp.StartingVcn
        base = ctypes.addressof(rp.Extents)
        for i in range(count):
            ext = EXTENT.from_address(base + i * ctypes.sizeof(EXTENT))
            extents.append((prev_vcn, ext.NextVcn, ext.Lcn))
            prev_vcn = ext.NextVcn
        return cluster, extents
    finally:
        kernel32.CloseHandle(h)


def get_disk_length(drive_letter):
    """볼륨 전체 크기(바이트). 실패 시 -1."""
    vh = kernel32.CreateFileW(
        f"\\\\.\\{drive_letter}:", 0,
        FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
    if vh == INVALID_HANDLE_VALUE:
        return -1
    try:
        buf = ctypes.create_string_buffer(8)
        ret = wt.DWORD(0)
        if kernel32.DeviceIoControl(
                vh, IOCTL_DISK_GET_LENGTH_INFO, None, 0,
                buf, ctypes.sizeof(buf), ctypes.byref(ret), None):
            return struct.unpack("<q", buf.raw)[0]
    finally:
        kernel32.CloseHandle(vh)
    return -1


class Elen1Map:
    """elen1 파일 오프셋 → 볼륨 절대 오프셋 변환.

    주의: FAT 볼륨에서 FSCTL_GET_RETRIEVAL_POINTERS의 LCN은 데이터 영역 기준
    상대값이다(FAT클러스터-2). 따라서 실제 볼륨 오프셋 = 데이터영역시작 + LCN×클러스터.
    데이터 영역 시작은 BPB(볼륨 LBA 0)에서 계산한다."""

    def __init__(self, path, drive_letter=None):
        self.cluster, self.extents = get_file_extents(path)
        self.file_size = os.stat(path).st_size
        # BPB에서 데이터 영역 시작 산출
        letter = drive_letter or os.path.split(path)[0].split(":")[0]
        boot = RawVolume(letter).read(0, 512)
        if boot[510:512] != b"\x55\xaa":
            raise OSError("볼륨 부트섹터 이상 — FAT 마운트 상태 확인 필요")
        bps, = struct.unpack("<H", boot[11:13])
        spc = boot[13]
        rsvd, = struct.unpack("<H", boot[14:16])
        nfats = boot[16]
        fat_sz, = struct.unpack("<I", boot[36:40])
        if bps != 512 or spc * bps != self.cluster:
            # 클러스터 크기 불일치 시 적어도 깨지지 않도록 보정
            pass
        self.data_base = (rsvd + nfats * fat_sz) * bps

    def vol_offset(self, file_offset):
        assert 0 <= file_offset < self.file_size
        vcn = file_offset // self.cluster
        for vcn_start, vcn_end, lcn in self.extents:
            if vcn_start <= vcn < vcn_end:
                if lcn < 0:
                    raise OSError("elen1이 스파스/할당해제 영역을 포함 (포맷 후 재시도)")
                return (self.data_base + (lcn + (vcn - vcn_start)) * self.cluster
                        + file_offset % self.cluster)
        raise OSError(f"elen1 오프셋 {file_offset:#x}의 매핑을 찾을 수 없음")

    def describe(self):
        lines = [f"  클러스터 크기: {self.cluster}B, extents: {len(self.extents)}, "
                 f"데이터영역 시작: LBA {self.data_base // 512}"]
        for v0, v1, lcn in self.extents:
            lines.append(f"  VCN {v0}..{v1} -> LCN {lcn} "
                         f"(실제 볼륨 LBA {self.data_base // 512 + lcn * self.cluster // 512})")
        return "\n".join(lines)


class XufsCamera:
    def __init__(self, drive_letter=None, verbose=True, use_scsi=False):
        cands = find_elen1_drives()
        if not cands:
            raise RuntimeError(T(
                "No drive containing an elen1 file was found. "
                "Check the camera connection and mounting."))
        if drive_letter:
            cands = [c for c in cands if c[0].lower() == drive_letter.lower()] or cands
        self.letter, self.path, _ = cands[0]
        if verbose:
            print(T("[i] Camera found: {p} (drive {l}:)").format(p=self.path, l=self.letter))
        self.map = Elen1Map(self.path)
        if verbose:
            print(self.map.describe())
        self.vol = ScsiVolumeReader(self.letter) if use_scsi else RawVolume(self.letter)
        if verbose and use_scsi:
            print(T("[i] SCSI passthrough mode (PhysicalDrive{n}, partition start {hx})")
                  .format(n=self.vol.device_number, hx=f"{self.vol.partition_start:#x}"))
        self.verbose = verbose

    def close(self):
        self.vol.close()

    # --- 저수준 읽기 ---
    def _read_at(self, file_offset, size):
        return self.vol.read(self.map.vol_offset(file_offset), size)

    # --- 명령 전송 (니블 인코딩) ---
    def send_command(self, cmd, arg1=0, payload=b""):
        payload = bytes(payload[:0x20])
        mbox = self.map.vol_offset(MAILBOX_OFF)

        def rd(n):
            self.vol.read(mbox, n)

        rd(0x2200)  # 패킷 시작 (17 블록)
        for b in struct.pack("<HH", cmd, arg1) + payload:
            lo, hi = b & 0xF, b >> 4
            rd((lo or 16) << 9)
            rd((hi or 16) << 9)
        rd(0x2400)  # 패킷 끝 (18 블록)

    # --- 응답/프레임 폴링 ---
    def poll(self):
        """한 주기 읽기. 반환:
           ('resp', cmd, arg, payload) 또는 ('frame', jpeg_bytes, marker) 또는 None"""
        sec0 = self._read_at(0, 0x200)
        if sec0[0:2] == b"\x55\xaa":
            cmd, arg, ln = struct.unpack("<HHH", sec0[2:8])
            payload = sec0[0xA:0x200]
            if ln > 0x1F7:
                # 이어지는 페이로드: 섹터0 이후 (ln+9)&~0x1FF 바이트 추가 읽기
                extra = self._read_at(0x200, (ln + 9) & ~0x1FF)
                payload = payload + extra[: ln - 0x1F6]
            else:
                payload = payload[:ln]
            return ("resp", cmd, arg, payload)
        ln = struct.unpack("<I", sec0[0:4])[0]
        if ln == 0 or ln - 1 > 0x4AFFE:
            return None  # 아직 데이터 없음/무의미
        marker = sec0[4:6]
        total = (0x10 + ln + 511) & ~511
        if total > ELEN1_SIZE:
            total = ELEN1_SIZE
        rest = self._read_at(0x200, total - 0x200) if total > 0x200 else b""
        jpg = sec0[0x10:0x200] + rest[: max(0, ln - 0x1F0)]
        return ("frame", jpg, marker)

    def peek_resp(self):
        """섹터 0만 빠르게 읽어 55AA 제어 응답을 확인한다 (프레임 데이터는 읽지 않음).
        반환: ('resp', cmd, arg, payload≤0x1F6B) 또는 None.
        용도: cmd 57 응답처럼 다음 프레임에 금방 덮이는 응답의 고빈도 포착."""
        sec0 = self._read_at(0, 0x200)
        if sec0[0:2] == b"\x55\xaa":
            cmd, arg, ln = struct.unpack("<HHH", sec0[2:8])
            payload = sec0[0xA:0x200]
            if ln < len(payload):
                payload = payload[:ln]
            return ("resp", cmd, arg, payload)
        return None

    # --- 편의 명령 ---
    def sync_datetime(self, when=None):
        t = when or datetime.datetime.now()
        # 연도는 2000년 기준 2자리 (2026년이면 26)
        payload = bytes([t.year - 2000, t.month, t.day, t.hour, t.minute, t.second])
        self.send_command(19, 0, payload)  # cmd 19: 날짜시간 설정

    def request_frame(self, when=None):
        """cmd 58: 프레임 요청.
        페이로드 = 오버레이용 날짜시간 ASCII 문자열 "%02d-%02d-%02d %02d:%02d:%02d.10"."""
        t = when or datetime.datetime.now()
        s = f"{t.year % 100:02d}-{t.month:02d}-{t.day:02d} " \
            f"{t.hour:02d}:{t.minute:02d}:{t.second:02d}.10"
        self.send_command(58, 0, s.encode("ascii"))

    def start_stream(self, record=True):
        """세션 시작 시퀀스: 19(날짜시간) → 12(분할 시간),13(마이크),14(오버레이) → [1(startrecord)].
        cmd 33(카메라 전환)은 녹화 시작에 불필요 — 전송하지 않는다(관찰).
        (SD 착탈 시 카메라가 녹화하면 SD 쓰기와 스트리밍이 경합해 fps가 떨어짐 — 실측)"""
        self.sync_datetime()
        self.send_command(12, 3)   # 녹화 분할 시간 3분 (기본값)
        self.send_command(13, 1)   # 오디오(마이크) 켬
        self.send_command(14, 1)   # 타임스탬프 오버레이 켬
        if record:
            self.send_command(1)   # startrecord (SD에 파일 생성)

    # --- SD 카드 파일 (목록/재생 스트림 캡처) ---
    # 카테고리 플래그: 0x0000=MOVI(비디오), 0x8000=LOCK(잠금), 0x4000=PICT(사진)
    # cmd 5 arg1 = cameraId<<14 | 카테고리플래그
    # cmd 6/20/21/22 arg1 = 파일 id | 카테고리플래그
    # cmd 7 = 재생 일시정지, cmd 8 = 재생 정지

    def list_files(self, wait=2.0):
        """SD 파일 목록 — 비디오(MOVI)/잠금(LOCK)/사진(PICT) 카테고리를 순서대로 조회.

        반환: [(index, datetime, kind)] — kind는 "MOVI"/"LOCK"/"PICT"."""
        out = []
        seen = set()
        # LOCK은 패킹 날짜시간 비트0, 카테고리는 조회 인자로 구분 —
        # 0x0000=비디오(MOVI+LOCK 함께), 0x8000=사진(PICT).
        for arg, cat in ((0x0000, "video"), (0x8000, "PICT")):
            self.send_command(5, arg)
            t0 = time.time()
            payload = None
            while time.time() - t0 < wait:
                r = self.poll()
                if r and r[0] == "resp" and r[1] == 5:
                    payload = r[3]
                    break
                time.sleep(0.01)
            if payload is None:
                continue
            for idx, dt, locked in parse_file_list(payload):
                kind = "PICT" if cat == "PICT" else ("LOCK" if locked else "MOVI")
                key = (kind, idx)
                if key not in seen:
                    seen.add(key)
                    out.append((idx, dt, kind))
        return out

    def play_file(self, idx, kind="MOVI"):
        """cmd 6: 파일 재생 시작. 스트림은 poll()의 frame(JPEG) + resp 0x21(오디오)로 도착."""
        self.send_command(6, file_arg(idx, kind))

    def pause_playback(self):
        self.send_command(7)

    def stop_playback(self):
        self.send_command(8)

    def download_file(self, idx, kind="MOVI", on_progress=None,
                      quiet_secs=2.5, max_secs=3600):
        """파일 재생 스트림을 캡처해 프레임/오디오로 수집.

        반환: (frames, audio) — frames: EOI 절단된 JPEG 바이트 목록,
        audio: 원시 PCM 청크 목록(resp cmd 0x21, 32kHz 모노 s16le 추정).
        on_progress(frames, video_bytes, audio_chunks, elapsed).
        콜백이 False를 반환하면 캡처를 중단한다."""
        self.play_file(idx, kind)
        frames, audio = [], []
        vbytes = 0
        t_start = time.time()
        t_last = t_start
        got = False
        while True:
            r = self.poll()
            now = time.time()
            if r is None:
                if got and now - t_last > quiet_secs:
                    break
                if not got and now - t_start > 12.0:
                    raise RuntimeError("재생 스트림 시작 실패 (cmd 6 후 데이터 없음)")
                continue
            if r[0] == "frame":
                data = r[1]
                if data[:3] == b"\xff\xd8\xff":
                    e = data.find(b"\xff\xd9", 3)
                    if e > 0:
                        data = data[:e + 2]
                    frames.append(data)
                    vbytes += len(data)
                got = True
                t_last = now
            else:
                if r[0] == "resp" and r[1] == 0x21 and r[3]:
                    audio.append(r[3])
                got = True
                t_last = now
            if on_progress:
                if on_progress(len(frames), vbytes, len(audio), now - t_start) is False:
                    break  # 콜백이 False 반환 = 사용자 취소
            if now - t_start > max_secs:
                break
        self.stop_playback()
        return frames, audio


def parse_file_list(payload):
    """cmd=5 응답 페이로드 → [(index, datetime, locked)] 목록.

    네이티브 parse_files 분석 확정: 패킹 날짜시간 u32의 **비트0 = LOCK 플래그**
    (bit0==0 → "MOVI%04d" 포맷, bit0==1 → "LOCK%04d"). 잠금 파일의 초 값은
    이 비트가 포함되어 ±1초 오차가 날 수 있다(펌웨어 특성)."""
    files = []
    for i in range(0, len(payload) - 5, 6):
        idx, v = struct.unpack("<HI", payload[i:i + 6])
        if idx == 0xFFFF:
            break
        locked = bool(v & 1)
        dt = datetime.datetime(
            year=2000 + (v >> 26), month=(v >> 22) & 0xF, day=(v >> 17) & 0x1F,
            hour=(v >> 12) & 0x1F, minute=(v >> 6) & 0x3F,
            second=(v >> 1) & 0x1F)
        files.append((idx, dt, locked))
    return files


class MjpegAviWriter:
    """MJPEG AVI 스트리밍 라이터 — 프레임을 하나씩 add()하고 close()에서 헤더 패치."""

    def __init__(self, path, fps=25.0, width=1280, height=720):
        self.path = path
        self.fps = fps
        self.width = width
        self.height = height
        self.n = 0
        self.total = 0
        self.sizes = []
        self.f = open(path, "wb")
        self.f.write(b"RIFF" + struct.pack("<I", 0) + b"AVI ")
        self.pos_hdrl = self.f.tell()
        self.f.write(b"LIST" + struct.pack("<I", 0) + b"hdrl")
        self.f.write(b"avih" + struct.pack("<I", 56))
        self.pos_avih = self.f.tell()
        self.f.write(struct.pack("<14I", 0, 0, 0, 0x10, 0, 0, 1, 0, width,
                                 height, 0, 0, 0, 0))
        self.pos_strl = self.f.tell()
        self.f.write(b"LIST" + struct.pack("<I", 0) + b"strl")
        strh = (b"vids" + b"MJPG" + struct.pack("<I", 0) + struct.pack("<HH", 0, 0)
                + struct.pack("<IIII", 0, 1000, int(round(fps * 1000)), 0)
                + struct.pack("<IIII", 0, 0, 0, 0)
                + struct.pack("<hhhh", 0, 0, width, height))
        assert len(strh) == 56
        self.f.write(b"strh" + struct.pack("<I", 56) + strh)
        strf = struct.pack("<IiiHH4sIIIII", 40, width, height, 1, 24, b"MJPG",
                           0, 0, 0, 0, 0)  # BITMAPINFOHEADER 40B 전체
        assert len(strf) == 40
        self.f.write(b"strf" + struct.pack("<I", len(strf)) + strf)
        strl_end = self.f.tell()
        self.f.seek(self.pos_strl + 4)
        self.f.write(struct.pack("<I", strl_end - self.pos_strl - 8))
        self.f.seek(self.pos_hdrl + 4)
        self.f.write(struct.pack("<I", strl_end - self.pos_hdrl - 8))
        self.f.seek(strl_end)
        self.pos_movi = self.f.tell()
        self.f.write(b"LIST" + struct.pack("<I", 0) + b"movi")

    def add(self, frame):
        self.f.write(b"00dc" + struct.pack("<I", len(frame)))
        self.f.write(frame)
        if len(frame) & 1:
            self.f.write(b"\x00")
        self.sizes.append(len(frame))
        self.n += 1
        self.total += len(frame)

    def close(self):
        """마무리: idx1 기록 후 movi/RIFF/헤더 패치. 반환 (프레임 수, 총 바이트)."""
        movi_end = self.f.tell()
        self.f.write(b"idx1" + struct.pack("<I", 16 * self.n))
        pos = 4  # 첫 청크 오프셋 관례: 'movi' fourcc 바로 뒤 기준
        for sz in self.sizes:
            self.f.write(b"00dc" + struct.pack("<III", 0x10, pos, sz))
            pos += 8 + sz + (sz & 1)
        end = self.f.tell()
        self.f.seek(self.pos_movi + 4)
        self.f.write(struct.pack("<I", movi_end - self.pos_movi - 8))
        self.f.seek(4)
        self.f.write(struct.pack("<I", end - 8))
        self.f.seek(self.pos_avih)
        self.f.write(struct.pack("<14I", int(round(1e6 / max(self.fps, 0.1))),
                                 0, 0, 0x10, self.n, 0, 1,
                                 max(self.sizes) if self.sizes else 0,
                                 self.width, self.height, 0, 0, 0, 0))
        self.f.close()
        return self.n, self.total


def write_mjpeg_avi(frames_iter, path, fps=25.0, width=1280, height=720):
    """JPEG 프레임 시퀀스를 MJPEG AVI로 기록. 반환: (프레임 수, 총 바이트)."""
    w = MjpegAviWriter(path, fps, width, height)
    for fr in frames_iter:
        w.add(fr)
    return w.close()


def write_avi_with_audio(frames, pcm, path, fps=25.0, width=1280, height=720,
                         audio_rate=32000):
    """MJPEG 비디오 + PCM 오디오를 하나의 AVI로 뮥싱 (비인터리브, 의존성 없음).

    frames: JPEG 프레임 바이트 목록, pcm: 원시 PCM 청크 목록(32kHz 모노 s16le).
    대부분의 플레이어(VLC/WMP)가 비인터리브 AVI를 정상 재생한다."""
    import struct as _s

    def _chunk(fcc, data):
        c = _s.pack("<4sI", fcc, len(data)) + data
        if len(data) & 1:
            c += b"\x00"
        return c

    n_v = len(frames)
    n_a = len(pcm)
    v_total = sum(len(f) for f in frames)
    a_total = sum(len(c) for c in pcm)
    usec = int(round(1e6 / max(fps, 0.1)))
    max_v = max((len(f) for f in frames), default=0)
    max_a = max((len(c) for c in pcm), default=0)

    with open(path, "wb") as f:
        f.write(b"RIFF" + _s.pack("<I", 0) + b"AVI ")

        # --- hdrl ---
        pos_hdrl = f.tell()
        f.write(b"LIST" + _s.pack("<I", 0) + b"hdrl")
        f.write(b"avih" + _s.pack("<I", 56))
        f.write(_s.pack("<14I", usec, int(max(v_total, a_total) * 8), 0,
                        0x10,                       # AVIF_HASINDEX
                        n_v, 0, 2,                  # 총 프레임, 스트림 2개
                        max(max_v, max_a), width, height, 0, 0, 0, 0))
        # 비디오 strl
        pos_v_strl = f.tell()
        f.write(b"LIST" + _s.pack("<I", 0) + b"strl")
        f.write(b"strh" + _s.pack("<I", 56) + b"vids" + b"MJPG"
                + _s.pack("<I", 0) + _s.pack("<HH", 0, 0)
                + _s.pack("<IIII", 0, 1000, int(round(fps * 1000)), 0)
                + _s.pack("<IIII", n_v, max_v, 0, 0)
                + _s.pack("<hhhh", 0, 0, width, height))
        strf_v = _s.pack("<IiiHH4sIIIII", 40, width, height, 1, 24, b"MJPG",
                         0, 0, 0, 0, 0)  # BITMAPINFOHEADER 40B 전체
        f.write(b"strf" + _s.pack("<I", len(strf_v)) + strf_v)
        size_v = f.tell() - pos_v_strl - 8   # seek 전에 계산
        f.seek(pos_v_strl + 4)
        f.write(_s.pack("<I", size_v))
        f.seek(0, 2)
        # 오디오 strl (PCMWAVEFORMAT)
        pos_a_strl = f.tell()
        f.write(b"LIST" + _s.pack("<I", 0) + b"strl")
        f.write(b"strh" + _s.pack("<I", 56) + b"auds" + b"\x00\x00\x00\x00"
                + _s.pack("<I", 0) + _s.pack("<HH", 0, 0)
                + _s.pack("<IIII", 0, 1, audio_rate, 0)        # InitialFrames/Scale/Rate/Start
                + _s.pack("<IIII", a_total // 2, max_a, 0, 2)  # Length(샘플수)/BufSize/Quality/SampleSize
                + _s.pack("<hhhh", 0, 0, 0, 0))
        strf_a = _s.pack("<HHIIHH", 1, 1, audio_rate, audio_rate * 2, 2, 16)  # 모노 1채널
        f.write(b"strf" + _s.pack("<I", len(strf_a)) + strf_a)
        size_a = f.tell() - pos_a_strl - 8
        f.seek(pos_a_strl + 4)
        f.write(_s.pack("<I", size_a))
        f.seek(0, 2)

        # --- movi ---
        pos_movi = f.tell()
        f.write(b"LIST" + _s.pack("<I", 0) + b"movi")
        idx_entries = []
        # 비디오 청크
        for fr in frames:
            here = f.tell()
            f.write(b"00dc" + _s.pack("<I", len(fr)))
            f.write(fr)
            if len(fr) & 1:
                f.write(b"\x00")
            idx_entries.append((b"00dc", here - pos_movi - 8, len(fr)))
        # 오디오 청크 (모든 비디오 뒤에 배치 — 비인터리브)
        a_pos = f.tell()
        f.write(b"01wb" + _s.pack("<I", a_total))
        for c in pcm:
            f.write(c)
        if a_total & 1:
            f.write(b"\x00")
        idx_entries.append((b"01wb", a_pos - pos_movi - 8, a_total))  # 'movi' fourcc 기준
        movi_end = f.tell()

        # --- idx1 (movi 데이터 다음 파일 끝에 기록) ---
        f.write(b"idx1" + _s.pack("<I", 16 * len(idx_entries)))
        for fcc, off, sz in idx_entries:
            f.write(fcc + _s.pack("<III", 0x10, off, sz))
        end = f.tell()

        # --- 패치 ---
        f.seek(4)
        f.write(_s.pack("<I", end - 8))                      # RIFF 크기
        f.seek(pos_movi + 4)
        f.write(_s.pack("<I", movi_end - pos_movi - 8))      # movi LIST 크기
        f.seek(pos_hdrl + 4)
        f.write(_s.pack("<I", pos_movi - pos_hdrl - 8))      # hdrl LIST 크기
        f.seek(pos_v_strl + 4)
        f.write(_s.pack("<I", pos_a_strl - pos_v_strl - 8))  # video strl 크기
        f.seek(pos_a_strl + 4)
        f.write(_s.pack("<I", pos_movi - pos_a_strl - 8))    # audio strl 크기
        # avih 패치 — 데이터는 pos_hdrl+20에서 시작. dwTotalFrames(+36), dwSuggestedBufferSize(+48)
        f.seek(pos_hdrl + 20 + 16)
        f.write(_s.pack("<I", n_v))
        f.seek(pos_hdrl + 20 + 28)
        f.write(_s.pack("<I", max(max_v, max_a)))
    return n_v, v_total, n_a, a_total


