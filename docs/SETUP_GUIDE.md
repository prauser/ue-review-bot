# Setup Guide — Self-hosted Runner 설치 가이드

UE5 Code Review Bot을 실행하기 위한 Self-hosted Runner 환경 설정 가이드입니다.

## 필수 도구

### Python 3.10+

```bash
python3 --version
# Python 3.10 이상 필요
```

### Python 패키지

```bash
pip install pyyaml
```

> `requests`는 Stage 3 LLM 리뷰어가 `urllib`를 직접 사용하므로 별도 설치 불필요합니다.

### jq

GitHub Actions에서 JSON 파싱에 사용됩니다.

```bash
# Ubuntu/Debian
sudo apt-get install jq

# CentOS/RHEL
sudo yum install jq

# macOS
brew install jq
```

## Stage 1: clang-format (권장)

코드 포맷팅 suggestion을 생성합니다. 설치되어 있지 않으면 해당 Stage는 빈 결과를 반환합니다.

```bash
# Ubuntu/Debian
sudo apt-get install clang-format-16

# macOS
brew install clang-format

# 버전 확인
clang-format --version
# clang-format version 16.0.0 이상 권장
```

> `configs/.clang-format`에 UE5 Epic 코딩 스타일이 정의되어 있습니다.

## Stage 2: clang-tidy (선택)

정적 분석을 실행합니다. `compile_commands.json`이 있어야만 실행됩니다.

```bash
# Ubuntu/Debian
sudo apt-get install clang-tidy-16

# macOS
brew install llvm
# PATH에 llvm 추가 필요

# 버전 확인
clang-tidy --version
# clang-tidy version 16.0.0 이상 권장
```

### compile_commands.json 생성

UE5 프로젝트에서 `compile_commands.json`을 생성하는 방법:

```bash
# UnrealBuildTool로 생성
# Engine/Build/BatchFiles/RunUBT.sh에서:
UnrealBuildTool -mode=GenerateClangDatabase -project=<project>.uproject

# 또는 CMake 기반 빌드:
cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON ...
```

> `compile_commands.json`이 프로젝트 루트 또는 `build/` 디렉토리에 있으면 자동 감지됩니다.

## Stage 3: LLM 리뷰 (Anthropic API)

### API 키 설정

1. [Anthropic Console](https://console.anthropic.com/)에서 API 키를 발급
2. 게임 레포의 Settings > Secrets > `ANTHROPIC_API_KEY`에 등록

### 네트워크 요구사항

Runner에서 다음 엔드포인트로 HTTPS 아웃바운드 연결이 필요합니다:

```
api.anthropic.com:443
```

> 프록시 환경에서는 `HTTPS_PROXY` 환경변수를 설정하거나 Runner 네트워크 설정에서 허용 목록에 추가하세요.

### 비용 안전장치

- PR당 최대 토큰: 100,000
- 파일당 최대 토큰: 20,000
- PR당 최대 비용: $2 USD
- 대규모 PR (50+ 파일): Stage 3 자동 차단

## GitHub Secrets 설정

게임 레포의 Settings > Secrets and variables > Actions에 등록:

| Secret | 값 | 필수 |
|--------|---|------|
| `BOT_REPO_TOKEN` | 봇 레포 read 권한 PAT | O |
| `ANTHROPIC_API_KEY` | Anthropic API 키 | Stage 3 사용 시 |
| `GHES_URL` | GHES 주소 (예: `https://github.company.com`) | GHES 환경 |
| `GHES_TOKEN` | PR Review 쓰기 권한 PAT | O |

### PAT (Personal Access Token) 권한

**BOT_REPO_TOKEN:**
- `repo` (또는 `read:contents`) — 봇 레포 코드 읽기

**GHES_TOKEN:**
- `repo` — PR 코멘트 작성
- `write:discussion` (GHES 3.4+) — PR Review 게시

## Runner 라벨

워크플로우는 `[self-hosted, ue5-review]` 라벨을 사용합니다. Runner에 이 라벨을 추가하세요:

```bash
# Runner 설정 시
./config.sh --labels self-hosted,ue5-review
```

또는 GitHub UI에서 Settings > Actions > Runners에서 라벨을 추가할 수 있습니다.

> 라벨을 변경하려면 워크플로우 YAML의 `runs-on` 값을 수정하세요.

## 향후 추가 예정

### PVS-Studio (계획)

- PVS-Studio 라이선스 및 설치 필요
- Stage 2에 통합 예정 (`--pvs-report` 인터페이스 준비됨)

### compile_commands.json 자동 생성 (계획)

- CI에서 UBT를 통한 자동 생성 파이프라인
- 빌드 캐싱으로 Stage 2 실행 시간 최적화

## 문제 해결

### "clang-format not found"

Stage 1 포맷 검사가 빈 결과를 반환합니다. clang-format을 설치하거나 PATH에 추가하세요.

### "compile_commands.json not found"

Stage 2가 스킵됩니다. Stage 3 LLM이 override, virtual 소멸자 등 Tier 2 항목을 대신 검사합니다.

### "ANTHROPIC_API_KEY not set"

Stage 3가 실패합니다. Secrets에 API 키를 등록했는지 확인하세요.

### Rate limit (429) 에러

Stage 3 LLM 리뷰어가 자동으로 exponential backoff (최대 3회)합니다. 지속되면 API 사용량을 확인하세요.

### 대규모 PR 알림

50개 파일 초과 또는 대규모 레이블이 있으면 Stage 3(LLM)이 차단됩니다. `gate_config.yml`의 `max_reviewable_files`를 조정할 수 있습니다.
