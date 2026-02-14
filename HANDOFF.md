# HANDOFF — UE5 코드리뷰 자동화 시스템 구현 진행상황

> 세션 간 작업 인계를 위한 문서
> 최종 업데이트: 2026-02-13

---

## 📋 전체 개요

**목표:** GitHub Enterprise Server에서 UE5 C++ 프로젝트를 자동으로 코드리뷰하는 봇 시스템 구축

**총 7개 Step 중 현재 진행:**
- ✅ **Step 1 완료** (설정 파일 생성)
- 🔜 **Step 2 진행 예정** (테스트 픽스처 + Gate Checker)

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
  - `check_side_effect`, `unbraced_shipping_macro`, `sync_load_runtime`
- **Tier 2** (Stage 2 clang-tidy): `override`, `virtual_destructor`, `unnecessary_copy`
- **Tier 3** (Stage 3 LLM): 이관 항목 7개 + 추가 항목 30+

**`gate_config.yml` 주요 설정:**
- 파일 필터: ThirdParty, 자동생성, 바이너리 제외
- 대규모 PR 판정: `max_reviewable_files: 50`
- 레이블 기반 판정: `migration`, `large-change`, `engine-update` 등

---

## 🔜 다음 작업: Step 2

### Step 2: 테스트 픽스처 + Gate Checker

**상세 스펙:** `docs/steps/STEP2_GATE.md`
**브랜치 명명:** `claude/review-plan-step2-<SESSION_ID>` (새 세션에서 생성)

#### 생성할 파일

```
ue5-review-bot/
├── scripts/
│   ├── gate_checker.py          # Gate 로직 (대규모 PR 판정)
│   └── utils/                   # 유틸리티 (향후 추가)
├── tests/
│   ├── fixtures/                # 테스트용 PR diff 샘플
│   │   ├── normal_pr.diff
│   │   ├── large_pr.diff
│   │   └── filtered_files.diff
│   └── test_gate_checker.py     # Gate Checker 유닛 테스트
```

#### 주요 구현 사항

1. **gate_checker.py:**
   - `gate_config.yml` 파싱
   - PR diff 파일 목록 추출
   - skip_patterns 필터링
   - 대규모 PR 판정 (파일 수 + 레이블)
   - 출력: `is_large_pr: true/false`, `reviewable_files: []`

2. **테스트 픽스처:**
   - 정상 PR (20파일, .cpp/.h)
   - 대규모 PR (60파일)
   - 필터링 대상 포함 PR (ThirdParty, .generated.h)

3. **pytest 유닛 테스트:**
   - 파일 필터링 검증
   - 대규모 판정 로직 검증
   - edge case 처리

---

## 📁 현재 레포지토리 구조

```
ue5-review-bot/
├── PLAN.md                      # 전체 계획서
├── HANDOFF.md                   # 이 파일
├── configs/                     # ✅ Step 1 완료
│   ├── .clang-format
│   ├── .editorconfig
│   ├── checklist.yml
│   └── gate_config.yml
└── docs/
    └── steps/                   # Step별 상세 스펙
        ├── STEP1_CONFIGS.md     # ✅ 완료
        ├── STEP2_GATE.md        # 🔜 다음
        ├── STEP3_STAGE1.md
        ├── STEP5_STAGE2.md      # (STEP4는 없음)
        ├── STEP6_STAGE3.md
        └── STEP7_WORKFLOWS.md
```

---

## 🔑 중요 정보

### Git 브랜치 규칙

- **브랜치 명명:** `claude/review-plan-step<N>-<SESSION_ID>`
- **Step 1 브랜치:** `claude/review-plan-step1-D8194` (이미 푸시됨)
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

2. **Step 2 스펙 읽기:**
   ```bash
   cat docs/steps/STEP2_GATE.md
   ```

3. **새 브랜치 생성 (또는 기존 브랜치 체크아웃):**
   ```bash
   git checkout -b claude/review-plan-step2-<NEW_SESSION_ID>
   ```

4. **작업 시작:**
   - `scripts/gate_checker.py` 구현
   - `tests/fixtures/` 생성
   - `tests/test_gate_checker.py` 작성
   - pytest 실행 및 검증
   - 커밋/푸시

---

## 📝 메모

- PDF 파일 (`CodeReviewCheckList.pdf`, `CodingConvention.pdf`)은 main 브랜치의 `docs/` 디렉토리에 보관
- 현재 환경에서는 PDF 파싱 도구 설치 불가 → STEP1_CONFIGS.md 스펙 기반으로 작성 완료
- `.clang-tidy` 설정은 Step 5에서 생성 (compile_commands.json과 함께)
- `checklist.yml`의 tier 분류가 각 Stage 스크립트 구현의 기준이 됨

---

**이 문서는 세션 간 작업 인계를 위한 것입니다.**
**최신 상태 확인:** `git log --oneline --graph --all`
