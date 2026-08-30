# PLACHEM Agent Control 프로젝트 종합 기획 보고서

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**작성 목적:** 오디세이를 Main, 아킬레스를 Worker로 운영하는 범용 멀티에이전트 시스템의 개요, 구조, 운영 원칙, 개발 범위, 실행계획 및 검증 기준을 확정한다.

**프로젝트명:** PLACHEM Agent Control

**권장 프로젝트 위치:** `C:\Users\egomine2\PLACHEM-Agent-Control`

---

## 1. 프로젝트 개요

PLACHEM Agent Control은 특정 업무를 자동화하는 단일 프로그램이 아니다.

김대표님의 **어떤 자연어 지시든 하나의 공통 프로세스로 접수하고**, 오디세이가 작업을 설계한 뒤 아킬레스에게 실행시키며, 코드가 전체 작업 과정의 범위·권한·순서·시간·중단·검증을 관리하는 **범용 AI 업무 위임 시스템**이다.

```text
김대표님의 자연어 지시
        ↓
오디세이: 의도 해석·계획·위험 판단
        ↓
Agent Control: 작업계약·권한·상태·한도 관리
        ↓
아킬레스: 실제 조사·분석·코딩·도구 실행
        ↓
Agent Control: 결과·증거 수집
        ↓
오디세이: 독립 검증·후속 판단
        ↓
김대표님께 최종 보고 또는 승인 요청
```

### 핵심 원칙

> **오디세이가 판단하고, 아킬레스가 작업하며, 코드는 전체 프로세스를 통제한다.**

- AI는 판단·분석·계획·문제 해결을 담당한다.
- 코드는 상태·순서·권한·제한·중단·검증을 담당한다.
- 작업 종류마다 별도 코드를 만들지 않는다.
- 모든 지시는 동일한 프로세스를 사용하고 TaskSpec과 Permission Package만 달라진다.

---

## 2. 프로젝트가 해결하려는 문제

현재 Main이 Worker에게 자유문장으로 직접 지시하면 다음 문제가 발생할 수 있다.

1. 지시가 짧아 아킬레스가 목적과 기준을 오해한다.
2. 현재 프로젝트 상태나 최신 자료가 전달되지 않는다.
3. 작업 범위를 벗어나거나 불필요한 변경을 한다.
4. 같은 실패를 반복하며 시간과 GPU 자원을 소비한다.
5. 완료되지 않은 상태를 완료로 보고한다.
6. 작업 결과와 근거가 일정한 형식으로 남지 않는다.
7. Production·배포·삭제 등 위험 작업을 프롬프트로만 통제하게 된다.
8. 아킬레스와 ComfyUI가 RTX 3090을 동시에 사용해 시스템이 불안정해질 수 있다.
9. 김대표님이 작업 과정 전체를 직접 확인해야 한다.

이 프로젝트는 모델을 바꾸는 것이 아니라 **AI가 일하는 운영체계 자체를 코드화**하여 문제를 해결한다.

---

## 3. 목표와 기대효과

### 3.1 최종 목표

김대표님이 오디세이에게 평소처럼 자연어로 지시하면 다음 과정이 자동으로 이루어지게 한다.

1. 지시의 목표와 완료조건 파악
2. 필요한 작업 분해
3. 아킬레스 수행 가능 여부 판단
4. 필요한 정보와 권한 구성
5. 아킬레스 작업 실행
6. 진행 상태와 반복 횟수 관리
7. 결과와 증거 수집
8. 오디세이 독립 검증
9. 후속 작업 또는 최종 보고

### 3.2 기대효과

- 아킬레스 로컬 LLM을 실제 실무 Worker로 활용
- 클라우드 Main 모델의 반복 실행 부담 절감
- 로컬 GPU 자원의 계획적 사용
- 작업 누락과 무한 반복 감소
- 위험 작업의 승인 절차 명확화
- 결과 품질과 보고 형식 통일
- 작업 과정과 근거 추적 가능
- 향후 ERPcoder, Researcher 등 Worker 확장 기반 확보

---

## 4. 현재 구성과 활용 방식

### 오디세이(Main)

- Hermes 기본 프로필
- 모델: `gpt-5.6-sol`
- Provider: `openai-codex`
- 담당: 사용자 소통, 판단, 계획, 위험분류, 검증, 최종 보고

### 아킬레스(Worker)

- Hermes 별도 프로필: `achilles`
- 모델: `Qwen3.8-27B-Uncensored-Q4_K_M.gguf`
- API: `http://127.0.0.1:8080/v1`
- 컨텍스트: 80K 설정
- 담당: 조사, 분석, 문서 초안, 코드 작성, 테스트, 허용된 도구 실행

### 하드웨어

- RTX 3090: 아킬레스 로컬 LLM 및 CUDA 작업
- RTX 3070: 49인치 모니터 디스플레이
- ComfyUI도 RTX 3090을 사용하므로 자원 충돌 관리 필요

### 아킬레스 실행 방식

Hermes의 일반 `delegate_task`는 별도 `achilles` 프로필 전체를 선택하는 공식 프로필 인자가 현재 공식 문서에서 확인되지 않았다. 따라서 V1에서는 Gateway가 다음 명령을 안전하게 구성하고 실행한다.

```bash
hermes -p achilles chat \
  --query-file <작업지시파일> \
  --source tool \
  --toolsets <작업별 허용도구> \
  --max-turns <제한값> \
  --run-budget <시간제한> \
  --quiet
```

---

## 5. 역할과 책임

### 5.1 김대표님 — 최종 승인권자

- 일반 지시는 자연어로 전달한다.
- Production, 배포, merge, 삭제, 자격증명 사용 등 중요 작업을 최종 승인한다.
- 기술적인 중간 과정을 모두 검토하지 않고 핵심 결정만 내린다.

### 5.2 오디세이 — Main·Planner·Supervisor·Verifier

- 김대표님의 지시를 하나의 측정 가능한 목표로 정규화한다.
- 필요하면 여러 개의 순차 작업으로 분리한다.
- 위험도와 필요한 권한을 결정한다.
- TaskSpec과 Context Pack을 생성한다.
- 아킬레스 작업 결과를 그대로 믿지 않고 직접 검증한다.
- 후속 작업, 재시도, 승인 요청 또는 종료를 결정한다.
- 김대표님께 핵심만 간단하게 보고한다.

### 5.3 Agent Control — 범용 프로세스 엔진

- TaskSpec 형식 검증
- Worker 능력 확인
- 권한·금지사항 판정
- 실행 환경 선택
- 작업 상태 관리
- 최대 단계·재시도·시간 제한
- GPU 자원 잠금
- 아킬레스 프로세스 실행·중지
- 결과 스키마 검증
- 증거와 감사 로그 저장
- 승인 필요 상태 관리

### 5.4 아킬레스 — Worker

- 전달받은 작업 범위 안에서 실제 작업을 수행한다.
- Context Pack에 포함된 최신 정보만 기준으로 사용한다.
- 임의로 목표와 범위를 확대하지 않는다.
- 필요한 권한이 없거나 정보가 충돌하면 중단하고 보고한다.
- 결과, 변경내용, 검사결과, 증거, 남은 위험을 구조화하여 반환한다.
- 김대표님에게 직접 판단을 요구하지 않고 오디세이에게 반환한다.

---

## 6. 범용 작업 처리 프로세스

### 6.1 입력

김대표님은 별도 형식을 맞출 필요 없이 평소처럼 지시한다.

예:

- “회사 홈페이지의 기술자료 구조를 개선해줘.”
- “이 코드를 검토하고 오류를 수정해줘.”
- “회사 서버 상태를 점검해줘.”
- “시장 자료를 조사해서 보고서를 작성해줘.”
- “이 이미지를 기반으로 영상을 만들어줘.”

### 6.2 오디세이의 작업 컴파일

오디세이는 자연어 지시를 다음 항목으로 변환한다.

- 단일 목표
- 대상과 범위
- 제외 범위
- 위험도
- 실행 환경
- 필요한 도구와 권한
- 금지 작업
- 단계·재시도·시간 제한
- 완료조건
- 필요한 증거
- 중단 및 승인 조건

### 6.3 Gateway 판정 결과

모든 지시는 다음 네 상태 중 하나로 결정된다.

| 상태 | 의미 |
|---|---|
| `READY` | 아킬레스가 바로 실행 가능 |
| `NEEDS_INFO` | 작업에 필요한 정보가 부족함 |
| `NEEDS_APPROVAL` | 위험 작업이므로 김대표님 승인 필요 |
| `DENIED` | 정책상 아킬레스가 실행할 수 없음 |

### 6.4 실행

- Gateway가 TaskSpec과 Context Pack을 아킬레스용 작업지시 파일로 변환한다.
- 작업 위험도에 맞는 도구만 허용한다.
- 별도 프로세스로 `achilles` 프로필을 실행한다.
- 상태·시간·반복·GPU 자원을 감시한다.

### 6.5 결과와 검증

- 아킬레스는 정해진 Result JSON을 반환한다.
- Gateway는 결과 형식과 증거 존재 여부를 검사한다.
- 오디세이는 파일, diff, 테스트, 서버 상태 등 실제 대상을 직접 확인한다.
- 검증을 통과해야만 작업을 완료 처리한다.

---

## 7. TaskSpec과 Result 계약

### 7.1 TaskSpec

TaskSpec은 오디세이와 아킬레스 사이의 작업계약이다.

필수 내용:

```json
{
  "task_id": "고유 작업번호",
  "objective": "하나의 측정 가능한 목표",
  "agent": "achilles",
  "risk": "low|medium|high|critical",
  "environment": "local|development|staging|production",
  "scope": {
    "include": [],
    "exclude": []
  },
  "permissions": [],
  "deny": [],
  "limits": {
    "max_steps": 12,
    "max_retries": 1,
    "timeout_seconds": 900
  },
  "completion": [],
  "evidence": [],
  "escalation": {}
}
```

### 7.2 Result

아킬레스는 자유로운 장문 보고 대신 다음 구조로 결과를 반환한다.

```json
{
  "task_id": "동일 작업번호",
  "status": "completed|blocked|failed|partial",
  "summary": "짧은 사실 보고",
  "changes": [],
  "checks": [],
  "evidence": [],
  "permission_use": [],
  "production_changes": 0,
  "remaining_risks": [],
  "next_action": "none|review|approve|retry|escalate"
}
```

---

## 8. 권한과 위험 관리

### 8.1 위험등급

| 등급 | 작업 예시 | 처리 원칙 |
|---|---|---|
| LOW | 조사, 읽기, 비교, 로그 분석 | 자동 실행 가능 |
| MEDIUM | 로컬 코드 수정, 테스트, 문서 작성 | 격리된 작업공간에서 실행 |
| HIGH | DB 변경안, 서버 설정안, 배포 준비 | 아킬레스는 초안까지만, 승인 필요 |
| CRITICAL | Production 변경, merge, deploy, 삭제, 비밀값 사용 | 아킬레스 실행 금지, 김대표님 명시 승인 |

### 8.2 기본 보안 원칙

- 기본값은 허용이 아니라 거부(`deny-by-default`).
- 필요한 최소 권한만 작업마다 제공한다.
- Production URL과 비밀값은 아킬레스 Context Pack에 포함하지 않는다.
- `merge`, `deploy`, `production`, `destructive_delete`, `secrets_export`, `permission_change`는 기본 금지한다.
- 프로필 분리는 보안 샌드박스가 아니므로 파일·도구·네트워크 권한을 별도로 제한한다.
- 외부 시스템 변경은 오디세이가 김대표님 승인 후 수행하고 다시 읽어 확인한다.

### 8.3 코드 수정 정책

- LOW: 읽기 전용
- MEDIUM: 별도 Git worktree에서만 수정
- 원본 `main/master` branch 직접 수정 금지
- 실행 전후 diff 수집
- TaskSpec 범위 밖 변경 발견 시 실패 처리
- 테스트와 검증을 통과하기 전 병합 금지

---

## 9. 상태와 반복 관리

작업 상태는 코드가 관리한다.

```text
RECEIVED
→ COMPILED
→ VALIDATED
→ READY / NEEDS_INFO / NEEDS_APPROVAL / DENIED
→ RUNNING
→ VERIFYING
→ COMPLETED / BLOCKED / FAILED / PARTIAL
```

중단 조건:

- 최대 단계 초과
- 최대 재시도 초과
- 시간 제한 초과
- 동일 실패 반복
- 작업 범위 확대 필요
- 금지 권한 필요
- Context Pack 정보 충돌
- 증거 생성 실패
- 아킬레스 프로세스 비정상 종료

재시도는 같은 행동을 반복하는 것이 아니라 새로운 근거나 다른 접근이 있을 때만 허용한다.

---

## 10. RTX 3090 자원 관리

아킬레스와 ComfyUI가 RTX 3090을 공유하므로 Agent Control에 GPU Resource Guard를 포함한다.

확인 항목:

- 아킬레스 API `127.0.0.1:8080` 상태
- ComfyUI API `127.0.0.1:8188` 대기열
- RTX 3090 VRAM·사용률·온도·전력
- `gpu3090.lock` 단일 실행 잠금

기본 정책:

- ComfyUI 영상·대형 이미지 생성 중에는 아킬레스 작업을 대기시킨다.
- 아킬레스 작업 중에는 ComfyUI의 대형 작업을 시작하지 않는다.
- GPU 메모리가 부족하면 작업을 시작하지 않는다.
- LLM 서버가 비정상일 때 자동 재시작하지 않고 오디세이에게 반환한다.
- GPU 임계값은 실제 파일럿 측정 후 확정한다.

---

## 11. 시스템 구성

```text
C:\Users\egomine2\PLACHEM-Agent-Control\
├─ README.md
├─ pyproject.toml
├─ config\
│  ├─ agents.yaml
│  └─ project-policy.yaml
├─ schemas\
│  ├─ taskspec.schema.json
│  └─ result.schema.json
├─ gateway\
│  ├─ models.py
│  ├─ compiler.py
│  ├─ policy.py
│  ├─ context_pack.py
│  ├─ resource_guard.py
│  ├─ achilles_runner.py
│  ├─ verifier.py
│  ├─ state.py
│  ├─ audit.py
│  └─ cli.py
├─ templates\
│  └─ achilles-task.md
├─ runtime\
│  ├─ tasks\
│  ├─ results\
│  ├─ evidence\
│  ├─ locks\
│  └─ audit.jsonl
└─ tests\
   ├─ test_models.py
   ├─ test_compiler.py
   ├─ test_policy.py
   ├─ test_context_pack.py
   ├─ test_resource_guard.py
   ├─ test_achilles_runner.py
   ├─ test_verifier.py
   ├─ test_state.py
   └─ test_e2e_smoke.py
```

### 핵심 모듈

| 모듈 | 역할 |
|---|---|
| `compiler.py` | 자연어 요구사항을 TaskSpec으로 변환·보완 |
| `models.py` | TaskSpec과 Result 데이터 모델 |
| `policy.py` | 위험도, 권한, 금지사항 판정 |
| `context_pack.py` | 아킬레스에게 전달할 최신 정보 구성 |
| `resource_guard.py` | RTX 3090과 ComfyUI 충돌 방지 |
| `achilles_runner.py` | `achilles` 프로필 실행·중지·출력 수집 |
| `verifier.py` | 결과와 실제 증거 대조 |
| `state.py` | 작업 생명주기와 승인 상태 관리 |
| `audit.py` | 잠금·원자적 쓰기와 hash-chain 작업 이력(외부 trusted head 없이는 삭제/재작성 탐지 불가) |
| `cli.py` | 전체 프로세스의 단일 실행 진입점 |

---

## 12. 구현 실행계획

### 1단계: 기반과 작업계약

**목표:** 어떤 지시든 동일한 구조로 처리할 수 있는 기반 구축

작업:

1. 프로젝트와 pytest 환경 생성
2. TaskSpec·Result 모델 및 JSON Schema 작성
3. 작업 상태 모델 작성
4. `agents.yaml`에 아킬레스 능력과 제한 정의
5. `project-policy.yaml`에 공통 허용·금지 규칙 정의
6. 정상·누락·위험 TaskSpec 테스트

완료 기준:

- 어떤 작업이든 TaskSpec으로 표현 가능
- 잘못된 계약은 실행 전에 거부
- Production·merge·deploy 요청 자동 차단

### 2단계: 범용 Gateway 엔진

**목표:** TaskSpec에 따라 실행·보류·승인·거부를 결정

작업:

1. 자연어→TaskSpec Compiler 인터페이스
2. Policy Engine
3. Context Pack Builder
4. 상태 전이 엔진
5. 시간·단계·재시도 제한
6. 감사 로그와 증거 저장
7. CLI `run/status/verify/cancel/approve` 구현

완료 기준:

- 서로 다른 종류의 지시가 같은 코드 경로를 사용
- 작업별로 권한과 제한만 달라짐
- 모든 상태 변화가 기록됨

### 3단계: 아킬레스 실제 연동

**목표:** Gateway가 별도 `achilles` 프로필을 실제 Worker로 실행

작업:

1. `--query-file` 기반 작업지시 생성
2. 작업 위험도별 toolset 제한
3. 프로세스 timeout 및 하위 프로세스 종료
4. 출력 크기 제한과 Result JSON 추출
5. 아킬레스 비정상 응답·형식 오류 처리
6. 실제 LOW 위험 작업 Smoke Test

완료 기준:

- RTX 3090 로컬 Qwen 모델로 실제 응답
- timeout 시 잔여 프로세스 없음
- Result 형식이 맞지 않으면 완료 처리하지 않음

### 4단계: 검증과 GPU 보호

**목표:** Worker의 자기보고를 독립적으로 확인하고 자원 충돌 방지

작업:

1. 증거 기반 Verifier
2. 파일 변경 전후 diff 검증
3. 테스트·빌드 결과 검증
4. 외부 쓰기 read-back 검증 구조
5. RTX 3090 Resource Guard
6. ComfyUI 동시 작업 차단

완료 기준:

- 증거 없는 `completed` 결과 거부
- 범위 밖 변경 탐지
- ComfyUI와 아킬레스 대형 작업 동시 실행 방지

### 5단계: 운영 스킬과 파일럿

**목표:** 오디세이가 모든 아킬레스 위임에서 Gateway를 일관되게 사용

작업:

1. `plachem-odyssey-achilles` Hermes 스킬 작성
2. Gateway 우회 금지 절차 정의
3. 읽기·조사 파일럿
4. 문서 생성 파일럿
5. Git worktree 코드 수정 파일럿
6. 실패·timeout·승인 필요 상황 시험
7. 결과 측정 및 정책 보정

완료 기준:

- 오디세이가 아킬레스에게 직접 지시하지 않음
- 모든 작업에 TaskSpec·Result·Evidence 존재
- 성공·차단·실패가 일관된 형식으로 보고됨

---

## 13. 시험 시나리오

### 시험 1: 읽기 전용 분석

- 지정 파일 구조와 문제 후보 조사
- 파일 수정 및 네트워크 금지
- 근거 파일과 라인 제출

### 시험 2: 문서 작성

- 제공된 자료만 사용해 정해진 경로에 문서 생성
- 생성 파일을 오디세이가 직접 읽어 검증

### 시험 3: 제한된 코드 수정

- 별도 Git worktree 생성
- 지정 파일 한정 수정
- 테스트 실행
- 범위 밖 변경 검사

### 시험 4: 위험 작업 차단

- Production 변경 또는 deploy가 필요한 지시 입력
- 아킬레스 실행 전 `NEEDS_APPROVAL` 또는 `DENIED` 확인

### 시험 5: 실패 반복 중단

- 의도적으로 실패하는 작업 입력
- 재시도 제한 후 `BLOCKED` 반환 확인

### 시험 6: GPU 충돌

- ComfyUI 실행 중 아킬레스 작업 요청
- Gateway가 실행을 대기 또는 차단하는지 확인

---

## 14. V1 성공 기준

V1은 다음 조건을 모두 만족해야 한다.

1. 어떤 자연어 지시든 공통 프로세스로 접수할 수 있다.
2. 모든 실행 작업은 TaskSpec으로 변환된다.
3. Worker는 항상 실제 `achilles` 프로필로 실행된다.
4. 작업 종류가 바뀌어도 별도 실행 코드를 만들지 않는다.
5. 위험도에 따라 권한과 승인 절차가 자동 결정된다.
6. Production·merge·deploy는 승인 없이 실행되지 않는다.
7. 작업의 단계·재시도·시간이 제한된다.
8. 아킬레스 결과는 오디세이가 독립 검증한다.
9. ComfyUI와 아킬레스의 RTX 3090 충돌을 방지한다.
10. 김대표님께는 핵심 결과와 필요한 결정만 간단히 보고한다.

---

## 15. V1 범위와 제외사항

### 포함

- 오디세이 Main
- 아킬레스 단일 Worker
- 모든 자연어 지시의 공통 접수
- TaskSpec·Result 계약
- 정책·권한·상태·제한·검증
- RTX 3090 자원 보호
- 로컬·개발 환경 작업
- 승인 대기 구조

### 제외

- 여러 Worker 중 자동 선택
- 아킬레스의 하위 Agent 재위임
- 복잡한 DAG 자동 생성
- Production 자동 변경
- 자동 merge·deploy
- 비밀값 자동 전달
- 검증되지 않은 완전 자율 운영

제외 기능은 범용 구조를 제한하는 것이 아니라 V1의 운영 위험을 제한하기 위한 것이다.

---

## 16. V2 확장 방향

V1 실작업 결과를 확인한 뒤 다음 기능을 추가할 수 있다.

- Hermes Kanban 기반 지속 작업 큐
- Researcher·ERPcoder·ERPqa 등 Worker 추가
- 작업 성격에 따른 Worker 자동 선택
- 프로젝트별 최신 문서·Artifact Registry
- 자동 작업 분해와 의존성 DAG
- 반복 패턴 감지와 자동 승격
- Reviewer 전용 Agent
- 서버·DB·배포를 위한 별도 승인 실행기
- 작업 통계와 품질 대시보드

---

## 17. 주요 위험과 대응

| 위험 | 대응 |
|---|---|
| 아킬레스의 지시 오해 | 명확한 TaskSpec과 Context Pack |
| 로컬 모델의 무한 반복 | 단계·재시도·시간 제한 |
| 완료 허위 보고 | 오디세이 독립 검증 |
| 범위 밖 파일 변경 | worktree와 diff 검사 |
| Production 접근 | 자격정보 미제공, toolset 제한, 승인 게이트 |
| 프로필을 보안경계로 오해 | 별도 정책과 실행 제한 적용 |
| RTX 3090 VRAM 충돌 | Resource Guard와 단일 GPU 잠금 |
| 장문 보고 증가 | Result Schema와 간단 최종 보고 |
| 오래된 자료 사용 | Context Pack에 권위 있는 현재 자료만 포함 |

---

## 18. 운영 예시

김대표님 지시:

> “PLACHEM 홈페이지의 기술자료 구성을 검토하고 개선안을 만들어줘.”

오디세이:

1. 목표와 조사 대상 확정
2. LOW 위험 읽기 작업과 문서 초안 작업으로 분리
3. TaskSpec 생성

Gateway:

1. 홈페이지 읽기와 자료 분석 권한 허용
2. 로그인·게시·사이트 수정 금지
3. 시간과 단계 제한 설정
4. 아킬레스 실행

아킬레스:

1. 현재 구조 조사
2. 문제점과 근거 정리
3. 개선안 작성
4. Result와 Evidence 반환

오디세이:

1. 실제 페이지와 근거 재검증
2. 중요한 오류 수정
3. 김대표님께 핵심 개선안만 보고
4. 실제 사이트 변경이 필요하면 별도 승인 요청

이와 같은 동일한 프로세스를 코드, 서버 점검, 문서, 조사, 이미지·영상 작업에도 적용한다.

---

## 19. 최종 제안

처음부터 복잡한 AI 조직을 만들지 않는다.

V1에서는 다음 한 가지를 완성하는 데 집중한다.

> **김대표님의 모든 지시가 오디세이를 통해 하나의 범용 Gateway로 들어가고, Gateway가 작업계약과 실행조건을 만든 뒤 아킬레스에게 작업시키며, 오디세이가 결과를 검증하는 구조.**

첫 파일럿은 안전한 읽기 작업으로 시작하지만 시스템 자체는 처음부터 범용으로 설계한다. 이후 정책만 단계적으로 열어 조사, 문서, 코드, 시스템 점검, 이미지·영상 등으로 확대한다.

구현 착수 시 첫 작업은 `C:\Users\egomine2\PLACHEM-Agent-Control` 프로젝트 생성과 TaskSpec/Result 테스트 작성이다.
