# -*- coding: utf-8 -*-
"""xufs_player.py — 카메라 통합 GUI: 라이브 뷰 탭 + SD 녹화 파일 탐색 탭.

사용법 (관리자 cmd, 카메라 연결 상태):
    python xufs_player.py

[라이브 뷰 탭]  라이브 시작/정지(=녹화), 스냅샷, 로컬 녹화(AVI), 볼륨
[녹화 탐색 탭]  SD 파일 목록 / 실시간 재생(영상+볼륨) / 이전·다음 / 삭제 /
                다운로드(선택·전체 — 스트림 캡처 방식, 수 분 소요, 취소 가능)

모든 카메라 I/O는 백그라운드 스레드가 전담한다 (UI 프리징 방지).
메일박스가 단일 채널이므로 라이브와 파일 재생을 동시에 쓸 수 없다 —
탭 전환 시 현재 동작이 정리된다.
"""
import contextlib
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from datetime import datetime

import cv2
import numpy as np

import xufs_lib as X
import i18n
from i18n import T

import ctypes

AUDIO_RATE = 32000   # 카메라 재생 PCM (응답 cmd 0x21) — 32kHz 모노 s16le


# ---- 오디오 출력: Windows 내장 winmm(waveOut)만 사용 — 별도 패키지 설치 불필요 ----
_WAVE_MAPPER = 0xFFFFFFFF   # (UINT)-1 = 기본 출력 장치
_WHDR_DONE = 0x1


class _WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort),
                ("nSamplesPerSec", ctypes.c_uint32), ("nAvgBytesPerSec", ctypes.c_uint32),
                ("nBlockAlign", ctypes.c_ushort), ("wBitsPerSample", ctypes.c_ushort),
                ("cbSize", ctypes.c_ushort)]


class _WAVEHDR(ctypes.Structure):
    _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", ctypes.c_uint32),
                ("dwBytesRecorded", ctypes.c_uint32), ("dwUser", ctypes.c_size_t),
                ("dwFlags", ctypes.c_uint32), ("dwLoops", ctypes.c_uint32),
                ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_size_t)]


try:
    _winmm = ctypes.WinDLL("winmm")
    _winmm.waveOutOpen.argtypes = (ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint,
                                   ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t,
                                   ctypes.c_uint)
    _winmm.waveOutPrepareHeader.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)
    _winmm.waveOutWrite.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)
    _winmm.waveOutUnprepareHeader.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)
    _winmm.waveOutReset.argtypes = (ctypes.c_void_p,)
    _winmm.waveOutClose.argtypes = (ctypes.c_void_p,)
    for _f in ("waveOutOpen", "waveOutPrepareHeader", "waveOutWrite",
               "waveOutUnprepareHeader", "waveOutReset", "waveOutClose"):
        getattr(_winmm, _f).restype = ctypes.c_uint
except Exception:
    _winmm = None


class _WinAudioOut:
    """PCM 출력 — winmm waveOut, 기본 출력 장치(WAVE_MAPPER = Windows 믹서) 사용.
    사운드카드는 재생할 때마다 새로 연다 (외부 플레이어와 동일 방식)."""

    N_BUF = 4   # 선버퍼: 오디오 청크 144ms × 4 ≈ 0.6초

    def __init__(self, rate, channels=1):
        if _winmm is None:
            raise OSError(T("winmm unavailable"))
        fmt = _WAVEFORMATEX(0x0001, channels, rate, rate * 2 * channels,
                            2 * channels, 16, 0)
        self._h = ctypes.c_void_p()
        rc = _winmm.waveOutOpen(ctypes.byref(self._h), _WAVE_MAPPER,
                                ctypes.byref(fmt), 0, 0, 0)   # CALLBACK_NULL
        if rc != 0:
            raise OSError(T("waveOutOpen failed (code {rc})").format(rc=rc))
        self._slots = [[_WAVEHDR(), None, False] for _ in range(self.N_BUF)]

    def write(self, data):
        """PCM 바이트 출력. 빈 버퍼가 날 때까지 대기한다 (실시간 스트림용 블로킹)."""
        while True:
            for slot in self._slots:
                h, _, prepared = slot
                if prepared and not (h.dwFlags & _WHDR_DONE):
                    continue                    # 아직 재생 중인 버퍼
                if prepared:
                    _winmm.waveOutUnprepareHeader(self._h, ctypes.byref(h),
                                                  ctypes.sizeof(h))
                    h.dwFlags = 0
                buf = ctypes.create_string_buffer(data, len(data))   # 수명 보관 필수
                h.lpData = ctypes.cast(buf, ctypes.c_void_p)
                h.dwBufferLength = len(data)
                ok = (_winmm.waveOutPrepareHeader(self._h, ctypes.byref(h),
                                                  ctypes.sizeof(h)) == 0
                      and _winmm.waveOutWrite(self._h, ctypes.byref(h),
                                              ctypes.sizeof(h)) == 0)
                if not ok:
                    raise OSError(T("waveOut write failed"))
                slot[1] = buf
                slot[2] = True
                return
            time.sleep(0.01)   # 버퍼 4개 모두 재생 중 — 하나 끝날 때까지 대기

    def stop(self):
        if self._h:
            _winmm.waveOutReset(self._h)   # 큐의 버퍼 전부 WHDR_DONE 처리

    def close(self):
        if self._h:
            for h, _, prepared in self._slots:
                if prepared:
                    _winmm.waveOutUnprepareHeader(self._h, ctypes.byref(h),
                                                  ctypes.sizeof(h))
            _winmm.waveOutClose(self._h)
            self._h = None


def trim_jpeg(buf):
    """SOI 앞 잔여물 제거 + 첫 EOI(FFD9)에서 절단 (인코더 패딩 제거)."""
    s = buf.find(b"\xff\xd8\xff")
    if s < 0:
        return buf
    e = buf.find(b"\xff\xd9", s + 3)
    return buf[s:e + 2] if e >= 0 else buf[s:]


def ppm_from_bgr(img):
    """OpenCV BGR 프레임 → PPM 바이트 (tkinter PhotoImage 직접 입력)."""
    rgb = np.ascontiguousarray(img[:, :, ::-1])
    h, w = rgb.shape[:2]
    return b"P6\n%d %d\n255\n" % (w, h) + rgb.tobytes()


def stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def fmt_len(sec):
    """초 → h:mm:ss / m:ss 표시."""
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@contextlib.contextmanager
def muted_stderr():
    """C 레벨(libjpeg) stderr 잠깐 음소거 — 카메라 인코더가 스캔 데이터와 EOI 사이에
    ~2.3KB 패딩을 넣는 특성 때문에 매 프레임 "Corrupt JPEG data: ... before marker
    0xd9" 경고가 콘솔을 채운다 (PROTOCOL §5 — 화질 문제 없음, 앱이 첫 EOI 절단 처리).
    오류를 숨기는 게 아니라 알려진 무해 경고만 침묵시키는 용도."""
    sys.stderr.flush()
    saved = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 2)
        yield
    finally:
        sys.stderr.flush()
        os.dup2(saved, 2)
        os.close(devnull)
        os.close(saved)


class Session(threading.Thread):
    """카메라 I/O 전담 스레드. UI는 큐로만 통신한다.

    mode: 'browse'(녹화 탐색) / 'live'(라이브 뷰)
    state: connecting / idle / playing(탐색 탭 재생) / live / downloading / dead
    """

    def __init__(self):
        super().__init__(daemon=True)
        self.cmdq = queue.Queue()
        self.display_q = queue.Queue(maxsize=2)
        self.msg_q = queue.Queue()
        self.volume = 0.8
        self.stop_flag = False
        self.busy = False
        self.cancel_dl = False
        self.mode = "browse"
        self.state = "connecting"
        self.files = []
        self.playing_idx = None
        self.audio_out = None
        self.audio_q = queue.Queue(maxsize=8)   # ≈1.1초 버퍼 — 밀리면 실시간이므로 버림
        self.audio_thread = None
        self.play_len = None     # 재생 중 파일 길이(초) — cmd 57 응답
        self.len_gave_up = False
        self.len_gave_up = False
        self.got_frame = False   # 현재 재생에서 프레임 수신 여부 (시작 실패 판정)
        self.t_start = 0.0
        self.live_avi = None
        self.lrec_on = False
        self.resume_record = False   # 종료 시 녹화 재개 (on_close가 quit 데이터로 지정)

    def cmd(self, kind, data=None):
        self.cmdq.put((kind, data))

    def post(self, kind, data=None):
        self.msg_q.put((kind, data))

    # ---- 스레드 본체 ----
    def run(self):
        try:
            self.cam = X.XufsCamera(use_scsi=True)
        except OSError:
            try:
                self.cam = X.XufsCamera()
            except Exception as e:
                self.post("fatal", T("Camera (elen1 drive) not found. Check the USB "
                          "connection, power and administrator rights. Details: {e}").format(e=e))
                self.state = "dead"
                return
        # 출력 스트림은 첫 오디오 청크가 필요할 때 연다 (_open_audio) — 장치가 바뀌어도
        # 재생 시점마다 기본 출력 장치를 새로 연다 (외부 플레이어와 동일 방식)
        self.audio_thread = threading.Thread(target=self._audio_writer, daemon=True)
        self.audio_thread.start()
        try:
            self.cam.send_command(2)        # 자동 루프 녹화 정지
            self.cam.stop_playback()        # 잔여 재생 정리
            self.cam.sync_datetime()
        except OSError as e:
            self.post("fatal", T("Init failed: {e}").format(e=e))
            self.state = "dead"
            return
        self.state = "idle"
        self.post("ready", None)
        self._enter_live()                  # 시작 버튼 없이 즉시 라이브 자동 시작

        err_n = 0
        while not self.stop_flag:
            try:
                self._drain_commands()
                if self.state == "dead":
                    break
                if self.state == "live":
                    self._poll_live()
                elif self.state == "playing":
                    self._poll_playing()
                else:
                    time.sleep(0.03)
                err_n = 0
            except OSError as e:
                err_n += 1
                self.post("status", T("⚠ Read error ({n}/5): {e}").format(n=err_n, e=e))
                if err_n >= 5:
                    self.post("fatal", T("Disconnected — use the [🔁 Reconnect] button"))
                    self.state = "dead"
                time.sleep(0.4)
            except Exception as e:
                self.post("fatal", T("Internal error: {e}").format(e=e))
                self.state = "dead"
        # 종료 정리 — 카메라는 녹화 중일 때만 스트리밍에 응답하므로, 종료 후에도
        # 대시캠 본래 동작(상시 녹화)을 유지하도록 녹화를 다시 켠다.
        try:
            self.cam.stop_playback()
            if self.resume_record and not (self.mode == "live" or self.state == "live"):
                self.cam.send_command(1)      # startrecord — 라이브 중이면 이미 녹화 중
        except Exception:
            pass
        if self.live_avi is not None:
            try:
                self._close_lrec()
            except Exception:
                self.live_avi = None
        try:
            if self.audio_out:
                self.audio_out.stop()
                self.audio_out.close()
        except Exception:
            pass
        try:
            self.cam.close()
        except Exception:
            pass

    def _drain_commands(self):
        while True:
            try:
                kind, data = self.cmdq.get_nowait()
            except queue.Empty:
                return
            if kind == "quit":
                self.stop_flag = True
                self.resume_record = bool(data)   # 종료 후 카메라를 녹화 모드로 되돌림
                return
            elif kind == "volume":
                self.volume = data
            elif kind == "list":
                self.files = self.cam.list_files()
                self.post("files", self.files)
            elif kind == "play":
                if self.busy:
                    self.post("status", T("⚠ Cannot play during download"))
                    continue
                idx, kind_name = data
                if self.mode == "live":
                    self._exit_live()
                self.cam.stop_playback()
                self._drain_playback_residual()   # 이전 재생 잔여 프레임 비움 + 정리 시간 확보
                self._flush_display()
                # cmd 57 길이 조회 — 요청 형식: 파일ID|플래그. 응답이 없으면 이후 재생에서
                # 조회하지 않음(펌웨어 미지원 판정).
                self.len_arg = X.file_arg(idx, kind_name) | 0x4000
                self.play_len = None
                if not self.len_gave_up:
                    self.cam.send_command(57, self.len_arg)
                    print(f"[길이조회] 최종 시도: arg={self.len_arg:#06x} (1.5초 대기)")
                    q_deadline = time.time() + 1.5
                    while time.time() < q_deadline:
                        rr = self.cam.peek_resp()
                        if rr and rr[0] == "resp" and rr[1] == 57:
                            if rr[2]:
                                self.play_len = rr[2]
                                print(f"[길이조회] 응답 수신: {self.play_len}초 → 슬라이더 표시")
                            else:
                                print("[길이조회] 응답 arg=0 — 거부")
                            break
                        if rr is not None:
                            self.cam.poll()   # 프레임 등 다른 데이터는 정상 소진
                    if self.play_len is None:
                        self.len_gave_up = True
                        print("[길이조회] 포기 — 이 펌웨어는 길이 조회에 응답하지 않음 "
                              "(슬라이더 미제공, 재생은 정상)")
                self.cam.play_file(idx, kind_name)
                self.playing_idx = idx
                self.playing_kind = kind_name
                self.state = "playing"
                self.got = False
                self.got_frame = False   # 이번 재생에서 '프레임'을 받았는가 (시작 실패 판정 기준)
                self.t_start = time.time()
                self.t_last = time.time()
                self.post("playing", (idx, kind_name))
                if self.play_len:
                    self.post("playlen", (idx, self.play_len))
            elif kind == "pause":
                self.cam.pause_playback()
                self.post("status", T("Pause/Resume toggled"))
            elif kind == "stop":
                self.cam.stop_playback()
                self.state = "idle"
                self.playing_idx = None
                self.playing_kind = None
                self.post("stopped", None)
            elif kind == "delete":
                if self.mode == "live":
                    self.post("status", T("⚠ Cannot delete during live — stop live first"))
                    continue
                idx, kind_name = data
                self.cam.send_command(20, X.file_arg(idx, kind_name))   # cmd 20 = 파일 삭제
                time.sleep(0.5)
                self.files = self.cam.list_files()
                still = any(i == idx and k == kind_name for i, _, k in self.files)
                self.post("files", self.files)
                self.post("deleted", ((idx, kind_name), not still))
            elif kind == "protect":
                if self.mode == "live":
                    self.post("status", T("⚠ Cannot change while live/recording — stop live first"))
                    continue
                idx, kind_name = data
                want_lock = (kind_name != "LOCK")
                # 잠금=cmd 21, 해제=cmd 22, arg는 플레인 id.
                # 성공 여부는 목록 재조회의 날짜시간 비트0(LOCK 플래그)으로 판정한다.
                self.cam.send_command(21 if want_lock else 22, idx)
                time.sleep(0.6)
                self.files = self.cam.list_files()
                now_kind = next((k for i, _, k in self.files if i == idx), None)
                self.post("files", self.files)
                self.post("protected", (idx, kind_name, now_kind, None))
            elif kind == "download":
                self._download(data)      # data = [(idx, base, kind)] — download_all과 동일
            elif kind == "download_all":
                self._download(data)
            elif kind == "live_start":
                self._enter_live()
            elif kind == "live_stop":
                self._exit_live()
            elif kind == "lrec":
                self._toggle_lrec(data)
            elif kind == "mode":
                if data == "live":
                    self._enter_live()
                else:
                    self._exit_live()

    def _enter_live(self):
        if self.busy:
            self.post("status", T("⚠ Cannot start live during download"))
            return
        if self.mode == "live" and self.state == "live":
            return
        if self.state == "playing":
            self.cam.stop_playback()
        self.cam.stop_playback()
        # 카메라는 녹화 중(cmd 1)에만 프레임을 스트리밍한다(실측) — 항상 녹화 모드.
        # SD가 꽂혀있으면 fps가 저하되는 것은 카메라 펌웨어의 SD 쓰기 경합 때문.
        self.cam.start_stream(record=True)
        self.mode = "live"
        self.state = "live"
        self.post("live_started", None)

    def _exit_live(self):
        if self.mode == "live":
            self.cam.send_command(2)
            self.mode = "browse"
            self.state = "idle"
            self._flush_display()   # 라이브 마지막 프레임이 새 모드 캔버스에 남지 않게
            self._close_lrec()
            self.post("live_stopped", None)

    def _close_lrec(self):
        """로컬 녹화를 마무리하고 저장 경로를 알린다.
        (MjpegAviWriter.close() 의 반환값은 (프레임 수, 바이트) — 경로가 아니다.)"""
        if self.live_avi is None:
            return
        path = self.live_avi.path
        self.live_avi.close()
        self.live_avi = None
        self.lrec_on = False
        self.post("lrec_saved", path)

    def _flush_display(self):
        """세션 스레드에서 표시 큐를 비운다 (모드 전환 경합 제거)."""
        try:
            while True:
                self.display_q.get_nowait()
        except queue.Empty:
            pass

    def _toggle_lrec(self, on):
        if on:
            if self.mode != "live" or self.state != "live":
                self.post("status", T("⚠ Local recording requires live view"))
                return
            if self.live_avi is not None:
                return
            path = f"REC_{stamp()}.avi"
            self.live_avi = X.MjpegAviWriter(path, fps=25.0)
            self.lrec_on = True
            self.post("lrec_started", path)
        else:
            self._close_lrec()

    def _poll_live(self):
        self.cam.request_frame()
        r = self.cam.poll()
        if r is None:
            return
        if r[0] == "frame":
            data = trim_jpeg(r[1])
            if data[:3] != b"\xff\xd8\xff":
                return
            if self.live_avi is not None:
                self.live_avi.add(data)
            try:
                self.display_q.put_nowait(data)
            except queue.Full:
                try:
                    self.display_q.get_nowait()
                    self.display_q.put_nowait(data)
                except Exception:
                    pass
        elif r[0] == "resp" and r[1] == 0x21 and r[3]:
            self._write_audio(r[3])

    def _drain_playback_residual(self):
        """재생 정지 직후 메일박스에 남은 이전 파일의 프레임을 비운다.
        데이터가 0.25초간 조용해지면 즉시 통과 (최대 0.8초).
        — 남은 잔여 프레임이 다음 재생의 '데이터 온다' 오판을 만들어
        재생 시작 실패를 감추던 것을 방지하고, 펌웨어가 스트림 정리할 시간을 준다."""
        quiet_at = time.time() + 0.25
        give_up = time.time() + 0.8
        while time.time() < give_up:
            if self.cam.poll() is None:
                if time.time() >= quiet_at:
                    break
                time.sleep(0.02)
            else:
                quiet_at = time.time() + 0.25   # 잔여 데이터가 더 오면 조용 카운트 리셋

    def _poll_playing(self):
        r = self.cam.poll()
        now = time.time()
        if r is None:
            if self.got_frame and now - self.t_last > 3.0:
                self.cam.stop_playback()
                self.state = "idle"
                self.post("eof", (self.playing_idx, self.playing_kind))
                self.playing_idx = None
                self.playing_kind = None
            elif not self.got_frame and now - self.t_start > 6.0:
                self._abort_play_start()
            return
        self.got = True
        self.t_last = now   # 데이터가 왔을 때만 갱신 — 파일 끝(조용) 판정의 기준 시점
        if r[0] == "frame":
            self.got_frame = True
            data = trim_jpeg(r[1])
            if data[:3] != b"\xff\xd8\xff":
                return
            try:
                self.display_q.put_nowait(data)
            except queue.Full:
                try:
                    self.display_q.get_nowait()
                    self.display_q.put_nowait(data)
                except Exception:
                    pass
        elif r[0] == "resp" and r[1] == 57 and r[2]:
            self.play_len = r[2]           # MediaLength(초) — UI 슬라이더에 전달
            print(f"[길이조회] 응답 수신: {self.play_len}초 → 슬라이더 표시")
            self.post("playlen", (self.playing_idx, self.play_len))
        elif r[0] == "resp" and r[1] == 0x21 and r[3]:
            self._write_audio(r[3])
        elif r[0] == "resp":
            print(f"[poll] 미분류 응답: cmd={r[1]} arg={r[2]} len={len(r[3])}")
        if not self.got_frame and now - self.t_start > 6.0:
            # 오디오/응답 패킷만 오고 프레임이 없는 상태 — 시작 실패로 본다
            self._abort_play_start()

    def _abort_play_start(self):
        """재생 시작 실패 복구: cmd 6 후 6초 내 '프레임'이 한 장도 안 온 경우.
        오디오 청크만 와도 여기 걸린다 (무한 '재생 시작 중...' 방지)."""
        self.cam.stop_playback()
        self.state = "idle"
        idx, kind = self.playing_idx, self.playing_kind
        self.playing_idx = None
        self.playing_kind = None
        self.post("playfail", (idx, kind))

    def _open_audio(self):
        """사운드카드 출력 열기 (Windows 내장 winmm). 실패 시 None — 다음 청크 때 재시도."""
        try:
            return _WinAudioOut(AUDIO_RATE, 1)
        except Exception:
            return None

    def _write_audio(self, data):
        """세션 스레드: 오디오 청크를 큐에 적재만 한다 (블로킹 없음 — 비디오 스터터링 방지)."""
        if self.volume <= 0:
            return
        if self.audio_out is None:
            self.audio_out = self._open_audio()   # 출력 장치가 준비되면 그때 연다
            if self.audio_out is None:
                return
        try:
            self.audio_q.put_nowait(data)
        except queue.Full:
            pass  # 실시간 스트림 — 밀리면 버림

    def _audio_writer(self):
        """오디오 출력 전용 스레드: 큐 → 사운드카드 (볼륨 적용).
        출력 오류가 나면 스트림을 닫아 다음 청크 때 재연결한다 (죽은 채로 무음에 머물지 않음)."""
        while not self.stop_flag:
            try:
                chunk = self.audio_q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                a = np.frombuffer(chunk, dtype="<i2").astype(np.float32) * self.volume
                np.clip(a, -32768, 32767, out=a)
                self.audio_out.write(a.astype("<i2").tobytes())
            except Exception:
                try:
                    self.audio_out.stop()
                    self.audio_out.close()
                except Exception:
                    pass
                self.audio_out = None   # 다음 청크에서 재시도

    def _download(self, items):
        """items: [(idx, avi_base, kind)] — avi_base는 확장자 없는 저장 경로.
        끝나면 dlfinished에 요약 (saved, skipped, cancelled)를 보낸다
        (예외·종료 중이면 None — 완료 알림 대신 치명 오류 팝업이 뜬다)."""
        if self.state == "playing":
            self.cam.stop_playback()
            self.state = "idle"
        if self.mode == "live":
            self._exit_live()
        self._flush_display()
        self.busy = True
        self.state = "downloading"
        saved, skipped, ok = [], [], False
        try:
            for idx, base, kind_name in items:
                if self.stop_flag or self.cancel_dl:
                    break
                self.post("dlstart", (idx, base + ".avi"))

                def progress(frames, vbytes, audio_n, elapsed, _i=idx):
                    self.post("dlprog", (_i, frames, vbytes, audio_n, elapsed))
                    return False if self.cancel_dl else None

                frames, audio = self.cam.download_file(idx, kind_name,
                                                       on_progress=progress)
                if self.cancel_dl:
                    self.post("status", T("⬇ Cancelled — saving what was received so far"))
                if not frames:
                    self.post("status", T("{idx:04d}: no frames — skipped").format(idx=idx))
                    skipped.append(idx)
                    continue
                audio_bytes = sum(len(c) for c in audio)
                has_audio = audio_bytes >= 64000   # 1초 미만 오디오는 무시
                if has_audio:
                    fps = max(1.0, min(60.0, len(frames) / (audio_bytes / 64000.0)))
                else:
                    fps = 25.0
                avi = base + ".avi"
                if has_audio:   # 오디오를 AVI 하나에 뮥싱 (별도 WAV 없음)
                    n, v_total, n_a, a_total = X.write_avi_with_audio(
                        frames, audio, avi, fps=fps, audio_rate=AUDIO_RATE)
                    self.post("dldone", (avi, n, v_total + a_total,
                                         self.cancel_dl, True))
                else:
                    n, total = X.write_mjpeg_avi(iter(frames), avi, fps=fps)
                    self.post("dldone", (avi, n, total, self.cancel_dl, False))
                saved.append((avi, n, self.cancel_dl, has_audio))
            ok = True
        finally:
            cancelled = self.cancel_dl
            self.busy = False
            self.cancel_dl = False
            self.state = "idle"
            self.post("dlfinished",
                      (saved, skipped, cancelled) if ok and not self.stop_flag else None)


class PlayerUI:
    def __init__(self, root):
        self.root = root
        root.title(T("uCarDvr — Live View / Recording Browser"))
        root.geometry("960x720")
        self.sess = None
        self.dead = False
        self.active_tab = 0
        self.rows = []            # 목록 행 데이터 [(idx, dt, kind)]
        self.last_jpeg = None     # 표시 중인 마지막 JPEG (스냅샷용)
        self.dlg = None           # 다운로드 진행 다이얼로그
        self.dl_var = tk.StringVar(value="")
        self._fail_count = 0      # 재생 시작 연속 실패 횟수 (프레임이 그려지면 0으로 복구)
        self._photos = {}       # 캔버스별 PhotoImage 참조 (GC 방지)
        self._canvas_state = {} # 캔버스별 마지막 렌더 상태


        top = tk.Frame(root)
        top.pack(fill="x", padx=6, pady=4)
        self.status_var = tk.StringVar(value=T("Connecting..."))
        tk.Label(top, textvariable=self.status_var, anchor="w", bd=1,
                 relief="sunken", font=("Malgun Gothic", 10, "bold"),
                 padx=8).pack(side="left", fill="x", expand=True)
        self.reconn_btn = tk.Button(top, text=T("🔁 Reconnect"), state="disabled",
                                    command=self.reconnect)
        self.reconn_btn.pack(side="right", padx=4)

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=6, pady=4)
        self.tab_live = tk.Frame(self.notebook)
        self.tab_files = tk.Frame(self.notebook)
        self.notebook.add(self.tab_live, text=T("Live View"))
        self.notebook.add(self.tab_files, text=T("Recording Browser"))
        self._build_live_tab()
        self._build_files_tab()
        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_change)

        self.start_session()
        root.after(25, self.pump)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---- 탭 구성 ----
    def _build_live_tab(self):
        f = self.tab_live
        self.live_canvas = tk.Canvas(f, bg="black", width=640, height=360,
                                     highlightthickness=0)
        self.live_canvas.pack(fill="both", expand=True, padx=4, pady=4)
        self.live_canvas.bind("<Configure>",
                              lambda e: self._canvas_rerender(self.live_canvas))
        row = tk.Frame(f)
        row.pack(fill="x", padx=4, pady=4)
        self.btn_live = tk.Button(row, text=T("📹 Start Live"), width=12,
                                  command=self.live_toggle)
        self.btn_snap = tk.Button(row, text=T("📷 Snapshot"), width=10,
                                  command=self.snapshot)
        self.btn_lrec = tk.Button(row, text=T("🔴 Local Rec: Off"), width=16,
                                  command=self.toggle_lrec)
        for b in (self.btn_live, self.btn_snap, self.btn_lrec):
            b.pack(side="left", padx=2)
        tk.Label(row, text=T("Live view has no sound (check recorded files)"),
                 fg="#666").pack(side="right")
        self._canvas_placeholder(self.live_canvas, T("Live standby"))

    def _build_files_tab(self):
        f = self.tab_files
        main = tk.Frame(f)
        main.pack(fill="both", expand=True)

        # 좌측: 파일 목록 (헤더 클릭으로 정렬 전환)
        left = tk.LabelFrame(main, text=T("Files"))
        left.pack(side="left", fill="y", padx=(0, 6))
        btnrow = tk.Frame(left)
        btnrow.pack(fill="x", padx=4, pady=(4, 2))
        self.refresh_btn = tk.Button(btnrow, text=T("🔄 Refresh"), command=self.refresh_list)
        self.refresh_btn.pack(side="left", fill="x", expand=True)
        self.del_btn = tk.Button(btnrow, text=T("🗑 Delete"), command=self.delete_selected)
        self.del_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.protect_btn = tk.Button(btnrow, text=T("🔒 Protect"), command=self.toggle_protect)
        self.protect_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))
        inner = tk.Frame(left)
        inner.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.sort_key, self.sort_desc = "dt", True   # 기본: 촬영일시 최신 먼저
        self.tree = ttk.Treeview(inner, columns=("num", "dt"), show="headings",
                                 selectmode="browse", height=12)
        self.tree.heading("num", text=T("No."), command=lambda: self.sort_by("num"))
        self.tree.heading("dt", text=T("Date/Time"), command=lambda: self.sort_by("dt"))
        self.tree.column("num", width=86, anchor="w", stretch=False)
        self.tree.column("dt", width=140, anchor="w")
        sb = ttk.Scrollbar(inner, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-Button-1>", lambda e: self.play_selected())
        dlrow = tk.Frame(left)
        dlrow.pack(fill="x", padx=4, pady=(0, 4))
        self.btn_dl = tk.Button(dlrow, text=T("📥 Save Selected"), command=self.download_one)
        self.btn_dl.pack(side="left", fill="x", expand=True)
        self.btn_dlall = tk.Button(dlrow, text=T("📦 Save All"), command=self.download_all)
        self.btn_dlall.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # 우측: 재생 화면
        right = tk.LabelFrame(main, text=T("Playback (1280×720)"))
        right.pack(side="left", fill="both", expand=True)
        self.play_canvas = tk.Canvas(right, bg="black", width=640, height=360,
                                     highlightthickness=0)
        self.play_canvas.pack(fill="both", expand=True, padx=4, pady=4)
        self.play_canvas.bind("<Configure>",
                              lambda e: self._canvas_rerender(self.play_canvas))

        row = tk.Frame(f)
        row.pack(fill="x", padx=4, pady=4)

        # 좌측: 자동재생 체크박스 + 반복 토글 (누를 때마다 없음→한 파일→전체 순환)
        left = tk.Frame(row)
        left.pack(side="left")
        self.auto_next_var = tk.BooleanVar(value=False)
        tk.Checkbutton(left, text=T("Auto-play next"),
                       variable=self.auto_next_var).pack(side="left", padx=(2, 8))
        self.repeat_mode = "none"   # none / one / all
        self.btn_repeat = tk.Button(left, text=T("🔁 Repeat: Off"), width=13,
                                    command=self.cycle_repeat)
        self.btn_repeat.pack(side="left")

        # 중앙: 재생 컨트롤 (좌우 그룹 사이에서 진짜 중앙 정렬)
        mid = tk.Frame(row)
        mid.place(relx=0.5, rely=0.5, anchor="center")
        self.btn_play = tk.Button(mid, text=T("🎬 Play"), width=8, command=self.play_selected)
        self.btn_pause = tk.Button(mid, text=T("🅿️ Pause"), width=12, command=self.pause)
        self.btn_stop = tk.Button(mid, text=T("🛑 Stop"), width=8, command=self.stop)
        self.btn_prev = tk.Button(mid, text=T("🔙 Prev"), width=8, command=lambda: self.step(-1))
        self.btn_next = tk.Button(mid, text=T("Next 🔜"), width=8, command=lambda: self.step(1))
        for b in (self.btn_play, self.btn_pause, self.btn_stop, self.btn_prev, self.btn_next):
            b.pack(side="left", padx=2)

        # 우측: 볼륨
        self.vol_play = tk.Scale(row, from_=0, to=100, orient="horizontal",
                                 length=160, command=self.set_volume)
        self.vol_play.set(80)
        self.vol_play.pack(side="right")
        tk.Label(row, text=T("Volume")).pack(side="right")

        # 재생 위치 슬라이더 — cmd 57(file_arg)로 길이를 확인한 파일에만 표시 (모르면 숨김)
        self.seekrow = tk.Frame(f)
        self.seekrow.pack(fill="x", padx=4, pady=(0, 4))
        self.play_len = None       # 재생 중 파일 길이(초) — 미확인 시 None
        self._seeking = False
        self._pos_base = 0.0       # 마지막 기준점(재생 시작/시크)의 파일 내 위치(초)
        self._pos_t0 = 0.0
        self.seek_scale = ttk.Scale(self.seekrow, from_=0, to=100, orient="horizontal",
                                    command=self._on_seek_move)
        self.seek_scale.bind("<ButtonPress-1>", lambda e: setattr(self, "_seeking", True))
        self.seek_scale.bind("<ButtonRelease-1>", self._on_seek_release)
        self.seek_lbl = tk.Label(self.seekrow, text="", width=16, anchor="e")

    # ---- 세션 관리 ----
    def start_session(self):
        self.dead = False
        self.reconn_btn.config(state="disabled")
        self.sess = Session()
        self.sess.start()

    def reconnect(self):
        self.start_session()

    def on_close(self):
        if messagebox.askyesno(
                T("Exit"),
                T("The program will exit and the camera returns to recording mode.\n"
                  "(The camera keeps recording to the SD card.)\n\nExit?")):
            if self.sess:
                self.sess.cmd("quit", True)
            self.root.after(150, self.root.destroy)

    # ---- 캔버스 라우팅 ----
    def _target_canvas(self):
        return self.live_canvas if self.active_tab == 0 else self.play_canvas

    # ---- UI 액션 ----
    def refresh_list(self):
        if self.sess:
            self.sess.cmd("list")

    def _sync_list_selection(self, idx, kind_name):
        """좌측 목록의 선택을 재생 중인 파일에 맞춘다 (자동재생·반복·이전/다음 따라가기).
        목록이 재생 내용보다 오래된 경우(삭제 직후 등)는 그대로 둔다."""
        for iid in self.tree.get_children():
            vals = self.tree.item(iid, "values")
            if int(vals[2]) == idx and vals[3] == kind_name:
                self.tree.selection_set(iid)
                self.tree.see(iid)   # 화면 밖이면 스크롤해서 보이게
                return

    def sort_by(self, key):
        """헤더 클릭 정렬 — 같은 헤더를 누르면 방향 전환, 다른 헤더면 내림차순부터."""
        if self.sort_key == key:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_key, self.sort_desc = key, True
        self._fill_tree()
        col = T("Date/Time") if key == "dt" else T("No.")
        order = T("descending") if self.sort_desc else T("ascending")
        self.set_status(T("List sorted: {col} {order}").format(col=col, order=order))

    def _fill_tree(self):
        """현재 정렬 기준으로 목록을 다시 그린다. self.rows 자체를 표시 순서로 맞춘다
        (자동재생·이전/다음이 화면 순서를 따르도록)."""
        self.tree.delete(*self.tree.get_children())
        keyf = (lambda r: r[0]) if self.sort_key == "num" else (lambda r: r[1])
        self.rows.sort(key=keyf, reverse=self.sort_desc)
        icon = {"LOCK": "🔒", "PICT": "📷"}
        arrow = " ▼" if self.sort_desc else " ▲"
        self.tree.heading("num", text=(T("No.") + arrow) if self.sort_key == "num" else T("No."))
        self.tree.heading("dt", text=(T("Date/Time") + arrow) if self.sort_key == "dt" else T("Date/Time"))
        for idx, dt, kind_name in self.rows:
            prefix = icon.get(kind_name, "") + " " if kind_name in icon else ""
            self.tree.insert("", "end", values=(f"{prefix}{idx:04d}",
                                                f"{dt:%y-%m-%d %H:%M:%S}", idx, kind_name))

    def _selected_row(self):
        """목록에서 선택된 행 → (idx, kind) 또는 None."""
        sel = self.tree.selection()
        if sel:
            vals = self.tree.item(sel[0], "values")
            return int(vals[2]), vals[3]
        return None

    def play_selected(self):
        if self.sess is None or self.sess.busy:
            return
        row = self._selected_row()
        if row is None and self.sess.files:
            idx, _, kind = self.sess.files[0]
            row = (idx, kind)
        if row is None:
            self.set_status(T("First refresh the list (an SD card is required)"))
            return
        self.sess.cmd("play", row)

    def pause(self):
        if self.sess:
            self.sess.cmd("pause")

    def stop(self):
        if self.sess:
            self.sess.cmd("stop")

    def step(self, delta):
        if self.sess is None or self.sess.busy:
            return
        rows = self.rows or self.sess.files
        if not rows:
            return
        cur = self.sess.playing_idx
        cur_kind = self.sess.playing_kind
        if cur is None:
            row = self._selected_row()
            if row:
                cur, cur_kind = row
        if cur is None:
            idx0, _, kind0 = rows[0]
            cur, cur_kind = idx0, kind0
        pos = next((i for i, (x, _, k) in enumerate(rows)
                    if x == cur and k == cur_kind), 0)
        nxt = pos + delta
        if self.repeat_mode == "all":
            nxt %= len(rows)   # 반복 '전체' — 목록 끝/처음을 감아서 이동
        else:
            nxt = min(len(rows) - 1, max(0, nxt))
        idx, _, kind = rows[nxt]
        self.sess.cmd("play", (idx, kind))

    REPEAT_STEPS = (("none", "Off"), ("one", "One file"), ("all", "All"))

    def cycle_repeat(self):
        """반복 토글: 없음 → 한 파일 → 전체 → 없음 … (누를 때마다 순환)"""
        order = [m for m, _ in self.REPEAT_STEPS]
        self.repeat_mode = order[(order.index(self.repeat_mode) + 1) % len(order)]
        label = T(dict(self.REPEAT_STEPS)[self.repeat_mode])
        self.btn_repeat.config(text=T("🔁 Repeat: {m}").format(m=label))
        self.set_status(T("Repeat: {m}").format(m=label))

    def _play_neighbor(self, idx, kind_name, delta):
        """끝난 파일 기준 다음(delta=1) 행을 재생. 반복 '전체'면 목록 끝에서 감는다.
        재생을 시작했으면 True."""
        if self.sess is None or self.sess.busy:
            return False
        pos = next((i for i, (x, _, k) in enumerate(self.rows)
                    if x == idx and k == kind_name), None)
        if pos is None:
            return False
        nxt = pos + delta
        if nxt >= len(self.rows) or nxt < 0:
            if self.repeat_mode != "all":
                return False
            nxt %= len(self.rows)
        x, _, k = self.rows[nxt]
        self.sess.cmd("play", (x, k))
        return True

    # ---- 재생 위치 슬라이더 ----
    def _reset_seek(self):
        """길이 미확인 상태로 되돌림 — 슬라이더 숨김 (길이를 모르면 표시하지 않는 규칙)."""
        self.play_len = None
        self._pos_base = 0.0
        self.seek_scale.pack_forget()
        self.seek_lbl.pack_forget()

    def _on_seek_move(self, value):
        if self.play_len:
            self.seek_lbl.config(text=f"{fmt_len(float(value))} / {fmt_len(self.play_len)}")

    def _on_seek_release(self, _e):
        """드래그 놓기 → cmd 48 시크."""
        self._seeking = False
        if self.sess and self.sess.state == "playing" and self.play_len:
            sec = int(float(self.seek_scale.get()))
            self._pos_base, self._pos_t0 = sec, time.time()
            self.sess.cmd("seek", sec)

    def delete_selected(self):
        if self.sess is None or self.sess.busy:
            return
        row = self._selected_row()
        if row is None:
            self.set_status(T("Select a file to delete"))
            return
        idx, kind = row
        if kind == "LOCK":
            self.set_status(T("🔒 {idx:04d} is protected — unlock it before deleting").format(idx=idx))
            return
        ok = messagebox.askyesno(
            T("Confirm Delete"),
            T("[{kind} {idx:04d}] Delete this file from the SD card?\n\n"
              "⚠ This cannot be undone.").format(kind=kind, idx=idx))
        if ok:
            self.sess.cmd("delete", (idx, kind))

    def set_volume(self, v):
        if self.sess:
            self.sess.cmd("volume", int(v) / 100.0)

    def live_toggle(self):
        if self.sess is None or self.sess.busy:
            return
        if self.sess.mode == "live":
            self.sess.cmd("live_stop")
        else:
            self.sess.cmd("live_start")

    def snapshot(self):
        if self.last_jpeg is None:
            self.set_status(T("Snapshot: no live frame — start live first"))
            return
        p = f"SNAP_{stamp()}.jpg"
        with open(p, "wb") as f:
            f.write(self.last_jpeg)
        self.set_status(T("📸 Snapshot saved: {p}").format(p=p))

    def toggle_lrec(self):
        if self.sess is None or self.sess.busy:
            return
        self.sess.cmd("lrec", not self.sess.lrec_on)

    def download_one(self):
        if self.sess is None or self.sess.busy:
            return
        row = self._selected_row()          # → (idx, kind)
        if row is None and self.sess.files:
            idx, _, kind = self.sess.files[0]
            row = (idx, kind)
        if row is None:
            self.set_status(T("The list is empty"))
            return
        idx, kind = row
        if not messagebox.askyesno(T("Confirm Download"), T(self.DL_WARN) + T("\nProceed?")):
            return
        path = filedialog.asksaveasfilename(
            title=T("Save File As"),
            initialfile=f"{kind}{idx:04d}_{stamp()}.avi",
            defaultextension=".avi", filetypes=[(T("AVI video"), "*.avi")])
        if not path:
            return
        self.sess.cancel_dl = False
        self.sess.cmd("download", [(idx, os.path.splitext(path)[0], kind)])

    def download_all(self):
        if self.sess is None or self.sess.busy or not self.sess.files:
            return
        msg = (T(self.DL_WARN) + "\n" + T("[Save All] All {n} files in the list will be "
               "captured in order.\nProceed?").format(n=len(self.sess.files)))
        if not messagebox.askyesno(T("Confirm Save All"), msg):
            return
        folder = filedialog.askdirectory(title=T("Select Folder"),
                                         initialdir="capture" if os.path.isdir("capture") else ".")
        if not folder:
            return
        self.sess.cancel_dl = False
        self.sess.cmd("download_all",
                      [(idx, os.path.join(folder, f"{kind}{idx:04d}_{dt:%Y%m%d_%H%M%S}"), kind)
                       for idx, dt, kind in self.sess.files])

    def cancel_download(self):
        if self.sess:
            self.sess.cancel_dl = True
            self.set_status(T("⬇ Cancel requested — finishing the current block, then saving partial"))

    def notify_download_done(self, saved, skipped, cancelled):
        """다운로드 끝 알림 한 번. 저장 파일은 자동으로 열지 않는다 — 사용자가 직접 재생."""
        if not saved:
            if cancelled:
                self.set_status(T("■ Download cancelled — no files saved"))
                messagebox.showinfo(T("Download Cancelled"), T("Cancelled — no files were saved."))
            else:
                self.set_status(T("⚠ Download failed — no files saved"))
                messagebox.showinfo(T("Download Failed"),
                                    T("No files saved ({n} skipped without frames).").format(n=len(skipped)))
            return
        n = len(saved)
        head = (T("Cancelled — saved partial data ({n} file(s)).").format(n=n) if cancelled
                else T("Saved ({n} file(s)).").format(n=n))
        if skipped:
            head += T("\nSkipped {n} file(s) with no frames.").format(n=len(skipped))
        names = [os.path.basename(p) + (T(" (partial)") if part else "")
                 for p, _, part, _ in saved]
        if len(names) > 10:
            names = names[:10] + [T("… and {more} more").format(more=n - 10)]
        self.set_status(("■ " if cancelled else "✓ ") + head.splitlines()[0])
        messagebox.showinfo(
            T("Download Cancelled") if cancelled else T("Download Complete"),
            head + T("\n\nSaved to: {d}").format(d=os.path.dirname(saved[0][0]))
            + "".join("\n  " + nm for nm in names))

    def toggle_protect(self):
        if self.sess is None or self.sess.busy:
            return
        row = self._selected_row()
        if row is None:
            self.set_status(T("Select a file to protect/unlock"))
            return
        idx, kind = row
        if self.sess.mode == "live":
            self.set_status(T("⚠ Cannot change while live/recording — stop live first"))
            return
        action = T("Unlock") if kind == "LOCK" else T("Lock")
        ok = messagebox.askyesno(T("Confirm Protect Change"),
                                 T("[{kind} {idx:04d}] {action}?").format(kind=kind, idx=idx, action=action))
        if ok:
            self.sess.cmd("protect", (idx, kind))

    DL_WARN = "Downloads capture the live stream in real time, so it takes a while (1 min of recording ≈ 2-3 min)."
    def pump(self):
        s = self.sess
        if s:
            while True:
                try:
                    kind, data = s.msg_q.get_nowait()
                except queue.Empty:
                    break
                self.handle_msg(kind, data)
            try:
                data = s.display_q.get_nowait()
                self.last_jpeg = data
                self._canvas_draw(self._target_canvas(), data)
                if s.state == "playing":
                    self._fail_count = 0   # 프레임이 그려졌다 = 이 파일은 재생 성공
            except queue.Empty:
                pass
            if s.state == "playing" and self.play_len and not self._seeking:
                # 드래그 중이 아니면 경과 시간으로 슬라이더 위치 갱신
                self.seek_scale.set(min(self._pos_base + time.time() - self._pos_t0,
                                        self.play_len))
            busy = s.busy or s.state == "connecting"
            has_sel = bool(self.tree.selection())
            sel_row = self._selected_row() if has_sel else None
            sel_is_lock = bool(sel_row and sel_row[1] == "LOCK")
            for b, need_sel in (
                    (self.refresh_btn, False), (self.del_btn, True),
                    (self.protect_btn, True), (self.btn_dl, True),
                    (self.btn_play, True), (self.btn_pause, True),
                    (self.btn_stop, True), (self.btn_prev, True),
                    (self.btn_next, True),
                    (self.btn_dlall, False), (self.btn_live, False),
                    (self.btn_snap, False), (self.btn_lrec, False)):
                enabled = (not self.dead) and (not busy) and \
                    (has_sel or not need_sel)
                if b is self.del_btn:
                    enabled = enabled and not sel_is_lock  # 보호 파일 삭제 불가
                if b in (self.btn_snap, self.btn_lrec):
                    enabled = enabled and s.mode == "live" and \
                        s.state == "live"
                b.config(state="normal" if enabled else "disabled")
        self.root.after(25, self.pump)

    def handle_msg(self, kind, data):
        if kind == "ready":
            self.set_status(T("Connected — auto-starting live view..."))
        elif kind == "files":
            self.rows = list(data)
            self._fill_tree()
            self.set_status(T("List: {n} files — select one and press ▶ Play").format(n=len(data)))
        elif kind == "playing":
            idx, kind_name = data
            self._drain_display()
            self._canvas_placeholder(self.play_canvas, T("Starting playback..."))
            self.set_status(T("▶ Playing: {kind}{idx:04d}").format(kind=kind_name, idx=idx))
            self.notebook.select(self.tab_files)
            self._sync_list_selection(idx, kind_name)
            self._reset_seek()                 # 길이 확인(cmd 57) 전까지 슬라이더 숨김
            self._pos_t0 = time.time()
        elif kind == "playlen":
            idx, length = data
            if length:
                self.play_len = length
                self.seek_scale.config(to=length)
                self.seek_scale.set(0)
                self._pos_base, self._pos_t0 = 0.0, time.time()
                self.seek_scale.pack(side="left", fill="x", expand=True)
                self.seek_lbl.pack(side="right")
        elif kind == "playfail":
            idx, kind_name = data
            self._canvas_placeholder(self.play_canvas, T("Playback standby — select a file and press ▶"))
            # 자동재생/반복이 켜져 있으면 실패한 파일에서 다음 파일로 넘어간다
            # (연속 실패가 목록 한 바퀴를 넘으면 멈춤 — 무한 루프 방지)
            if (self.auto_next_var.get() or self.repeat_mode != "none") \
                    and self._fail_count < max(1, len(self.rows)) \
                    and self._play_neighbor(idx, kind_name, 1):
                self._fail_count += 1
                self.set_status(T("⚠ {kind}{idx:04d} playback failed — moving to next file").format(kind=kind_name, idx=idx))
            else:
                self._fail_count = 0
                self.set_status(T("⚠ Playback failed to start — try again"))
        elif kind == "stopped":
            self.set_status(T("Stopped"))
        elif kind == "eof":
            idx, kind_name = data
            ended = T("■ Playback finished (end of file): {kind}{idx:04d}").format(kind=kind_name, idx=idx)
            if self.repeat_mode == "one":
                self.set_status(T("🔁 Repeating: {kind}{idx:04d}").format(kind=kind_name, idx=idx))
                self.sess.cmd("play", (idx, kind_name))
            elif self.repeat_mode == "all" or self.auto_next_var.get():
                if not self._play_neighbor(idx, kind_name, 1):
                    self.set_status(ended + T(" — end of list"))
            else:
                self.set_status(ended)
        elif kind == "deleted":
            (idx, kind_name), ok = data
            if ok:
                self.set_status(T("✓ {kind}{idx:04d} deleted (verified by re-listing)").format(kind=kind_name, idx=idx))
            else:
                self.set_status(T("⚠ {kind}{idx:04d} still in the list — cmd 20 may not be "
                                  "a delete command").format(kind=kind_name, idx=idx))
        elif kind == "protected":
            idx, old_kind, now_kind, _ = data
            if now_kind is None:
                self.set_status(T("⚠ {idx:04d} disappeared from the list — check immediately").format(idx=idx))
            elif now_kind != old_kind:
                self.set_status(T("✓ {idx:04d} → {kind} switch complete").format(idx=idx, kind=now_kind))
            else:
                self.set_status(T("⚠ {idx:04d} protect switch failed").format(idx=idx))
        elif kind == "status":
            self.set_status(data)
        elif kind == "live_started":
            self._drain_display()
            self._canvas_placeholder(self.live_canvas, T("Connecting live..."))
            self.btn_live.config(text=T("🛑 Stop Live"))
            self.set_status(T("● Live/recording in progress (saving to SD)"))
        elif kind == "live_stopped":
            self._drain_display()
            self._canvas_placeholder(self.live_canvas, T("Live standby"))
            self.btn_live.config(text=T("▶ Start Live"))
            self.set_status(T("■ Live/recording stopped"))
        elif kind == "lrec_started":
            self.btn_lrec.config(text=T("💾 Local Rec: Stop(Save)"))
            self.set_status(T("⏺ Local recording started: {p}").format(p=data))
        elif kind == "lrec_saved":
            self.btn_lrec.config(text=T("⏺ Local Rec: Off"))
            self.set_status(T("✓ Local recording saved: {p}").format(p=data))
        elif kind == "dlstart":
            idx, path = data
            self.dl_var.set(f"⬇ #{idx:04d} → {os.path.basename(path)}")
            self.set_status(T("⬇ Downloading: #{idx:04d}").format(idx=idx))
            self.open_dl_dialog()
        elif kind == "dlprog":
            idx, frames, vbytes, audio_n, elapsed = data
            rate = vbytes / max(elapsed, 0.1) / 1048576
            mb = vbytes / 1048576
            self.dl_var.set(T("⬇ #{idx:04d}: {frames} frames {mb:.1f}MB audio {n} chunks "
                              "{rate:.1f}MB/s {elapsed:.0f}s").format(idx=idx, frames=frames,
                              mb=mb, n=audio_n, rate=rate, elapsed=elapsed))
        elif kind == "dldone":
            path, n, total, cancelled, has_audio = data
            tag = T(" (cancelled — partial)") if cancelled else ""
            audio_txt = T(" · audio included") if has_audio else ""
            mb = total / 1048576
            self.dl_var.set(T("✓ {name} ({n} frames {mb:.1f}MB{audio}){tag}").format(
                name=os.path.basename(path), n=n, mb=mb, audio=audio_txt, tag=tag))
        elif kind == "dlfinished":
            self.close_dl_dialog()
            if data:                   # None = 오류/종료 중 → 알림 대신 치명 오류 팝업
                self.notify_download_done(*data)
        elif kind == "fatal":
            was_dead = self.dead
            self.dead = True
            self.set_status("⛔ " + data)
            self.close_dl_dialog()
            self.reconn_btn.config(state="normal")
            if not was_dead:   # 같은 치명 오류의 팝업 중복 방지
                messagebox.showerror(T("Camera Disconnected"),
                                     data + T("\n\nReconnect the camera, then press [🔁 Reconnect]."))

    def open_dl_dialog(self):
        if self.dlg is not None:
            return
        self.dlg = tk.Toplevel(self.root)
        self.dlg.title(T("Download Progress"))
        self.dlg.geometry("420x130")
        self.dlg.transient(self.root)
        tk.Label(self.dlg, text=T("Capturing the camera stream..."),
                 anchor="w").pack(fill="x", padx=10, pady=(10, 2))
        bar = ttk.Progressbar(self.dlg, mode="indeterminate", length=380)
        bar.pack(padx=10, pady=4)
        bar.start(60)
        tk.Label(self.dlg, textvariable=self.dl_var, fg="#2060a0",
                 anchor="w", wraplength=390).pack(fill="x", padx=10)
        tk.Button(self.dlg, text=T("❌ Cancel"), width=10,
                  command=self.cancel_download).pack(pady=6)

    def close_dl_dialog(self):
        if self.dlg is not None:
            self.dlg.destroy()
            self.dlg = None

    def set_status(self, text):
        self.status_var.set(text)

    # ---- 캔버스 렌더링 (비율 유지 + 중앙 정렬, 리사이즈 대응) ----
    def _canvas_draw(self, canvas, jpeg):
        with muted_stderr():
            img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return
        cw = max(int(canvas.winfo_width()), 320)
        ch = max(int(canvas.winfo_height()), 180)
        ih, iw = img.shape[:2]
        scale = min(cw / iw, ch / ih)
        tw, th = max(1, int(iw * scale)), max(1, int(ih * scale))
        disp = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)
        photo = tk.PhotoImage(data=ppm_from_bgr(disp))
        canvas.delete("all")
        canvas.create_image((cw - tw) // 2, (ch - th) // 2, image=photo, anchor="nw")
        self._photos[canvas] = photo
        self._canvas_state[canvas] = ("img", jpeg)

    def _canvas_placeholder(self, canvas, text):
        canvas.delete("all")
        w = max(int(canvas.winfo_width()), 320)
        h = max(int(canvas.winfo_height()), 180)
        canvas.create_text(w // 2, h // 2, text=text, fill="#666",
                           font=("Arial", 14))
        self._canvas_state[canvas] = ("ph", text)

    def _canvas_rerender(self, canvas):
        """리사이즈 등에서 마지막 상태를 새 크기로 다시 그린다."""
        st = self._canvas_state.get(canvas)
        if not st:
            return
        if st[0] == "img":
            self._canvas_draw(canvas, st[1])
        else:
            self._canvas_placeholder(canvas, st[1])

    def on_tab_change(self, event):
        if self.sess is None:
            return
        target = self.notebook.index(self.notebook.select())
        if self.sess.busy:
            if target == 0:  # 다운로드는 탐색 탭에서만 진행 — 라이브 전환 차단
                self.notebook.select(self.tab_files)
                self.set_status(T("⚠ Cannot switch to the Live tab during download"))
            return
        self.active_tab = target
        self._drain_display()
        if target == 0:
            self._canvas_placeholder(self.live_canvas, T("Starting live..."))
        else:
            self._canvas_placeholder(self.play_canvas, T("Playback standby — select a file and press ▶"))
        want = "live" if target == 0 else "browse"
        if self.sess.mode != want:   # 같은 모드면 재생 중인 state를 건드리지 않음
            self.sess.cmd("mode", want)
        if want == "browse":
            self.refresh_list()   # 탐색 모드 진입(녹화 정지 후)에 목록 갱신

    def _drain_display(self):
        if self.sess is None:
            return
        try:
            while True:
                self.sess.display_q.get_nowait()
        except queue.Empty:
            pass


def is_admin():
    """관리자 권한 여부 — 물리 디스크 SPTD 접근(카메라 명령)에 필수."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def relaunch_as_admin():
    """관리자 모드로 자기 자신을 재실행 (UAC 승인 필요). 성공 True."""
    try:
        script = os.path.abspath(sys.argv[0])
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{script}"', None, 1)  # SW_SHOWNORMAL
        return rc > 32
    except Exception:
        return False


def main():
    for i, a in enumerate(sys.argv):
        if a == "--lang" and i + 1 < len(sys.argv):
            i18n.set_lang(sys.argv[i + 1])
    root = tk.Tk()
    root.withdraw()
    if not is_admin():
        if messagebox.askyesno(
                T("Administrator Required"),
                T("This program sends SCSI commands directly to the camera (physical disk) and "
                  "requires administrator rights. Without them the camera cannot be found.\n\n"
                  "Relaunch as administrator?")):
            if relaunch_as_admin():
                return 0   # 관리자 인스턴스에 넘기고 현재 창은 닫음
            messagebox.showwarning(
                T("Relaunch Failed"),
                T("Relaunch was cancelled or failed.\nRunning in normal mode — the camera "
                  "connection may fail."))
        else:
            messagebox.showwarning(
                T("Not Running as Administrator"),
                T("Running in normal mode — the camera connection will likely fail."))
    root.deiconify()
    PlayerUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
