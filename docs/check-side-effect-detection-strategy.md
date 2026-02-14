# check_side_effect 검출 전략

> **Rule ID**: `check_side_effect`
> **Current Tier**: 3 (LLM)
> **Target**: Hybrid Tier 1 (Regex Filter) + Tier 3 (LLM Verification)

---

## 🎯 문제 정의

### 규칙 요약
```yaml
summary: "check() 매크로 내부에 부작용 있는 코드 금지"
description: |
  check() 내부의 코드는 Shipping 빌드에서 제거됩니다.
  부작용이 있는 코드(함수 호출, 증감 연산 등)는 verify()를 사용하세요.
```

### 왜 중요한가?
```cpp
// ❌ CRITICAL BUG: Shipping 빌드에서 Index 증가 안됨!
check(++Index < MaxCount);  // Development: OK, Shipping: Index 증가 X

// ❌ CRITICAL BUG: Shipping에서 함수 호출 안됨!
check(ProcessItem(Item));   // Development: OK, Shipping: ProcessItem 호출 X

// ✅ CORRECT: verify()는 모든 빌드에서 실행
verify(++Index < MaxCount);
verify(ProcessItem(Item));

// ✅ CORRECT: check()는 순수 조건 검사만
check(Index < MaxCount);
check(Item != nullptr);
check(IsValid(Item));  // IsValid는 부작용 없음 (조회 함수)
```

### Shipping 빌드에서의 동작
```cpp
// UE5 매크로 정의 (Asserts.h)
#if DO_CHECK  // Development, Debug, DebugGame
  #define check(expr) { if (!(expr)) { FDebug::AssertFailed(...); } }
#else         // Shipping, Test
  #define check(expr) {}  // ← 완전히 제거됨!
#endif

#define verify(expr) { if (!(expr)) { FDebug::AssertFailed(...); } }  // 항상 실행
```

---

## 🤔 Regex 만으로 검출 가능한가?

### 결론
**순수 Regex로 100% 정확 검출은 불가능하지만, 1차 필터로는 충분히 유용합니다.**

### 문제점

#### 1. 부작용 여부는 "의미론적" 판단 필요

| 코드 | 부작용 여부 | Regex 판단 |
|------|------------|-----------|
| `check(IsValid(X))` | ❌ 없음 (조회) | ⚠️ 함수 호출 감지 |
| `check(X != nullptr)` | ❌ 없음 (비교) | ✅ 정확 |
| `check(++Index < N)` | ✅ 있음 (증감) | ✅ 정확 |
| `check(ProcessItem(X))` | ✅ 있음 (상태 변경) | ⚠️ 함수 호출 감지 |
| `check(X->GetNum() > 0)` | ❌ 없음 (getter) | ⚠️ 함수 호출 감지 |

**Regex 한계:**
- `IsValid()`, `GetNum()` 같은 순수 조회 함수도 "함수 호출"로 감지 → **False Positive**
- 함수 내부 구현을 알아야 부작용 여부 판단 가능 → **의미론적 분석 필요**

#### 2. 함수 호출 "화이트리스트" 필요

**안전한 UE5 함수들 (부작용 없음):**
```cpp
IsValid(), IsValidChecked(), IsInRange()
Num(), Len(), IsEmpty(), Max(), Min()
GetClass(), GetName(), GetFName()
HasAuthority(), HasLocalNetOwner()
GetWorld(), GetOwner(), GetOuter()
```

**위험한 패턴 (부작용 가능성):**
```cpp
++, --, +=, -=, *=, /=, %=, &=, |=, ^=, <<=, >>=  // 증감/복합 대입
= (단순 대입)
함수 호출 (화이트리스트 제외)
```

#### 3. False Positive vs False Negative 트레이드오프

| 전략 | 장점 | 단점 |
|------|------|------|
| **엄격한 Regex** (모든 함수 호출 감지) | 높은 재현율(Recall) | 높은 오탐(FP) - `IsValid()` 등 무고한 코드도 걸림 |
| **느슨한 Regex** (증감/대입만) | 낮은 오탐(FP) | 낮은 재현율 - `ProcessItem()` 같은 부작용 함수 놓침 |

---

## ✅ 권장 전략: Hybrid Approach

### 2단계 검출 파이프라인

```
┌──────────────────────────────────────────────┐
│ Stage 1 (Regex): Suspicious Pattern Filter  │
│ - 의심스러운 패턴 감지 (높은 재현율 목표)     │
│ - 화이트리스트 기반 False Positive 감소      │
│ - 빠른 1차 스크리닝                          │
└──────────────────┬───────────────────────────┘
                   │ 의심 케이스만 전달
                   ▼
┌──────────────────────────────────────────────┐
│ Stage 3 (LLM): Semantic Verification        │
│ - 부작용 실제 여부 정밀 분석                 │
│ - 함수 시그니처/구현 참고                    │
│ - 최종 판정 + 제안 (verify() 변환)          │
└──────────────────────────────────────────────┘
```

### Stage 1: Regex 패턴 (Tier 1 추가)

#### 목표
- **재현율(Recall) 최대화**: 위험 가능성 있는 모든 패턴 포착
- **화이트리스트로 정밀도(Precision) 보완**: 알려진 안전 함수 제외

#### 패턴 설계

**1) 위험 신호 감지 (Suspicious Patterns)**

```regex
# 패턴 1: 증감 연산자
check\s*\([^)]*(?:\+\+|--)[^)]*\)

# 패턴 2: 복합 대입 연산자
check\s*\([^)]*(?:\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=)[^)]*\)

# 패턴 3: 단순 대입 (비교 연산자 ==, != 제외)
check\s*\([^)]*[^=!<>]=(?!=)[^)]*\)

# 패턴 4: 함수 호출 (화이트리스트 제외)
check\s*\([^)]*\b(?!(?:IsValid|IsValidChecked|IsInRange|Num|Len|IsEmpty|Max|Min|GetClass|GetName|GetFName|HasAuthority|GetWorld|GetOwner|GetOuter)\b)\w+\s*\([^)]*\)
```

**2) 화이트리스트 (Known Safe Functions)**

```yaml
# safe_functions.yml
safe_ue5_functions:
  # UObject/GC
  - IsValid
  - IsValidChecked
  - IsPendingKill  # Deprecated but safe

  # Containers
  - Num
  - Len
  - IsEmpty
  - IsValidIndex
  - Contains
  - Find

  # Math/Comparison
  - IsInRange
  - Max
  - Min
  - Abs
  - Clamp

  # Reflection
  - GetClass
  - GetName
  - GetFName
  - GetFullName
  - GetPathName

  # Networking
  - HasAuthority
  - HasLocalNetOwner
  - GetLocalRole
  - GetRemoteRole

  # Hierarchy
  - GetWorld
  - GetOwner
  - GetOuter
  - GetAttachParent

  # Type Checks
  - IsA
  - IsChildOf
  - ImplementsInterface
```

**3) 통합 Regex (Tier 1 후보)**

```regex
# 최종 패턴 (화이트리스트 통합)
check\s*\(
  [^)]*
  (?:
    \+\+|--                                    # 증감
    |[+\-*/%&|^]=|<<=|>>=                      # 복합 대입
    |(?<![=!<>])=(?!=)                         # 단순 대입
    |\b(?!SAFE_FUNCS_HERE)\w+\s*\(             # 비화이트리스트 함수 호출
  )
  [^)]*
\)
```

#### checklist.yml 수정안

```yaml
# configs/checklist.yml

# Stage 1 (Regex) — 새로 추가
- id: check_side_effect_suspicious
  summary: "check() 매크로에 의심스러운 부작용 패턴 감지"
  description: |
    check() 내부에 증감 연산자, 대입, 함수 호출 등 부작용 가능성이 있는 패턴을 감지합니다.
    Stage 3(LLM)에서 최종 검증합니다.
  tier: 1
  severity: warning  # error 아님 (의심만)
  auto_fixable: false
  pattern: "check\\s*\\([^)]*(?:\\+\\+|--|[+\\-*/%&|^]=|<<=|>>=|(?<![=!<>])=(?!=))[^)]*\\)"
  rationale: "1차 필터로 의심 케이스 포착, LLM 부하 감소"
  tags: ["requires_llm_verification"]  # Stage 3에서 재검증 플래그

# Stage 3 (LLM) — 기존 유지 (최종 판정)
- id: check_side_effect
  summary: "check() 매크로 내부에 부작용 있는 코드 금지"
  description: |
    check() 내부의 코드는 Shipping 빌드에서 제거됩니다.
    부작용이 있는 코드(함수 호출, 증감 연산 등)는 verify()를 사용하세요.
  tier: 3
  severity: error
  auto_fixable: false
  rationale: "부작용 여부는 시맨틱 분석 필요 (LLM). Stage 1의 의심 케이스를 정밀 검증"
  suggestion: "check()를 verify()로 변경하거나 로직을 분리하세요"
```

---

### Stage 3: LLM 검증 프롬프트

```markdown
# LLM Prompt for check_side_effect

## Context
UE5에서 check() 매크로는 Shipping 빌드에서 완전히 제거됩니다.
따라서 check() 내부에 부작용이 있는 코드를 넣으면 Shipping에서 실행되지 않아 버그가 발생합니다.

## Task
다음 코드에서 check() 내부에 부작용이 있는지 분석하세요.

### 부작용 정의
- **있음**: 함수 호출 시 프로그램 상태 변경 (변수 수정, I/O, 외부 상태 변경)
- **없음**: 순수 조회 (getter, 비교, 타입 체크)

### 안전한 함수 목록 (부작용 없음)
{SAFE_FUNCTIONS_LIST}

### 분석 코드
```cpp
{CODE_SNIPPET}
```

### 질문
1. check() 내부에 부작용이 있는가? (Yes/No)
2. 근거는?
3. verify()로 변경이 필요한가?

### 예시 답변
**코드**: `check(++Index < MaxCount)`
**답변**:
1. Yes - `++Index`는 Index를 증가시키므로 부작용이 있습니다.
2. Shipping 빌드에서 Index 증가가 실행되지 않아 로직이 깨집니다.
3. ✅ verify()로 변경 필요
   ```cpp
   verify(++Index < MaxCount);  // 모든 빌드에서 실행
   ```

**코드**: `check(IsValid(Actor))`
**답변**:
1. No - IsValid()는 순수 조회 함수로 부작용이 없습니다.
2. 포인터 유효성만 검사하며 상태를 변경하지 않습니다.
3. ❌ 변경 불필요 - check() 사용이 적절합니다.
```

---

## 📊 예상 성능 지표

### Baseline (Tier 3 Only)
- **검출 케이스**: 모든 check() 사용
- **LLM 호출**: 매우 많음 (모든 check() 분석)
- **비용**: 높음
- **정확도**: 높음

### Hybrid (Tier 1 Filter + Tier 3)
- **Tier 1 필터**: 의심 패턴만 추출 (예상 50% 감소)
- **LLM 호출**: 절반으로 감소
- **비용**: 50% 절감
- **정확도**: 동일 (LLM이 최종 판정)

### 측정 지표
```python
# scripts/measure_check_side_effect.py
import re

def analyze_check_usage(codebase_path):
    """check() 사용 패턴 통계"""

    # 1. 전체 check() 사용 횟수
    total_checks = count_pattern(r'check\s*\(', codebase_path)

    # 2. Tier 1 필터에 걸리는 케이스
    suspicious_checks = count_pattern(
        r'check\s*\([^)]*(?:\+\+|--|[+\-*/%&|^]=)[^)]*\)',
        codebase_path
    )

    # 3. 화이트리스트 함수 사용 (안전)
    safe_checks = count_pattern(
        r'check\s*\(.*\b(IsValid|Num|GetClass|HasAuthority)\b.*\)',
        codebase_path
    )

    # LLM 호출 필요 케이스
    llm_required = suspicious_checks
    llm_reduction = 1 - (llm_required / total_checks)

    return {
        'total_checks': total_checks,
        'suspicious': suspicious_checks,
        'safe_checks': safe_checks,
        'llm_reduction': f"{llm_reduction * 100:.1f}%"
    }

# 예시 결과
# {
#   'total_checks': 1000,
#   'suspicious': 250,      # ← Tier 1 필터에 걸림
#   'safe_checks': 750,     # ← IsValid 등 안전 패턴
#   'llm_reduction': '75%'  # ← LLM 호출 75% 감소!
# }
```

---

## 🛠️ 구현 체크리스트

### Phase 1: Regex 패턴 개발
- [ ] 증감 연산자 패턴 테스트
- [ ] 복합 대입 연산자 패턴 테스트
- [ ] 함수 호출 화이트리스트 구축
  - [ ] UE5 공식 문서에서 순수 함수 목록 추출
  - [ ] 프로젝트별 커스텀 getter 함수 추가
- [ ] False Positive 테스트 케이스 작성
- [ ] False Negative 테스트 케이스 작성

### Phase 2: checklist.yml 통합
- [ ] `check_side_effect_suspicious` (Tier 1) 항목 추가
- [ ] `check_side_effect` (Tier 3) 항목 유지
- [ ] `tags: ["requires_llm_verification"]` 추가

### Phase 3: LLM 프롬프트 개발
- [ ] 화이트리스트 동적 주입 로직
- [ ] Few-shot 예시 작성
- [ ] Chain-of-Thought 프롬프트 최적화

### Phase 4: 성능 측정
- [ ] 기존 코드베이스에서 check() 사용 통계 수집
- [ ] Tier 1 필터 효과 측정 (LLM 호출 감소율)
- [ ] False Positive/Negative 비율 측정

### Phase 5: 점진적 롤아웃
- [ ] Week 1: Tier 1 필터만 warning으로 활성화
- [ ] Week 2: LLM 검증 파이프라인 연동
- [ ] Week 3: 결과 모니터링 및 화이트리스트 튜닝
- [ ] Week 4: severity를 error로 승격

---

## 🔍 실전 예시

### Case 1: 증감 연산자 (쉬운 케이스)

```cpp
// ❌ BAD
int Index = 0;
check(++Index < MaxCount);  // Shipping에서 Index 증가 안됨!

// ✅ FIX 1: verify() 사용
verify(++Index < MaxCount);

// ✅ FIX 2: 로직 분리
++Index;
check(Index < MaxCount);
```

**Tier 1 Regex**: ✅ 감지 (`\+\+`)
**Tier 3 LLM**: ✅ 부작용 확정, verify() 제안

---

### Case 2: 함수 호출 (문맥 필요)

```cpp
// 🤔 안전한가?
check(IsValid(Actor));
check(Actor->GetName().Len() > 0);

// ❌ 위험!
check(ProcessNextItem());  // ProcessNextItem이 상태 변경함
```

**Tier 1 Regex**:
- `IsValid()` → ⚠️ 함수 호출 감지, 하지만 화이트리스트에 있음 → ✅ Pass
- `GetName().Len()` → ⚠️ Len()은 화이트리스트, GetName()도 안전 → ✅ Pass
- `ProcessNextItem()` → ❌ 화이트리스트 없음, LLM 검증 필요

**Tier 3 LLM**:
- `ProcessNextItem()` 시그니처 분석
  ```cpp
  bool ProcessNextItem() {
      CurrentIndex++;  // ← 상태 변경 감지!
      return CurrentIndex < Items.Num();
  }
  ```
- 판정: ❌ 부작용 있음, verify() 변환 필요

---

### Case 3: 복잡한 표현식

```cpp
// 🤔 안전한가?
check(Actor && Actor->IsA<ACharacter>() && Actor->GetOwner() != nullptr);
```

**Tier 1 Regex**: ✅ Pass (모든 함수가 화이트리스트)
**Tier 3 LLM**: 검증 불필요 (Tier 1에서 통과)

---

### Case 4: 프로젝트별 커스텀 함수

```cpp
// 프로젝트 커스텀 함수
bool IsPlayerAlive(ACharacter* Character) {
    if (!Character) return false;
    return Character->GetHealth() > 0.0f;  // ← 순수 조회
}

// 사용
check(IsPlayerAlive(Player));
```

**Tier 1 Regex**: ⚠️ `IsPlayerAlive()`는 화이트리스트 없음 → LLM 검증
**Tier 3 LLM**: 함수 구현 분석 → 순수 조회 → ✅ 안전
**후속 조치**: `IsPlayerAlive`를 프로젝트 화이트리스트에 추가

---

## 💡 추가 개선 아이디어

### 1. 프로젝트별 화이트리스트 자동 학습

```python
# scripts/learn_safe_functions.py
def auto_learn_safe_functions(codebase_path):
    """LLM이 승인한 안전 함수를 자동으로 화이트리스트에 추가"""

    # 1. Tier 1에서 의심으로 감지된 함수들 수집
    suspicious_funcs = collect_suspicious_functions()

    # 2. LLM 검증 결과 수집
    for func in suspicious_funcs:
        if llm_verdict[func] == "safe" and llm_confidence > 0.9:
            add_to_whitelist(func)

    # 3. 화이트리스트 업데이트
    save_whitelist("configs/project_safe_functions.yml")
```

### 2. CI/CD 통합 시 Incremental 분석

```yaml
# .github/workflows/code-review.yml
- name: Check Side Effect (Incremental)
  run: |
    # 변경된 파일만 분석
    git diff --name-only origin/main | grep '\.cpp$' | \
      xargs python scripts/check_side_effect.py --incremental
```

### 3. IDE Plugin (실시간 경고)

```json
// VSCode Extension 설정
{
  "ue-review-bot.checkSideEffect": {
    "enabled": true,
    "level": "warning",  // Development 단계에서는 warning
    "whitelist": ["IsValid", "Num", "GetClass"]
  }
}
```

---

## 📚 참고 자료

### UE5 공식 문서
- [Assertions (check, verify, ensure)](https://dev.epicgames.com/documentation/en-us/unreal-engine/asserts-in-unreal-engine)
- [Build Configurations](https://dev.epicgames.com/documentation/en-us/unreal-engine/build-configurations-reference-for-unreal-engine)

### 관련 이슈
- [UE-12345: check() with side effects in Shipping builds](https://example.com) (가상 링크)

### 내부 문서
- `configs/checklist.yml` - 규칙 정의
- `docs/tier2-implementation-plan.md` - Tier 2 구현 계획

---

## ✅ 최종 권장사항

| 접근법 | 채택 여부 | 이유 |
|--------|----------|------|
| **Tier 3 Only** (현재) | ❌ | LLM 호출 과다, 비용 높음 |
| **Tier 1 Only** (Regex) | ❌ | False Positive/Negative 높음 |
| **Hybrid (Tier 1 + Tier 3)** | ✅ | 비용 50% 절감 + 정확도 유지 |

### 다음 단계
1. **Short-term**: Tier 1 regex 패턴을 `checklist.yml`에 추가 (warning)
2. **Mid-term**: 화이트리스트 구축 및 테스트
3. **Long-term**: LLM 학습으로 프로젝트별 화이트리스트 자동 확장

---

## 📝 변경 이력

| Date | Author | Changes |
|------|--------|---------|
| 2026-02-13 | Claude | 초안 작성 - check_side_effect 검출 전략 |
