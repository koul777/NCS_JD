# NCS_JD Repository Guidelines

## 목적

이 프로젝트는 기존 `C:\workspace\NCS_MCP`의 읽기 전용 공개 도구를 사용해 NCS 근거가 추적되는 직무기술서와 직무명세서 초안을 만든다.

## 먼저 읽을 문서

작업 전에 다음 파일을 읽는다.

- `README.md`
- `docs/IMPLEMENTATION_PLAN.md`
- `docs/MCP_INTEGRATION.md`
- `docs/JOB_PROFILE_CONTRACT.md`
- NCS 소스 계약을 변경해야 하는 경우에만 `C:\workspace\NCS_MCP\AGENTS.md`와 해당 저장소의 핵심 문서를 추가로 읽는다.

## 저장소 경계

- 애플리케이션 코드는 `C:\workspace\NCS_JD`에만 둔다.
- `C:\workspace\NCS_MCP`의 내부 Python 모듈을 직접 import하지 않는다.
- 첫 MVP는 NCS MCP의 `ncs_search`, `ncs_unit_detail`, `ncs_analysis`만 명시적으로 호출한다.
- 첫 MVP에서 자연어 `query_router` 결과에 의존하지 않는다.
- NCS SQLite 파일을 애플리케이션에서 직접 열거나 수정하지 않는다.
- 새 프로젝트의 draft 저장소와 NCS 원천 저장소를 분리한다.

## NCS 데이터 불변조건

- `ksa_items.ksa_text_raw`를 수정하지 않는다.
- NCS 원문, 수행준거, KSA, 능력단위 정의를 조직이 승인한 최종 문장으로 오인시키지 않는다.
- 자동화가 `human_reviewed`, `accepted`, `reviewed`를 쓰지 않는다.
- 온톨로지 정의 후보와 boilerplate 정의는 확정적 직무요건 근거로 사용하지 않는다.
- 자격과 직업기초능력은 수집·연결 범위를 확인하고 `reference` 등급으로만 제공한다.
- SQF와 NCS 학습모듈은 활성 기본 근거로 사용하지 않는다.

## 생성 원칙

- 직무기술서와 직무명세서를 분리된 객체로 생성한다.
- NCS 기반 필드에는 최소 하나 이상의 `source_ref`가 있어야 한다.
- 조직 고유 필드는 `organization_input`으로 표시한다.
- 근거가 없으면 생성하지 말고 `review_flags`에 누락 상태를 남긴다.
- LLM을 사용하더라도 근거 선택은 결정적 단계에서 먼저 완료한다.
- LLM은 재서술만 수행하며 새로운 사실, 자격, 학력, 경력연수, 법적 의무를 추가할 수 없다.
- LLM OFF 상태의 동일 입력은 동일한 구조화 출력을 만들어야 한다.

## 문서 상태

- MVP에서 생성 가능한 문서 상태는 `draft`뿐이다.
- 향후 조직 승인 상태를 추가하더라도 NCS 원천의 review status와 다른 필드·이름·저장소를 사용한다.
- 프로그램 결과는 채용 결정, 법적 적격성 판단, 공식 자격 인정이 아니다.

## 검증 규칙

변경 후 최소한 다음을 확인한다.

- JSON schema/Pydantic validation
- NCS MCP mock contract tests
- NCS 기반 문장에 source reference가 누락되지 않았는지 검사
- 사용자 제외 업무가 결과에 다시 포함되지 않는지 검사
- LLM OFF 결정성 검사
- NCS MCP 장애·빈 결과·불완전 근거 처리 검사
- 원본 NCS DB 쓰기 경로가 없는지 검사

## 작업 방식

- 사용자 파일과 다른 에이전트 변경을 되돌리지 않는다.
- 생성 파일, 캐시, draft 문서, 로컬 설정은 Git 추적 정책을 명시한다.
- 비밀값과 API 키를 문서·로그·Git에 넣지 않는다.
- 커밋과 배포는 사용자가 명시적으로 요청했을 때만 수행한다.
