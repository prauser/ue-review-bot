# STEP2_GATE.md — 테스트 픽스처 + Gate Checker

## 산출물

| 파일 | 설명 |
|------|------|
| `tests/fixtures/sample_bad.cpp` | 의도적 규칙 위반 샘플 |
| `tests/fixtures/sample_good.cpp` | 규칙 준수 샘플 |
| `tests/fixtures/sample_network.cpp` | 네트워크 위반 샘플 |
| `tests/fixtures/sample_diff.patch` | 테스트용 diff |
| `scripts/gate_checker.py` | 대규모 PR 판정 + 파일 필터링 |
| `tests/test_gate_checker.py` | gate 로직 테스트 |

---

## 1. 테스트 픽스처

### `sample_bad.cpp` — 포함할 위반:

**Stage 1 (regex로 검출):**
- `UE_LOG(LogTemp, ...)`
- `check(SomeFunction())` (사이드이펙트)
- 중괄호 없는 `if` + `UE_LOG`
- `TEXT("/Game/Path/To/Asset")` (하드코딩 경로)
- `#pragma optimize("", off)`
- 매크로 끝에 세미콜론 누락
- `LoadObject<>` 런타임 동기 로딩

**Stage 3 (LLM으로 검출, 이관 항목 포함):**
- `auto` 사용 (람다 아닌 곳)
- `if (false == bFlag)` (요다 컨디션)
- `if (!bFlag)` (! 연산자)
- `FSimpleDelegateGraphTask` 사용
- UPROPERTY 없는 `UObject*` 멤버
- 매 Tick RPC 호출 패턴
- Transient 없는 런타임 UPROPERTY
- `GetWorld()->` null 체크 없이 사용
- `#define LOCTEXT_NAMESPACE` 후 `#undef` 누락
- ConstructorHelpers 생성자 외부 사용

### `sample_good.cpp` — 위 모든 항목을 올바르게 작성 (false positive 0 확인용)

### `sample_network.cpp` — 네트워크 특화 위반:

- `DOREPLIFETIME` 조건 미설정
- Reliable 남용
- 매 Tick Replication 변수

---

## 2. `scripts/gate_checker.py`

### CLI 인터페이스

```
python gate_checker.py \
  --base-ref origin/main \
  --pr-number 123 \
  --config configs/gate_config.yml \
  --output gate-result.json
```

### 2단계 로직

**Step 1: 파일 필터** — `gate_config.yml`의 `skip_patterns`으로 분석 대상(reviewable) / 제외(skipped) 분리. C++ 확장자(.cpp, .h, .inl)가 아닌 파일도 제외.

**Step 2: 규모 판정** — reviewable 파일 수 > `max_reviewable_files` OR PR 라벨이 `large_pr_labels`에 포함 → 대규모.

### 출력 JSON

```json
{
  "is_large_pr": false,
  "reasons": [],
  "allowed_stages": [1, 2, 3],
  "manual_allowed_stages": [1, 2, 3],
  "total_changed_files": 115,
  "reviewable_files": ["Source/MyGame/MyActor.cpp"],
  "reviewable_count": 3,
  "skipped_files": [
    {"file": "ThirdParty/protobuf/...", "reason": "경로 필터: ThirdParty/"}
  ],
  "skipped_count": 112
}
```

### PR 라벨 조회

`utils/gh_api.py`의 `get_pr_labels()` 사용. 이 Step에서는 mock으로 테스트.

---

## 3. `tests/test_gate_checker.py`

- reviewable 3개 + skipped 100개 → 일반 PR (Stage 1~3)
- reviewable 60개 → 대규모 PR (Stage 1 only)
- migration 라벨 + reviewable 5개 → 대규모 PR
- 라벨 없음 + reviewable 50개 → 일반 PR (경계값)
- 모든 파일이 ThirdParty → reviewable 0개 → 일반 PR (리뷰할 것 없음, 코멘트만)
- .generated.h, .uasset 필터 확인
