# 샘플 및 품질검증 산출물

- `administrative-support-announcement.txt`: 휴대용 배포 E2E에서 사용하는 가상 행정지원 공고문 입력 예시
- `ncs-jd-supported-template.hwpx`: 12개 지원 필드를 포함하고 `hwpx-preserve`로 실제 채워지는 빈 HWPX 양식 예시

이 디렉터리에는 실제 채용공고문이나 그 공고문에서 생성한 산출물을 두지 않는다. 회귀 검증에 공고문 입력이 필요하면 위 가상 예시처럼 기관·부서·사업명을 식별할 수 없는 문장으로 새로 작성한다.

능력단위 근거가 들어가는 fixture는 `FIXTURE-NOT-NCS-`로 시작하는 합성 코드를 쓴다. 실제 NCS 코드가 아니며, 운영 생성에서는 읽기 전용 NCS MCP의 `ncs_search`와 `ncs_unit_detail` 결과로 대체된다. 이 시연본은 공식 직무요건, 채용 결정 또는 자격 인정 자료가 아니다.
