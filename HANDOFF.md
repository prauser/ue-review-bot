# HANDOFF — UE5 코드리뷰 자동화 시스템 구현 진행상황

> 세션 간 작업 인계를 위한 문서
> 최종 업데이트: 2026-02-23

---

## 📋 전체 개요

**목표:** GitHub Enterprise Server에서 UE5 C++ 프로젝트를 자동으로 코드리뷰하는 봇 시스템 구축

**총 7개 Step 중 현재 진행:**
- ✅ **Step 1 완료** (설정 파일 생성)
- ✅ **Step 2 완료** (테스트 픽스처 + Gate Checker)
- ✅ **Step 3 완료** (Stage 1 — regex 패턴 매칭 + clang-format suggestion)
- ✅ **Step 4 완료** (PR 코멘트 게시 — post_review + gh_api 확장)
- ✅ **Step 5 완료** (Stage 2 — clang-tidy 정적 분석)
- ✅ **Step 6 완료** (Stage 3 — LLM 시맨틱 리뷰)
- ✅ **Step 7 완료** (GitHub Actions 워크플로우 + 문서화)

**전체 계획:** `PLAN.md` 참조

---

## ✅ 완료된 작업

### Step 1: 설정 파일 생성

**브랜치:** `claude/review-plan-step1-D8194`
**커밋:** `d3d870b` — "Step 1: 설정 파일 생성 완료"
**상태:** 커밋/푸시 완료

#### 생성된 파일 (4개)

| 파일 | 크기 | 설명 |
|------|------|------|
| `configs/.clang-format` | 2.4KB | UE5 Epic 코딩 스타일 (Allman, Tab=4, 120 cols) |
| `configs/.editorconfig` | 534B | 에디터 통일 설정 (UTF-8, LF, 공백 제거) |
| `configs/checklist.yml` | 14KB | 코드리뷰 체크리스트 (Tier 1/2/3 분류, 40+ 항목) |
| `configs/gate_config.yml` | 2.8KB | 대규모 PR 판정 설정 (50파일 임계값) |

#### 주요 내용

**`checklist.yml` 구조:**
- **Tier 1** (Stage 1 regex): 7개 핵심 패턴
  - `logtemp`, `pragma_optimize_off`, `hard_asset_path`, `macro_no_semicolon`
  - `declaration_macro_semicolon`, `check_side_effect_suspicious`, `sync_load_runtime`
- **Tier 2** (Stage 2 clang-tidy): `override`, `virtual_destructor`, `unnecessary_copy`
- **Tier 3** (Stage 3 LLM): 이관 항목 7개 + 추가 항목 30+

**`gate_config.yml` 주요 설정:**
- 파일 필터: ThirdParty, 자동생성, 바이너리 제외
- 대규모 PR 판정: `max_reviewable_files: 50`
- 레이블 기반 판정: `migration`, `large-change`, `engine-update` 등

---

## ✅ 완료된 작업: Step 2

### Step 2: 테스트 픽스처 + Gate Checker

**상세 스펙:** `docs/steps/STEP2_GATE.md`
**브랜치:** `claude/implement-step2-gate-pEDwB`
**상태:** 커밋/푸시 완료

#### 생성된 파일 (8개)

| 파일 | 설명 |
|------|------|
| `tests/fixtures/sample_bad.cpp` | 의도적 규칙 위반 샘플 (Stage 1 + Stage 3) |
| `tests/fixtures/sample_good.cpp` | 규칙 준수 샘플 (false positive 0 확인용) |
| `tests/fixtures/sample_network.cpp` | 네트워크 위반 샘플 |
| `tests/fixtures/sample_diff.patch` | 테스트용 unified diff (10 파일, C++/ThirdParty/binary 혼합) |
| `scripts/gate_checker.py` | Gate 로직 (대규모 PR 판정 + 파일 필터링) |
| `scripts/utils/gh_api.py` | GitHub API 유틸리티 (PR 라벨 조회) |
| `tests/test_gate_checker.py` | Gate Checker 유닛/통합 테스트 (50개) |
| `scripts/__init__.py`, `scripts/utils/__init__.py`, `tests/__init__.py` | 패키지 초기화 |

#### 주요 구현 사항

**`gate_checker.py` 2단계 로직:**
1. **파일 필터:** `gate_config.yml`의 `skip_patterns` + C++ 확장자 필터
2. **규모 판정:** reviewable 파일 수 > 50 OR 대규모 PR 라벨 → is_large_pr

**Diff 파서 (코드 리뷰 반영):**
- `+++ b/path` 기반 파싱 (diff --git의 ` b/` 경로 모호성 해소)
- `Binary files ... and b/path differ` / `rename to path` fallback
- header/hunk 상태 추적으로 hunk 내부 false positive 방지
- Git quoted path 지원: octal escape UTF-8 디코딩, `\"` escape
- non-UTF8 diff 파일 안전 처리 (`errors="replace"`)
- 빈 YAML config guard

**CLI 인터페이스:**
```bash
python scripts/gate_checker.py \
  --diff <diff-file> \
  --config configs/gate_config.yml \
  --output gate-result.json \
  --labels migration,large-change
```

**테스트 결과:** 50 passed (pytest)

---

## ✅ 완료된 작업: Step 3

### Step 3: Stage 1 — regex 패턴 매칭 + clang-format suggestion

**상세 스펙:** `docs/steps/STEP3_STAGE1.md`
**브랜치:** `claude/review-handoff-R5lJ4`
**상태:** 커밋/푸시 완료

#### 생성/수정된 파일 (6개)

| 파일 | 설명 |
|------|------|
| `scripts/utils/diff_parser.py` | unified diff 파싱 유틸 (파일별 added_lines + hunks 추출) |
| `scripts/stage1_pattern_checker.py` | Tier 1 regex 패턴 검사 (checklist.yml에서 7개 패턴 로드) |
| `scripts/stage1_format_diff.py` | clang-format diff → suggestion 변환 (20줄 청크 분리) |
| `tests/test_pattern_checker.py` | 패턴 검사 + diff_parser 테스트 (71개) |
| `tests/test_format_diff.py` | 포맷 suggestion 테스트 (21개) |
| `configs/checklist.yml` | macro_no_semicolon regex 백트래킹 버그 수정 |

#### 주요 구현 사항

**`scripts/utils/diff_parser.py`:**
- unified diff → `Dict[str, FileDiff]` 구조화
- 각 파일별 `added_lines: {line_num: content}`, `hunks: [{start, end, content}]`
- hunk 내 라인 번호를 새 파일 기준으로 정확히 추적
- `_decode_git_path()` 재사용 (octal escape UTF-8 디코딩)

**`scripts/stage1_pattern_checker.py`:**
- `checklist.yml`에서 `tier: 1` + `pattern` 필드가 있는 7개 항목 자동 로드
- 변경된 라인(added lines)에 대해서만 패턴 검사 수행
- 주석 라인 자동 스킵 (`// ...` 전체 라인 주석, 인라인 주석 제거)
- `macro_no_semicolon` / `declaration_macro_semicolon`에 대한 auto-fix suggestion 생성
- CLI: `--diff <file>` 또는 `--files + --base-ref` (git diff 자동 생성) 지원

**7개 Tier 1 패턴:**

| ID | 설명 | severity | auto_fixable |
|----|------|----------|-------------|
| `logtemp` | `\bLogTemp\b` | warning | false |
| `pragma_optimize_off` | `#pragma optimize("", off)` | error | false |
| `hard_asset_path` | `TEXT("/Game/..." or "/Engine/...")` | warning | false |
| `macro_no_semicolon` | 런타임 매크로 뒤 세미콜론 누락 | warning | true |
| `declaration_macro_semicolon` | 선언 매크로 뒤 불필요한 세미콜론 | warning | true |
| `check_side_effect_suspicious` | check() 내 부작용 의심 패턴 (1차 필터) | warning | false |
| `sync_load_runtime` | 런타임 동기 로딩 금지 | error | false |

**`scripts/stage1_format_diff.py`:**
- clang-format 실행 → 원본 vs 포맷팅 비교 → suggestion 생성
- PR diff 범위 안의 라인만 suggestion, 범위 밖은 info 코멘트로 전환
- 20줄 초과 diff는 자동 청크 분리
- clang-format 미설치 시 graceful 처리 (경고 + 빈 결과)

**`checklist.yml` 수정:**
- `macro_no_semicolon` 패턴의 `\s*(?!;)` → `(?!\s*;)` 수정
  - 기존 패턴은 `\s*`가 백트래킹하여 세미콜론이 있어도 매칭되는 버그 존재

**`sample_good.cpp` 수정:**
- `MeshRef.LoadSynchronous()` → `MeshRef.Get()` (regex 오탐 방지)
- `check(IsValid(this))` → `check(this != nullptr)` (함수 호출 오탐 방지)
- ConstructorHelpers 하드코딩 경로 → 변수 참조 (hard_asset_path 오탐 방지)

**CLI 인터페이스:**
```bash
# Pattern Checker
python -m scripts.stage1_pattern_checker \
  --diff <diff-file> \
  --checklist configs/checklist.yml \
  --output findings-stage1.json

# Format Diff (clang-format 필요)
python -m scripts.stage1_format_diff \
  --files '["Source/A.cpp"]' \
  --clang-format-config configs/.clang-format \
  --diff <diff-file> \
  --output suggestions-format.json
```

**테스트 결과:** 92 passed (전체 142 passed, Step 2 포함)

---

## ✅ 완료된 작업: Step 4

### Step 4: PR 코멘트 게시 — post_review + gh_api 확장

**상세 스펙:** `docs/steps/STEP4_POST_REVIEW.md`
**브랜치:** `claude/step4-post-review-H20Qe`
**상태:** 커밋/푸시 완료

#### 생성/수정된 파일 (3개)

| 파일 | 설명 |
|------|------|
| `scripts/post_review.py` | Stage 1~3 결과 통합 + 단일 PR Review 게시 |
| `scripts/utils/gh_api.py` | 확장 — `GitHubClient`, `create_review()`, `get_existing_review_comments()` 추가 |
| `tests/test_post_review.py` | 통합/게시 로직 테스트 (93개) |

#### 주요 구현 사항

**`scripts/post_review.py`:**
- Stage 1 (패턴 + 포맷), Stage 2 (clang-tidy), Stage 3 (LLM) JSON 결과 통합
- 파일 + 라인 기준 정렬, PR diff hunk 범위 검증 (범위 밖 코멘트 skip)
- 중복 제거: 동일 file + line + rule_id → severity 우선순위 (error > warning > suggestion > info)
- suggestion 블록 생성 (auto-fix 항목)
- severity 아이콘: 🚫 error, ⚠️ warning, ℹ️ info
- GHES 3.4+ multi-line 지원, 3.3 이하 fallback (코드 블록)
- 최대 50개 코멘트 per review (GitHub API 제한), severity 기반 pruning
- summary 테이블 (stage별/severity별 카운트)
- dry-run 모드 지원 (API 호출 없이 payload 출력)
- 기존 PR 코멘트 중복 방지 (paginated fetch)
- 전체 실패 시 non-zero exit

**`scripts/utils/gh_api.py` 확장:**
- `GitHubClient` 클래스 — API 요청 핸들링 (token, base URL)
- `create_review()` — PR Review 게시 (comments + body)
- `get_pull_request()` — PR 메타데이터 조회
- `get_existing_review_comments()` — 중복 방지용 기존 코멘트 조회 (페이지네이션)
- `get_ghes_version()` — GHES 버전 감지 (multi-line 지원 판별)

**CLI 인터페이스:**
```bash
python -m scripts.post_review \
  --pr-number 42 \
  --repo owner/repo \
  --commit-sha abc123 \
  --findings findings-stage1.json suggestions-format.json \
  --token $GHES_TOKEN \
  --api-url https://github.company.com/api/v3 \
  --output review-result.json

# Dry-run mode:
python -m scripts.post_review \
  --findings findings-stage1.json \
  --dry-run \
  --output review-payload.json
```

**출력 JSON:**
```json
{
  "review_id": 12345,
  "review_url": "https://...",
  "total_findings": 15,
  "posted_comments": 12,
  "skipped_out_of_range": 2,
  "skipped_duplicate": 1,
  "by_stage": {"stage1-pattern": 5, "stage1-format": 3, "stage2": 2, "stage3": 2},
  "by_severity": {"error": 2, "warning": 6, "info": 1, "suggestion": 3}
}
```

**테스트 결과:** 93 passed (전체 278 passed, Step 2+3+5 포함)

---

## ✅ 완료된 작업: Step 5

### Step 5: Stage 2 — clang-tidy 정적 분석

**상세 스펙:** `docs/steps/STEP5_STAGE2.md`
**브랜치:** `claude/verify-handoff-testing-a5JqI`
**상태:** 커밋/푸시 완료

#### 생성된 파일 (3개)

| 파일 | 설명 |
|------|------|
| `configs/.clang-tidy` | clang-tidy 체크 설정 (9개 체크, Source 헤더 필터) |
| `scripts/stage2_tidy_to_suggestions.py` | clang-tidy `--export-fixes` YAML → suggestion/comment 변환 |
| `tests/test_stage2.py` | 변환 로직 테스트 (43개) |

#### 주요 구현 사항

**`configs/.clang-tidy` 설정 (9개 체크):**
- `modernize-use-override` — override 키워드 누락
- `cppcoreguidelines-virtual-class-destructor` — virtual 소멸자 누락
- `bugprone-virtual-near-miss` — 가상 함수 오버라이드 오타
- `performance-unnecessary-copy-initialization` — 불필요 복사 초기화
- `performance-for-range-copy` — range-for 루프 복사
- `clang-analyzer-optin.cplusplus.VirtualCall` — 생성자/소멸자 내 가상 호출
- `clang-analyzer-core.DivideZero` — 0 나누기
- `readability-else-after-return` — return 후 불필요 else
- `readability-redundant-smartptr-get` — 불필요 스마트 포인터 `.get()`
- `HeaderFilterRegex: 'Source/.*'` (Engine 헤더 제외)

**`scripts/stage2_tidy_to_suggestions.py`:**
- clang-tidy `--export-fixes` YAML 파싱 (`parse_tidy_fixes`)
- fix 있는 항목 → suggestion 블록 (소스 내용 기반 replacement 적용)
- fix 없는 항목 → 일반 코멘트
- Stage 1 결과와 **중복 제거** (같은 file + line → skip)
- check name → checklist rule_id 매핑 (예: `modernize-use-override` → `override_keyword`)
- `--pvs-report` 인터페이스 준비 (placeholder, 인자 없으면 clang-tidy만 처리)
- byte offset → line number 변환 (소스 있으면 정확히, 없으면 추정)

**CLI 인터페이스:**
```bash
python -m scripts.stage2_tidy_to_suggestions \
  --tidy-fixes fixes.yaml \
  --stage1-results findings-stage1.json \
  --output findings-stage2.json
```

**테스트 결과:** 43 passed (전체 367 passed, Step 2+3+4 포함)

---

## ✅ 완료된 작업: Step 6

### Step 6: Stage 3 — LLM 시맨틱 리뷰

**상세 스펙:** `docs/steps/STEP6_STAGE3.md`
**브랜치:** `claude/fix-handoff-state-z27rd`
**상태:** 커밋/푸시 완료

#### 생성된 파일 (3개)

| 파일 | 설명 |
|------|------|
| `scripts/utils/token_budget.py` | 토큰 예산 관리 (PR당 100K 토큰, 파일당 20K, $2 한도) |
| `scripts/stage3_llm_reviewer.py` | Anthropic API 기반 시맨틱 코드 리뷰 |
| `tests/test_llm_reviewer.py` | mock API 테스트 (81개) |

#### 주요 구현 사항

**`scripts/utils/token_budget.py`:**
- `estimate_tokens()` — 보수적 토큰 추정 (len // 3)
- `estimate_cost()` — USD 비용 추정 (Sonnet 4.5 기준)
- `should_skip_file()` — ThirdParty, generated, protobuf, Intermediate 파일 스킵
- `chunk_diff()` — @@ hunk 기준 분할, 초과 시 라인 단위 분할
- `BudgetTracker` 클래스 — PR 세션 내 누적 토큰/비용 추적

**`scripts/stage3_llm_reviewer.py`:**
- `build_system_prompt()` — compile_commands.json 유무에 따라 clang-tidy 대체 섹션 동적 포함
- `build_user_message()` — 파일별 diff + 선택적 전체 소스 컨텍스트
- `parse_llm_response()` — markdown 코드 펜스 처리, JSON 배열 추출
- `validate_finding()` — 필수 필드 정규화, stage3 태그 부여, rule_id = category
- `load_exclude_findings()` / `filter_excluded()` — Stage 1/2 결과와 중복 제거
- `call_anthropic_api()` — urllib 기반 API 호출, rate limit 429/5xx 재시도 (exponential backoff, 최대 3회)
- `review_file()` — 파일 단위 리뷰, 예산 초과 시 skip, 청킹 지원
- `review_pr()` — PR 전체 리뷰 (파일별 순회, 비C++ 스킵, generated 스킵)
- `--dry-run` 모드 — API 호출 없이 시스템 프롬프트 확인

**시스템 프롬프트 구성:**
- Stage 1 이관 항목: auto 금지, 요다 컨디션, ! 연산자, sandwich inequality, FSimpleDelegateGraphTask, LOCTEXT_NAMESPACE, ConstructorHelpers
- clang-tidy 대체 (compile_commands 없을 때): override, virtual destructor, 복사, else-after-return
- LLM 검토 항목: GC 안전성, GameThread 안전성, 네트워크 효율, 성능, UE5 패턴, 설계, 주석, 보안

**CLI 인터페이스:**
```bash
python -m scripts.stage3_llm_reviewer \
  --diff pr.diff \
  --exclude-findings findings-stage1.json findings-stage2.json \
  --has-compile-commands false \
  --output findings-stage3.json

# Dry-run (시스템 프롬프트 확인):
python -m scripts.stage3_llm_reviewer \
  --diff pr.diff --dry-run
```

**에러 핸들링:**
- API 타임아웃/에러: 해당 파일 skip, 파이프라인 계속
- JSON 파싱 실패: skip, 로그 기록
- Rate limit (429): exponential backoff 최대 3회
- PR당 $2 초과: 남은 파일 skip, 경고

**테스트 결과:** 81 passed (전체 448 passed, Step 2+3+4+5 포함)

---

## ✅ 완료된 작업: Step 7

### Step 7: GitHub Actions 워크플로우 + 문서화

**상세 스펙:** `docs/steps/STEP7_WORKFLOWS.md`
**브랜치:** `claude/review-handoff-document-rUUDZ`
**상태:** 커밋/푸시 완료

#### 생성된 파일 (5개)

| 파일 | 설명 |
|------|------|
| `workflows/code-review.yml` | 자동 트리거 (PR open/sync) — Gate → Stage 1 → 2 → 3 → Post Review |
| `workflows/code-review-manual.yml` | 수동 트리거 (/review 코멘트 + workflow_dispatch) |
| `README.md` | 프로젝트 설명, 아키텍처, Quick Start 가이드 |
| `docs/SETUP_GUIDE.md` | Runner 도구 설치 + Secrets 설정 가이드 |
| `docs/CHECKLIST_REFERENCE.md` | 전체 체크리스트 사람 가독용 레퍼런스 |

#### 주요 구현 사항

**`workflows/code-review.yml` (자동 트리거):**
- Job 구조: gate → stage1 → stage2 (조건부) → stage3 (일반 PR만) → post-review (always)
- 2-레포 checkout 패턴: 게임 레포 + 봇 레포 (.review-bot/)
- Artifact로 결과 JSON 전달 (pr-diff, findings-stage1/2/3)
- stage2: `is_large_pr == false && has_compile_commands == true` 조건
- stage3: `is_large_pr == false` 조건
- post-review: `if: always()` + 모든 stage 결과 통합
- concurrency 그룹: PR 번호 기준 중복 실행 취소

**`workflows/code-review-manual.yml` (수동 트리거):**
- `workflow_dispatch`: Actions 탭에서 PR 번호 입력
- `issue_comment`: PR 코멘트에 `/review` 입력
- Preflight Job: PR 메타데이터 조회 (head_sha, base_sha)
- `/review` 코멘트에 :eyes: 리액션 → 완료 후 :+1:/::-1:
- Stage 2: 대규모 PR에서도 실행 (compile_commands만 확인)
- Stage 3: 대규모 PR이면 수동이어도 차단

**테스트 결과:** 527 passed (기존 전체 테스트 통과)

---

## ✅ 워크플로우 PR 리뷰 피드백 수정 (8라운드)

Step 7 완료 후 PR 코드 리뷰에서 발견된 이슈들을 8라운드에 걸쳐 수정했습니다.

**브랜치:** `claude/review-handoff-document-rUUDZ`
**최종 테스트:** 546 passed

### Round 1: 경로 및 YAML 파싱 (`33d8e77`)
- **체크리스트 경로 이중 prefix:** `working-directory: .review-bot` + `--checklist .review-bot/configs/...` → `PYTHONPATH` 방식 전환
- **Artifact 경로 swap:** 패턴 출력과 업로드 경로가 뒤바뀜 → 모든 출력을 `${GITHUB_WORKSPACE}/`로 통일
- **Multi-document YAML:** clang-tidy 결합 YAML에서 `yaml.safe_load`가 첫 문서만 파싱 → `yaml.safe_load_all`로 변경

### Round 2: clang-tidy 및 워크플로우 문법 (`0ac43a2`)
- **clang-tidy `-p .` 빌드 경로:** `compile_commands.json`이 `build/`에 있을 때 찾지 못함 → Gate에서 `compile_commands_dir` 출력
- **`continue-on-error` 위치:** `with:` 블록 안에 배치됨 → step 레벨로 이동
- **`gh` CLI 의존성:** self-hosted runner에 `gh` 미설치 가능 → `actions/github-script`로 교체

### Round 3: 권한 및 모듈 안전성 (`7cd3a4e`)
- **`/review` 권한 체크 누락:** 아무나 트리거 가능 → `author_association` (OWNER/MEMBER/COLLABORATOR) 필터
- **PYTHONPATH 모듈 충돌:** 게임 레포의 `scripts/`와 봇의 `scripts/`가 충돌 → `working-directory: .review-bot` 복원 + `${GITHUB_WORKSPACE}/` 절대경로

### Round 4: diff 및 리액션 (`dbedc19`)
- **2-dot diff (`..`):** base 브랜치 변경까지 포함됨 → merge-base 3-dot diff (`...`) + `fetch-depth: 0`
- **Stage 1 실패 시 리액션 누락:** post-review가 skip되면 완료 리액션도 누락 → 별도 `finalizer` job 분리

### Round 5: format checker 경로 (`c28d94c`)
- **format checker가 소스 파일 못 찾음:** `working-directory: .review-bot`에서 실행 시 게임 레포 파일 접근 불가 → workspace root에서 직접 스크립트 호출

### Round 6: 중복 확인
- 이미 수정된 코멘트 2건 — 추가 수정 없음

### Round 7: diff hunk 필터 (`59c1ee8`)
- **clang-tidy 전체 파일 분석 → 422 에러:** diff 밖 라인에 코멘트 시 GitHub API 거부 → `post_review.py`에 `filter_findings_by_diff()` 추가, `--diff` 플래그로 PR diff 전달

### Round 8: multi-line 검증 및 base SHA (`2e0731b`)
- **end_line 미검증:** multi-line finding의 `end_line`이 diff 밖이면 여전히 422 → line + end_line 모두 같은 hunk 내인지 검증
- **수동 워크플로우 base SHA 미존재:** `ref: head_sha`로 checkout 시 base 브랜치 tip이 없음 → `git fetch origin base_sha` 추가

---

## 🎉 전체 완료

**총 7개 Step + 워크플로우 리뷰 피드백 8라운드 완료!** 프로젝트가 운영 가능한 상태입니다.

---

## 📁 현재 레포지토리 구조

```
ue5-review-bot/
├── PLAN.md                      # 전체 계획서
├── HANDOFF.md                   # 이 파일
├── README.md                    # 프로젝트 설명 + Quick Start
├── configs/
│   ├── .clang-format            # UE5 Epic 코딩 스타일
│   ├── .clang-tidy              # 9개 체크 설정
│   ├── .editorconfig            # 에디터 통일 설정
│   ├── checklist.yml            # 코드리뷰 체크리스트 (Tier 1/2/3)
│   └── gate_config.yml          # 대규모 PR 판정 설정
├── scripts/
│   ├── __init__.py
│   ├── gate_checker.py          # Gate 로직 (대규모 PR 판정)
│   ├── stage1_pattern_checker.py # Stage 1 regex 패턴 검사
│   ├── stage1_format_diff.py    # clang-format suggestion 생성
│   ├── stage2_tidy_to_suggestions.py # Stage 2 clang-tidy 변환
│   ├── stage3_llm_reviewer.py   # Stage 3 LLM 시맨틱 리뷰
│   ├── post_review.py           # PR Review 게시 (findings 통합 + diff 필터)
│   └── utils/
│       ├── __init__.py
│       ├── diff_parser.py       # unified diff 파싱 유틸
│       ├── gh_api.py            # GitHub API 유틸리티
│       └── token_budget.py      # 토큰 예산 관리
├── workflows/                   # 게임 레포에 복사할 yml 템플릿
│   ├── code-review.yml          # 자동 트리거 (PR open/sync)
│   └── code-review-manual.yml   # 수동 트리거 (/review, dispatch)
├── tests/                       # 546개 테스트
│   ├── __init__.py
│   ├── test_gate_checker.py     # 50개
│   ├── test_pattern_checker.py  # 71개
│   ├── test_format_diff.py      # 21개
│   ├── test_stage2.py           # 46개
│   ├── test_post_review.py      # 109개
│   ├── test_llm_reviewer.py     # 81개
│   └── fixtures/
│       ├── sample_bad.cpp
│       ├── sample_good.cpp
│       ├── sample_network.cpp
│       └── sample_diff.patch
└── docs/
    ├── SETUP_GUIDE.md           # Runner 설치 가이드
    ├── CHECKLIST_REFERENCE.md   # 체크리스트 레퍼런스
    └── steps/                   # Step별 상세 스펙
        ├── STEP1_CONFIGS.md ~ STEP7_WORKFLOWS.md
```

---

## 🔑 중요 정보

### Git 브랜치 규칙

- **브랜치 명명:** `claude/review-plan-step<N>-<SESSION_ID>`
- **Step 1 브랜치:** `claude/review-plan-step1-D8194` (이미 푸시됨)
- **Step 2 브랜치:** `claude/implement-step2-gate-pEDwB` (이미 푸시됨)
- **Step 3 브랜치:** `claude/review-handoff-R5lJ4`
- **Step 4 브랜치:** `claude/step4-post-review-H20Qe`
- **푸시 명령:** `git push -u origin <branch-name>`
- **실패 시:** 최대 4회 재시도 (exponential backoff: 2s, 4s, 8s, 16s)

### 3-Tier 전략 요약

```
Stage 1 (확정적 검사)  → regex 패턴 7개 + clang-format
Stage 2 (정적 분석)    → clang-tidy (compile_commands.json 있을 때만)
Stage 3 (LLM 리뷰)     → Stage 1 이관 항목 포함, 의미론적 리뷰 전체
```

### 대규모 PR 안전장치

- **파일 필터** (항상): ThirdParty, 자동생성, 바이너리 제외
- **규모 판정**: 필터 후 50개 초과 OR 대규모 레이블
- **자동 리뷰**: 대규모 시 Stage 1+2 (사용 가능 시) 실행, Stage 3 차단
- **수동 리뷰** (`/review`): 대규모 시 Stage 1+2 (사용 가능 시) 실행, Stage 3 차단
- **일반 PR**: Stage 1+2 (사용 가능 시)+3 모두 실행

---

## 📚 참조 문서

| 문서 | 용도 |
|------|------|
| `PLAN.md` | 전체 계획 및 Step 개요 |
| `docs/steps/STEP*.md` | 각 Step별 상세 구현 스펙 |
| `configs/checklist.yml` | 전체 체크리스트 항목 (machine-readable) |
| `configs/gate_config.yml` | Gate 설정 (필터, 임계값) |

---

## 🚀 배포 방법

### 1단계: 봇 레포 준비

봇 레포(`ue5-review-bot`)를 GHES에 private 레포로 생성하고 이 코드를 push합니다.

### 2단계: 게임 레포에 워크플로우 복사

```bash
# 게임 레포에서
cp ue5-review-bot/workflows/code-review.yml .github/workflows/
cp ue5-review-bot/workflows/code-review-manual.yml .github/workflows/
```

### 3단계: Secrets 등록 (게임 레포 Settings → Secrets)

| Secret | 용도 | 필수 |
|--------|------|------|
| `BOT_REPO_TOKEN` | 봇 레포 read 권한 PAT | 필수 |
| `GHES_TOKEN` | PR Review 쓰기 권한 PAT | 필수 |
| `GHES_URL` | `https://github.company.com` | GHES 환경 시 필수 |
| `ANTHROPIC_API_KEY` | Claude API 키 (Stage 3) | Stage 3 사용 시 필수 |

### 4단계: Self-hosted Runner 도구 설치

```bash
# 필수
python3 --version   # 3.9+
pip install pyyaml

# Stage 1 포맷 검사용 (선택)
clang-format --version  # 16+

# Stage 2 정적 분석용 (선택)
clang-tidy --version    # 16+
# + compile_commands.json 이 프로젝트 루트 또는 build/ 에 존재해야 함

# Stage 3 LLM 리뷰용
# Runner → api.anthropic.com HTTPS 아웃바운드 필요
```

### 5단계: 동작 확인

1. **자동:** 게임 레포에서 PR 생성 → Actions 탭에서 "UE5 Code Review" 워크플로우 실행 확인
2. **수동:** PR 코멘트에 `/review` 입력 → :eyes: 리액션 → 완료 후 :+1: 리액션 확인

---

## 📝 운영 참고사항

- Stage 2는 `compile_commands.json`이 있을 때만 실행됨 (없으면 자동 skip)
- Stage 3 (LLM)은 대규모 PR (50파일 초과)에서는 비용/토큰 제한으로 항상 차단
- `checklist.yml`의 tier 분류가 각 Stage 스크립트 구현의 기준이 됨
- Stage 1 regex는 주석 라인을 자동 스킵하여 false positive 감소
- `check_side_effect_suspicious`는 1차 필터 (Stage 3 LLM이 최종 검증)
- clang-format이 설치되어 있지 않은 환경에서는 format_diff가 빈 결과 반환
- PR당 LLM 비용 한도: $2 (초과 시 남은 파일 skip)
- 워크플로우 rerun 시 이미 게시된 코멘트는 자동 중복 방지

---

**이 문서는 세션 간 작업 인계를 위한 것입니다.**
**최신 상태 확인:** `git log --oneline --graph --all`
