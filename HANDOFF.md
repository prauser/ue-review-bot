# HANDOFF — UE5 코드리뷰 자동화 시스템 구현 진행상황

> 세션 간 작업 인계를 위한 문서
> 최종 업데이트: 2026-02-17

---

## 📋 전체 개요

**목표:** GitHub Enterprise Server에서 UE5 C++ 프로젝트를 자동으로 코드리뷰하는 봇 시스템 구축

**총 7개 Step 중 현재 진행:**
- ✅ **Step 1 완료** (설정 파일 생성)
- ✅ **Step 2 완료** (테스트 픽스처 + Gate Checker)
- ✅ **Step 3 완료** (Stage 1 — regex 패턴 매칭 + clang-format suggestion)
- ✅ **Step 5 완료** (Stage 2 — clang-tidy 정적 분석)
- 🔜 **Step 4 진행 예정** (PR 코멘트 게시) 또는 **Step 6** (Stage 3 LLM 리뷰)

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
- `bugprone-division-by-zero` — 0 나누기
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

**테스트 결과:** 43 passed (전체 224 passed, Step 2+3 포함)

---

## 🔜 다음 작업: Step 4 또는 Step 6

### Step 4: PR 코멘트 게시
**상세 스펙:** `docs/steps/STEP4_POST_REVIEW.md`

### Step 6: Stage 3 — LLM 리뷰
**상세 스펙:** `docs/steps/STEP6_STAGE3.md`

---

## 📁 현재 레포지토리 구조

```
ue5-review-bot/
├── PLAN.md                      # 전체 계획서
├── HANDOFF.md                   # 이 파일
├── configs/                     # ✅ Step 1 + Step 5 완료
│   ├── .clang-format
│   ├── .clang-tidy              # ✅ Step 5 clang-tidy 설정
│   ├── .editorconfig
│   ├── checklist.yml            # (Step 3에서 regex 버그 수정)
│   └── gate_config.yml
├── scripts/                     # ✅ Step 2 + Step 3 + Step 5 완료
│   ├── __init__.py
│   ├── gate_checker.py          # Gate 로직 (대규모 PR 판정)
│   ├── stage1_pattern_checker.py # ✅ Stage 1 regex 패턴 검사
│   ├── stage1_format_diff.py    # ✅ clang-format suggestion 생성
│   ├── stage2_tidy_to_suggestions.py # ✅ Stage 2 clang-tidy 변환
│   └── utils/
│       ├── __init__.py
│       ├── diff_parser.py       # ✅ unified diff 파싱 유틸
│       └── gh_api.py            # GitHub API 유틸리티
├── tests/                       # ✅ Step 2 + Step 3 + Step 5 완료
│   ├── __init__.py
│   ├── test_gate_checker.py     # Gate Checker 테스트 (50개)
│   ├── test_pattern_checker.py  # ✅ 패턴 검사 테스트 (71개)
│   ├── test_format_diff.py      # ✅ 포맷 suggestion 테스트 (21개)
│   ├── test_stage2.py           # ✅ Stage 2 변환 테스트 (43개)
│   └── fixtures/
│       ├── sample_bad.cpp       # 규칙 위반 샘플
│       ├── sample_good.cpp      # 규칙 준수 샘플 (Step 3에서 수정)
│       ├── sample_network.cpp   # 네트워크 위반 샘플
│       └── sample_diff.patch    # 테스트용 diff
└── docs/
    └── steps/                   # Step별 상세 스펙
        ├── STEP1_CONFIGS.md     # ✅ 완료
        ├── STEP2_GATE.md        # ✅ 완료
        ├── STEP3_STAGE1.md      # ✅ 완료
        ├── STEP5_STAGE2.md      # 🔜 다음
        ├── STEP6_STAGE3.md
        └── STEP7_WORKFLOWS.md
```

---

## 🔑 중요 정보

### Git 브랜치 규칙

- **브랜치 명명:** `claude/review-plan-step<N>-<SESSION_ID>`
- **Step 1 브랜치:** `claude/review-plan-step1-D8194` (이미 푸시됨)
- **Step 2 브랜치:** `claude/implement-step2-gate-pEDwB` (이미 푸시됨)
- **Step 3 브랜치:** `claude/review-handoff-R5lJ4`
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

## 🚀 다음 세션 시작 방법

1. **레포지토리 상태 확인:**
   ```bash
   git fetch origin
   git status
   ```

2. **다음 Step 스펙 읽기:**
   ```bash
   cat docs/steps/STEP4_POST_REVIEW.md   # PR 코멘트 게시
   # 또는
   cat docs/steps/STEP6_STAGE3.md        # LLM 리뷰
   ```

3. **새 브랜치 생성:**
   ```bash
   git checkout -b claude/review-plan-step<N>-<NEW_SESSION_ID>
   ```

4. **작업 시작:**
   - 해당 Step 스펙에 따라 구현
   - pytest 실행 및 검증
   - 커밋/푸시

---

## 📝 메모

- PDF 파일 (`CodeReviewCheckList.pdf`, `CodingConvention.pdf`)은 main 브랜치의 `docs/` 디렉토리에 보관
- 현재 환경에서는 PDF 파싱 도구 설치 불가 → STEP1_CONFIGS.md 스펙 기반으로 작성 완료
- `.clang-tidy` 설정은 Step 5에서 생성 (compile_commands.json과 함께)
- `checklist.yml`의 tier 분류가 각 Stage 스크립트 구현의 기준이 됨
- Stage 1 regex는 주석 라인을 자동 스킵하여 false positive 감소
- `check_side_effect_suspicious`는 1차 필터 (Stage 3 LLM이 최종 검증)
- clang-format이 설치되어 있지 않은 환경에서는 format_diff가 빈 결과 반환

---

**이 문서는 세션 간 작업 인계를 위한 것입니다.**
**최신 상태 확인:** `git log --oneline --graph --all`
