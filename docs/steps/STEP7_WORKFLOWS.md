# STEP7_WORKFLOWS.md — GitHub Actions 워크플로우 + 문서

## 산출물

| 파일 | 설명 |
|------|------|
| `workflows/code-review.yml` | 자동 트리거 (PR open/sync) |
| `workflows/code-review-manual.yml` | 수동 트리거 (/review or dispatch) |
| `README.md` | 프로젝트 설명, Quick Start |
| `docs/SETUP_GUIDE.md` | Runner 도구 설치 가이드 |
| `docs/CHECKLIST_REFERENCE.md` | 전체 체크리스트 (사람 가독용) |

---

## 1. `workflows/code-review.yml` (자동)

### 2-레포 checkout 패턴

모든 Job의 첫 2 step:

```yaml
- uses: actions/checkout@v4
  with:
    lfs: false
    fetch-depth: 2

- uses: actions/checkout@v4
  with:
    repository: your-org/ue5-review-bot
    ref: main
    path: .review-bot
    token: ${{ secrets.GIT_ACTION_TOKEN }}
```

이후 스크립트는 `.review-bot/scripts/`, 설정은 `.review-bot/configs/` 경로.

### Job 구조

```
gate → stage1 (항상)
             → stage2 (일반 PR + compile_commands.json 있을 때만)
                     → stage3 (일반 PR만, --has-compile-commands 플래그 전달)
                             → post-review (always, 모든 결과 통합)
```

- Artifact로 결과 JSON 전달
- post-review는 `if: always()`로 앞 Stage 실패해도 실행
- stage2: `if: needs.gate.outputs.is_large_pr == 'false' && hashFiles('**/compile_commands.json') != ''`
- stage3에 `--has-compile-commands` 플래그를 stage2 실행 여부에 따라 전달
- stage2/stage3 결과는 `continue-on-error`로 download

### Secrets

| Secret | 용도 |
|--------|------|
| `GIT_ACTION_TOKEN` | 봇 레포 read + PR Review 쓰기 |
| `ANTHROPIC_API_KEY` | Stage 3 LLM |
| `GHES_URL` | GHES 주소 |

---

## 2. `workflows/code-review-manual.yml` (수동)

### 트리거

- `workflow_dispatch`: Actions 탭에서 PR 번호 입력
- `issue_comment`: PR 코멘트에 `/review` 입력

### 자동 트리거와 차이점

- **Stage 2**: 대규모 PR이어도 실행 (if 조건 없음)
- **Stage 3**: 대규모 PR이면 수동이어도 차단
- `/review` 코멘트에 👀 리액션 → 완료 후 ✅

---

## 3. README.md

```
포함:
- 한 줄 요약
- 아키텍처 다이어그램 (Gate → Stage 1 → 2 → 3 → Post)
- Quick Start: Runner 설정 → Secrets 등록 → yml 복사
- 대규모 PR 안전장치 설명
- /review 수동 트리거 사용법
- 설정 커스터마이징 (gate_config.yml, checklist.yml)
```

## 4. docs/SETUP_GUIDE.md

```
Runner 설치 도구:
- Python 3.10+
- pip: anthropic, pyyaml, requests
- clang-format 16+
- clang-tidy 16+
- jq (GitHub Actions output 파싱)
- (향후) PVS-Studio
- (향후) compile_commands.json 생성 도구
```

## 5. docs/CHECKLIST_REFERENCE.md

```
원본 PDF 2개 기반, 사람 가독용.
- 수정사항 반영 (오타, 부정확, 추가 항목)
- 각 항목에 Tier 표시 (Stage 1/2/3)
- auto_fixable 여부 표시
```
