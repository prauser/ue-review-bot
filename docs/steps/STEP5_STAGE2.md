# STEP5_STAGE2.md — clang-tidy 결과 변환 (조건부 실행)

> **compile_commands.json이 없으면 Stage 2 전체 skip.** LLM(Stage 3)이 override 누락, virtual 소멸자, 불필요 복사 등을 대신 커버.

## 산출물

| 파일 | 설명 |
|------|------|
| `configs/.clang-tidy` | clang-tidy 체크 설정 |
| `scripts/stage2_tidy_to_suggestions.py` | clang-tidy 결과 → suggestion 변환 |
| `tests/test_stage2.py` | 변환 로직 테스트 |

---

## 1. `scripts/stage2_tidy_to_suggestions.py`

### CLI

```
python stage2_tidy_to_suggestions.py \
  --tidy-fixes fixes.yaml \
  --stage1-results findings-stage1.json \
  --output findings-stage2.json
```

### 동작

1. clang-tidy `--export-fixes` YAML 파싱
   - fix가 있는 항목 → suggestion 블록으로 변환 (예: `override` 추가)
   - fix가 없는 항목 → 일반 코멘트로 변환
2. Stage 1 결과와 **중복 제거** (같은 file + line이면 skip)

### PVS-Studio 확장 포인트

`--pvs-report` 옵션 인터페이스만 준비. 인자 없으면 clang-tidy만 처리.

```python
# 향후 구현 시:
# --pvs-report pvs-report.json
# V609 → error (0 나누기)
# V530 → error (check 사이드이펙트)
# 기타 → warning
```

### 출력 JSON

Stage 1과 동일한 포맷:

```json
[
  {
    "file": "Source/MyActor.cpp",
    "line": 55,
    "rule_id": "modernize-use-override",
    "severity": "warning",
    "message": "override 키워드를 추가하세요.",
    "suggestion": "    virtual void BeginPlay() override;"
  }
]
```

---

## 2. `.clang-tidy` 설정

```
cppcoreguidelines-virtual-class-destructor
bugprone-virtual-near-miss
performance-unnecessary-copy-initialization
performance-for-range-copy
modernize-use-override
clang-analyzer-optin.cplusplus.VirtualCall
bugprone-division-by-zero
readability-else-after-return
readability-redundant-smartptr-get
```

- `HeaderFilterRegex`: 프로젝트 Source 경로만 (Engine 헤더 제외)

## 3. compile_commands.json — 조건부 실행의 핵심

UE5에서 생성 방법 (워크플로우에서 처리, 이 스크립트 범위 밖):

- `UnrealBuildTool -Mode=JsonExport`
- 또는 `GenerateProjectFiles` → .sln → `compdb` 변환

**compile_commands.json이 없으면 Stage 2 전체를 skip.** 워크플로우에서 파일 존재 여부로 분기. LLM(Stage 3)이 override, virtual 소멸자, 불필요 복사 등을 대신 커버하므로 품질 저하는 제한적.

---

## 4. 테스트

- fixes.yaml에 fix 있는 항목 → suggestion 생성 확인
- fix 없는 항목 → 일반 코멘트 확인
- Stage 1과 같은 라인 지적 → 중복 제거 확인
- fixes.yaml 없거나 비어있을 때 → 빈 결과
- `--pvs-report` 없이 실행 → clang-tidy만 처리 확인
