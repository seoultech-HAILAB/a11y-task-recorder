# Task Recorder 0.4.0: 한 창 기록기 (single window), 참가자 단독 진행

작성 2026-10-12, 2026-10-15 개정(진행자 없이 참가자 혼자 진행), 2026-10-18 개정(끝내기 확인 단계, 단축키 안내, 테스터 잠금), 2026-10-20 개정(접속 코드 키트 내장, 사용자 쪽 코드 입력 삭제, 키트 0.4.1). 계획서 `261012_single_window_plan.md`의 MVP 1~5를 구현한 상태다. **Windows/NVDA 실기 미확인**이며, `261013_spike_nvda_iframe.md`와 `261013_실기_점검표.md`를 통과한 뒤에만 수집에 쓴다.

## 10/20 결정: 접속 코드는 키트에 숨겨 넣고, 사용자 쪽 입력은 없앤다

- 10/18의 "키트에 넣지 않고 사이트 칸에서 입력" 결정을 뒤집었다. **참가자·담당자는 접속 코드를 보지도 입력하지도 않는다.** 내부 인원만 쓰는 도구라 사용자 쪽 코드 확인은 의미가 없다.
- 키트는 `scripts/build_release_kit.py --access-code-file <사본 서버 자격 파일>`로 만든다(`app/study_access.txt`, 아래 "접속 코드 내장"). 기록기가 숨긴 form으로 사이트를 자동 로그인하고, 같은 코드로 **자동 업로드도 다시 동작한다**(아래 "서버 전송"). 이 변경으로 키트 버전을 0.4.1로 올렸다(`study.js`가 바뀌었고, `평가시작.bat`이 0.4.0 서버 창을 구분하며, 수집 데이터의 기록기·확장 버전으로 코드 내장 키트를 가린다).
- 사이트 칸에서 코드를 입력하던 폴백은 없앴다. 코드가 없거나 사본 서버가 거부하면 프레임(접속 화면)은 숨긴 채 "사이트에 접속할 수 없습니다. 연구진에게 알려 주세요."를 오류로 띄운다.
- **사본 서버 쪽 잠금은 유지한다**(`/access` 로그인 쿠키, 업로드 bearer). 4319가 Tailscale Funnel로 인터넷에 공개돼 있고, 법률 검토 문서(`261005_website_research_legal_review.md`)의 "연구용 사본은 접근을 통제한다" 방침 때문이다. 권리가 확인되지 않은 사본을 주소만 아는 사람이 열 수 있으면 안 된다.
- 같은 이유로 **코드가 든 키트 ZIP과 `dist/kit-build/`는 공개 저장소의 Releases에 올리지 않는다**(이 저장소는 공개). 내부 경로로만 전달한다.

## 10/18 개정 요약

- **끝내기는 단축키가 기본**이다. `NVDA+Ctrl+Shift+E`는 확인 없이 바로 끝내고, 10분 자동 종료도 그대로다. 사이트 아래 **과업 끝내기** 버튼은 보조 수단으로 Tab 순서에서 뺐다(`tabindex=-1`, 화면에는 보이고 접근성 트리에도 있음). 참가자가 사이트를 Tab으로 훑다가 사이트에 없던 버튼이 끼어들어 흐름과 기록이 오염되는 것을 막기 위함이다. 마우스 클릭과 NVDA 브라우즈 모드(가상 커서로 내려가 Enter)로는 누를 수 있다.
- 버튼을 누르면 그 자리에 확인 단계(`#study-end-confirm`, `role=group`)가 열린다: "과업을 끝낼까요?" + **끝내기**, **계속하기**. 초점은 **계속하기**(실수 방지). **끝내기**는 `endTask("participant_button")`, **계속하기**와 Escape는 확인 단계를 닫고 초점을 사이트 프레임으로 돌린다(기록은 계속). 두 버튼은 확인 단계가 열려 있을 때만 Tab으로 닿는다. 수행 단계를 벗어나면(제스처 종료, 제한 시간) 확인 단계는 저절로 닫힌다.
- 단축키 안내: 연습 지시문 앞에 세 단축키와 확인음 안내를 붙였고(화면 `PRACTICE_INSTRUCTION`), 준비 단계에서만 과업 시작 아래 한 줄(`#study-shortcuts`)을 보인다. `NVDA+Ctrl+Shift+T`의 응답(`recorder/signals.py`)은 지시문 뒤에 "끝낼 때는 NVDA+Ctrl+Shift+E를 누르세요."를 붙인다(결과 입력 중에는 붙이지 않음).
- 테스터 잠금: 테스터가 정해지면 번호 칸·배정 확인·기록 관리 버튼을 숨긴다(`hidden`, Tab과 가상 버퍼에서 빠짐). 준비 단계에서 Tab으로 닿는 곳은 과업 시작과 (첫 과업 전) 연습 시작뿐이고, 지시문은 초점 이동용(`tabindex=-1`), 단축키 줄은 읽기 전용이다. 연구자는 `/?change-tester=1`(번호 칸 다시 보임) 또는 `/?tester=T03`(그 번호로 바로 열기)으로만 바꾼다. 기록 중인 과업이 다른 번호면 바꾸지 못하는 보호는 그대로다. 번호 칸이 없으므로 기억된 번호로 열리면 현재 과업을 곧바로 준비한다(예전에는 "배정 확인을 누르면…" 안내만 했음).
- (10/18에는 접속 코드를 키트에 넣지 않고 사이트 칸에서 입력하기로 했으나 10/20에 뒤집었다. 위 "10/20 결정" 참고.)

## 10/15 개정 요약

- 진행자 패널(`#study-operator`)과 접속 코드 입력을 없앴다. 참가자(NVDA 사용자)가 혼자 지시문을 듣고, 아래로 내려가 사이트에서 과업을 하고, 끝나면 결과를 저장한 뒤 다음 과업으로 넘어간다.
- 접속 코드는 키트에 내장한다(`app/study_access.txt`). 기록기가 사이트 로그인(iframe 자동 로그인)과 업로드 bearer에 쓴다. (10/18에 넣지 않기로 했다가 10/20에 다시 내장)
- 과업 끝내기는 세 가지: 사이트 칸 바로 아래 **과업 끝내기** 버튼, `NVDA+Ctrl+Shift+E`, 10분 자동 종료. (10/18: 버튼은 Tab 순서 밖 보조, 확인 단계 추가)
- 서버 API(pause, resume, resume-task, retry, uploads/config, uploads/flush)는 데이터·테스트 호환을 위해 그대로 두었다. 화면에서 부르지 않을 뿐이다.

## 화면 구조

한 페이지, 한 열이다. 사이트 칸이 가로를 다 쓴다(`fitFrame`: 1440×900 기준 화면 맞춤, 가로 스크롤 없음, 아래 96px은 과업 끝내기 버튼 자리).

| 영역 | 내용 | 준비(ready) | 수행(active) | 결과 입력(answering) | 저장됨(ended) |
|---|---|---|---|---|---|
| 위: 목표(`#study-top`) | 테스터 선택·배정 확인·기록 관리(테스터가 정해지면 숨김), 안내(`#study-message`, live), 과업 n/N, 사이트 이름(h2), 지시문(`tabindex=-1`), 타이머, **과업 시작**, 첫 과업 전 **연습 시작**, 단축키 한 줄(`#study-shortcuts`) | 읽힘. 준비되면 지시문에 초점. 단축키 줄은 이 단계에서만 보임 | `aria-hidden`, 버튼 `tabindex=-1`, 단축키 줄 숨김 | 읽힘(단축키 줄 숨김) | 같음 |
| 가운데: 창 속 창(`#study-stage`) | 사본 사이트 `<iframe name="study-frame" sandbox="allow-scripts allow-same-origin allow-forms allow-modals">`, 바로 뒤 **과업 끝내기**(`#study-end`, `tabindex=-1`)와 확인 단계(`#study-end-confirm`: "과업을 끝낼까요?", **끝내기**, **계속하기**) | 프레임 `hidden`(로그인 화면도 보이지 않음), 끝내기 숨김 | 프레임 보임·초점. 끝내기 보임(shelve 대상 아님, Tab 순서 밖). 누르면 그 자리에 확인 단계, 초점은 계속하기 | 프레임 `inert`·`aria-hidden`·`tabindex=-1`, 끝내기·확인 단계 숨김 | 같음 |
| 아래: 결과(`#study-result`) | 머리글, 라디오(마쳤습니다/마치지 못했습니다), 답, 저장 / 저장 안내, 블록 휴식 안내, **다음 과업**, **결과 수정**, (연습) **연습 끝내기** | 숨김 | 숨김 | 양식 보임, 머리글에 초점 | 저장 안내에 초점(제한 시간이면 다음 과업에 초점) |

수행 중 창 제목은 프레임의 제목으로 바뀐다(NVDA+T). 결과 입력에 들어가면 "A11y Task Recorder"로 돌아온다. 위쪽 머리의 `기록 관리` 버튼은 연구자용 수동 세션 화면으로 가는 링크다.

## 참가자 흐름

1. **배정 확인**: 테스터 번호를 고르고 누르면 곧바로 사이트 준비를 한다(이전의 `사이트 준비` 버튼 없음). 그 뒤로 번호 칸은 숨겨진다. 새로고침·키트 재실행으로 기억된 번호가 있으면 현재 과업을 곧바로 (다시) 준비한다.
2. **자동 준비**: 프레임에 `start_url`을 열고 확장(`bridge prepare/check`)으로 준비를 확인한다. `login_required`면 `GET /api/study/access`로 코드를 받아 숨긴 form(`POST <사본 origin>/access`, `target=study-frame`, `code`, `next=start_url 경로`)을 제출하고 다시 확인한다. 코드가 없거나 로그인 뒤에도 준비되지 않으면 프레임은 숨긴 채 "사이트에 접속할 수 없습니다. 연구진에게 알려 주세요."를 오류로 띄운다. 참가자에게 코드를 묻는 경로는 없다(10/20).
3. **준비됨**: 지시문(`#study-instruction`)에 초점을 옮겨 NVDA가 읽게 하고 `instruction_presented`를 남긴다. Tab 한 번이면 **과업 시작**이다.
4. **과업 시작**(또는 `NVDA+Ctrl+Shift+S`): 기록 시작, 초점은 사이트 프레임. 위쪽은 shelve된다.
5. **끝내기**: 기본은 `NVDA+Ctrl+Shift+E`(확인 없이 끝남, `participant_gesture`). 보조로 **과업 끝내기** 버튼(Tab 순서 밖) → 확인 단계 → **끝내기**(`participant_button`). **계속하기**/Escape는 프레임으로 돌아간다. 결과 머리글에 초점.
6. **저장**: 마쳤습니다/마치지 못했습니다 + 답. `entered_by=participant`. 저장 뒤 **결과 수정**은 다음 과업을 준비하기 전까지 된다(`answer_history`에 모두 남음).
7. **제한 시간**: 순수 시간이 `time_limit_seconds`(10분)에 닿으면 `time_limit_reached` → `end-task(time_limit)` → 곧바로 `stop(outcome=timeout, answer="", entered_by=system)`. "제한 시간이 되어 과업을 마쳤습니다."를 안내하고 **다음 과업**에 초점(설명에 저장 안내 연결). 저장이 실패하면 폴링이 4초마다 다시 저장한다.
8. **다음 과업**: 4과업마다(`order % 4 === 0`, 마지막 제외) "4개 과업이 끝났습니다. 원하는 만큼 쉬었다가 다음 과업을 눌러 주세요."를 저장 안내와 함께 읽힌다. 누르면 2로 돌아간다. 예전 데이터의 `technical` 결과는 다음 과업 대신 재준비(`retry`)한다.
9. **완료**: "배정된 과업을 모두 마쳤습니다." 참가자에게는 "더 하실 일은 없습니다. 담당자에게 끝났다고 알려 주세요."라고 안내하고, 담당자가 `결과 패키지 만들기 (담당자용)`로 ZIP을 만든다(자동 전송과 별개로 항상 만든다).

**연습**: 첫 과업 전(`finished=0`, 현재 과업 1)에만 **연습 시작**이 보인다. `/practice.html`(오늘의 요리)을 프레임에 넣고 같은 버튼(과업 시작, 과업 끝내기, 저장)과 같은 제스처로 돈다. 저장 뒤 **연습 끝내기**를 누르면 본 과업 준비(2)로 이어진다. 세션·이벤트를 만들지 않는다. 제스처는 `/api/signals/practice`로 서버가 수락한다.

## 신호와 단축키

| 제스처 | 동작 |
|---|---|
| `NVDA+Ctrl+Shift+S` | 시작. 준비된 과업이 있을 때만 수락(확인음 880Hz 90ms). 1초 안에 수행 단계로 전환, 초점은 프레임 |
| `NVDA+Ctrl+Shift+T` | 지시문 다시 듣기(확인음 880Hz 90ms). 애드온이 `ui.message`로 읽음. 준비·수행 중(과 연습)에는 지시문 뒤에 "끝낼 때는 NVDA+Ctrl+Shift+E를 누르세요."를 붙임(`signals.py` `END_HINT`, 결과 입력 중에는 붙이지 않음). `instruction_repeat` 이벤트, 발화 `origin=recorder_prompt` |
| `NVDA+Ctrl+Shift+E` | 종료. 기록 중일 때만 수락(확인음 660Hz 90ms). 확인 단계 없이 끝남. 결과 머리글에 초점 |
| 수락되지 않으면 | 낮은음 220Hz 160ms |

흐름: 애드온 → `POST /api/signals` → 서버가 현재 과업 상태로 판단해 `{accepted, speak}` 응답 → 대시보드가 1초 폴링(`GET /api/signals?after=`)으로 받아 **버튼과 같은 코드**를 실행. 안내 방식은 환경 변수 `A11Y_RECORDER_ANNOUNCE=focus|nvda|both`(기본 focus).

## 기록 범위와 세션 필드

- 확장: `all_frames: true`. background가 **대시보드 탭의 사본 프레임**(frameId)에서 온 이벤트만 보낸다. 다른 탭·대시보드 문서·다른 프레임은 `outside_task_frame`으로 버린다. 이벤트 `payload.tab_id`, `payload.frame_id`.
- 애드온 0.4.0: `element.frame`이 `task`(INTERNALFRAME 조상 있음) 또는 `recorder`. 자기 발화는 `payload.origin=recorder_prompt`.
- 종료 뒤 세션 상태는 `answering`. 이때 NVDA·확장 이벤트는 거부(409)되고, `task_ended_at` 5초 안의 늦은 이벤트만 `received_after_stop`으로 받는다.
- 세션 필드: `task_ended_at`, `end_reason`(`participant_button`/`participant_gesture`/`time_limit`, 예전 값 `facilitator_gesture`/`facilitator_button`/`facilitator_stop`도 허용), `net_task_seconds`, `study_answer_entered_by`(`participant`/`system`, 예전 값 `facilitator`), `answer_history[]`, `paused_at`, `pause_reason`, `study.ui_mode="single_window"`, `study.requires_answer`.
- 대시보드 이벤트(source `dashboard`): `signal`, `instruction_presented`, `instruction_repeat`, `time_limit_reached`, `task_end`, `answer_saved`(예전 데이터에는 `help_level_1`, `task_resume`, `pause`, `resume`도 있을 수 있음).
- 카탈로그 `requires_answer`: `tester_tasks.csv`의 `scoring`에 "(진행자 확인)"이 있는 12건이 `false`. 정답은 넣지 않는다.

## 접속 코드 내장

> 10/20 결정: 배포 키트는 항상 이 경로로 코드를 넣는다. 코드 파일은 운영 사본 서버(4319)의 자격 파일을 그대로 준다(`~/.local/share/a11y-study-hosting/access.json`, `accessCode` 필드만 읽음). 코드 값은 문서·로그·커밋에 남기지 않는다.

- 출처 우선순위: 환경 변수 `A11Y_RECORDER_ACCESS_CODE` > `A11Y_RECORDER_ACCESS_CODE_FILE`이 가리키는 파일 또는 키트의 `app/study_access.txt` > 로컬 DB `settings.upload_code`(예전 진행자 입력). 파일은 코드 한 줄 또는 `code`/`accessCode` 필드가 있는 JSON(사본 서버 자격 파일을 그대로 줄 수 있음, 다른 필드는 쓰지 않음).
- `recorder/upload.py`가 이 코드를 업로드 bearer로 쓴다. `GET /api/uploads`의 `configured`, `code_source`(`kit`/`local`/``)에 반영되고 코드 값은 내보내지 않는다.
- `GET /api/study/access` → `{origin, code}`(코드 없으면 `null`). 대시보드 자동 로그인 전용이다. 루프백 접속, `Host`가 `127.0.0.1`/`localhost`, `Origin`이 없거나 자기 origin, `Sec-Fetch-Site`가 same-origin/none일 때만 답하고 나머지는 403. CORS 헤더를 붙이지 않는다. 서버는 기본 `--host 127.0.0.1`에만 바인딩한다(키트 `kit_server.py`도 같은 기본값).
- 키트 빌드: `scripts/build_release_kit.py --access-code-file PATH`. 주면 `app/study_access.txt`를 넣고, 빼면 넣지 않는다. 뺀 키트는 사이트에 접속하지 못하므로 배포하지 않는다(빌드 출력에 "배포 금지"). 코드 값은 빌드 출력·`KIT-INFO.txt`·서버 창에 나오지 않는다(서버 창에는 "키트에 내장됨/내장되지 않음"만). 저장소 루트의 `study_access.txt`는 `.gitignore`에 있다.

## 사본 서버(a11y-user-context-lab) 변경

- `Content-Security-Policy: frame-ancestors 'self' http://127.0.0.1:8765 http://localhost:8765` (이전 `'none'`). 서비스 인자 `--frame-ancestor`로 추가 가능.
- `POST /access`: 자기 origin 외에 **설정된 frame-ancestor origin**(와일드카드 없는 `http(s)://host[:port]`만)에서 온 POST도 받는다. 대시보드의 숨긴 form 로그인용. 다른 origin은 그대로 403.
- 접속 쿠키 `SameSite=None; Secure; Partitioned` (HTTPS 또는 loopback일 때). 대시보드 안의 프레임에서 로그인하면 그 분할에 쿠키가 생겨 다음 과업부터 묻지 않는다.
- 업로드 엔드포인트 `POST /upload/session`, `GET /upload/status`. 기본 저장 폴더 `<public의 상위>/uploads`.
- 운영 서비스(4319 `a11y-study-sites.service`, Funnel 공개; 4320 `a11y-study-sites-review.service`, 로컬 미리보기)는 **2026-10-20 18:52에 재시작해 반영했다**. 재시작 전(10/14 19:54 기동)에는 CSP `frame-ancestors`와 `Partitioned` 쿠키는 이미 있었지만 `/access`가 `http://127.0.0.1:8765` origin의 POST를 403으로 막아 자동 로그인이 실패하는 상태였다. 재시작 뒤 공개 주소에서 확인: 쿠키 없는 `GET /` 401(잠금 유지), `POST /access`(Origin `http://127.0.0.1:8765`, 키트 코드) 303 + `SameSite=None; Secure; Partitioned`, 다른 origin 403, `GET /upload/status`(키트 코드 bearer) 200.

## 10/18 사용자 관점 검토 반영

- NVDA가 읽는 단축키는 "NVDA, Ctrl, Shift, E를 함께"처럼 쉼표로 끊는다("+"는 "더하기"로 읽힘). 화면 표시 줄은 "+"를 두고 `aria-label`만 쉼표 형식.
- 확인음 설명을 사실대로: 시작·다시 듣기 880Hz(높은 음), 끝내기 660Hz(한 단계 낮은 음), 거부 220Hz(낮은 경고음).
- 연습 지시문은 연습 과업을 먼저, 단축키 안내를 뒤에.
- 참가자에게 "블록", "기록기", "브라우저 확장", "A11y Agents" 같은 내부 말을 노출하지 않는다.
- 검수 도구(`검수실행.bat`, `verify_live_pipeline.ps1`, `windows-release-validation.md`)와 `KIT-INFO.txt`는 키트의 `_연구진용/` 폴더로 옮겼다. 키트 최상위에는 `평가시작.bat`, `설치안내.html`, `진행안내.md`, NVDA 애드온만 남는다.

## 서버 전송(업로드)

**코드 내장 키트(0.4.1)에서는 자동 업로드가 다시 동작한다(10/20).** `GET /api/uploads`는 `configured: true`, `code_source: "kit"`이고, 업로드 주소는 카탈로그 origin + `/upload/session`(운영 4319)이다. 운영 서버가 키트 코드를 bearer로 받는 것은 10/20에 확인했다(`/upload/status` 200). 코드 없이 만든 키트에서는 10/18에 확인한 대로 `flush()`가 전송 시도 없이 `{"skipped": "not_configured"}`를 돌려주지만, 그런 키트는 사이트에도 접속하지 못하므로 배포하지 않는다. 설치안내·진행안내의 "결과는 ZIP으로만 나간다"는 문구도 자동 전송 사실에 맞게 고쳤다.

코드가 있을 때는 결과 패키지 ZIP을 그대로 만들고, 과업이 끝날 때마다 그 세션의 `export.json`을 사본 서버에 올린다. 실패해도 세션은 계속되고 참가자에게 알리지 않는다. 대기열은 다음 과업 종료, 패키지 생성, 120초 재시도(서버를 다시 켜도 이어짐)에서 다시 보낸다. 화면의 전송 설정·`지금 전송`은 없앴다(API `POST /api/uploads/config`, `/api/uploads/flush`는 남음).

## 로컬 확인 (Linux)

```bash
python3 -m unittest discover -s tests -q
node --test tests/browser_extension.test.cjs tests/study_browser.test.cjs
PLAYWRIGHT_MODULE=/home/ys/workspace/A11y-Agents/node_modules/playwright node scripts/verify_single_window.cjs
```

`verify_single_window.cjs`는 임시 포트에서 실제 확장·실제 기록 서버(`A11Y_RECORDER_ACCESS_CODE`로 코드 내장)·실제 사본 서버를 띄워 다음을 돈다: 진행자 요소 부재, 배정 확인 → 자동 준비·숨긴 form 로그인(코드 입력 없음) → 지시문 초점, 테스터 칸 숨김·준비 단계 Tab 순서(과업 시작, 연습 시작)·단축키 줄, 연습(지시문 앞 단축키 안내, T 응답 끝의 E 안내, 과업 시작·제스처 종료(확인 없음)·저장·연습 끝내기 → 본 과업 준비), 제스처 시작, 수행 중 Tab 순서(프레임만, 과업 끝내기는 보이지만 `tabindex=-1`, 사이트 마지막 요소에서 Tab해도 끝내기로 가지 않음), 끝내기 버튼 → 확인 단계(초점 계속하기, 그때만 끝내기·계속하기 Tab 가능) → 계속하기(Enter)·Escape로 프레임 복귀·기록 계속, sandbox 팝업 차단, 프레임 한정 이벤트, 끝내기 버튼 → 끝내기 → 결과 입력, 참가자 저장, 업로드·중복·수정본, 다음 과업(재로그인 없음), 제한 시간 자동 저장, 업로드 실패·재시도, 마지막 과업(T 응답, 제스처 종료에 확인 단계 없음), 완료 안내, `?change-tester=1`·`?tester=` 동작, 마지막으로 **코드 없는 기록기**(같은 포트로 재시작, 새 DB, 쿠키 삭제) → 프레임(접속 화면)은 숨긴 채 "사이트에 접속할 수 없습니다" 오류, 5초 뒤 배경 확인 뒤에도 그대로, 기록 화면 어디에도 "접속 코드" 없음, 업로드 `not_configured`(`dist/qa/single-window/no-code-error.png`). `SELFRUN_DIR`를 주면 화면 캡처 3장(1-ready: 준비 단계와 단축키 줄, 2-active-confirm: 수행 중 확인 단계, 3-result: 결과 입력)을 그곳에 저장한다. NVDA는 합성 하트비트다.

**미확인**: 실제 NVDA 브라우즈 모드에서 사이트 문서 끝을 지나 `tabindex=-1`인 "과업 끝내기"에 닿는지(가상 버퍼가 iframe 밖 다음 요소로 이어지는지)와 Enter로 눌리는지, 확인 단계가 열릴 때 NVDA가 "과업을 끝낼까요? 그룹, 계속하기 버튼"처럼 읽는지는 Windows 실기에서 확인해야 한다. 코드 내장 키트(0.4.1)로 첫 과업을 준비할 때 숨긴 프레임에서 일어나는 로그인·이동이 NVDA에 읽히지 않는지, 운영 사본(HTTPS, Funnel)에 대해 자동 로그인과 다음 과업 재로그인 없음이 Windows Chrome for Testing에서도 되는지, 과업 뒤 자동 전송이 운영 업로드 폴더에 쌓이는지도 실기에서 확인한다.
