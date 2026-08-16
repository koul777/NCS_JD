# Windows 휴대용 배포

정식 Windows 산출물은 `NCS_JD.exe` 한 파일이 아니라 앱, 독립 NCS MCP 사이드카, 읽기 전용 serving DB를 같은 상대 경로에 둔 ZIP이다. 사용자는 Python, Node.js, NCS_MCP 소스 저장소를 별도로 설치하지 않는다.

```text
NCS_JD-windows-x64-v0.1.4/
├─ NCS_JD.exe
├─ NCS_MCP/
│  ├─ NCS_MCP.exe
│  ├─ _internal/
│  └─ data/
│     ├─ ncs_jd_serving.db
│     └─ ncs_jd_serving.report.json
├─ examples/
│  ├─ administrative-support-announcement.txt
│  └─ ncs-jd-supported-template.hwpx
└─ README_FIRST.txt
```

## 런타임 경계

`NCS_JD.exe`는 NCS SQLite를 직접 열거나 NCS_MCP 내부 Python 모듈을 import하지 않는다. 앱은 HTTP/MCP로만 `NCS_MCP.exe`에 접근하고, 사이드카만 `NCS_DB_PATH`로 serving DB를 읽는다.

```text
브라우저 → NCS_JD.exe → 127.0.0.1 HTTP/MCP → NCS_MCP.exe → read-only serving DB
```

- 앱 기본 포트: `127.0.0.1:8000`
- MCP 기본 포트: `127.0.0.1:8766`
- `NCS_MCP_READ_ONLY=1`
- `NCS_MCP_ENABLE_OPERATOR_TOOLS=0`
- 전체 12GB+ canonical DB가 아닌 JD 조회용 약 117MB 파생본만 배포
- 기본 로컬 모드는 외부 AI 자격증명과 API 키를 요구하거나 포함하지 않음
- 어떤 경로에서도 사용자에게 API 키나 토큰을 요구하지 않음. AI 기능은 공식 CLI 자체 구독 로그인만 사용

## 사용자 실행

1. GitHub Release의 Windows ZIP을 내려받는다.
2. ZIP을 완전히 압축 해제한다. ZIP 안에서 직접 실행하지 않는다.
3. `NCS_JD.exe`를 실행한다.
4. 브라우저에서 공고문과 선택적인 예시 양식을 올린다.
5. 기본값인 `로컬 전용`으로 결정적 로컬 모드를 실행한다.
6. 표준 명칭과 크게 다른 양식은 CLI가 로그인되어 있으면 자동으로 보조 매핑을 시도한다.

런처는 인접한 `NCS_MCP\NCS_MCP.exe`와 `NCS_MCP\data\ncs_jd_serving.db`를 먼저 찾는다. 개발 환경에서는 `NCS_MCP_ROOT` 또는 형제 `NCS_MCP` 소스 저장소의 `.venv\Scripts\python.exe`를 사용한다.

진단은 `NCS_JD.exe --diagnostics`, readiness 검사는 `NCS_JD.exe --check-only --no-browser`로 수행한다. 로그는 우선 `%LOCALAPPDATA%\NCS_JD\logs\launcher.log`과 `ncs_mcp.log`에 기록하고, 쓸 수 없으면 `%TEMP%\NCS_JD\logs`로 전환한다.

| 종료 코드 | 의미 |
| ---: | --- |
| 0 | 정상 종료 또는 점검 통과 |
| 10 | 같은 앱이 이미 실행 중 |
| 20 | 앱 포트 충돌 |
| 30 | 포함 Node/Kordoc 점검 실패 |
| 40 | MCP 또는 DB 준비 실패 |
| 41 | MCP 읽기 전용 안전 계약 실패 |
| 50 | 웹 서버 또는 예기치 않은 런처 실패 |

## 양식 지원 계약

렌더러가 지원하는 표준 대상은 다음 13개 필드다.

`채용분야`, `대분류`, `중분류`, `소분류`, `세분류`, `능력단위`, `직무수행내용`, `필요지식`, `필요기술`, `직무수행태도`, `필요자격`, `직업기초능력`, `비고/근거`

빈 HWPX는 대상 항목을 안전하게 단일 매칭할 수 있을 때 원본 구조를 보존해 채운다. 값이 채워진 HWP·HWPX와 PDF 예시는 필드명을 새 값에 매핑하고 기존 예시값과 예시 고유 문구를 제거한 뒤 HWPX로 재구성한다. 표준 별칭 규칙이 먼저 적용되며, CLI가 로그인되어 있을 때만 필드명 연결을 보완한다. 최소 두 필드와 `직무수행내용`을 식별하지 못하거나 중복·모호한 매핑이면 `template_not_applied`로 중단한다. 양식이 없을 때만 표준 HWPX를 만든다. `examples/ncs-jd-supported-template.hwpx`가 실제 지원 예시다.

CLI 보조 매핑은 필드명과 최대 300자의 기존 예시값만 전송한다. 자격은 공식 CLI 자체 저장소에만 있으며 이 프로그램은 토큰·키 환경변수를 만들지도, 자식 프로세스에 넘기지도, 기록하지도 않는다. JobProfile, 생성 문장, NCS 근거는 보내지 않으며 `X-NCS-JD-Generation-Mode`는 이 경로에서 항상 `deterministic`이다.

## 재현 가능한 빌드

앱 빌드 환경에는 NCS JD의 `web,mcp` 의존성과 PyInstaller 6.x가, 사이드카 빌드 환경에는 NCS_MCP의 선언 의존성이 설치되어 있어야 한다. Kordoc은 4.2.9로 고정한다.

```powershell
python -m pip install -e ".[web,mcp,dev]"
python -m pip install "pyinstaller>=6.18,<7"
npm ci
node .\scripts\generate_supported_template.mjs
.\scripts\package_windows_portable.ps1 `
  -PythonExecutable .\.venv-build313\Scripts\python.exe `
  -NcsMcpRoot C:\workspace\NCS_MCP
```

`package_windows_portable.ps1`은 다음을 수행한다.

1. `NCS_JD.exe` one-file 빌드
2. NCS_MCP를 별도 onedir `NCS_MCP.exe`로 빌드
3. canonical DB에서 필요한 7개 표만 serving DB로 export
4. 예제와 사용자 안내를 포함한 휴대용 폴더 구성
5. ZIP과 `.sha256` 생성

`build/`, `release/`, `NCS_JD.exe`, serving DB와 ZIP은 Git 추적 대상이 아니다. 소스·테스트·작은 예제만 커밋하고 바이너리는 GitHub Release 자산으로 게시한다.

## 배포 전 검증

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
python -m pytest
.\scripts\smoke_windows_exe.ps1 `
  -Executable release\NCS_JD-windows-x64-v0.1.4\NCS_JD.exe `
  -McpPort 18771
.\scripts\smoke_windows_portable_e2e.ps1
```

두 번째 smoke는 휴대용 폴더의 앱과 MCP를 별도 포트에서 시작하고, 포함된 행정지원 공고문·HWPX 양식과 `provider=off`로 자동 생성 API를 호출한다. 응답에서 `X-HWPX-Template-Used: true`, `hwpx-preserve`, `X-NCS-JD-Generation-Mode: deterministic`, 외부 AI 미사용을 검사하고 결과 HWPX가 생성됐는지 확인한다. CLI 보조 매핑 경로는 실제 로그인 없이 모의 어댑터로 스키마·비밀값 비노출·오류 처리를 검증한다.

## 공개 정책

코드 저장소와 생성 바이너리는 분리한다. NCS 기준정보 공개 API는 공공데이터포털에서 이용허락범위를 제한 없음으로 안내하지만, serving DB에는 API별 공개 범위와 다른 필드가 섞일 수 있다. 저장소를 Public으로 전환하거나 DB 자산을 공개 Release로 배포하기 전에는 포함 필드별 출처와 NCS 저작권정책을 재확인한다. 확인 전에는 비공개 저장소 Release로만 전달한다.
