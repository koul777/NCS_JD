# JobProfile v1 데이터 계약

## 설계 목적

`JobProfile`은 한 문서 안에서 직무기술서와 직무명세서를 분리하고, 각 항목이 NCS 근거인지 조직 입력인지 추적한다. 렌더러, 저장 기능, 선택적 LLM은 이 계약을 공유한다.

## 최상위 구조

```json
{
  "schema": "ncs_job_profile_v1",
  "document_id": "uuid",
  "document_status": "draft",
  "created_at": "RFC3339 timestamp",
  "updated_at": "RFC3339 timestamp",
  "source_snapshot": {
    "mcp_url_label": "local-ncs-mcp",
    "retrieved_at": "RFC3339 timestamp",
    "selected_unit_codes": []
  },
  "scope_selection": {},
  "job_description": {},
  "person_specification": {},
  "references": [],
  "review_flags": []
}
```

MVP의 `document_status`는 항상 `draft`다.

## `scope_selection`

```json
{
  "organization_job_title": "채용 담당자",
  "classification_paths": [
    {
      "major_code": "02",
      "middle_code": "02",
      "small_code": "02",
      "sub_code": "01",
      "label": "경영·회계·사무 > 총무·인사 > 인사·조직 > 인사"
    }
  ],
  "included_units": [
    {
      "unit_code": "0202020103_23v4",
      "unit_name": "인력채용",
      "unit_level": "5",
      "selection_reason": "사용자 선택"
    }
  ],
  "excluded_unit_codes": [],
  "excluded_task_terms": [],
  "target_level_input": "조직 입력값 또는 null"
}
```

선택 사유는 사용자 선택, 검색 후보, 저장 문서 재사용 등을 구분한다. 자동 추천을 사용자 결정으로 표시하지 않는다.

## `job_description`

```json
{
  "job_title": {
    "text": "채용 담당자",
    "origin": "organization_input",
    "source_refs": []
  },
  "job_purpose": {
    "text": "조직 입력과 선택 NCS 범위를 병합한 초안",
    "origin": "mixed",
    "source_refs": ["ref-classification-1", "ref-unit-1"]
  },
  "duties": [
    {
      "id": "duty-1",
      "title": "인력채용",
      "summary": "선택한 능력단위 정의를 축약한 문장",
      "origin": "ncs_direct",
      "source_refs": ["ref-unit-1"]
    }
  ],
  "tasks": [
    {
      "id": "task-1",
      "duty_id": "duty-1",
      "title": "능력단위요소 기반 과업명",
      "description": "결정적 규칙으로 만든 초안",
      "origin": "ncs_direct",
      "source_refs": ["ref-element-1", "ref-criteria-1"]
    }
  ],
  "responsibilities": [],
  "decision_authority": [],
  "kpis": [],
  "collaboration": [],
  "reporting_relationships": []
}
```

`responsibilities`, `decision_authority`, `kpis`, `collaboration`, `reporting_relationships`는 기본적으로 `organization_input`이다. 비어 있으면 NCS에서 추정하지 않는다.

## `person_specification`

```json
{
  "target_level": {
    "organization_value": null,
    "ncs_level_references": ["5"],
    "mapping_status": "not_equated"
  },
  "knowledge": [],
  "skills": [],
  "attitudes": [],
  "experience_requirements": [],
  "education_requirements": [],
  "qualification_requirements": [],
  "preference_requirements": [],
  "qualification_references": [],
  "job_base_references": []
}
```

각 K/S/A 항목은 다음 형식이다.

```json
{
  "id": "ksa-1",
  "text": "원문 또는 의미를 바꾸지 않은 축약 표시",
  "type": "knowledge",
  "origin": "ncs_direct",
  "requiredness": "review_required",
  "source_refs": ["ref-ksa-1"]
}
```

NCS에 등장했다는 사실만으로 조직의 필수 요건이 되지 않는다. MVP는 `requiredness`를 자동으로 `required`로 만들지 않는다.

`qualification_requirements`와 `preference_requirements`는 공고문에 명시된 문장을 `organization_input`으로 보존한다. NCS 자격 참고인 `qualification_references`와 별도이며, LLM이 추가·삭제·승격할 수 없다.

## `references`

```json
{
  "ref_id": "ref-criteria-1",
  "source_system": "NCS_MCP",
  "source_type": "performance_criterion",
  "source_id": "criteria_id or stable locator",
  "unit_code": "0202020103_23v4",
  "element_id": "element identifier",
  "field": "criteria_text_raw",
  "evidence_grade": "direct",
  "text_preview": "검토용 제한 길이 원문",
  "retrieved_at": "RFC3339 timestamp"
}
```

허용 `source_type`:

- `classification`
- `competency_unit`
- `competency_element`
- `performance_criterion`
- `ksa_item`
- `career_path`
- `qualification`
- `job_base_competency`
- `organization_input`

허용 `evidence_grade`:

- `direct`: NCS 원천 분류·단위·요소·수행준거·원문 KSA
- `supporting`: 출처가 있지만 직무요건 확정에는 추가 판단 필요
- `reference`: 수집 범위 또는 연결 품질상 참고만 가능
- `organization_input`: 사용자가 조직 맥락으로 제공

## `review_flags`

```json
{
  "code": "organization_kpi_missing",
  "severity": "info",
  "section": "job_description.kpis",
  "message": "NCS는 조직 고유 KPI를 제공하지 않습니다. 조직 입력이 필요합니다.",
  "source_refs": []
}
```

초기 flag vocabulary:

- `scope_confirmation_required`
- `automatic_matching_rationale`
- `organization_purpose_missing`
- `organization_responsibility_missing`
- `organization_kpi_missing`
- `authority_missing`
- `reporting_relationship_missing`
- `ncs_level_not_equal_to_org_grade`
- `qualification_evidence_incomplete`
- `job_base_evidence_reference_only`
- `ksa_requiredness_review_required`
- `weak_or_missing_source_reference`
- `partial_unit_evidence`
- `llm_rewrite_not_applied`

## 재서술 계약

공개 자동 생성 API는 외부 AI 재서술을 호출하지 않고 결정적 초안을 그대로 사용한다. CLI 로그인 기반 보조 매핑은 업로드 양식의 필드명 연결에만 사용하며 JobProfile 또는 아래 재서술 필드를 입력으로 받거나 수정하지 않는다. 저장소의 실험용 재서술 구성요소는 다음 필드만 다룰 수 있도록 제한되어 있지만 기본 웹 앱에는 연결되지 않는다.

- `job_purpose.text`
- `duties[].summary`
- `tasks[].description`

제약:

- 객체 수와 ID 유지
- `source_refs` 유지
- 새로운 자격, 학력, 경력연수, 법적 의무, KPI 추가 금지
- K/S/A 유형 변경 금지
- 선택·제외 unit 변경 금지
- 원문과 의미가 충돌하면 재서술 결과 폐기

재서술 전 결정적 JSON을 기준본으로 보존한다. provider 응답은 `field_locator`, `original`, `suggestion`, `provider`를 가지며 원문의 정확한 locator가 일치할 때만 적용한다. 수치·URL·식별자·보호 용어가 달라지거나 새 사실이 추가되면 전체 적용을 거부한다. 로그인 자격증명과 계정 식별정보는 앱 계약에 포함하지 않는다.

팀 모드는 정확히 두 라운드만 수행한다. 각 제공자의 제안을 기존 재서술 검증기로 먼저 검증한 뒤 상대 제공자가 필드별 `accept/reject`와 고정 코드만 반환한다. 결정적 로컬 합의가 없으면 해당 필드는 기준본 원문을 유지한다. 팀 합의가 NCS 범위, 근거, 조직 자격조건 또는 문서 상태를 바꾸는 권한은 없다.

## 검증 불변조건

1. `origin=ncs_direct`이면 `source_refs`가 비어 있지 않다.
2. 모든 `source_refs`는 `references.ref_id`에 존재한다.
3. 제외된 unit과 task term은 결과에 포함되지 않는다.
4. `qualification_references`는 자동 필수 요건이 아니다.
5. NCS 수준과 조직 직급의 `mapping_status` 기본값은 `not_equated`다.
6. 생성 문서는 NCS 원천 review status를 변경하거나 승인 상태로 복사하지 않는다.
7. MVP document status는 `draft`다.
