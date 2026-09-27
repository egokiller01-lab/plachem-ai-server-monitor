# War Room Project Document Registry

## 목적

War Room의 공식 문서 소유자는 Agent나 Session이 아니라 Project다.
Agent와 Session은 문서를 생성하거나 수정한 실행 주체로만 기록한다.
Session이 종료되거나 교체되어도 Project 문서와 버전 이력은 유지되어야 한다.

## 저장 원칙

실제 파일은 Git 저장소, 프로젝트 worktree 또는 승인된 파일 경로에 그대로 둔다.
War Room SQLite에는 파일 자체를 BLOB으로 저장하지 않고 관리정보만 기록한다.

- Project ID / Document ID
- 문서 분류와 상태 / 현재 Version ID
- 파일 URI / SHA-256 / 크기 / MIME type
- 관련 Task / 작성 Agent / 작성 Session / 실행 Run
- 요약과 생성시각

## 데이터 모델

### war_documents

문서의 논리적 정체성과 현재 버전을 관리한다. 분류는 requirements, architecture, decision, reference, report, handoff, other만 허용한다.

### war_document_versions

문서의 물리 버전을 append-only로 보존한다. URI, SHA-256, 크기, MIME type, Task/Agent/Session/Run provenance를 기록한다. 기존 Version row는 UPDATE/DELETE 할 수 없다.

### war_document_links

Document와 Task의 관계를 input, output, reference, decision, handoff 중 하나로 append-only 보존한다.

## Evidence와 Document의 구분

Document는 프로젝트 지식과 산출물이다. Evidence는 작업 완료와 QA 판정을 입증하는 증거다.

- 01-ARCHITECTURE.md → Document
- 요구사항/결정/보고서 → Document
- pytest 결과 → Evidence
- QA screenshot → Evidence
- 해시 검증 파일 → Evidence

같은 파일이 작업 산출물이면서 QA Evidence로도 사용될 수 있지만 두 Registry의 의미와 lifecycle은 분리한다.

## 버전 규칙

기존 문서를 수정할 때 document_id와 expected_version을 전달할 수 있다.
현재 버전이 expected_version과 다르면 409 Version Conflict로 거부한다.
동일 URI와 동일 SHA-256이면 새 버전을 만들지 않고 no-op 처리한다.
내용이 변경되면 다음 정수 버전을 append-only로 추가하고 current_version_id만 갱신한다.

## 경로 안전성

문서 Registry는 서버가 승인한 worktree/approved_paths 내부의 실제 파일만 허용한다.

- 절대경로 필수
- .. traversal 금지
- 파일 존재 필수
- 디렉터리 등록 금지
- symlink realpath escape 차단
- 문서형 확장자 allowlist 적용

클라이언트나 Worker가 임의의 approved root를 추가할 수 없다.

## Worker 자동 등록

일반 Worker는 최종 structured result에 선택적으로 documents 배열을 반환할 수 있다. 각 항목은 title, category, path, action(create/update), summary와 선택적 document_id, expected_version, relation을 사용한다.

FastGateway의 기존 strict result contract는 변경하지 않는다. FastGateway artifacts 중 문서형 파일만 Worker가 자동으로 Project Document Registry에 투영한다.

자동 등록 실패는 작업 결과를 변경하지 않고 document_registration_rejected audit event로 남긴다.

## Task Context 주입

Task prepare 시 immutable grounding packet에는 최대 12개의 compact Project Document metadata를 포함할 수 있다.

- document_id
- title
- category
- version
- uri
- sha256
- summary

grounding.required_document_ids를 지정하면 해당 문서만 넣으며, 존재하지 않는 Document ID가 있으면 Task prepare를 거부한다.

새 Agent/Session은 과거 전체 대화를 읽는 대신 현재 Task에 필요한 프로젝트 문서만 읽을 수 있다.

## API

- GET /api/war-room/projects/{project_id}/documents
- GET /api/war-room/documents/{document_id}
- GET /api/war-room/documents/{document_id}/versions
- POST /api/war-room/projects/{project_id}/documents/register

Document detail GET은 Document → Project → participant 관계로 read permission을 검증한다.
문서 등록은 Project manage permission과 기존 idempotency 경계를 사용한다.

## UI

War Room 상단에 프로젝트 문서 화면을 제공한다.

- Project 문서 목록
- 분류/검색
- Task 연결
- 신규 문서 Registry 등록
- 기존 문서 새 버전 등록
- Version History 조회

Agent별 문서함은 만들지 않는다.

## 기존 canonical 문서 초기 등록

운영 서버 코드 배포 후 실제 production worktree에서 아래처럼 실행한다.

    python3 war_room_seed_documents.py --db ~/.openclaw/war-room/war_room.sqlite3 --worktree /actual/repository/worktree

도구는 docs/openclaw-project/*.md를 등록하며 동일 파일 재실행은 no-op이므로 idempotent하다.

## 운영/복구

War Room startup의 war_room.provision_database()가 Document Registry schema도 함께 provision한다.
DB 재시작 후에도 Registry는 SQLite에 보존된다.
실제 문서는 Git/filesystem 원본을 사용하므로 SQLite backup과 파일/Git backup은 별도로 유지한다.

Project가 archived 상태가 되면 새 문서/버전 등록을 거부하고 기존 문서는 read-only로 유지한다.
