# NCS MCP 연동 계약

## 목적

`NCS_JD`는 기존 NCS MCP를 NCS 원천 사실의 유일한 런타임 공급자로 사용한다. 내부 Python 모듈이나 SQLite 스키마를 애플리케이션 계약으로 삼지 않는다.

## 환경설정

```text
NCS_MCP_URL=http://127.0.0.1:8766/mcp
NCS_MCP_HEALTH_URL=http://127.0.0.1:8766/health
NCS_MCP_READY_URL=http://127.0.0.1:8766/ready
NCS_MCP_TIMEOUT_SECONDS=30
```

앱 자체 서버는 기본적으로 `127.0.0.1`에만 바인딩한다. 외부 주소 바인딩은 MVP 범위가 아니다.

## 휴대용 Windows 런타임

휴대용 배포에서도 애플리케이션의 계약은 바뀌지 않는다. `NCS_JD.exe` 옆의 `NCS_MCP\NCS_MCP.exe`는 별도 프로세스로 실행되며 앱은 계속 HTTP/MCP만 호출한다.

```text
NCS_JD.exe
  └─ HTTP/MCP → NCS_MCP\NCS_MCP.exe
                    └─ read-only → NCS_MCP\data\ncs_jd_serving.db
```

런처는 사이드카에 `NCS_DB_PATH`, `NCS_MCP_READ_ONLY=1`, `NCS_MCP_ENABLE_OPERATOR_TOOLS=0`을 전달하고 `/health`와 `/ready`를 검증한다. serving DB는 NCS_MCP의 export 스크립트가 만드는 배포 산출물이며 애플리케이션 코드가 직접 열지 않는다. 소스 실행에서는 기존 NCS_MCP의 격리 Python으로 동일 서버를 시작한다.

## 애플리케이션 포트

도메인·application 계층은 MCP SDK 형식을 직접 알지 않는다.

```text
NcsSourcePort
  search_scope_candidates(query, limit)
  load_unit_evidence(unit_code)
  load_optional_references(unit_code, kinds)
  check_readiness()
```

`NcsMcpSourceAdapter`가 위 포트를 구현한다.

## 사용하는 MCP 도구

### `ncs_search`

목적: 직무 검색어에서 NCS 분류·능력단위 후보를 찾는다.

고정 정책:

- `scope="all"`
- 초기 `limit=20`
- 결과를 분류 경로와 능력단위 기준으로 앱에서 그룹화
- 검색 결과는 후보이며 자동 선택하지 않음

### `ncs_unit_detail`

목적: 사용자가 선택한 능력단위의 직접 근거를 가져온다.

MVP include:

```json
["elements", "criteria", "ksa"]
```

`training`은 직무기술서 핵심 근거가 아니므로 기본 제외한다. `qualification`은 수집 완전성과 serving DB 테이블 가용성을 확인한 경우에만 선택적으로 호출한다.

### `ncs_analysis`

목적: 사용자가 요청한 경우 경력경로, 자격, 직업기초능력 참고자료를 가져온다.

허용 mode:

- `career_path`
- `qualification`
- `job_base`

`ontology` 결과의 definition은 MVP 문장 생성 원천으로 사용하지 않는다.

## 의도적으로 사용하지 않는 기능

- 자연어 `query_router`: 현재 직무기술서 의도를 교육 추천으로 오분류할 수 있음
- 교육 추천 facade: JD 생성의 직접 근거가 아님
- operator/review 도구: 신규 앱에 상태 변경 권한이 없음
- legacy SQF/학습모듈 도구

## 응답 정규화

MCP 응답은 adapter에서 다음 application DTO로 정규화한다.

```text
ScopeCandidate
  classification_path
  duty_definition
  unit_code
  unit_name
  unit_level
  unit_definition

UnitEvidenceBundle
  unit
  elements[]
    element_id
    element_name
    criteria[]
    ksa[]
  source_audit
  warnings[]
```

원본 응답의 모든 필드를 도메인 계층으로 통과시키지 않는다. 필요한 필드만 명시적으로 매핑한다.

## 오류 처리

| 상태 | 앱 동작 |
| --- | --- |
| health 실패 | 새 검색·생성 비활성화, 저장 draft 열람 허용 |
| readiness 실패 | 원인 요약과 NCS MCP 실행 안내 표시 |
| timeout | 한 번의 제한된 재시도 후 복구 메시지 |
| 빈 검색 결과 | 검색어 수정과 분류 탐색 제안 |
| 일부 unit 실패 | 성공 unit만 임시 보존하고 생성 전 사용자 확인 |
| 필수 근거 누락 | 생성 중단 또는 명시적 `review_flag` |
| 선택 참고자료 누락 | 본문 생성은 계속하고 incomplete 플래그 |

무제한 재시도와 자동 NCS API 수집은 하지 않는다.

## 캐시

MVP에서는 세션 메모리 캐시만 사용한다.

- 검색 결과: 검색어+limit 키
- 단위 상세: unit_code 키
- 앱 재시작 시 캐시 폐기
- canonical NCS DB나 MCP 응답을 앱 DB로 장기 복제하지 않음

성능 측정 후에만 파일 캐시 또는 JD용 serving artifact를 검토한다.

## 계약 테스트

- `ncs_search` fixture가 `ScopeCandidate`로 변환되는지 확인
- `ncs_unit_detail`의 elements/criteria/KSA 중 일부가 비어도 구조가 유지되는지 확인
- 알 수 없는 필드가 추가되어도 adapter가 실패하지 않는지 확인
- 필수 식별자가 누락되면 명확한 contract error를 내는지 확인
- MCP payload의 review status를 새 앱의 문서 승인 상태로 복사하지 않는지 확인
- 사용자 화면과 로그에 API 키·원본 `source_payload`가 노출되지 않는지 확인

## 후속 변경 조건

다음 중 하나가 실제 측정되기 전에는 NCS_MCP에 JD 전용 facade를 추가하지 않는다.

- 한 문서 생성에 MCP 호출이 과도하게 많음
- unit detail 응답 크기가 UX 목표를 초과함
- 동일한 bundle 조립이 여러 소비자에서 반복됨
- 배포에 canonical DB 대신 더 작은 artifact가 반드시 필요함

조건이 충족되면 producer인 NCS_MCP에 versioned `job_profile_evidence_bundle` facade 또는 JD serving export를 추가하고, NCS_JD는 새 계약만 소비한다.
