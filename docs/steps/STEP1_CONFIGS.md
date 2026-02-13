# STEP1_CONFIGS.md — 설정 파일 생성

## 산출물

| 파일 | 설명 |
|------|------|
| `configs/.clang-format` | UE5 Epic 스타일 포맷 설정 |
| `configs/.editorconfig` | 에디터 통일 설정 |
| `configs/checklist.yml` | 코드리뷰 체크리스트 (기계 판독용) |
| `configs/gate_config.yml` | 대규모 PR 판정 설정 |

> `.clang-tidy` 설정은 Step 5(Stage 2)에서 compile_commands.json과 함께 생성. 조건부 실행이므로 여기서는 제외.

---

## 1. `.clang-format`

```yaml
# 핵심 요구사항:
UseTab: Always
TabWidth: 4
IndentWidth: 4
BreakBeforeBraces: Allman
ColumnLimit: 120
SortIncludes: false          # UE 헤더 순서 보존 (PCH 이슈 방지)
PointerAlignment: Left       # int* Ptr (UE 스타일)
AccessModifierOffset: -4
AllowShortIfStatementsOnASingleLine: false
AllowShortLoopsOnASingleLine: false
NamespaceIndentation: All
```

## 2. `.editorconfig`

```ini
[*.{cpp,h,inl}]
indent_style = tab
indent_size = 4
charset = utf-8
end_of_line = lf
trim_trailing_whitespace = true
insert_final_newline = true
```

## 3. `checklist.yml`

프로젝트에 포함된 `CodeReviewCheckList.pdf`와 `CodingConvention.pdf`를 파싱하여 생성.

구조:

```yaml
categories:
  - id: cpp_common
    name: "C++ Common"
    items:
      - id: no_auto
        summary: "auto 사용 금지 (람다 변수 제외)"
        tier: 3          # Stage 3(LLM)으로 이관됨
        severity: warning
        auto_fixable: false
      - id: logtemp
        summary: "LogTemp 대신 적절한 로그 카테고리 사용"
        tier: 1          # 1=Stage1 regex (유지)
        severity: warning
        auto_fixable: false
        pattern: "\\bLogTemp\\b"
      - id: virtual_destructor
        summary: "다형성 최상위 클래스 소멸자에 virtual 필수"
        tier: 3          # compile_commands.json 없으면 LLM이 커버
        severity: error
```

**tier 설명:**
- `1`: Stage 1 regex로 확정적 검출 (7개 패턴)
- `2`: Stage 2 clang-tidy (compile_commands.json 있을 때만)
- `3`: Stage 3 LLM이 커버 (이관된 항목 + 원래 LLM 담당)

**원본 대비 수정사항:**

- 중복 병합: const& 규칙, Smart Pointer 규칙
- 오타 수정: CutomizedUV → CustomizedUV, Bolier → Boiler, structurical → Structural
- "재귀 금지" → "깊이 제한 없는 재귀/UObject 그래프 재귀"로 범위 한정
- WIP 항목(logical/structural) 제외
- 추가 항목:
  - NewObject<> Outer null 체크
  - GetWorld() null 체크
  - override 키워드 누락
  - 클라이언트 RPC 권한 검증 누락
  - UFUNCTION(BlueprintCallable) Category 누락
  - ConstructorHelpers를 생성자 외부에서 사용

## 4. `gate_config.yml`

```yaml
# 파일 필터 (항상 적용, 규모 판정과 무관)
skip_patterns:
  - "ThirdParty/"
  - "Plugins/.*/ThirdParty/"
  - "External/"
  - "Vendor/"
  - "\\.generated\\.h$"
  - "\\.gen\\.cpp$"
  - "Intermediate/"
  - "DerivedDataCache/"
  - "\\.uasset$"
  - "\\.umap$"

# 규모 판정 (필터 후 남은 파일 기준)
max_reviewable_files: 50
large_pr_labels:
  - "migration"
  - "large-change"
  - "engine-update"
  - "mass-refactor"
```
