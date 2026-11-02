# 0.6.1 수집 데이터·원격 배정 검토

기준은 `fb134ad`, 수정 위치는 `review/codex-0611`의 별도 worktree다. 실기 중인 작업 트리, 배포 서버, 키트, HANDOFF는 수정하지 않았다. 아래 위치는 수정 후 코드 기준이며 실패 설명은 수정 전 동작이다.

## 수정한 문제

| 심각도 | 위치 | 실패 시나리오와 수정 |
|---|---|---|
| HIGH | `recorder/remote_catalog.py:122` | 대기 중인 새 판을 늦게 도착한 낮은 판이 덮음. 적용·대기 중 가장 높은 판과 트랜잭션 안에서 비교하며 같은 판의 내용 교체도 거부한다. |
| HIGH | `recorder/remote_catalog.py:190` | 수행한 배정 ID의 지시문·대상·소유자를 바꾸면 새 과업이 완료로 표시됨. 수행 스냅샷과 소유자를 고정하고 삭제 후 재등장도 검사한다. |
| HIGH | `recorder/common.py:9`, `recorder/remote_catalog.py:102`, `recorder/upload.py:277` | HTTP 리다이렉트가 bearer를 다른 목적지로 전달할 수 있음. 카탈로그와 업로드 모두 리다이렉트를 거부하고 재시도 가능한 실패로 남긴다. |
| MEDIUM | `recorder/remote_catalog.py:139` | 실행기가 거부할 URL이나 잘못된 시간·불리언 값을 배정에 적용함. 수신 단계에서 검증한다. |
| HIGH | `browser-extension/background.js:86` | popup 생성 중 다른 창의 새 탭을 과업 탭으로 잘못 등록함. 실제 생성된 창 ID를 확인한 뒤 그 창의 이벤트만 전송한다. 초기 대기열 한도 초과는 누락 표시를 남긴다. |
| HIGH | `browser-extension/background.js:116`, `static/free-task.js:96` | 창 열기 실패 후 세션이 active로 남거나 종료 요청 실패 후 감시가 끊김. 창 생성 실패는 technical로 기록하고, 종료 응답 유실은 서버 상태와 대조해 복구한다. 같은 오류의 반복 낭독도 막는다. |
| HIGH | `recorder/free_task.py:45`, `static/free-task.js:140`, `static/study.js:271` | 결과 입력 중 새로고침하면 자유 과업을 찾지 못해 저장도 새 과업도 불가능해짐. 미완료 자유 과업 조회 API와 대시보드 준비 후 복원을 연결한다. |
| HIGH | `nvda-addon/globalPlugins/a11yTaskRecorder.py:339`, `:758` | 늦게 처리된 입력에 다음 세션의 요소가 붙거나 주소창 입력이 이전 웹페이지 행동으로 분류됨. 입력의 원래 세션을 보존하고 확인 불가능한 맥락은 명시한다. |
| MEDIUM | `nvda-addon/globalPlugins/a11yTaskRecorder.py:431`, `:862`, `:890` | 취소된 발화가 끝나지 않거나 비 URL 버퍼 ID가 실제 문서 URL을 가림. 문서가 다른데도 같은 요소로 발화가 합쳐짐. 취소·URL 대체·문서별 결합 기준을 수정한다. |
| HIGH | `recorder/server.py:1434`, `:1479`, `:1503` | 파생 interactions에서 요소 상태·입력 맥락·발화 시각이 빠짐. 원시 관측과 도착 요소를 구분해 보존하고 발화를 원 이벤트로 추적할 수 있게 한다. |
| MEDIUM | `recorder/server.py:1677` | 초기 이벤트 유실 표시가 일반 marker로 축약되어 원인을 잃음. marker 내용과 유실 경고를 유지하며 품질 확인을 통과시키지 않는다. |

## 해결됐다고 주장하지 않는 항목

| 심각도 | 위치 | 필요한 판단·후속 작업 |
|---|---|---|
| HIGH | `nvda-addon/globalPlugins/a11yTaskRecorder.py:689`, `:818` | NVDA는 URL만으로 자유 과업 popup과 다른 브라우저 창을 확실히 구분할 수 없다. 확장 창 ID와 운영체제 창 식별자를 연결하는 소유권 프로토콜이 필요하다. `url_only_unverified`를 남기며 자유 과업 export의 품질 확인은 미통과로 표시한다. 이는 캡처를 차단한 것이 아니다. |
| HIGH | 같은 파일 `:988` | NVDA 전송 실패·대기열 누락을 완전히 계수하지 못한다. 식별자·누락 표식·중복 방지 재시도 정책이 필요하다. 발화가 없다는 이유만으로 FE 무반응이라 단정할 수 없다. |
| HIGH | 별도 lab의 `src/render-c1-recorder.ts:147`, `src/memory-source.ts:43` | 자유 과업 여러 건이 같은 참가자-00 ID로 변환돼 중복 오류가 난다. 안정적인 세션 ID와 종류·시도 출처를 유지해야 한다. 이번 수정 범위 밖이라 변경하지 않았다. |
| HIGH | 별도 lab의 `src/memory-source.ts:66` | 취소·기술 실패·품질 미통과·자유 과업 구분을 소비기가 무시한다. recorder의 품질 플래그만으로 자동 제외가 이뤄지지 않는다. 메모리 적격성 및 FE/SNC 평가 제외 규칙을 소비기에 적용해야 한다. |
| MEDIUM | 별도 lab의 `src/render-c1-recorder.ts:71` | 현재 종료키가 사용자 페이지 행동으로 남는다. 제어 이벤트 제외 규칙을 갱신해야 한다. |
| MEDIUM | 별도 사본 서버의 `src/hosted-benchmark-server.ts:122` | 배정 파일 읽기 오류를 모두 404로 바꿔 클라이언트가 미배포로 해석한다. 파일 없음만 404, 나머지는 오류로 구분하고 원자적으로 배정 파일을 교체해야 한다. 서버는 이번에 수정하지 않았다. |
| 운영 결정 | 인증·전송 경계 | 테스트 페이지의 HTTP 허용과 별개로, 카탈로그·업로드의 HTTPS 정책 및 공유 코드의 키트별 권한·회수 범위를 정해야 한다. 운영 자격 파일을 읽어 현 배포를 조사한 것은 아니다. |
| 운영 결정 | 자유 과업 창 수동 닫기 | 기록 종료·기술 실패·사용자 중단 중 무엇으로 처리할지 정해야 한다. 임의로 참여자 실패로 판정하지 않았다. |

## 해석과 검증

- 자유 과업은 `study.kind=free`, `free_index`, 세션 ID를 원본 export에서 유지하며 `data_quality.intended_use=memory_only`로 표시한다. 자동 메모리 채택이나 공식 평가 적격성을 의미하지 않는다.
- `collection_check_passed`와 `no_reported_capture_gap`은 관측된 수집 검사다. 보고되지 않은 손실의 부재를 증명하지 않는다.
- `listened_s`는 발화 시작과 종료·취소 사이 기록 시간이다. 사람이 실제로 들었거나 주의를 기울인 시간으로 해석하지 않는다.
- 테스트는 합성 데이터와 임시 로컬 서버를 사용한다. 실제 참가자 자료·자격 파일은 읽지 않았고 Windows/NVDA 실기는 수행하지 않았다.
- 재현 명령: `python3 -m unittest discover -s tests`, `node --test tests/*.test.cjs`.
