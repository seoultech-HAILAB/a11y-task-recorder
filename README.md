# A11y Task Recorder

NVDA를 사용한 웹 과업 평가에서 키 입력과 당시 접근성 객체, NVDA 발화, 페이지
이동, DOM 변화, 힌트와 불편 구간을 하나의 타임라인으로 기록하는 로컬 우선
연구 도구입니다. 이 문서에서 Android TalkBack의 “announcement”에 대응하는
NVDA 데이터는 **NVDA 발화** 또는 **스크린 리더 발화(speech output)**라고
표현합니다.

## 처음부터 설치하기

개발 경험이 없는 평가자도 아래 순서대로 설치할 수 있습니다.

### 준비물

- 64비트 Windows 10 또는 Windows 11
- [NVDA 공식 다운로드 페이지](https://www.nvaccess.org/download/)에서 설치한 NVDA
  2024.1 이상
- Google Chrome 또는 Microsoft Edge
- [Python Windows 다운로드 페이지](https://www.python.org/downloads/windows/)에서
  설치한 Python 3.9 이상

Python 설치 화면에서는 **Add python.exe to PATH**를 선택하는 것을 권장합니다.
설치 확인은 시작 메뉴에서 `명령 프롬프트`를 열고 다음 명령으로 할 수 있습니다.

```powershell
py --version
```

`Python 3.9` 이상의 버전이 나오면 준비가 끝난 것입니다.

### 1. 프로그램 받기

GitHub 저장소 상단의 **Code → Download ZIP**을 선택하고 받은 파일의 압축을
풉니다. OneDrive 동기화 폴더보다는 `C:\A11yTaskRecorder`처럼 짧고 단순한
경로를 권장합니다.

### 2. 로컬 서버 실행

압축을 푼 폴더의 `start-recorder.bat`을 실행합니다. 명령 프롬프트 창이 열리고
브라우저에 다음 주소가 표시됩니다.

```text
http://127.0.0.1:8765
```

Windows 방화벽 안내가 나타나더라도 외부 네트워크 공개는 필요하지 않습니다.
이 서버는 현재 컴퓨터에서만 접근 가능한 `127.0.0.1` 주소를 사용합니다.

창이 바로 닫히면 명령 프롬프트에서 압축을 푼 폴더로 이동한 뒤 직접 실행해 오류를
확인합니다.

```powershell
py -3 run.py
```

### 3. Chrome 또는 Edge 확장 설치

Chrome은 주소창에 `chrome://extensions`, Edge는 `edge://extensions`를
입력합니다.

1. **개발자 모드**를 켭니다.
2. **압축해제된 확장 프로그램을 로드합니다**를 선택합니다.
3. 압축을 푼 폴더 안의 `browser-extension` 폴더를 선택합니다.
4. 브라우저 도구 모음에 A11y Task Recorder 확장을 고정합니다.

세션을 시작하면 확장 아이콘에 `REC` 배지가 표시됩니다.

### 4. NVDA 애드온 설치

`dist\a11yTaskRecorder-0.2.0.nvda-addon` 파일을 실행합니다. NVDA가 설치 여부를
물으면 승인하고 NVDA를 재시작합니다.

설치 파일이 없거나 소스를 수정한 경우 다음 명령으로 다시 만들 수 있습니다.

```powershell
py -3 scripts\build_nvda_addon.py
```

NVDA 재시작 후 `NVDA+Control+Shift+R`을 누릅니다.

- 상승음 두 번: 대시보드에서 세션을 기록 중
- 낮은음 한 번: 서버가 꺼져 있거나 진행 중인 세션이 없음

### 5. 첫 평가 시작

1. 대시보드에서 시나리오 제목, 참여자 코드, 시작 URL, 사이트 사전 경험과 평가
   환경을 입력합니다.
2. 세션을 만들고 과업을 분석할 단위별로 step을 추가합니다.
3. **기록 시작** 후 첫 step의 **이 step 시작**을 누릅니다.
4. 대상 웹사이트에서 NVDA로 과업을 수행하고 step이 바뀔 때 다음 step을 시작합니다.
5. 힌트를 주었다면 내용을 입력하고 **힌트 시점 기록**을 누릅니다.
6. 불편한 순간 `NVDA+Control+Shift+M`을 누릅니다.
7. 과업을 마치면 **완료하고 종료**를 누릅니다.
8. 이벤트 타임라인에서 **불편·힌트 칩**으로 해당 지점에 바로 이동해 참여자와
   함께 확인하고, 구간의 시작·끝 이벤트를 선택해 문제 설명을 등록합니다.
9. 참여자 종합 의견을 저장하고 JSON 또는 CSV로 내보냅니다.

서버의 명령 프롬프트 창을 닫으면 수집 서버가 종료됩니다. 다음 평가 때
`start-recorder.bat`을 다시 실행하면 기존 세션이 그대로 표시됩니다.

## 구현된 기능

- 평가 세션 생성, 시작, 완료 및 중단
- Tab/Shift+Tab, 뒤로가기, 단축키 종류별 집계
- NVDA `inputCore` 입력 제스처와 그 순간의 accessible name, role, state,
  IA2UniqueID를 한 이벤트로 기록
- 합성 대기열의 여러 조각을 요소별 **발화 에피소드**로 병합하고 원문과 보수적으로
  전처리한 텍스트를 함께 보존
- 실제 합성 완료 시각 `speech_end_ts`와 다음 입력/취소에 의한 발화 중단 여부 기록
- Chrome/Edge 확장은 URL·페이지 전환과 집계된 DOM 변화만 기록
- 시나리오 step 시작/종료, 힌트 제공 시점과 모든 이벤트의 현재 step 자동 연결
- 평가 중 불편 지점 마커
- 종료 후 타임라인 검토: 불편·힌트 지점 빠른 이동, 선택 구간 시각화
- 이벤트 구간과 step 구간에 문제 설명, 심각도, 기대 발화와 태그 연결
- 사용자 종합 의견 저장
- 전체 세션 JSON 및 이벤트 CSV 내보내기
- SQLite 로컬 저장과 입력 문자·URL 쿼리 기본 비수집

## 수집 구조와 연구 근거

이 구현은 [A11yNavigator 논문](https://ics.uci.edu/~seal/publications/2025_ASE.pdf)의
구조를 참고합니다. NVDA 애드온이 `inputCore`와 speech 경로를 관찰해 입력과
접근성 객체를 한 시점에 결합하고, 브라우저 확장은 NVDA가 직접 제공하지 않는
URL 전환과 DOM 변화만 보완합니다. 발화 완료 판정은 NVDA 공식 소스의
[`synthDoneSpeaking`](https://github.com/nvaccess/nvda/blob/master/source/synthDriverHandler.py)
알림을 사용합니다.

```text
NVDA inputCore ── 키 + accessible name/role/state/IA2UniqueID ─┐
NVDA speech ───── 원문 조각 → 요소별 발화 에피소드 → 종료/중단 ├─ SQLite/JSON
브라우저 확장 ── URL 전환 + 집계된 DOM 변화 ──────────────────┘
대시보드 ─────── step + 힌트 + 타임라인 검토 + 불편 구간 라벨 ──┘
```

## 구성

```text
.
├── recorder/                Python 표준 라이브러리 기반 API와 SQLite 저장소
├── static/                  접근 가능한 세션 대시보드
├── browser-extension/       Chrome/Edge Manifest V3 확장
├── nvda-addon/              NVDA Global Plugin 소스
├── scripts/                 NVDA 애드온 패키징 도구
├── tests/                   저장소와 HTTP API 자동 테스트
├── run.py                   로컬 서버 진입점
└── start-recorder.bat       Windows 실행 파일
```

## 상세 설치 정보

### 1. 로컬 서버

Python 3.9 이상을 설치한 다음 이 폴더에서 `start-recorder.bat`을 실행합니다.
또는 터미널에서 다음을 실행합니다.

```powershell
py -3 run.py
```

대시보드는 `http://127.0.0.1:8765`에서 열립니다. 외부 네트워크에는 바인딩하지
않습니다. 세션 데이터는 `data/recorder.sqlite3`에 저장됩니다.

### 2. Chrome 또는 Edge 확장

Chrome은 `chrome://extensions`, Edge는 `edge://extensions`를 엽니다.

1. 개발자 모드를 켭니다.
2. **압축해제된 확장 프로그램을 로드합니다**를 선택합니다.
3. 이 저장소의 `browser-extension` 폴더를 선택합니다.
4. 도구 모음에 A11y Task Recorder를 고정합니다.

확장은 평가 대상 페이지의 URL 전환과 DOM 변화를 얻기 위해 모든 사이트 읽기 권한을
요청합니다. 데이터는 `127.0.0.1:8765`로만 전송합니다. 일반 입력 문자와 폼 값은
전송하지 않으며 URL은 origin과 path만 남겨 쿼리와 해시를 제거합니다.

### 3. NVDA 애드온

개발 PC에서 설치 파일을 만듭니다.

```powershell
py -3 scripts\build_nvda_addon.py
```

생성된 `dist\a11yTaskRecorder-0.2.0.nvda-addon` 파일을 실행하고 NVDA의 안내에
따라 설치한 후 NVDA를 재시작합니다.

지원 기준은 NVDA 2024.1 이상이며 manifest의 마지막 시험 대상으로 NVDA 2026.1을
표시했습니다. 실제 평가 환경에는 NV Access가 배포하는 최신 공식 안정 버전을
권장합니다. 이 저장소는 macOS에서 개발되었으므로 첫 Windows 시험에서는 아래
체크리스트로 실제 NVDA 호환성을 확인해야 합니다.

NVDA 2025.1 이상에서는 음성 합성 직전의 처리된 대기열을 기록합니다. NVDA 2024.x는
동일한 확장 지점이 없어, NVDA가 음성 출력을 시도하는 한 단계 앞의 이벤트를
기록하도록 자동 폴백합니다.

세션이 시작되면 애드온은 NVDA 버전, 합성기와 발화 속도를, 확장은 브라우저와
확장 버전을 세션 환경에 자동 병합합니다. 대시보드에서 미리 입력한 값은 해당
컴퓨터에서 실제 감지한 값으로 갱신될 수 있습니다.

## 평가 기관 배포용 올인원 키트

평가를 진행할 기관(예: 협력 연구실)에 Python 설치, 확장 개발자 모드 로드 같은
개발 단계를 요구하지 않으려면 올인원 키트를 만들어 전달합니다.

```powershell
py -3 scripts\build_release_kit.py
```

`dist\A11yTaskRecorder-<버전>-chrome<버전>.zip`이 생성됩니다. 키트에는 기록
서버, 임베디드 Python, Chrome for Testing, 브라우저 확장, NVDA 애드온 설치
파일과 `설치안내.html`이 들어 있습니다. 전달받은 평가자는 ① 압축 해제
② NVDA 애드온 더블클릭 ③ `평가시작.bat` 더블클릭만 하면 됩니다.

`평가시작.bat`은 서버를 띄운 뒤 Chrome for Testing을 `--load-extension`으로
실행해 확장을 자동 로드합니다. 브랜드 Chrome은 137부터 이 플래그를 지원하지
않지만 Chrome for Testing에서는 계속 지원되므로 스토어 등록이나 개발자 모드가
필요 없습니다. 평가용 브라우저는 키트 안의 전용 프로필을 사용해 평가자의 평소
브라우저와 분리되며, 자동 업데이트가 없어 모든 평가자가 같은 버전을 사용하게
됩니다. 보안 패치 반영을 위해 평가 라운드마다 키트를 새로 생성해 배포하세요.
Chrome for Testing에는 일부 상용 코덱(H.264 등)이 없으므로 동영상 재생이
핵심인 과업에는 사전 확인이 필요합니다.

## 사용 방법

1. 로컬 서버와 브라우저 확장, NVDA 애드온을 실행합니다.
2. 대시보드에서 과업과 참여자 코드, 평가 환경을 입력해 세션을 만듭니다.
3. **기록 시작**을 누릅니다. 시작 URL이 있으면 새 탭에서 열립니다.
4. 사용자는 평소처럼 NVDA로 과업을 수행합니다.
5. 불편을 느낀 순간 `NVDA+Control+Shift+M`을 누릅니다. 높은 확인음이 납니다.
   확장 프로그램에서는 `Alt+Shift+M`도 사용할 수 있습니다.
6. 과업 완료 후 대시보드에서 세션을 종료합니다.
7. 종료 후 타임라인 상단의 **불편·힌트 칩**으로 표시 지점에 바로 이동해
   참여자와 함께 확인합니다. 구간의 시작과 끝 이벤트를 선택하면 해당 범위가
   문제 양식에 연결됩니다.
8. 문제 설명과 기대 발화를 등록하고 종합 의견을 저장한 뒤 JSON 또는 CSV로
   내보냅니다. 여러 세션을 한꺼번에 전달할 때는 기록 보관함의
   **결과 패키지 만들기**로 완료 세션 전체와 DB 사본이 담긴 ZIP 하나를 만들어
   보냅니다.

`NVDA+Control+Shift+R`을 누르면 애드온 기록 상태를 소리로 확인할 수 있습니다.
상승음은 기록 중, 낮은음은 대기 중을 뜻합니다.

## 데이터 의미

- `speech_episode`은 NVDA가 합성기에 전달한 텍스트 조각을 동일 IA2 요소와 짧은
  시간 간격 기준으로 묶은 분석 단위입니다. `fragments`와 `raw_text`에는 원문을,
  `normalized_text`에는 끝부분의 `heading`, `clickable`, `버튼` 같은 흔한
  보조 서술자를 보수적으로 제거한 값을 저장합니다.
- `speech_end_ts`는 NVDA 합성기의 `synthDoneSpeaking` 완료 알림 시각입니다.
  완료 전에 다음 키가 입력되거나 NVDA가 음성을 취소하면 그 시각을 종료로 기록하고
  `interrupted: true`로 표시합니다. 실제 오디오를 녹음하거나 몇 번째 음소까지
  들었는지를 판정하지는 않습니다.
- 발화 횟수(`speech_episode_count`), 원래 조각 수(`speech_fragment_count`),
  고유 발화 요소 수(`unique_spoken_element_count`)는 서로 다른 지표입니다.
- NVDA 탐색 모드의 가상 커서는 DOM 포커스를 이동시키지 않을 수 있습니다. 그래서
  키 입력의 요소 식별은 브라우저 DOM 포커스가 아니라 NVDA의 IA2UniqueID를
  우선 사용합니다. 확장은 URL과 DOM 변화의 보조 시간축만 제공합니다.
- 브라우저 뒤로가기 방향은 탭별 방문 기록과 `webNavigation` 정보를 결합해
  추론합니다. 복잡한 SPA나 같은 URL을 반복하는 페이지에서는
  `back_or_forward`로 기록될 수 있습니다.
- Tab 횟수만으로 접근성 문제를 자동 판정하지 않습니다. 과업의 기준 경로와 사용자
  설명을 함께 비교해야 합니다.

## 개인정보 보호

- 일반 입력 문자와 입력 필드 값은 수집하지 않습니다.
- 편집창에서 발생한 NVDA 음성 및 문자 에코는
  `[입력 문자 음성 출력 숨김]`으로 대체합니다.
- 보호 상태의 NVDA 객체에서 생성된 음성도 같은 방식으로 숨깁니다.
- 브라우저 URL의 query와 hash는 저장하지 않습니다.
- 기본 서버는 loopback 주소에만 열립니다.
- 실제 사용자 평가 전 동의 문구, 보존 기간과 폐기 절차를 별도로 정해야 합니다.

## 데이터 백업과 제거

세션 데이터는 압축을 푼 폴더의 `data\recorder.sqlite3`에 저장됩니다.

- 백업: 서버를 종료한 뒤 `recorder.sqlite3` 파일을 안전한 위치에 복사합니다.
- 다른 PC로 이동: 프로그램 폴더와 `data` 폴더를 함께 복사합니다.
- 데이터 초기화: 서버를 종료하고 `data\recorder.sqlite3`을 별도 보관하거나
  삭제한 뒤 서버를 다시 실행합니다.
- 브라우저 확장 제거: 브라우저 확장 관리 화면에서 **삭제**를 선택합니다.
- NVDA 애드온 제거: NVDA 메뉴 → 도구 → 애드온 스토어 → 설치된 애드온에서
  A11y Task Recorder를 제거하고 NVDA를 재시작합니다.

## 문제 해결

### 대시보드가 열리지 않음

- `start-recorder.bat` 명령 프롬프트 창이 열려 있는지 확인합니다.
- 브라우저에서 `http://127.0.0.1:8765/api/health`를 엽니다.
- `{"ok": true}`가 보이지 않으면 명령 프롬프트에서 `py -3 run.py`를 실행해
  오류 메시지를 확인합니다.

### 확장 아이콘에 REC가 표시되지 않음

- 대시보드에서 세션 상태가 **기록 중**인지 확인합니다.
- 확장 관리 화면에서 A11y Task Recorder가 활성화되어 있는지 확인합니다.
- 확장을 다시 로드한 뒤 대상 페이지도 새로고침합니다.

### NVDA 로그가 들어오지 않음

- `NVDA+Control+Shift+R`로 기록 상태를 확인합니다.
- NVDA 애드온 스토어의 설치된 애드온 목록에서 애드온이 활성 상태인지 확인합니다.
- 애드온 설치 후 NVDA를 재시작했는지 확인합니다.
- 현재 지원 브라우저인 Chrome, Edge, Firefox, Brave 또는 Opera를 사용 중인지
  확인합니다.

### 포트 8765가 이미 사용 중이라는 오류

다른 A11y Task Recorder 서버가 실행 중인지 확인합니다. 기존 명령 프롬프트 창을
찾아 사용하거나 종료한 뒤 다시 실행합니다. 확장과 NVDA 애드온은 기본 포트
`8765`를 사용하므로 임의의 다른 포트로 실행하면 별도 코드 설정이 필요합니다.

## 개발 및 검증

외부 Python 패키지는 필요하지 않습니다.

```bash
python3 -m unittest discover -v
node --check static/app.js
node --check browser-extension/background.js
node --check browser-extension/content.js
node --check browser-extension/popup.js
python3 scripts/build_nvda_addon.py
```

### Windows/NVDA 수동 시험 체크리스트

- 세션 시작 후 2초 안에 `NVDA+Control+Shift+R`이 상승음을 내는가
- Chrome/Edge에서 Tab과 Shift+Tab이 각각 한 번씩 집계되는가
- `H`, `K`, `B`, `NVDA+F7` 같은 NVDA 탐색 명령이 종류별로 기록되는가
- 입력창에 타이핑한 실제 글자가 이벤트나 음성 출력에 남지 않는가
- 탐색 모드와 포커스 모드에서 음성 출력이 기록되는가
- 발화를 끝까지 들으면 `speech_end_ts`와 `interrupted: false`가 기록되는가
- 발화 도중 다음 키를 누르면 `interrupted: true`가 기록되는가
- 한 요소의 나뉜 발화가 하나의 `speech_episode`과 여러 `fragments`로 저장되는가
- 키 입력 이벤트의 요소에 `ia2_unique_id`와 `unique_id`가 들어오는가
- step 시작 후 입력·발화·힌트에 같은 `step_id`가 연결되는가
- `NVDA+Control+Shift+M` 마커가 현재 포커스 맥락과 함께 나타나는가
- Alt+Left와 브라우저 뒤로가기 버튼이 페이지 이동으로 기록되는가
- 세션 종료 후 새 이벤트가 들어오지 않는가
- 종료 후 타임라인의 불편 칩으로 이동해 이벤트를 선택하면 구간이 문제 양식에 연결되는가
- JSON/CSV에 한글이 깨지지 않는가
