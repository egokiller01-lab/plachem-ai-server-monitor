# PLACHEM Agent Control

범용 오디세이(Main) → 아킬레스(Worker) 작업 위임 Gateway입니다.

## 핵심 흐름

1. 오디세이가 자연어 지시를 `TaskSpec`으로 컴파일합니다.
2. Policy Engine이 권한·위험도·환경을 판정합니다.
3. Resource Guard가 RTX 3090과 ComfyUI 충돌을 검사합니다.
4. Gateway가 별도 Hermes `achilles` 프로필을 실행합니다.
5. 아킬레스는 구조화된 `WorkerResult`를 반환합니다.
6. Verifier가 증거와 권한 사용을 검사한 뒤 완료 여부를 결정합니다.

## 설치

```bash
uv sync --extra test
```

## 검증

```bash
uv run python -m pytest -q
uv run python -m gateway.cli dry-run --task examples/pilot-readme-task.json
```

## 실제 실행

```bash
uv run python -m gateway.cli run \
  --task examples/pilot-readme-task.json \
  --context README.md \
  --runtime runtime
```

실제 실행 전 `127.0.0.1:8080`의 아킬레스 로컬 LLM이 응답해야 하며 ComfyUI 대기열이 비어 있어야 합니다.

## V1 보안 경계

- 정책에 고정된 Hermes/nvidia-smi/model 절대 경로와 SHA-256을 사용 전 확인하고, 각 경로 구성요소가 reparse point가 아닌 regular file인지 검사합니다. Worker launch `PATH`는 상속하지 않고 `C:/Windows/System32;C:/Windows`로 고정합니다. Hermes는 live profile을 다시 읽지 않고 provider, localhost base URL, 고정 모델만 담은 task별 `HERMES_HOME` snapshot으로 실행합니다.
- Worker에는 내장 `todo`만 전달합니다. `HERMES_SAFE_MODE=1` 환경으로 plugin/MCP/hook 등록을 차단하되, `--safe-mode` CLI 플래그는 프로필 모델 설정을 버리므로 사용하지 않습니다.
- `--runtime`은 정책에 고정된 `E:/PLACHEM-Agent-Control/repo/runtime`만 허용합니다. Runtime, lock, executable, model 및 artifact 경로의 기존 구성요소는 reparse point를 거부하고 최종 경로를 실용적으로 재검사합니다.
- Task/result/query 산출물은 저장 직후 다시 읽어 hash를 검증하고 가능한 곳에서 read-only로 설정합니다. Windows read-only 속성은 immutable 보안 경계가 아닙니다. Audit JSONL은 duplicate key, blank line, malformed/noncanonical JSONL을 거부하며 interprocess lock, atomic replacement, 지원되는 플랫폼의 directory fsync, 이전 이벤트 hash를 사용합니다.
- Context/query와 TaskSpec/WorkerResult 문자열·배열에는 byte/item 상한이 있습니다. Subprocess stdout/stderr는 실행 중 제한하며, 모든 retry는 하나의 monotonic 총 deadline을 공유합니다. Windows 종료는 timeout이 있는 `taskkill /T /F`와 bounded wait로 확인하고 확인할 수 없으면 fail closed합니다.
- GPU lock은 guard 전체 수명 동안 descriptor와 OS file lock을 유지하며, release 시 소유한 file identity가 그대로인 경우에만 경로를 삭제합니다.
- V1 evidence 요구사항은 scope 안의 정확한 `<path>:<line>`만 지원합니다. 다른 형식은 dry-run/policy 단계에서 거부됩니다.
- Audit 검증은 호출자가 보유한 신뢰 가능한 head hash나 외부 anchor가 없으면 전체 파일 삭제, suffix truncation, 또는 완전한 chain 재작성은 탐지하지 못합니다. 동일 OS 사용자 권한의 공격자를 견디려면 별도 계정/ACL 및 외부 append-only 저장소에 trusted head를 보관해야 합니다.
- Python stdlib 경로 검사와 hash-then-exec는 race-free no-follow open/execute 의미를 제공하지 않습니다. 같은 관리자/사용자 권한 공격자는 검사 뒤 파일을 교체할 수 있고, 외부 trust가 없으면 파일과 policy/hash를 함께 교체할 수도 있습니다. 별도 저권한 계정, ACL, 서명/외부 policy anchor가 필요한 잔여 위험입니다.
- Windows Job Object는 사용하지 않습니다. Bounded `taskkill` 실패 시 후손 프로세스가 남았을 가능성을 오류로 보고하지만, 외부 OS 관찰 없이 완전한 부재를 증명할 수는 없습니다. Lock close 후 identity 재검사와 unlink 사이에도 stdlib만으로 제거할 수 없는 짧은 race가 있습니다.

## 기본 금지

- Production 변경
- merge/deploy
- 파괴적 삭제
- 비밀값 출력
- 권한 변경

상세 기획: `docs/PROJECT-PROPOSAL.md`
