# Codex·Claude 팀 오케스트레이션 계약

## 목적과 경계

팀 모드는 결정적으로 완성·검증된 `JobProfile`의 설명 문구만 다듬는다. NCS 검색, 세분류·능력단위 선택, `source_ref` 연결, 자격·학력·경력 결정, 문서 승인 판단은 팀의 권한 밖이다. 제공자끼리 직접 연결하지 않고 로컬 오케스트레이터가 같은 최소 패킷을 각각 전달한다.

## 실행 순서

```text
Validated draft JobProfile
        │
        ├── Codex proposal ──┐   (동시 실행)
        └── Claude proposal ─┤
                             ▼
                  기존 allow-list 검증
                             │
        ┌── Codex reviews Claude ──┐   (동시 실행)
        └── Claude reviews Codex ──┤
                                   ▼
                         결정적 로컬 합의
                                   │
                       검증된 draft 또는 원문
```

1. 1라운드: Codex와 Claude가 `job_purpose.text`, `duties[].summary`, `tasks[].description`에 독립 제안을 반환한다.
2. 중간 게이트: ID, locator, 원문, 수치, URL, 식별자, 보호 요건과 새 사실 금지를 검증한다. 하나라도 위반하면 상호 검토 전에 전체 팀 실행을 중단한다.
3. 2라운드: 각 제공자는 상대 제안에 대해 필드별 `accept/meaning_preserved` 또는 `reject/meaning_not_preserved`만 반환한다.
4. 로컬 결정: 모델에게 최종 중재를 맡기지 않고 아래 표를 적용한다.

| 조건 | 결과 |
| --- | --- |
| 두 제안 문구가 정확히 같음 | 같은 문구 선택 |
| 다른 제안 중 하나만 상대가 승인 | 승인받은 제안 선택 |
| 다른 두 제안을 서로 모두 승인 | 방향이 모호하므로 정확한 원문 |
| 다른 두 제안을 서로 모두 거절 | 정확한 원문 |

모든 필드가 원문이면 `team_no_changes`, 하나 이상 합의되면 `team_rewrite_resolved`다.

## CLI 격리

- Codex: ephemeral, ignore-user-config, read-only sandbox, strict output schema
- Claude Code: print mode, no-session-persistence, safe mode, tools disabled, strict JSON schema
- `shell=False`, 제공자별 임시 작업 디렉터리, 제한된 환경변수 allow-list, 시간·입출력 크기 제한을 사용한다.
- 로그인 상태는 설치 여부·로그인 여부·정규화 코드만 노출한다. 이메일, 조직 ID, 토큰, 쿠키와 원시 인증 응답은 저장하지 않는다.

## 감사와 실패

감사 객체는 제한 패킷, 두 제안, 두 검토 결과의 SHA-256과 제공자별 정규화 상태·코드를 보관한다. 원시 prompt/stdout/stderr, NCS 원천 payload와 사용자 계정 식별자는 포함하지 않는다.

한 제공자의 미설치·미로그인·사용량 소진·timeout·비정상 JSON은 단일 제공자로 조용히 강등하지 않는다. 팀 실행을 명시적 오류로 중단하므로 사용자는 단일 제공자 모드를 의식적으로 선택하거나 로그인 상태를 복구할 수 있다.

## 호출 표면

- 웹: `/api/generate-job-description`의 form `provider=team`
- 상태: `/api/llm/providers`에서 Codex·Claude의 정규화된 상태 조회
- 명령줄: `ncs-jd-team status`, `ncs-jd-team deliberate`

명령줄 입력은 이미 검증 가능한 `ncs_job_profile_v1` JSON이어야 한다. `--report`는 팀 결과·감사를, `--output-profile`은 합의 문구를 적용하고 다시 Pydantic 검증한 `draft` JSON을 쓴다.
