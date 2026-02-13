# PLAN.md — UE5 코드리뷰 자동화 시스템

> Claude Code 실행용 메인 계획서. 각 Step의 상세 스펙은 `docs/steps/` 하위 문서를 참조.

## 환경

- GitHub Enterprise Server 3.x + Self-hosted Runner
- UE5 C++ + Blueprint 혼합 프로젝트
- 2-레포 구조: 봇 레포(`ue5-review-bot`) + 게임 레포(yml 2개만 추가)
- 모든 수정은 PR Review `suggestion` 블록으로만 제안 (자동 커밋 없음)

## 레포 구조

```
ue5-review-bot/              # Claude Code가 생성
├── configs/                  # .clang-format, .editorconfig, checklist.yml, gate_config.yml
├── scripts/                  # gate_checker, stage1~3, post_review, utils/
├── tests/                    # fixtures/ + 유닛 테스트
├── workflows/                # 게임 레포에 복사할 yml 템플릿 2개
└── docs/
    ├── steps/                # Step별 상세 스펙 (아래 참조)
    ├── CHECKLIST_REFERENCE.md
    └── SETUP_GUIDE.md

MyGameProject/               # 기존 게임 레포
└── .github/workflows/        # code-review.yml, code-review-manual.yml (복사)
```

## 3-Tier 전략

```
Stage 1 (확정적 검사)     — regex 패턴 7개 + clang-format
Stage 2 (정적 분석)       — clang-tidy (compile_commands.json 있을 때만)
Stage 3 (LLM 리뷰)       — Stage 1에서 이관된 7개 포함, 의미론적 리뷰 전체
```

### Stage 1 → Stage 3 이관 항목

regex 유지보수 비용 대비 LLM이 더 정확하거나 충분히 커버 가능하여 이관:

| 이관 항목 | 이관 이유 |
|----------|---------|
| `auto_non_lambda` | 람다 변수 여부를 regex로 판단하기 어려움 |
| `yoda_condition` | 컨벤션 위반, 오탐 시 영향 적음 |
| `not_operator_in_if` | `!IsValid` 등 예외 처리가 regex로 까다로움 |
| `sandwich_inequality` | 드문 패턴, regex 유지 가치 낮음 |
| `fsimpledelegate` | 드문 패턴, LLM이 충분히 검출 |
| `loctext_no_undef` | 파일 단위 검사라 별도 로직 필요 |
| `constructorhelpers_outside_ctor` | 생성자 안/밖은 AST 수준 판단 |

### Stage 1 유지 항목 (7개)

| 유지 항목 | 유지 이유 |
|----------|---------|
| `logtemp` | 단순 매칭, 100% 검출 보장 |
| `pragma_optimize_off` | 절대 놓치면 안 됨 |
| `hard_asset_path` | regex가 확실, 빈번 |
| `macro_no_semicolon` | auto-fix suggestion 생성 목적 |
| `check_side_effect` | shipping 빌드 이슈, 게이트키퍼 |
| `unbraced_shipping_macro` | 구조적 버그 |
| `sync_load_runtime` | 빈번하고 명확 (LLM이 로딩/런타임 문맥 보강) |

### Stage 2 — 조건부 실행

- compile_commands.json **있으면**: clang-tidy 실행
- **없으면**: Stage 2 skip → LLM이 커버 (override, virtual 소멸자, 불필요 복사 등)

## PR 호출 흐름

```
PR 생성 → yml 트리거 → checkout (게임+봇) → Gate
→ Stage 1 (regex 7개 + clang-format)
→ (compile_commands.json 있으면) Stage 2 (clang-tidy)
→ (일반 PR만) Stage 3 (LLM — 이관 항목 포함 전체 리뷰)
→ PR Review 코멘트 게시
```

## 대규모 PR 안전장치

- **파일 필터** (항상): ThirdParty, 자동생성, 바이너리 → 분석 대상에서 제외 (규모 판정 무관)
- **규모 판정**: 필터 후 reviewable 파일 50개 초과 OR 대규모 라벨 → 대규모 PR
- 대규모: 자동 시 Stage 1만 / `/review` 수동 시 Stage 1+2 / Stage 3은 항상 차단

---

## 실행 Step (7개)

| Step | 내용 | 상세 스펙 | 산출물 |
|------|------|---------|-------|
| 1 | configs 생성 | `docs/steps/STEP1_CONFIGS.md` | .clang-format, .editorconfig, checklist.yml, gate_config.yml |
| 2 | 테스트 픽스처 + Gate | `docs/steps/STEP2_GATE.md` | gate_checker.py, test_gate_checker.py, fixtures/ |
| 3 | Stage 1 (패턴 7개 + 포맷) | `docs/steps/STEP3_STAGE1.md` | stage1_pattern_checker.py, stage1_format_diff.py, utils/diff_parser.py, 테스트 |
| 4 | PR 코멘트 게시 | `docs/steps/STEP4_POST_REVIEW.md` | post_review.py, utils/gh_api.py, 테스트 |
| 5 | Stage 2 (clang-tidy, 조건부) | `docs/steps/STEP5_STAGE2.md` | stage2_tidy_to_suggestions.py, 테스트 |
| 6 | Stage 3 (LLM 리뷰, 확장) | `docs/steps/STEP6_STAGE3.md` | stage3_llm_reviewer.py, utils/token_budget.py, 테스트 |
| 7 | 워크플로우 + 문서 | `docs/steps/STEP7_WORKFLOWS.md` | workflows/*.yml, README.md, SETUP_GUIDE.md |

각 Step 시작 시 해당 `docs/steps/STEP*.md`를 읽고 구현하세요.

---

## Secrets (게임 레포에 등록)

| Secret | 용도 |
|--------|------|
| `BOT_REPO_TOKEN` | 봇 레포 read 권한 PAT |
| `ANTHROPIC_API_KEY` | Claude API 키 (Stage 3) |
| `GHES_URL` | `https://github.company.com` |
| `GHES_TOKEN` | PR Review 쓰기 권한 PAT |

## 제약 조건

1. Runner → `api.anthropic.com` HTTPS 아웃바운드 필요
2. LLM에는 diff만 전송 (전체 소스 X)
3. Blueprint(.uasset)은 범위 밖
4. compile_commands.json 없으면 Stage 2 skip → LLM이 커버
5. PVS-Studio는 현재 제외 (향후 추가 시 LLM에서 해당 항목 제거)
6. GHES 3.4+ 확인 시 멀티라인 suggestion 사용, 아니면 fallback
7. 봇 레포 private이면 `BOT_REPO_TOKEN` 필수
