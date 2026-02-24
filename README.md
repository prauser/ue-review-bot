# UE5 Code Review Bot

GitHub Enterprise Server에서 UE5 C++ 프로젝트를 자동으로 코드리뷰하는 봇 시스템입니다.

## Architecture

```
PR open/sync ─┬─▶ Gate ─────▶ Stage 1 (regex 패턴 + clang-format)
              │                   │
              │                   ├──▶ Stage 2 (clang-tidy, 조건부)
              │                   │        │
              │                   │        ├──▶ Stage 3 (LLM 시맨틱 리뷰, 일반 PR만)
              │                   │        │        │
              │                   ▼        ▼        ▼
              └─────────────────▶ Post Review (findings 통합 → PR 코멘트)
```

### 3-Tier 리뷰 전략

| Stage | 방식 | 실행 조건 | 항목 수 |
|-------|------|-----------|---------|
| **Stage 1** | regex 패턴 + clang-format | 항상 | 7개 패턴 |
| **Stage 2** | clang-tidy 정적 분석 | `compile_commands.json` 있을 때 | 9개 체크 |
| **Stage 3** | LLM 시맨틱 리뷰 (Claude) | 일반 PR만 (대규모 PR 차단) | 30+ 항목 |

### 대규모 PR 안전장치

- **파일 필터**: ThirdParty, 자동생성 파일, 바이너리 자동 제외
- **규모 판정**: 필터 후 50개 파일 초과 OR 대규모 레이블 → 대규모 PR
- **대규모 PR**: Stage 1 + Stage 2만 실행, Stage 3(LLM) 차단
- **일반 PR**: Stage 1 + Stage 2 + Stage 3 모두 실행

## Quick Start

### 1. Runner 환경 준비

기존 조직 러너를 사용하거나 새 러너를 등록합니다. 상세 가이드: [docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md)

**기존 러너 사용 시:** 워크플로우 YAML의 `runs-on` 값을 기존 러너 라벨로 변경하세요.

**Runner에 필요한 도구:**

```bash
# 필수
python3 --version   # 3.10+
pip install pyyaml

# Stage 1 (clang-format)
clang-format --version   # 16+

# Stage 2 (clang-tidy, 선택)
clang-tidy --version     # 16+

# Stage 3 (LLM)
# ANTHROPIC_API_KEY를 Secrets에 등록
```

> 도구가 설치되어 있지 않은 경우 워크플로우 내 자동 설치로 해결할 수 있습니다. [SETUP_GUIDE.md](docs/SETUP_GUIDE.md)를 참고하세요.

### 2. Secrets 등록

게임 레포의 Settings > Secrets에 등록:

| Secret | 용도 |
|--------|------|
| `BOT_REPO_TOKEN` | 봇 레포 read 권한 PAT |
| `ANTHROPIC_API_KEY` | Claude API 키 (Stage 3) |
| `GHES_URL` | GHES 주소 (예: `https://github.company.com`) |
| `GHES_TOKEN` | PR Review 쓰기 권한 PAT |

### 3. Workflow 파일 복사

`workflows/` 디렉토리의 YAML 파일을 게임 레포의 `.github/workflows/`에 복사합니다:

```bash
# 게임 레포에서
cp <bot-repo>/workflows/code-review.yml .github/workflows/
cp <bot-repo>/workflows/code-review-manual.yml .github/workflows/
```

## 사용법

### 자동 리뷰

PR을 생성하거나 커밋을 푸시하면 자동으로 리뷰가 실행됩니다.

### 수동 리뷰 (`/review`)

PR 코멘트에 `/review`를 입력하면 수동 리뷰가 실행됩니다.

- 코멘트에 :eyes: 리액션이 추가됨 → 리뷰 시작
- 완료 후 :+1: (성공) 또는 :-1: (실패) 리액션 추가

Actions 탭에서 `UE5 Code Review (Manual)` 워크플로우를 직접 실행할 수도 있습니다 (`workflow_dispatch`).

## 설정 커스터마이징

### `configs/gate_config.yml`

- `skip_patterns`: 분석 제외 파일 패턴 (ThirdParty, 바이너리 등)
- `max_reviewable_files`: 대규모 PR 임계값 (기본 50)
- `large_pr_labels`: 대규모 PR로 분류하는 레이블 목록

### `configs/checklist.yml`

- 전체 코드리뷰 체크리스트 (Tier 1/2/3 분류)
- Stage 1 regex 패턴 정의
- 항목 추가/수정 시 해당 Stage 스크립트에 자동 반영

### `configs/.clang-format`

- UE5 Epic 코딩 스타일 설정 (Allman, Tab=4, 120 cols)

### `configs/.clang-tidy`

- 9개 clang-tidy 체크 설정
- `HeaderFilterRegex: 'Source/.*'` (Engine 헤더 제외)

## 레포 구조

```
ue5-review-bot/
├── configs/                         # 설정 파일
│   ├── .clang-format                # UE5 코딩 스타일
│   ├── .clang-tidy                  # clang-tidy 체크 설정
│   ├── .editorconfig                # 에디터 설정
│   ├── checklist.yml                # 코드리뷰 체크리스트
│   └── gate_config.yml              # 대규모 PR 판정 설정
├── scripts/                         # 코드리뷰 스크립트
│   ├── gate_checker.py              # Gate: 대규모 PR 판정 + 파일 필터
│   ├── stage1_pattern_checker.py    # Stage 1: regex 패턴 검사
│   ├── stage1_format_diff.py        # Stage 1: clang-format suggestion
│   ├── stage2_tidy_to_suggestions.py # Stage 2: clang-tidy 변환
│   ├── stage3_llm_reviewer.py       # Stage 3: LLM 시맨틱 리뷰
│   ├── post_review.py               # PR Review 게시
│   └── utils/
│       ├── diff_parser.py           # unified diff 파싱
│       ├── gh_api.py                # GitHub API 유틸리티
│       └── token_budget.py          # 토큰 예산 관리
├── workflows/                       # GitHub Actions 워크플로우 (게임 레포에 복사)
│   ├── code-review.yml              # 자동 트리거 (PR open/sync)
│   └── code-review-manual.yml       # 수동 트리거 (/review, dispatch)
├── tests/                           # 테스트
│   ├── fixtures/                    # 테스트 픽스처
│   └── test_*.py                    # 유닛/통합 테스트
└── docs/
    ├── SETUP_GUIDE.md               # Runner 설치 가이드
    ├── CHECKLIST_REFERENCE.md       # 체크리스트 레퍼런스 (사람 가독용)
    └── steps/                       # Step별 구현 스펙
```

## 참조 문서

- [SETUP_GUIDE.md](docs/SETUP_GUIDE.md) — Runner 도구 설치 가이드
- [GHES_SETUP_GUIDE.md](docs/GHES_SETUP_GUIDE.md) — GitHub Enterprise Server 세팅 가이드
- [CHECKLIST_REFERENCE.md](docs/CHECKLIST_REFERENCE.md) — 전체 체크리스트 레퍼런스
- [PLAN.md](PLAN.md) — 전체 프로젝트 계획서
