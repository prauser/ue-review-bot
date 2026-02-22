# Checklist Reference — UE5 코드리뷰 체크리스트

> 원본: CodeReviewCheckList.pdf, CodingConvention.pdf 기반
> 각 항목의 검사 단계(Stage)와 자동 수정 가능 여부를 표기합니다.

---

## C++ Common

| # | ID | 항목 | Stage | Severity | Auto-fix |
|---|-----|------|-------|----------|----------|
| 1 | `auto_non_lambda` | auto 사용 금지 (람다 변수 제외) | Stage 3 (LLM) | warning | - |
| 2 | `yoda_condition` | Yoda 조건식 금지 (`if (5 == x)`) | Stage 3 (LLM) | warning | - |
| 3 | `not_operator_in_if` | if 조건에 `!` 연산자 사용 자제 | Stage 3 (LLM) | info | - |
| 4 | `const_ref_param` | 함수 파라미터에 `const&` 사용 | Stage 3 (LLM) | warning | - |
| 5 | `smart_pointer_usage` | 스마트 포인터 올바른 사용 | Stage 3 (LLM) | warning | - |
| 6 | `override_keyword` | 가상 함수 오버라이드 시 `override` 필수 | Stage 2 (clang-tidy) | error | - |
| 7 | `virtual_destructor` | 다형성 클래스 소멸자에 `virtual` 필수 | Stage 2 (clang-tidy) | error | - |
| 8 | `unnecessary_copy` | 불필요한 복사 방지 (`const auto&` 사용) | Stage 2 (clang-tidy) | warning | - |
| 9 | `deep_recursion` | 깊이 제한 없는 재귀 금지 | Stage 3 (LLM) | error | - |

### auto 사용 규칙 상세

- `auto`는 람다 변수를 제외하고 사용하지 않습니다
- 명시적 타입을 사용하여 가독성을 높이세요
- 예: `auto Lambda = [](){ ... };` (허용)
- 예: `auto Foo = GetFoo();` (금지 → `UFoo* Foo = GetFoo();`)

### 스마트 포인터 규칙 상세

| 타입 | 용도 |
|------|------|
| `TSharedPtr` | 공유 소유권 |
| `TWeakPtr` | 순환 참조 방지 |
| `TUniquePtr` | 독점 소유권 |
| UObject* | `UPROPERTY()`로 관리 (스마트 포인터 사용 금지) |

---

## UE-Specific

### Stage 1 (Regex 패턴 검사) — 7개 항목

| # | ID | 항목 | Severity | Auto-fix | 패턴 |
|---|-----|------|----------|----------|------|
| 1 | `logtemp` | `LogTemp` 대신 적절한 로그 카테고리 사용 | warning | - | `\bLogTemp\b` |
| 2 | `pragma_optimize_off` | `#pragma optimize("", off)` 금지 | error | - | `#pragma optimize ... off` |
| 3 | `hard_asset_path` | 하드코딩된 에셋 경로 금지 | warning | - | `TEXT("/Game/...")` or `TEXT("/Engine/...")` |
| 4 | `macro_no_semicolon` | 런타임 매크로 뒤 세미콜론 누락 | warning | O | `UE_LOG(...)`, `check(...)` 등 |
| 5 | `declaration_macro_semicolon` | 선언 매크로 뒤 불필요한 세미콜론 | warning | O | `UPROPERTY(...);`, `UFUNCTION(...);` 등 |
| 6 | `check_side_effect_suspicious` | `check()` 내 부작용 의심 패턴 (1차 필터) | warning | - | `++`, `--`, `=`, 함수호출 등 |
| 7 | `sync_load_runtime` | 런타임 동기 로딩 금지 | error | - | `LoadObject`, `LoadSynchronous` 등 |

#### `logtemp` 상세

- `LogTemp`는 임시 디버깅용입니다
- 프로덕션 코드에서는 `DEFINE_LOG_CATEGORY`로 정의된 로그 카테고리를 사용하세요
- 예: `UE_LOG(LogMyGame, Log, TEXT("Message"));`

#### `pragma_optimize_off` 상세

- 최적화 비활성화는 성능에 치명적입니다
- 디버깅 후 반드시 제거하세요
- **절대 커밋하면 안 됩니다**

#### `hard_asset_path` 상세

- `TEXT("/Game/Characters/Hero")` → DataTable, Config, `FSoftObjectPath` 사용
- `ConstructorHelpers::FObjectFinder`는 생성자 내에서만 허용

#### `macro_no_semicolon` / `declaration_macro_semicolon` 상세

- 런타임 매크로 (`UE_LOG`, `check`, `ensure` 등): 세미콜론 **필수**
- 선언 매크로 (`UPROPERTY()`, `UFUNCTION()` 등): 세미콜론 **불필요**
- 자동 수정(suggestion) 제공

#### `check_side_effect_suspicious` 상세

- `check()`/`checkf()` 내부에 부작용 코드가 있으면 Shipping 빌드에서 제거됨
- 1차 regex 필터 → Stage 3 LLM이 최종 판정
- `verify()`/`verifyf()`는 제외 (모든 빌드에서 실행되므로 부작용 허용)

#### `sync_load_runtime` 상세

- 런타임 동기 로딩은 프레임 드랍 유발
- 대상: `LoadObject`, `StaticLoadObject`, `LoadClass`, `StaticLoadClass`, `LoadSynchronous`
- 대안: `AsyncLoad`, `SoftObjectPtr` 비동기 로딩, `FStreamableManager`

### Stage 2 (clang-tidy) — 9개 체크

| # | clang-tidy 체크 | 대응 Rule ID | 설명 |
|---|----------------|-------------|------|
| 1 | `modernize-use-override` | `override_keyword` | override 키워드 누락 |
| 2 | `cppcoreguidelines-virtual-class-destructor` | `virtual_destructor` | virtual 소멸자 누락 |
| 3 | `bugprone-virtual-near-miss` | - | 가상 함수 오버라이드 오타 |
| 4 | `performance-unnecessary-copy-initialization` | `unnecessary_copy` | 불필요 복사 초기화 |
| 5 | `performance-for-range-copy` | `unnecessary_copy` | range-for 루프 복사 |
| 6 | `clang-analyzer-optin.cplusplus.VirtualCall` | - | 생성자/소멸자 내 가상 호출 |
| 7 | `clang-analyzer-core.DivideZero` | - | 0 나누기 |
| 8 | `readability-else-after-return` | - | return 후 불필요 else |
| 9 | `readability-redundant-smartptr-get` | - | 불필요 스마트 포인터 `.get()` |

> `compile_commands.json`이 없으면 Stage 2는 스킵되고, Stage 3 LLM이 override, virtual 소멸자, 불필요 복사 등을 대신 검사합니다.

### Stage 3 (LLM) — 이관 항목

Stage 1 regex 유지보수 비용 대비 LLM이 더 정확하여 이관된 항목:

| # | ID | 항목 | Severity | 이관 이유 |
|---|-----|------|----------|---------|
| 1 | `auto_non_lambda` | auto 사용 금지 (람다 제외) | warning | 람다 변수 판단이 regex로 어려움 |
| 2 | `yoda_condition` | Yoda 조건식 금지 | warning | 오탐 시 영향 적음 |
| 3 | `not_operator_in_if` | `!` 연산자 사용 자제 | info | `!IsValid` 예외 처리가 까다로움 |
| 4 | `sandwich_inequality` | 샌드위치 부등식 금지 (`a < b < c`) | error | 드문 패턴, regex 유지 가치 낮음 |
| 5 | `fsimpledelegate` | FSimpleDelegate 대신 명시적 시그니처 | warning | 드문 패턴, LLM이 충분히 검출 |
| 6 | `loctext_no_undef` | LOCTEXT_NAMESPACE 후 #undef 누락 | warning | 파일 단위 검사라 별도 로직 필요 |
| 7 | `constructorhelpers_outside_ctor` | ConstructorHelpers 생성자 외 사용 금지 | error | AST 수준 판단 필요 |

---

## Multiplayer / Networking

| # | ID | 항목 | Stage | Severity |
|---|-----|------|-------|----------|
| 1 | `client_rpc_authority` | 클라이언트 RPC 호출 시 권한 검증 필수 | Stage 3 (LLM) | error |
| 2 | `replicated_property_condition` | Replicated 프로퍼티 DOREPLIFETIME 등록 필수 | Stage 3 (LLM) | error |

### RPC 권한 검증 상세

- Client RPC를 호출하기 전 `HasAuthority()` 확인
- Server에서만 Client RPC를 호출해야 함
- 클라이언트가 임의로 호출하지 못하도록 방지

### Replicated 프로퍼티 상세

- `UPROPERTY(Replicated)` 선언 후 `GetLifetimeReplicatedProps()`에서 등록 필수
- `DOREPLIFETIME(ClassName, PropertyName)` 매크로 사용

---

## Blueprint Integration

| # | ID | 항목 | Stage | Severity |
|---|-----|------|-------|----------|
| 1 | `blueprintcallable_category` | `BlueprintCallable`에 Category 필수 | Stage 3 (LLM) | warning |
| 2 | `blueprintcallable_const` | `BlueprintPure` 함수는 const 권장 | Stage 3 (LLM) | info |

---

## Performance

| # | ID | 항목 | Stage | Severity |
|---|-----|------|-------|----------|
| 1 | `tick_disable_when_possible` | 불필요한 Tick 비활성화 | Stage 3 (LLM) | warning |
| 2 | `tarray_reserve` | TArray 크기를 알 때 Reserve 사용 | Stage 3 (LLM) | info |
| 3 | `avoid_blueprint_cast_in_tick` | Tick에서 Cast 사용 자제 | Stage 3 (LLM) | warning |

### Tick 비활성화 상세

```cpp
// 생성자에서
PrimaryActorTick.bCanEverTick = false;

// 동적 제어
SetActorTickEnabled(false);
```

---

## Memory / GC

| # | ID | 항목 | Stage | Severity |
|---|-----|------|-------|----------|
| 1 | `uobject_uproperty` | UObject 포인터는 UPROPERTY로 관리 | Stage 3 (LLM) | error |
| 2 | `shared_ptr_uobject` | UObject에 TSharedPtr 사용 금지 | Stage 3 (LLM) | error |

### GC 안전성 상세

- `UObject*` 멤버 변수는 반드시 `UPROPERTY()`로 선언
- `UPROPERTY()` 없으면 GC가 해당 객체를 추적하지 못해 댕글링 포인터 발생
- UObject는 UE의 GC 시스템으로 관리 → `TSharedPtr`/`TUniquePtr` 사용 금지

---

## Code Style / Convention

| # | ID | 항목 | Stage | Severity |
|---|-----|------|-------|----------|
| 1 | `boilerplate_comment` | 보일러플레이트 주석 제거 | Stage 3 (LLM) | info |
| 2 | `structural_comment` | 복잡한 로직에 구조적 주석 권장 | Stage 3 (LLM) | info |

### 추가 UE-Specific 항목

| # | ID | 항목 | Stage | Severity |
|---|-----|------|-------|----------|
| 1 | `check_side_effect` | check() 내부 부작용 코드 금지 (최종 판정) | Stage 3 (LLM) | error |
| 2 | `unbraced_shipping_macro` | 중괄호 없는 if/for문에서 shipping 매크로 사용 금지 | Stage 2 (PVS V640) | error |
| 3 | `newobject_outer_check` | `NewObject<>` Outer null 체크 | Stage 3 (LLM) | error |
| 4 | `getworld_null_check` | `GetWorld()` 반환값 null 체크 | Stage 3 (LLM) | error |
| 5 | `customizeduv_naming` | CustomizedUV 명명 규칙 준수 | Stage 3 (LLM) | info |

---

## 요약

| Stage | 검사 방식 | 항목 수 | 실행 조건 |
|-------|----------|---------|-----------|
| **Stage 1** | regex 패턴 + clang-format | 7개 패턴 | 항상 |
| **Stage 2** | clang-tidy 정적 분석 | 9개 체크 | `compile_commands.json` 있을 때 |
| **Stage 3** | LLM 시맨틱 리뷰 | 30+ 항목 | 일반 PR만 (대규모 PR 차단) |

**Severity 레벨:**
- **error**: 반드시 수정 필요 (머지 전 해결)
- **warning**: 수정 권장 (리뷰어 판단)
- **info**: 참고 사항

**Auto-fix:**
- `macro_no_semicolon`: 세미콜론 추가 suggestion
- `declaration_macro_semicolon`: 세미콜론 제거 suggestion
- clang-format: 코드 포맷팅 suggestion
- clang-tidy (일부): 코드 수정 suggestion
