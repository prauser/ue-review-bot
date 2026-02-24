# Setup Guide — Runner 환경 설정 가이드

UE5 Code Review Bot을 실행하기 위한 Runner 환경 설정 가이드입니다.
조직에 이미 등록된 Self-hosted Runner를 활용하거나, 새로 등록하는 경우 모두를 다룹니다.

---

## Runner 라벨 설정

워크플로우는 기본적으로 `runs-on: [self-hosted, ue5-review]`로 설정되어 있습니다.
**기존 조직 러너를 사용하려면** 워크플로우 YAML의 `runs-on` 값을 기존 러너 라벨로 변경하세요.

```yaml
# 변경 전 (기본값)
runs-on: [self-hosted, ue5-review]

# 변경 후 — 기존 러너 라벨에 맞춰 수정 (예시)
runs-on: [self-hosted, linux, x64]
```

기존 러너에 `ue5-review` 라벨을 추가하는 방법도 있습니다:
- GitHub UI: Settings > Actions > Runners > (러너 선택) > Labels > `ue5-review` 추가
- CLI: `./config.sh --labels self-hosted,ue5-review` (Runner 재설정 시)

---

## 기존 러너 환경 확인

기존 러너에 필요한 도구가 설치되어 있는지 확인하려면, 아래 진단 워크플로우를 게임 레포에 추가하고 실행하세요.

```yaml
# .github/workflows/check-runner-env.yml
name: Check Runner Environment
on: workflow_dispatch

jobs:
  check:
    runs-on: [self-hosted, linux, x64]  # 기존 러너 라벨로 변경
    steps:
      - name: OS info
        run: uname -a && cat /etc/os-release 2>/dev/null || true

      - name: Python
        run: |
          python3 --version || echo "❌ python3 not found"
          pip3 list 2>/dev/null | grep -i pyyaml || echo "❌ pyyaml not installed"

      - name: jq
        run: jq --version || echo "❌ jq not found"

      - name: clang-format
        run: clang-format --version 2>/dev/null || echo "⚠️ clang-format not found (Stage 1 format check disabled)"

      - name: clang-tidy
        run: clang-tidy --version 2>/dev/null || echo "ℹ️ clang-tidy not found (Stage 2 disabled)"

      - name: Network — Anthropic API
        run: |
          HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 https://api.anthropic.com/v1/messages 2>/dev/null)
          if [ "$HTTP_CODE" = "000" ]; then
            echo "❌ Cannot reach api.anthropic.com (connection failed)"
          else
            echo "✅ api.anthropic.com reachable (HTTP ${HTTP_CODE} — 401/405 is expected without auth)"
          fi

      - name: Disk space
        run: df -h .

      - name: sudo availability
        run: sudo -n true 2>/dev/null && echo "✅ sudo available" || echo "ℹ️ sudo not available (use workflow auto-install)"
```

Actions 탭에서 `Run workflow`로 수동 실행한 뒤, 로그에서 결과를 확인하세요.

---

## 필요 도구 목록

### 필수

| 도구 | 최소 버전 | 용도 |
|------|-----------|------|
| Python | 3.10+ | 모든 Stage 스크립트 실행 |
| pyyaml | - | YAML 설정 파싱 |
| jq | - | GitHub Actions에서 JSON 파싱 |

### Stage 1: clang-format (권장)

코드 포맷팅 suggestion을 생성합니다. 설치되어 있지 않으면 포맷 검사만 빈 결과를 반환하고, 패턴 검사는 정상 동작합니다.

```bash
clang-format --version   # 16.0.0 이상 권장
```

> `configs/.clang-format`에 UE5 Epic 코딩 스타일이 정의되어 있습니다.

### Stage 2: clang-tidy (선택)

정적 분석을 실행합니다. `compile_commands.json`이 있어야만 실행됩니다.

```bash
clang-tidy --version     # 16.0.0 이상 권장
```

#### compile_commands.json 생성

UE5 프로젝트에서 `compile_commands.json`을 생성하는 방법:

```bash
# UnrealBuildTool로 생성
UnrealBuildTool -mode=GenerateClangDatabase -project=<project>.uproject

# 또는 CMake 기반 빌드:
cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON ...
```

> `compile_commands.json`이 프로젝트 루트 또는 `build/` 디렉토리에 있으면 자동 감지됩니다.

---

## 도구 설치 방법

### 방법 1: 러너 머신에 직접 설치 (영구적)

러너 머신에 SSH 접근이 가능하거나 러너 관리자에게 요청할 수 있는 경우:

```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y python3 python3-pip jq clang-format-16

# Python 패키지
pip3 install pyyaml

# Stage 2 (선택)
sudo apt-get install -y clang-tidy-16
```

```bash
# CentOS/RHEL
sudo yum install -y python3 python3-pip jq

# macOS
brew install python jq clang-format
```

### 방법 2: 워크플로우에서 자동 설치

러너에 직접 접근할 수 없거나, 러너가 교체될 수 있는 환경에서는 워크플로우에 설치 step을 추가합니다.
봇의 워크플로우에는 이미 의존성 자동 설치 step이 포함되어 있으므로, 도구가 없어도 자동으로 설치됩니다.

**sudo 권한이 있는 경우 (워크플로우 내 자동 설치 step):**

```yaml
- name: Install dependencies
  run: |
    # Python 패키지
    pip3 install --user pyyaml 2>/dev/null || true

    # jq
    which jq || { sudo apt-get update && sudo apt-get install -y jq; }

    # clang-format (Stage 1)
    which clang-format || sudo apt-get install -y clang-format-16 || true
```

**sudo 권한이 없는 경우:**

```yaml
- name: Install dependencies (no sudo)
  run: |
    # Python 패키지는 --user로 설치 가능
    pip3 install --user pyyaml 2>/dev/null || true

    # clang-format — 정적 바이너리 다운로드
    if ! which clang-format >/dev/null 2>&1; then
      mkdir -p "$HOME/bin"
      curl -L -o "$HOME/bin/clang-format" \
        "https://github.com/muttleyxd/clang-tools-static-binaries/releases/download/master-22538c65/clang-format-16_linux-amd64"
      chmod +x "$HOME/bin/clang-format"
      echo "$HOME/bin" >> "$GITHUB_PATH"
    fi
```

> 워크플로우 내 자동 설치를 사용하면 `which`로 먼저 확인하고 없을 때만 설치하므로, 이미 설치된 환경에서는 추가 시간이 들지 않습니다.

---

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

---

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

---

## 향후 추가 예정

### PVS-Studio (계획)

- PVS-Studio 라이선스 및 설치 필요
- Stage 2에 통합 예정 (`--pvs-report` 인터페이스 준비됨)

### compile_commands.json 자동 생성 (계획)

- CI에서 UBT를 통한 자동 생성 파이프라인
- 빌드 캐싱으로 Stage 2 실행 시간 최적화

---

## 문제 해결

### "clang-format not found"

Stage 1 포맷 검사가 빈 결과를 반환합니다. 워크플로우에 자동 설치 step이 있으면 자동으로 설치됩니다. 그렇지 않으면 러너에 직접 설치하거나 PATH에 추가하세요.

### "compile_commands.json not found"

Stage 2가 스킵됩니다. Stage 3 LLM이 override, virtual 소멸자 등 Tier 2 항목을 대신 검사합니다.

### "ANTHROPIC_API_KEY not set"

Stage 3가 실패합니다. Secrets에 API 키를 등록했는지 확인하세요.

### Rate limit (429) 에러

Stage 3 LLM 리뷰어가 자동으로 exponential backoff (최대 3회)합니다. 지속되면 API 사용량을 확인하세요.

### 대규모 PR 알림

50개 파일 초과 또는 대규모 레이블이 있으면 Stage 3(LLM)이 차단됩니다. `gate_config.yml`의 `max_reviewable_files`를 조정할 수 있습니다.

### Runner가 Job을 받지 않음

- Runner가 Online 상태인지 확인 (Settings > Actions > Runners)
- 워크플로우의 `runs-on:` 라벨과 Runner 라벨이 일치하는지 확인
- 기존 러너를 사용하는 경우 `runs-on` 값을 기존 러너 라벨로 변경했는지 확인
