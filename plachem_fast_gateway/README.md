# PLACHEM Fast Delegation Gateway

목표는 단순합니다.

**Odyssey는 `agent + 작업문 + workspace`만 전달합니다.**
TaskSpec, permission 문서, validation 계획서를 Odyssey가 매번 만들지 않습니다.

## 가장 간단한 사용법

PowerShell:

```powershell
.\delegate.ps1 `
  -Workspace "delegation-demo" `
  -Task "Hermes Agent Monitor의 System Status에 Last Gateway Run을 추가해. 초기값 NONE, Start Demo는 RUNNING, Complete Task는 VERIFIED, Reset은 NONE으로 복원. 기존 기능은 유지해."
```

또는 Python:

```powershell
python fast_gateway.py --workspace delegation-demo --agent achilles --task "작업 내용"
```

JSON 요청도 가능하지만 필수 필드는 3개뿐입니다.

```json
{
  "agent": "achilles",
  "workspace": "delegation-demo",
  "task": "작업 내용"
}
```

## Gateway가 자동으로 하는 일

1. workspace 범위를 확정
2. workspace의 최신 텍스트 파일을 자동으로 Context Pack으로 수집
3. 공통 정책/timeout/retry 자동 적용
4. Achilles 호출
5. Worker JSON을 엄격 검증
6. workspace 밖 artifact 차단
7. staging 후 atomic replace
8. 실패 시 rollback
9. PASS / FAIL / BLOCKED 반환
10. runs.jsonl에는 코드 전체가 아니라 SHA/경로/결과만 기록

## 의도적으로 뺀 것

이 버전은 위임 마찰을 줄이기 위한 Fast Lane입니다.

- Task마다 별도 Permission Package 작성 안 함
- Task마다 별도 Validation 프로그램 작성 안 함
- Git/PR/Merge/Deploy 자동화 안 함
- 승인 workflow 안 함
- Odyssey가 Gateway 기능을 테스트 도중 수정하지 않음

Git/Production처럼 위험한 작업은 별도 Controlled Lane으로 나중에 분리하는 것이 맞습니다.

## Task Auth Broker v2

`mock_auth_broker.py`는 다음 두 계층을 분리합니다.

- `AuthorizationBackend`: 인증 저장소가 구현해야 하는 교체 가능한 인터페이스
- `TaskAuthBroker`: Task/Worker/Action 검증, 만료·취소·서명·재사용 방지와 감사 기록을 담당하는 핵심 로직

현재 개발·테스트 Backend는 JSON 기반 `LocalTestStore`입니다. 향후 OpenClaw 통합 인증관리는 `AuthorizationBackend`를 구현하여 교체하며, Gateway의 집행 로직이나 Worker 계약은 변경하지 않습니다.

서명된 v2 인증은 Task ID, Worker, ALLOW/DENY, 만료시각, 취소 상태, Git 테스트 제약을 함께 검증합니다. 성공적으로 사용된 인증 ID는 다시 사용할 수 없습니다. 발급·거부·사용 결과는 JSONL 감사 로그로 남습니다. 서명키·서비스 비밀번호·API Key는 WorkerResult나 Worker prompt에 전달하지 않습니다.

기존 `tasks` 형식 Mock 인증 데이터는 호환성을 위해 계속 읽을 수 있습니다. 새 v2 인증은 `schema_version: 2` Local Test Store를 사용합니다.
