# -*- coding: utf-8 -*-
"""i18n — 영어 기본, 한국어 선택. 코드에는 영어 msgid를 쓰고 T()로 출력 언어를 결정한다.

- LANG: "en" (기본) | "ko"
- 감지 순서: XCDVR_LANG 환경변수 → Windows UI 언어(한국어면 ko) → en
- set_lang()으로 명시 지정 가능 (CLI: --lang en|ko)
- T(msgid, **kw): 사전에 없으면 msgid(영어) 그대로 반환 — 누락 번역은 영어로 안전하게 표시됨
"""
import ctypes
import os

LANG = "en"

_KO = {
    # --- UI 골격 ---
    "uCarDvr — Live View / Recording Browser": "uCarDvr — 라이브 뷰 / 녹화 탐색",
    "Connecting...": "연결 중...",
    "🔁 Reconnect": "🔁 재연결",
    "Live View": "라이브 뷰",
    "Recording Browser": "녹화 탐색",
    "📹 Start Live": "📹 라이브 시작",
    "📷 Snapshot": "📷 스냅샷",
    "🔴 Local Rec: Off": "🔴 로컬 녹화: 끔",
    "Live view has no sound (check recorded files)":
        "라이브 뷰에서는 소리가 나오지 않습니다 (녹화된 파일에서는 확인 가능)",
    "Live standby": "라이브 대기 중",
    "Starting live...": "라이브 시작 중...",
    "Connecting live...": "라이브 연결 중...",
    "● Live/recording in progress (saving to SD)": "● 라이브/녹화 진행 중 (SD에 저장되고 있습니다)",
    "🛑 Stop Live": "🛑 라이브 정지",
    "▶ Start Live": "▶ 라이브 시작",
    "■ Live/recording stopped": "■ 라이브/녹화 정지",
    "Files": "파일 목록",
    "🔄 Refresh": "🔄 갱신",
    "🗑 Delete": "🗑 삭제",
    "🔒 Protect": "🔒 보호",
    "No.": "번호",
    "Date/Time": "촬영일시",
    "📥 Save Selected": "📥 선택저장",
    "📦 Save All": "📦 전체저장",
    "Playback (1280×720)": "재생 화면 (1280×720)",
    "Auto-play next": "다음파일 자동재생",
    "🔁 Repeat: Off": "🔁 반복: 없음",
    "🎬 Play": "🎬 재생",
    "🅿️ Pause": "🅿️ 일시정지",
    "🛑 Stop": "🛑 정지",
    "🔙 Prev": "🔙 이전",
    "Next 🔜": "다음 🔜",
    "Volume": "볼륨",
    "Starting playback...": "재생 시작 중...",
    "Playback standby — select a file and press ▶": "재생 대기 중 — 파일 선택 후 ▶",
    "Stopped": "정지",
    "Download Progress": "다운로드 진행",
    "Capturing the camera stream...": "카메라 스트림을 캡처하는 중...",
    "❌ Cancel": "❌ 취소",

    # --- 상태/재생 ---
    "Connected — auto-starting live view...": "연결됨 — 라이브 자동 시작 중...",
    "List: {n} files — select one and press ▶ Play": "목록 {n}개 — 파일 선택 후 ▶ 재생",
    "▶ Playing: {kind}{idx:04d}": "▶ 재생 중: {kind}{idx:04d}",
    "⚠ {kind}{idx:04d} playback failed — moving to next file":
        "⚠ {kind}{idx:04d} 재생 실패 — 다음 파일로 넘어갑니다",
    "⚠ Playback failed to start — try again": "⚠ 재생 시작 실패 — 다시 재생해 보세요",
    "■ Playback finished (end of file): {kind}{idx:04d}":
        "■ 재생 종료 (파일 끝): {kind}{idx:04d}",
    "🔁 Repeating: {kind}{idx:04d}": "🔁 반복 재생: {kind}{idx:04d}",
    " — end of list": " — 목록 끝",
    "✓ {kind}{idx:04d} deleted (verified by re-listing)":
        "✓ {kind}{idx:04d} 삭제 완료 (목록 재조회로 확인)",
    "⚠ {kind}{idx:04d} still in the list — cmd 20 may not be a delete command":
        "⚠ {kind}{idx:04d}가 여전히 목록에 있음 — cmd 20의 동작이 삭제가 아닐 수 있음",
    "⚠ {idx:04d} disappeared from the list — check immediately":
        "⚠ {idx:04d} 파일이 목록에서 사라짐 — 즉시 확인 바람",
    "✓ {idx:04d} → {kind} switch complete": "✓ {idx:04d} → {kind} 전환 완료",
    "⚠ {idx:04d} protect switch failed": "⚠ {idx:04d} 보호 전환 실패",
    "⏺ Local recording started: {p}": "⏺ 로컬 녹화 시작: {p}",
    "✓ Local recording saved: {p}": "✓ 로컬 녹화 저장: {p}",
    "⬇ Downloading: #{idx:04d}": "⬇ 다운로드 중: #{idx:04d}",
    "⬇ #{idx:04d}: {frames} frames {mb:.1f}MB audio {n} chunks {rate:.1f}MB/s {elapsed:.0f}s":
        "⬇ #{idx:04d}: {frames}프레임 {mb:.1f}MB 오디오 {n}청크 {rate:.1f}MB/s {elapsed:.0f}초",
    " (cancelled — partial)": " (취소 — 부분 저장)",
    " · audio included": " · 오디오 포함",
    "✓ {name} ({n} frames {mb:.1f}MB{audio}){tag}":
        "✓ {name} ({n}프레임 {mb:.1f}MB{audio}){tag}",
    "⬇ Cancel requested — finishing the current block, then saving partial":
        "⬇ 취소 요청 — 현재 블록을 마치고 부분 저장합니다",
    "⬇ Cancelled — saving what was received so far": "⬇ 취소 — 지금까지 받은 부분을 저장합니다",
    "{idx:04d}: no frames — skipped": "#{idx:04d}: 프레임 없음 — 건너뜀",
    "⚠ Read error ({n}/5): {e}": "⚠ 읽기 오류 ({n}/5): {e}",
    "Disconnected — use the [🔁 Reconnect] button": "연결 끊김 — [재연결] 버튼을 사용하세요",
    "Internal error: {e}": "내부 오류: {e}",
    "Init failed: {e}": "초기화 실패: {e}",
    "Pause/Resume toggled": "일시정지/재개 토글",
    "⚠ Cannot play during download": "⚠ 다운로드 중에는 재생할 수 없습니다",
    "⚠ Cannot start live during download": "⚠ 다운로드 중에는 라이브를 시작할 수 없습니다",
    "⚠ Cannot switch to the Live tab during download":
        "⚠ 다운로드 중에는 라이브 탭으로 전환할 수 없습니다",
    "⚠ Local recording requires live view": "⚠ 로컬 녹화는 라이브 시작 후에 가능합니다",
    "⚠ Cannot delete during live — stop live first":
        "⚠ 라이브 중에는 삭제할 수 없습니다 — 라이브를 먼저 정지하세요",
    "⚠ Cannot change while live/recording — stop live first":
        "⚠ 라이브 중에는 변경할 수 없습니다 — 라이브를 먼저 정지하세요",

    # --- 목록/정렬 ---
    "First refresh the list (an SD card is required)": "먼저 목록을 새로고침하세요 (카메라에 SD 필요)",
    "The list is empty": "목록이 비었습니다",
    "List sorted: {col} {order}": "목록 정렬: {col} {order}",
    "Date/Time descending": "촬영일시 내림차순",
    "Date/Time ascending": "촬영일시 오름차순",
    "No. descending": "번호 내림차순",
    "No. ascending": "번호 오름차순",
    "descending": "내림차순",
    "ascending": "오름차순",

    # --- 삭제/보호 ---
    "Select a file to delete": "삭제할 파일을 목록에서 선택하세요",
    "🔒 {idx:04d} is protected — unlock it before deleting":
        "🔒 {idx:04d}은(는) 보호된 파일 — 보호 해제 후 삭제할 수 있습니다",
    "🔒 {idx:04d} is protected — unlock it before deleting":
        "🔒 {idx:04d}은(는) 보호된 파일 — 보호 해제 후 삭제할 수 있습니다",
    "Confirm Delete": "삭제 확인",
    "[{kind} {idx:04d}] Delete this file from the SD card?\n\n⚠ This cannot be undone.":
        "[{kind} {idx:04d}] 파일을 SD카드에서 삭제할까요?\n\n⚠ 되돌릴 수 없습니다.",
    "Select a file to protect/unlock": "보호(잠금)/해제할 파일을 목록에서 선택하세요",
    "Confirm Protect Change": "보호 변경 확인",
    "Unlock": "해제",
    "Lock": "잠금(보호)",
    "[{kind} {idx:04d}] {action}?": "[{kind} {idx:04d}] {action}할까요?",

    # --- 스냅샷 ---
    "Snapshot: no live frame — start live first":
        "스냅샷: 저장할 라이브 화면이 없습니다 — 먼저 라이브 시작",
    "📸 Snapshot saved: {p}": "📸 스냅샷 저장: {p}",

    # --- 다운로드 ---
    "Downloads capture the live stream in real time, so it takes a while "
    "(1 min of recording ≈ 2-3 min).":
        "다운로드는 실시간 캡처 방식이라 오래 걸립니다 (녹화 1분 ≈ 2~3분).",
    "Confirm Download": "다운로드 확인",
    "\nProceed?": "\n진행할까요?",
    "Save File As": "저장할 파일 선택",
    "AVI video": "AVI 동영상",
    "Confirm Save All": "전체 다운로드 확인",
    "[Save All] All {n} files in the list will be captured in order.\nProceed?":
        "[전체 다운로드] 목록의 모든 파일 ({n}개)이 순서대로 캡처됩니다.\n진행할까요?",
    "Select Folder": "저장할 폴더 선택",
    "Download Cancelled": "다운로드 취소",
    "Download Failed": "다운로드 실패",
    "Download Complete": "다운로드 완료",
    "Cancelled — no files were saved.": "취소되어 저장된 파일이 없습니다.",
    "No files saved ({n} skipped without frames).":
        "저장된 파일이 없습니다 (프레임을 받지 못한 파일 {n}개).",
    "■ Download cancelled — no files saved": "■ 다운로드 취소 — 저장된 파일 없음",
    "⚠ Download failed — no files saved": "⚠ 다운로드 실패 — 저장된 파일 없음",
    "Cancelled — saved partial data ({n} file(s)).": "취소 — 받은 부분만 저장했습니다 ({n}개 파일).",
    "Saved ({n} file(s)).": "저장 완료 ({n}개 파일).",
    "\nSkipped {n} file(s) with no frames.": "\n프레임을 받지 못해 건너뛴 파일 {n}개.",
    "… and {more} more": "… 외 {more}개",
    " (partial)": " (부분)",
    "\n\nSaved to: {d}": "\n\n저장 위치: {d}",

    # --- 종료/관리자 ---
    "Exit": "종료 확인",
    "The program will exit and the camera returns to recording mode.\n"
    "(The camera keeps recording to the SD card.)\n\nExit?":
        "프로그램을 종료하고 녹화 모드로 돌아갑니다.\n"
        "(종료 후에도 카메라가 SD카드에 계속 녹화합니다)\n\n종료할까요?",
    "Administrator Required": "관리자 권한 필요",
    "This program sends SCSI commands directly to the camera (physical disk) and requires "
    "administrator rights. Without them the camera cannot be found.\n\n"
    "Relaunch as administrator?":
        "이 프로그램은 카메라(물리 디스크)에 직접 SCSI 명령을 보내므로\n"
        "관리자 권한이 필요합니다. 권한이 없으면 카메라를 찾지 못합니다.\n\n"
        "관리자 모드로 다시 실행할까요?",
    "Relaunch Failed": "관리자 모드 재실행 실패",
    "Relaunch was cancelled or failed.\nRunning in normal mode — the camera connection "
    "may fail.":
        "재실행이 취소되었거나 실패했습니다.\n"
        "일반 모드로 실행하지만 카메라 연결이 실패할 수 있습니다.",
    "Not Running as Administrator": "관리자 권한 없음",
    "Running in normal mode — the camera connection will likely fail.":
        "일반 모드로 실행합니다 — 카메라 연결이 실패할 가능성이 높습니다.",
    "Camera Disconnected": "카메라 연결 끊김",
    "\n\nReconnect the camera, then press [🔁 Reconnect].":
        "\n\n카메라를 다시 연결한 뒤 [🔁 재연결] 버튼을 누르세요.",

    # --- 세션/오류 (라이브러리에서 올라오는 메시지) ---
    "Camera (elen1 drive) not found. Check the USB connection, power and "
    "administrator rights. Details: {e}":
        "카메라(elen1 드라이브)를 찾지 못했습니다. USB 연결·전원·관리자 권한을 확인하세요. 상세: {e}",
    "No drive containing an elen1 file was found. Check the camera connection and mounting.":
        "elen1 파일을 가진 드라이브를 찾지 못했습니다. 카메라 연결·마운트를 확인하세요.",
    "winmm unavailable": "winmm 사용 불가",
    "waveOutOpen failed (code {rc})": "waveOutOpen 실패 (코드 {rc})",
    "waveOut write failed": "waveOut 버퍼 출력 실패",
    "Playback stream failed to start (no data after cmd 6)":
        "재생 스트림 시작 실패 (cmd 6 후 데이터 없음)",

    # --- 길이 조회 진단 ---
    "Length query: {arg:#06x} (12 tries, 0.7s apart)": "길이조회: {arg:#06x} (0.7초 간격 12회)",
    "Length query failed — slider stays hidden": "길이조회 실패 — 슬라이더는 숨김 유지",
    "Length received: {n}s → slider shown": "길이 수신: {n}초 → 슬라이더 표시",

    # --- probe (CLI) ---
    "    → Data area start LBA = {x}, cluster={c}B":
        "    → 데이터 영역 시작 LBA = {x}, 클러스터={c}B",
    "    → ELEN1 data start LBA = {x} (Windows LCN1 calc = {c})":
        "    → ELEN1 데이터 시작 LBA = {x} (Windows LCN1 계산값 = {c})",
    "    [!] ELEN1 entry not found — the root may span multiple clusters":
        "    [!] ELEN1 엔트리를 못 찾음 — 루트가 여러 클러스터일 수 있음",
    "    [{t:5.1f}s] resp! cmd={cmd}({cmd_hx}) arg={arg_hx} {pl!r}":
        "    [{t:5.1f}초] 응답! cmd={cmd}({cmd_hx}) arg={arg_hx} {pl!r}",
    "    [{t:5.1f}s] frame! marker={mk!r} {n}B":
        "    [{t:5.1f}초] 프레임! marker={mk!r} {n}B",
    "    [{t:5.1f}s] elen1 changed! sectors: {secs}":
        "    [{t:5.1f}초] elen1 변화! 섹터(elen1기준) {secs}",
    "[FS changes] changed={c} new={n} removed={g}":
        "[파일시스템 변화] 변경={c} 신규={n} 삭제={g}",
    "[FS changes] none (incl. zlog)": "[파일시스템 변화] 없음 (zlog 포함)",
    "🔁 Repeat: {m}": "🔁 반복: {m}",
    "Repeat: {m}": "반복 재생: {m}",
    "Off": "없음",
    "One file": "한 파일",
    "All": "전체",
    "\n\nSaved to: {d}": "\n\n저장 위치: {d}",
    "[!] No drive with an elen1 file was found.": "[!] elen1 파일을 가진 드라이브가 없습니다.",
    "[i] Read backend: {m}": "[i] 읽기 백엔드: {m}",
    "SCSI passthrough": "SCSI 패스스루",
    "Volume ReadFile": "볼륨 ReadFile",
    "\n[1] LBA 0 boot sector check:": "\n[1] LBA 0 부트섹터 검증:",
    "[i] BPB: sector={bps}B sec/cluster={spc} reserved={rsvd} FATs={nfats} "
    "FAT size={fat_sz32} sectors root cluster={root_clus} label='{label}'":
        "[i] BPB: 섹터={bps}B 섹터/클러스터={spc} 예약={rsvd} FAT수={nfats} "
        "FAT크기={fat_sz32}섹터 루트클러스터={root_clus} 라벨='{label}'",
    "\n[2] Root directory:": "\n[2] 루트 디렉터리 확인:",
    "    Entry: {name:<12} attr={attr:#04x} cluster={fclus} size={fsize}":
        "    항목: {name:<12} attr={attr:#04x} 클러스터={fclus} 크기={fsize}",
    "\n[3] Sending full command sequence:": "\n[3] 전체 명령 시퀀스 전송:",
    "    cmd19 (datetime)": "    cmd19(날짜시간)",
    "    cmd{cmd} (preset)": "    cmd{cmd}(프리셋)",
    "\n[4] 10s loop: cmd58 + elen1 full (600 sectors) watch":
        "\n[4] 10초 루프: cmd58 + elen1 전체(600섹터) 감시",
    "[Result] frames={f} resps={r} area changes={c}":
        "[결과] 프레임={f} 응답={r} 영역변화={c}",
    "\n→ No response at all. Next: insert an SD card and retest.":
        "\n→ 완전 무반응. 다음 단계: SD카드 삽입 후 동일 테스트.",
    "No boot signature — SPTD is reading the wrong location!":
        "부트 시그니처 없음 — SPTD가 잘못된 위치를 읽음!",
}

autodetect_lang = os.environ.get("XCDVR_LANG")
if autodetect_lang not in ("en", "ko"):
    try:
        if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0xFFFF == 0x0412:
            autodetect_lang = "ko"
        else:
            autodetect_lang = "en"
    except Exception:
        autodetect_lang = "en"

LANG = autodetect_lang


def set_lang(lang):
    """언어 명시 지정 ("en" | "ko"). 알 수 없는 값은 무시된다."""
    global LANG
    if lang in ("en", "ko"):
        LANG = lang


def T(msg, **kw):
    """msgid(영어)를 현재 언어로 변환해 반환. 사전에 없으면 영어 그대로(안전한 기본값)."""
    s = _KO.get(msg, msg) if LANG == "ko" else msg
    return s.format(**kw) if kw else s