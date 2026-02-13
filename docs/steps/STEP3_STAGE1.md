# STEP3_STAGE1.md — 패턴 검사 + 포맷 Suggestion

## 산출물

| 파일 | 설명 |
|------|------|
| `scripts/stage1_pattern_checker.py` | regex 기반 패턴 검사 |
| `scripts/stage1_format_diff.py` | clang-format diff → suggestion 변환 |
| `scripts/utils/diff_parser.py` | unified diff 파싱 유틸 |
| `tests/test_pattern_checker.py` | 패턴 검사 테스트 |
| `tests/test_format_diff.py` | 포맷 suggestion 테스트 |

---

## 1. `scripts/utils/diff_parser.py`

unified diff를 파싱하여 파일별 + 라인별로 구조화하는 유틸.

```python
# 입력: git diff 텍스트
# 출력: {
#   "Source/MyActor.cpp": {
#     "added_lines": {42: "auto x = GetSomething();", 43: "..."},
#     "hunks": [{"start": 40, "end": 50, "content": "..."}]
#   }
# }
```

- 추가된 라인(+)만 추출, 삭제(-) 무시
- 라인 번호는 새 파일 기준으로 매핑
- hunk 컨텍스트도 보존 (Stage 3에서 사용)

---

## 2. `scripts/stage1_pattern_checker.py`

### CLI

```
python stage1_pattern_checker.py \
  --files '["Source/A.cpp", "Source/B.h"]' \
  --base-ref origin/main \
  --output findings-stage1.json
```

### 검사 패턴

**변경된 라인에 대해서만 검사. 이전 14개에서 7개로 축소 (나머지는 Stage 3 LLM으로 이관).**

| ID | regex/로직 | severity | auto_fixable |
|----|----------|----------|-------------|
| `logtemp` | `\bLogTemp\b` | warning | false |
| `pragma_optimize_off` | `#pragma\s+optimize\s*\(\s*""\s*,\s*off\s*\)` | error | false |
| `hard_asset_path` | `TEXT\s*\(\s*"\/(?:Game\|Engine)\/` | warning | false |
| `sync_load_runtime` | `\b(LoadObject\|StaticLoadObject\|LoadSynchronous)\s*[<(]` | warning | false |
| `macro_no_semicolon` | `(UE_LOG\|check\|ensure)...)\s*$` | warning | true (줄 끝에 `;` 추가) |
| `check_side_effect` | `(check\|checkf)\s*\([^;]*\b\w+\s*\(` | error | false |
| `unbraced_shipping_macro` | 중괄호 없는 if/for + UE_LOG/check | error | false |

> **이관된 항목 (Stage 3에서 처리):** `auto_non_lambda`, `yoda_condition`, `not_operator_in_if`, `sandwich_inequality`, `fsimpledelegate`, `loctext_no_undef`, `constructorhelpers_outside_ctor`

**파일 단위 검사는 모두 Stage 3으로 이관됨.** Stage 1은 라인 단위 regex만 수행.

### 출력 JSON

```json
[
  {
    "file": "Source/MyActor.cpp",
    "line": 42,
    "rule_id": "logtemp",
    "severity": "warning",
    "message": "LogTemp 대신 적절한 로그 카테고리를 사용하세요.",
    "suggestion": null
  }
]
```

---

## 3. `scripts/stage1_format_diff.py`

### CLI

```
python stage1_format_diff.py \
  --files '["Source/A.cpp"]' \
  --clang-format-config configs/.clang-format \
  --output suggestions-format.json
```

### 동작

1. 변경된 파일에 `clang-format` 실행 (실제 포맷팅)
2. 원본 vs 포맷팅 diff 생성
3. diff 있는 부분만 suggestion 블록으로 변환

### 주의

- suggestion은 PR diff 범위 안에 있는 라인에만 가능
- PR에서 변경되지 않은 라인은 일반 코멘트로 전환
- 하나의 suggestion은 최대 20줄로 청크 분리

### 출력 JSON

```json
[
  {
    "file": "Source/MyActor.cpp",
    "line": 10,
    "end_line": 12,
    "rule_id": "clang_format",
    "severity": "suggestion",
    "message": "clang-format 자동 수정 제안",
    "suggestion": "    if (bFlag == false)\n    {\n        DoSomething();\n    }"
  }
]
```

---

## 4. 테스트

### `test_pattern_checker.py`

- `sample_bad.cpp` diff에서 7개 패턴 전부 검출 확인
- `sample_good.cpp` diff에서 false positive 0 확인
- 주석 안의 `LogTemp`는 (선택적으로) 무시 확인
- 이관된 항목(auto, yoda 등)이 Stage 1에서 검출되지 않음을 확인

### `test_format_diff.py`

- 탭/스페이스 혼용 → suggestion 생성
- 이미 올바른 포맷 → suggestion 없음
- 20줄 초과 diff → 청크 분리 확인
