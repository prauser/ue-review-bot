# Tier 2 Implementation Plan: PVS-Studio Integration

> **Status**: Planning Phase
> **Target**: Future Sprint (after Tier 1 & Tier 3 completion)
> **CI/CD**: TeamCity Integration Required

---

## 📋 Overview

Tier 2는 **PVS-Studio 정적 분석 도구**를 활용하여 컴파일 타임 체크를 수행합니다.
Regex(Tier 1)로 감지하기 어렵고, LLM(Tier 3)까지 필요 없는 중간 수준의 규칙들을 처리합니다.

### 핵심 장점
- ✅ **AST 기반 분석**: 구문 구조 정확히 파악
- ✅ **False Positive 낮음**: UE5 매크로 특별 처리 (v7.33+)
- ✅ **CI/CD 통합 가능**: TeamCity에서 자동 실행
- ✅ **비용 효율적**: LLM보다 저렴하고 빠름

---

## 🎯 Tier 2 규칙 목록

### 현재 Tier 2로 분류된 규칙

| ID | Summary | PVS-Studio Check | Severity |
|----|---------|------------------|----------|
| **unbraced_shipping_macro** | 중괄호 없는 if/for문에서 shipping 매크로 사용 금지 | **V640** | error |
| **override_keyword** | 가상 함수 오버라이드 시 override 키워드 필수 | modernize-use-override | error |
| **virtual_destructor** | 다형성 최상위 클래스 소멸자에 virtual 필수 | cppcoreguidelines-virtual-class-destructor | error |
| **unnecessary_copy** | 불필요한 복사 방지 | performance-for-range-copy | warning |

### Tier 2 추가 검토 대상

다음 규칙들도 PVS-Studio로 감지 가능 여부 확인 필요:

| 후보 규칙 | 예상 Check | 우선순위 |
|----------|-----------|---------|
| nullptr 역참조 | V522, V595 | High |
| 초기화되지 않은 변수 사용 | V573, V614 | High |
| Division by zero | V609 | Medium |
| 배열 범위 초과 | V557 | Medium |
| Use after free | V762 | High |

---

## 🛠️ 구현 요구사항

### 1. PVS-Studio 설치 및 설정

#### 필수 요구사항
- **PVS-Studio 버전**: 7.33+ (UE5.5+ 지원)
- **Unreal Engine**: 5.0+
- **컴파일 데이터베이스**: `compile_commands.json` 필요

#### 설치 방법
```bash
# Linux/Mac
wget https://files.pvs-studio.com/pvs-studio.tar.gz
tar -xzf pvs-studio.tar.gz
sudo ./install.sh

# Windows
# PVS-Studio installer 다운로드 및 실행
```

#### UE5 프로젝트 설정
```json
// .pvs-studio.cfg
{
  "analysis-mode": "4",  // UE5 모드
  "exclude-path": [
    "*/Intermediate/*",
    "*/ThirdParty/*"
  ],
  "preprocessor": "clang",
  "platform": "x64",
  "configuration": "Development"
}
```

---

### 2. compile_commands.json 생성

#### Unreal Build Tool 설정
```bash
# UE5에서 compile_commands.json 생성
cd /path/to/YourProject
UnrealBuildTool -mode=GenerateClangDatabase -project=YourProject.uproject
```

#### 대안: Bear 사용 (Linux/Mac)
```bash
# Bear 설치
sudo apt install bear  # Ubuntu
brew install bear      # macOS

# 빌드 중 컴파일 명령 캡처
bear -- make
```

---

### 3. TeamCity CI/CD 통합

#### 빌드 Step 추가

**Step 1: Generate Compilation Database**
```bash
#!/bin/bash
echo "Generating compile_commands.json..."
UnrealBuildTool -mode=GenerateClangDatabase \
  -project=%PROJECT_PATH%/%PROJECT_NAME%.uproject
```

**Step 2: Run PVS-Studio Analysis**
```bash
#!/bin/bash
echo "Running PVS-Studio analysis..."

# 분석 실행
pvs-studio-analyzer analyze \
  -j8 \
  -o /tmp/pvs-report.log \
  --compilation-database compile_commands.json

# 리포트 변환
plog-converter -t errorfile \
  /tmp/pvs-report.log \
  -o pvs-report.txt

# 결과 확인
if [ -s pvs-report.txt ]; then
  echo "PVS-Studio found issues:"
  cat pvs-report.txt
  exit 1  # Fail build
else
  echo "PVS-Studio analysis passed!"
  exit 0
fi
```

**Step 3: Parse and Report**
```python
# scripts/parse_pvs_report.py
import re
import json

def parse_pvs_report(report_file):
    """PVS-Studio 리포트를 파싱하여 checklist.yml 규칙과 매핑"""
    results = []

    # V640: Code formatting doesn't match logic
    v640_pattern = r"V640.*?(\w+\.cpp):(\d+)"

    with open(report_file) as f:
        for match in re.finditer(v640_pattern, f.read()):
            results.append({
                'rule_id': 'unbraced_shipping_macro',
                'file': match.group(1),
                'line': int(match.group(2)),
                'pvs_check': 'V640'
            })

    return results

if __name__ == '__main__':
    results = parse_pvs_report('pvs-report.txt')
    print(json.dumps(results, indent=2))
```

#### TeamCity Build Configuration

```xml
<!-- .teamcity/settings.kts -->
object PVSStudioCheck : BuildType({
    name = "PVS-Studio Static Analysis"

    steps {
        script {
            name = "Generate Compilation Database"
            scriptContent = """
                UnrealBuildTool -mode=GenerateClangDatabase \
                  -project=%PROJECT_NAME%.uproject
            """.trimIndent()
        }

        script {
            name = "Run PVS-Studio"
            scriptContent = """
                pvs-studio-analyzer analyze -j8 \
                  -o pvs-report.log \
                  --compilation-database compile_commands.json

                plog-converter -t errorfile \
                  pvs-report.log \
                  -o pvs-report.txt
            """.trimIndent()
        }

        python {
            name = "Parse and Report"
            command = "scripts/parse_pvs_report.py"
        }
    }

    failureConditions {
        errorMessage = true
        nonZeroExitCode = true
    }
})
```

---

## 📊 PVS-Studio 체크 목록 상세

### V640: Code's operational logic does not correspond with its formatting

**감지 대상**: `unbraced_shipping_macro`

#### 감지 예시
```cpp
// ❌ BAD: V640 경고 발생
if (bShouldProcess)
    check(Actor != nullptr);  // ← Shipping에서 사라짐!
    ProcessActor(Actor);      // ← 항상 실행됨 (의도 X)

// ✅ GOOD
if (bShouldProcess) {
    check(Actor != nullptr);
    ProcessActor(Actor);
}

// 또는 verify() 사용
if (bShouldProcess)
    verify(Actor != nullptr);  // ← verify는 모든 빌드에서 실행
```

#### UE5 매크로 특별 처리 (PVS-Studio 7.33+)
```cpp
// PVS-Studio가 UE5 매크로를 인지함
check()     // Shipping에서 제거
verify()    // 모든 빌드에서 실행
ensure()    // Shipping에서 제거
UE_LOG()    // Shipping에서 제거 (특정 verbosity)
```

---

### modernize-use-override

**감지 대상**: `override_keyword`

#### 설정 (.clang-tidy)
```yaml
Checks: 'modernize-use-override'
CheckOptions:
  - key: modernize-use-override.IgnoreDestructors
    value: false
  - key: modernize-use-override.AllowOverrideAndFinal
    value: false
```

#### 감지 예시
```cpp
class AMyActor : public AActor {
public:
    // ❌ BAD
    virtual void BeginPlay();

    // ✅ GOOD
    virtual void BeginPlay() override;
};
```

---

### cppcoreguidelines-virtual-class-destructor

**감지 대상**: `virtual_destructor`

#### 감지 예시
```cpp
// ❌ BAD
class UMyObject : public UObject {
public:
    ~UMyObject();  // ← virtual 없음!
};

// ✅ GOOD
class UMyObject : public UObject {
public:
    virtual ~UMyObject();
};
```

---

### performance-for-range-copy

**감지 대상**: `unnecessary_copy`

#### 감지 예시
```cpp
TArray<FString> Names = GetAllNames();

// ❌ BAD: 불필요한 복사
for (auto Name : Names) {  // ← 복사 발생!
    UE_LOG(LogTemp, Log, TEXT("%s"), *Name);
}

// ✅ GOOD
for (const auto& Name : Names) {
    UE_LOG(LogTemp, Log, TEXT("%s"), *Name);
}
```

---

## 🔧 구현 체크리스트

### Phase 1: 환경 구축
- [ ] PVS-Studio 라이선스 확보
- [ ] TeamCity 빌드 에이전트에 PVS-Studio 설치
- [ ] UE5 프로젝트에서 `compile_commands.json` 생성 테스트
- [ ] `.pvs-studio.cfg` 설정 파일 작성

### Phase 2: CI/CD 통합
- [ ] TeamCity 빌드 스텝 작성
  - [ ] Compilation DB 생성
  - [ ] PVS-Studio 실행
  - [ ] 리포트 파싱
- [ ] 리포트 파싱 스크립트 작성 (`parse_pvs_report.py`)
- [ ] GitHub PR 코멘트 연동
- [ ] Slack 알림 설정

### Phase 3: 규칙 매핑
- [ ] `checklist.yml`의 Tier 2 규칙 → PVS-Studio Check 매핑 테이블 작성
- [ ] 각 규칙별 테스트 케이스 작성
- [ ] False Positive 필터링 로직 구현

### Phase 4: 테스트 및 롤아웃
- [ ] 테스트 프로젝트에서 검증
- [ ] 기존 코드베이스에 실행하여 기준선(baseline) 설정
- [ ] 점진적 롤아웃 (warning → error)
- [ ] 팀 교육 및 문서화

---

## 📈 성공 지표

### KPI
- **감지율**: Tier 2 규칙 95% 이상 자동 감지
- **False Positive**: 10% 이하
- **빌드 시간**: +5분 이내
- **개발자 만족도**: 4.0/5.0 이상

### 측정 방법
```python
# scripts/tier2_metrics.py
def calculate_metrics(pvs_reports, manual_reviews):
    """Tier 2 성능 지표 계산"""
    tp = len(set(pvs_reports) & set(manual_reviews))  # True Positive
    fp = len(set(pvs_reports) - set(manual_reviews))  # False Positive
    fn = len(set(manual_reviews) - set(pvs_reports))  # False Negative

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1_score = 2 * (precision * recall) / (precision + recall) \
               if (precision + recall) > 0 else 0

    return {
        'precision': precision,  # 정확도 (FP 낮을수록 좋음)
        'recall': recall,        # 재현율 (감지율)
        'f1_score': f1_score     # 종합 지표
    }
```

---

## 💡 추가 고려사항

### 1. 라이선스 비용
- PVS-Studio 상용 라이선스 필요
- 대안: 오픈소스 프로젝트면 무료 라이선스 신청 가능

### 2. 성능 최적화
```bash
# 증분 분석 (변경된 파일만)
pvs-studio-analyzer analyze \
  --incremental \
  --changed-files $(git diff --name-only HEAD~1)
```

### 3. Suppression 관리
```cpp
// PVS-Studio 경고 억제
//-V640  // 특정 줄만 억제
```

### 4. UE5 버전 호환성
| UE Version | PVS-Studio Version | Status |
|------------|-------------------|--------|
| UE 5.0-5.4 | 7.20+ | 부분 지원 |
| UE 5.5+ | 7.33+ | **완전 지원** (check() 매크로 처리) |

---

## 🔗 참고 자료

### PVS-Studio 공식 문서
- [V640 Documentation](https://pvs-studio.com/en/docs/warnings/v640/)
- [Unreal Engine Integration Guide](https://pvs-studio.com/en/docs/manual/ue/)
- [TeamCity Plugin](https://pvs-studio.com/en/docs/plugins/teamcity/)

### Unreal Engine 문서
- [Build Configuration](https://dev.epicgames.com/documentation/en-us/unreal-engine/build-configuration-for-unreal-engine)
- [Asserts (check/verify)](https://dev.epicgames.com/documentation/en-us/unreal-engine/asserts-in-unreal-engine)

### 내부 문서
- `checklist.yml` - Tier 2 규칙 정의
- `docs/steps/step1-*.md` - 구현 단계별 가이드

---

## 📝 변경 이력

| Date | Author | Changes |
|------|--------|---------|
| 2026-02-13 | Claude | 초안 작성 - Tier 2 구현 계획 |

---

## ✅ Next Steps

1. **Step 1**: PVS-Studio 라이선스 확보 및 설치
2. **Step 2**: TeamCity 통합 POC (Proof of Concept)
3. **Step 3**: `unbraced_shipping_macro` 규칙 검증
4. **Step 4**: 전체 Tier 2 규칙 롤아웃

**Estimated Timeline**: 2-3 sprints
**Dependencies**: Tier 1 & Tier 3 완료 후 시작
