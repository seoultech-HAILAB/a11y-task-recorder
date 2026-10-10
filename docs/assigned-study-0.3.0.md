# Task Recorder 0.3.3: 테스터별 배정 과업

## 사용 순서

1. 이전 기록이 진행 중이면 먼저 종료한다. 이전 서버 창과 평가용 Chrome을 닫는다.
2. 새 ZIP을 별도 폴더에 풀고 `평가시작.bat`를 실행한다.
3. SNC에서 배정받은 T01~T20 중 자신의 번호를 선택하고 `배정 확인`을 누른다.
4. `사이트 준비`를 누른다. 처음 한 번 접속 코드가 필요하며, 기록 전에 입력한다.
5. 화면의 과업을 확인하고 `기록 시작`을 누르면 준비된 사이트로 이동한다.
6. 수행 후 Recorder 탭에서 답변과 수행 결과를 입력하고 `답변 저장하고 기록 종료`를 누른다.
7. `다음 과업`으로 진행한다. 4개 과업마다 5분 휴식 안내가 표시된다.
8. 12개를 마친 뒤 결과 패키지를 생성한다. 원본 로그는 이 PC의 `data/recorder.sqlite3`에 보관된다.

URL, 과업 문장, 순서를 수동 입력하지 않는다. T01과 T02는 서로 다른 기존 배정표를 사용한다.
번호는 인증 계정이 아니라 배정 슬롯이다. SNC 진행자가 실제 참여자에게 올바른 번호를 전달한다.
`http://127.0.0.1:8765/?tester=T01`처럼 로컬 Recorder에 직접 슬롯을 지정할 수도 있다.
서로 다른 PC 사이의 진행률은 자동 동기화하지 않는다. 같은 슬롯을 여러 PC에서 동시에 수집하지 않는다.

## 확정 배정표와 개인정보

- 기존 `a11y-research/docs/261007_scenario_plan/tester_tasks.csv` 240행을 그대로 사용한다.
- T01~T20 각각 12과업, 사이트 3개씩, 원래 block/order 및 지시문을 보존한다.
- `hosted_task_id`로 현재 HTTPS 시작 URL을 연결한다. legacy caseId만으로 연결하지 않는다.
- 배포 파일 `recorder/study_catalog.json`에는 정답, 채점 기준, FE 유형, 정상/FE 조건을 넣지 않는다.
- 원본 명세·배정표 해시와 버전은 저장한다. 첫 접근 때 SQLite에 배정표를 고정해 업데이트 중 기존 기록을 재분류하지 않는다.
- 접근 코드·서명 키·기존 사용자 로그는 ZIP에 포함하지 않는다.
- 진행자 기록 관리는 기존 수동 세션/라벨링/내보내기 기능을 보존한다. 별도 관리자 인증 경계는 아니다.

## 기록 및 복구

- 각 과업은 새 탭으로 준비한다. 이전 탭의 sessionStorage 상태와 이동 이력을 이어받지 않는다.
- 다이소 수량 변경 과업은 기존 화면 버튼으로 상품 3개와 장바구니 열린 상태를 준비한다.
- 준비 조작은 기록 시작 전에만 수행하고 `study_setup`에 `operator_automation`으로 별도 보관한다. FE 속성을 고치지 않는다.
- NVDA 연결과 사이트 준비가 확인되어야 배정 과업의 기록을 시작한다. 연결 상태만으로 실제 발화 수집 성공을 보장하지 않는다.
- 완료/수행 불가/시간 종료는 다음 과업으로 진행한다. 기술 문제는 같은 과업을 새 시도로 재준비하며 이전 기록도 보존한다.
- 새로고침과 Recorder 재시작 후 기존 진행률을 복원한다. 기록 중인 다른 테스터의 세션을 덮어쓰지 않는다.
- 10분 경과를 표시하지만 현재 버전은 자동 종료하지 않는다. 진행자가 제한 시간 결과를 선택한다.
- 과업 답변 입력 뒤 종료하기까지의 원본 NVDA 로그도 보존된다. 분석에서는 대상 사이트 구간과 Recorder 응답 작성 구간을 구분해야 한다. 전체 session duration을 순수 과업 수행 시간으로 사용하지 않는다.
- 자기보고 결과는 `study_evaluation.status=NOT_SCORED`이며 객관적 성공률이나 FE 판정이 아니다.
- 0.3.3부터 수집 시점 가림이 없다. 입력한 글자(NVDA `input`, `input_kind=character`), 입력란·보호 필드의 발화와 문자 에코, URL의 쿼리와 해시를 그대로 저장한다. `speech_episode`의 `redacted`는 호환용 필드이며 항상 false다. 과업 입력값은 연구진이 제공한 가상 값이다.

## 이전 기록 유지

서버가 완전히 종료된 상태에서 이전 키트의 `data` 폴더 전체를 새 키트에 복사한다.
기존 폴더는 백업으로 보존하고, 실행 중인 SQLite 파일 하나만 복사하지 않는다.
새 버전은 이전 버전 서버를 강제로 종료하지 않는다. 같은 버전의 활성 기록이 있으면 NVDA를 재시작하지 않고 대시보드만 연다.

## 검증과 남은 확인

- 카탈로그: 20명 x 12과업, 100개 원본 hosted task와 일치 여부, 해시, 순서, 변조 거부 검사.
- 백엔드: 중복/동시 준비, 재시작, 교차 테스터 보호, 기술 재시도, 12개 종료, 자기보고/GT 분리.
- UI: 새로고침, 다음 과업, 유실된 시작/종료 응답 복구, 기술 재시도, 데스크톱/모바일 화면.
- 실제 Chromium 확장 + HTTPS 사이트: 로그인 전 기록 금지, T18 4과업, 정상/FE 장바구니 준비, 새로고침 복구, FE 이름 유지.
- 위 브라우저 실험의 NVDA heartbeat는 합성 신호다.
- 실제 Windows/NVDA 확인: 0.3.2를 2026-10-10에 자동 수행기 엔지니어링 파일럿(사람 데이터 아님)으로 T01 12과업을 수행했다. 기록기·NVDA 연결, 키 입력 기록 665/668, 결과 패키지 14세션 수집 점검 통과(저장소 `dist/pilot-20261010/RESULT.md`, `FOLLOWUP.md`). 장애인 참여자 시험은 아직 수행하지 않았다.
- 0.3.3(입력 문자·입력란 발화 가림 제거, URL 전체 기록)은 Windows/NVDA 재확인 전이다. `평가시작.bat`와 배정 과업 1건(파일럿 수행기)을 다시 확인한 후 SNC 수집용으로 사용한다.

## 재생성 및 검사

```bash
python3 scripts/build_study_catalog.py
python3 -m unittest discover -s tests -q
node --test tests/browser_extension.test.cjs tests/study_browser.test.cjs
PLAYWRIGHT_MODULE=/home/ys/workspace/A11y-Agents/node_modules/playwright node scripts/verify_study_dashboard.cjs
PLAYWRIGHT_MODULE=/home/ys/workspace/A11y-Agents/node_modules/playwright node scripts/verify_study_extension.cjs
python3 scripts/build_release_kit.py
python3 scripts/verify_release_artifacts.py
```

실제 확장 검사는 8765 포트가 비어 있을 때만 실행하며 임시 DB/브라우저 프로필을 사용한다.
원래의 운영/참여자 DB에는 테스트 기록을 넣지 않는다. 커밋과 푸시는 별도 작업이다.
