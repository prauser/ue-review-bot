# STEP6_STAGE3.md — LLM 에이전트 리뷰어 (확장)

> Stage 1에서 이관된 7개 항목 + 원래 LLM 담당 항목 전체를 커버.
> compile_commands.json이 없을 경우 Stage 2 역할도 일부 대체.

## 산출물

| 파일 | 설명 |
|------|------|
| `scripts/stage3_llm_reviewer.py` | Anthropic API 기반 의미론적 리뷰 |
| `scripts/utils/token_budget.py` | 토큰 예산 관리 |
| `tests/test_llm_reviewer.py` | mock API 테스트 |

---

## 1. `scripts/utils/token_budget.py`

```python
BUDGET_PER_PR = 100_000   # input tokens
BUDGET_PER_FILE = 20_000
COST_LIMIT_PER_PR = 2.00  # USD

def estimate_tokens(text: str) -> int:
    return len(text) // 3  # 보수적 추정

def chunk_diff(file_diff: str, max_tokens: int) -> list[str]:
    """@@ hunk 헤더 기준으로 분리, 초과 시 더 작은 단위로"""
    ...

def should_skip_file(file_path: str) -> bool:
    """자동생성/서드파티 등 제외 (gate와 중복이지만 방어적)"""
    ...
```

---

## 2. `scripts/stage3_llm_reviewer.py`

### CLI

```
python stage3_llm_reviewer.py \
  --base-ref origin/main \
  --checklist configs/checklist.yml \
  --files '["Source/A.cpp", "Source/B.h"]' \
  --exclude-findings findings-stage1.json findings-stage2.json \
  --has-compile-commands false \
  --output findings-stage3.json
```

- `--has-compile-commands`: false이면 시스템 프롬프트에 "clang-tidy 대체 검사" 섹션 활성화. true이면 해당 섹션 제거 (Stage 2가 이미 처리).

### System Prompt

```
당신은 UE5 C++ 시니어 코드 리뷰어입니다.

## Stage 1에서 이관된 검사 (반드시 체크)

### 코딩 컨벤션
- auto 사용 금지 (람다 함수를 변수에 담을 때만 허용)
- 요다 컨디션 금지: `if (false == bFlag)` → `if (bFlag == false)`
- 조건문에 !(not) 연산자 금지: `if (!bFlag)` → `if (bFlag == false)`
- Sandwich inequality 금지: `if (0 < x && x < 10)` → `if (x > 0 && x < 10)`
- FSimpleDelegateGraphTask 사용 금지

### 파일 단위 검사
- `#define LOCTEXT_NAMESPACE` 이후 `#undef` 누락
- ConstructorHelpers를 생성자 외부에서 사용

## clang-tidy 대체 검사 (compile_commands.json 없을 때 Stage 2 대신)

- override 키워드 누락
- 다형성 최상위 클래스 소멸자에 virtual 누락
- 생성자/소멸자에서 virtual 함수 호출
- 불필요한 복사 초기화
- range-for에서 불필요한 복사
- else-after-return (Guard Clause 스타일)

## 기존 LLM 검토 항목

### GC 안전성
- UObject 파생 포인터 멤버에 UPROPERTY/TWeakObjectPtr 누락
- NewObject<> Outer가 nullptr
- USTRUCT 멤버 중 GC 대상인데 UPROPERTY 누락
- 순환 참조 가능성

### GameThread 안전성
- UObject를 다른 스레드에서 접근
- 람다 캡처에서 UObject raw pointer + 비동기 실행

### 네트워크 효율
- 불필요한 Replication, 매 Tick RPC, Reliable 남용
- DOREPLIFETIME 조건 미설정, 큰 구조체 RPC 파라미터

### 성능 (Tick / Memory / Hitch)
- 비어있는 Tick, 이벤트 기반 대체 가능, TickInterval 미설정
- 소량 데이터에 TMap, Reserve 없는 루프 Add, 풀링 미사용
- 런타임 동기 로딩, 런타임 Rigid Body 동적 생성

### UE5 패턴
- UCLASS 생성자에서 게임 로직, Transient 누락, DisableNativeTick 누락
- GetWorld() null 체크, UFUNCTION Category 누락

### 설계
- 캡슐화 위반, 과도한 함수 책임, Guard Clause 미사용, 중복 코드

### 주석
- 외부 수식 출처 누락, #todo-ovdr 태그 누락

### 보안
- 클라이언트 RPC 권한 검증 누락

## 출력 형식
JSON 배열만 반환. 이슈 없으면 [].

[{
  "file": "Source/MyActor.cpp",
  "line": 42,
  "end_line": 45,
  "severity": "warning|error|suggestion",
  "category": "convention|gc_safety|network|tick_perf|...",
  "message": "한국어 지적 메시지",
  "suggestion": "수정 코드 또는 null"
}]

## 규칙
- 확실하지 않은 지적은 하지 마세요.
- diff에 보이는 코드만으로 판단. 보이지 않는 코드 추측 금지.
- message는 한국어로, 이유 + 수정 방향 간결하게.
- Stage 1에서 이관된 항목은 반드시 빠짐없이 체크하세요.
```

### API 호출

- Model: `claude-sonnet-4-5-20250929`
- temperature: 0
- 파일별로 호출, 각 응답에서 JSON 파싱
- exclude-findings의 file+line은 skip

### 에러 핸들링

- API 타임아웃/에러: 해당 파일 skip, 파이프라인은 계속
- JSON 파싱 실패: skip, 로그 기록
- Rate limit: exponential backoff 최대 3회
- PR당 $2 초과: 남은 파일 skip, 경고

---

## 3. 테스트 (mock API)

- mock 응답으로 JSON 파싱 정상 동작
- 토큰 예산 초과 시 파일 skip
- API 에러 시 graceful degradation
- exclude-findings 중복 제거
- 빈 diff → API 호출 안 함
