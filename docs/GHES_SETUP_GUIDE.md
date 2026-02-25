# GHES (GitHub Enterprise Server) 세팅 가이드

이 문서는 UE5 Code Review Bot을 GitHub Enterprise Server 환경에서 운영하기 위한 설정 절차와 유의사항을 정리합니다.

---

## 목차

1. [전체 아키텍처 개요](#1-전체-아키텍처-개요)
2. [사전 요구사항](#2-사전-요구사항)
3. [Step-by-Step 세팅](#3-step-by-step-세팅)
4. [GHES 유의사항](#4-ghes-유의사항)
5. [검증 체크리스트](#5-검증-체크리스트)
6. [문제 해결](#6-문제-해결)

---

## 1. 전체 아키텍처 개요

```
┌─────────────────────────────────────────────────────┐
│  GHES (github.company.com)                          │
│                                                     │
│  ┌──────────────┐     ┌──────────────────────────┐  │
│  │ ue5-review-bot│     │ 게임 레포 (UE5 프로젝트) │  │
│  │ (봇 레포)     │     │                          │  │
│  │ - scripts/    │◄────│ .github/workflows/       │  │
│  │ - configs/    │ 체크 │  code-review.yml         │  │
│  │ - workflows/  │ 아웃 │  code-review-manual.yml  │  │
│  └──────────────┘     └──────────┬───────────────┘  │
│                                  │                   │
│                        PR open / │ /review           │
│                        sync      │                   │
│                    ┌─────────────▼────────────────┐  │
│                    │  Self-hosted Runner           │  │
│                    │  (기존 조직 러너 또는 신규)    │  │
│                    │                              │  │
│                    │  Gate → Stage1 → Stage2 →    │  │
│                    │  Stage3 → Post Review        │  │
│                    └──────────────┬───────────────┘  │
└───────────────────────────────────┼──────────────────┘
                                    │ HTTPS (Stage 3)
                                    ▼
                           api.anthropic.com:443
```

**두 개의 레포가 필요합니다:**
- **봇 레포** (`ue5-review-bot`): 스크립트, 설정, 워크플로우 템플릿 보관
- **게임 레포**: 실제 UE5 프로젝트. 워크플로우 YAML만 복사하여 사용

---

## 2. 사전 요구사항

### GHES 버전

| 최소 버전 | 이유 |
|-----------|------|
| **3.0+** | GitHub Actions 지원 |
| **3.3+** | `actions/upload-artifact@v4`, `actions/download-artifact@v4` 호환 |
| **3.6+** | `concurrency` 그룹, `actions/github-script@v7` 안정 지원 |
| **3.9+** (권장) | Artifact v4 업로드, 최신 Actions 기능 완전 지원 |

> GHES 버전 확인: `https://github.company.com/api/v3/meta` 접속

### GHES Admin 설정 (서버 관리자 필요)

- [ ] GitHub Actions가 활성화되어 있어야 함 (관리 콘솔 > Actions)
- [ ] Self-hosted Runner가 등록 가능한 상태여야 함
- [ ] Actions에서 사용하는 GitHub-provided 액션 접근 방식 설정:
  - **GitHub Connect** 활성화 (github.com의 actions/* 자동 접근), 또는
  - **수동 동기화**: `actions-sync` 도구로 필요한 액션을 GHES에 미러링

### 필요한 GitHub Actions

워크플로우에서 사용하는 액션 목록. GHES에서 이 액션들이 접근 가능해야 합니다:

| Action | 용도 |
|--------|------|
| `actions/checkout@v4` | 레포 체크아웃 |
| `actions/upload-artifact@v4` | Stage 간 결과 전달 |
| `actions/download-artifact@v4` | Stage 간 결과 수신 |
| `actions/github-script@v7` | `/review` 코멘트 리액션 (수동 워크플로우) |

**GitHub Connect 미사용 시 수동 동기화 방법:**

```bash
# actions-sync CLI 설치 후
actions-sync sync \
  --cache-dir /tmp/actions-cache \
  --destination-url https://github.company.com \
  --destination-token $GHES_ADMIN_TOKEN \
  --repo-name actions/checkout \
  --repo-name actions/upload-artifact \
  --repo-name actions/download-artifact \
  --repo-name actions/github-script
```

### Runner 환경

Runner 머신에 필요한 도구입니다. 기존 조직 러너를 사용하는 경우 이미 설치되어 있을 수 있으니, 진단 워크플로우로 먼저 확인하세요 (자세한 내용은 [SETUP_GUIDE.md](SETUP_GUIDE.md) 참조).

```bash
# 필수
python3 --version       # 3.10+
pip install pyyaml
jq --version            # JSON 파싱용

# Stage 1 (clang-format)
clang-format --version  # 16+

# Stage 2 (선택, compile_commands.json 필요)
clang-tidy --version    # 16+
```

> 도구가 설치되어 있지 않은 경우, 워크플로우에 자동 설치 step을 추가하여 해결할 수 있습니다. 상세 방법은 [SETUP_GUIDE.md](SETUP_GUIDE.md)의 "도구 설치 방법" 섹션을 참고하세요.

---

## 3. Step-by-Step 세팅

### Step 1: 봇 레포를 GHES에 생성

GHES에 봇 레포를 생성하고 코드를 푸시합니다.

```bash
# GHES에 새 레포 생성 후
git remote add ghes https://github.company.com/your-org/ue5-review-bot.git
git push ghes main
```

봇 레포의 위치는 게임 레포와 **같은 Organization** 에 있는 것을 권장합니다.
워크플로우에서 `${{ github.repository_owner }}/ue5-review-bot`로 참조하기 때문입니다.

> 다른 Organization에 봇 레포가 있으면 워크플로우 YAML에서 `repository:` 값을 직접 수정해야 합니다.

### Step 2: PAT (Personal Access Token) 생성

GHES의 Settings > Developer settings > Personal access tokens에서 생성합니다.

**BOT_REPO_TOKEN** — 봇 레포 읽기용:
- 권한: `repo` (또는 최소 `read:contents`)
- 용도: 워크플로우에서 봇 레포를 체크아웃

**GHES_TOKEN** — PR 리뷰 작성용:
- 권한: `repo` (PR 읽기/쓰기 포함)
- 용도: PR에 리뷰 코멘트를 게시

> **GHES PAT 유의사항**: GHES에서는 Fine-grained PAT가 버전에 따라 지원되지 않을 수 있습니다. Classic PAT를 사용하는 것이 안전합니다.

### Step 3: Secrets 등록

게임 레포의 Settings > Secrets and variables > Actions에 등록:

| Secret | 값 | 예시 |
|--------|---|------|
| `BOT_REPO_TOKEN` | 봇 레포 읽기용 PAT | `ghp_xxxx...` |
| `GHES_TOKEN` | PR 리뷰 쓰기용 PAT | `ghp_yyyy...` |
| `GHES_URL` | GHES 인스턴스 URL (**슬래시 없이**) | `https://github.company.com` |
| `ANTHROPIC_API_KEY` | Anthropic API 키 (Stage 3용) | `sk-ant-...` |

> `GHES_URL`은 반드시 **끝에 `/` 없이** 설정하세요. 코드에서 `{GHES_URL}/api/v3`로 조합합니다.

### Step 4: Runner 설정

#### 옵션 A: 기존 조직 러너 사용 (권장)

이미 조직에 등록된 Self-hosted Runner가 있다면 별도 등록 없이 사용할 수 있습니다.

1. **기존 러너 라벨 확인**: Settings > Actions > Runners에서 기존 러너의 라벨을 확인합니다 (예: `self-hosted, linux, x64`)
2. **워크플로우의 `runs-on` 수정**: 게임 레포에 복사한 워크플로우 YAML에서 `runs-on` 값을 기존 러너 라벨로 변경합니다

```yaml
# 변경 전 (기본값)
runs-on: [self-hosted, ue5-review]

# 변경 후 — 기존 러너 라벨에 맞춰 수정 (예시)
runs-on: [self-hosted, linux, x64]
```

3. **환경 확인**: 진단 워크플로우로 필요 도구 설치 여부를 확인합니다 ([SETUP_GUIDE.md](SETUP_GUIDE.md)의 "기존 러너 환경 확인" 참조)
4. **도구 미설치 시**: 러너에 직접 설치하거나, 워크플로우에 자동 설치 step을 추가합니다 ([SETUP_GUIDE.md](SETUP_GUIDE.md)의 "도구 설치 방법" 참조)

#### 옵션 B: 새 Runner 등록

전용 러너를 새로 등록하려면, 게임 레포 또는 Organization 레벨에서 Runner를 등록합니다.

```bash
# GHES에서 Runner 패키지를 다운로드하고 설정
./config.sh \
  --url https://github.company.com/your-org/game-repo \
  --token <runner-registration-token> \
  --labels self-hosted,ue5-review
```

새 러너의 경우 라벨 `self-hosted,ue5-review`가 기본 설정됩니다. 워크플로우의 `runs-on: [self-hosted, ue5-review]`와 매칭되므로 YAML 수정이 필요 없습니다.

### Step 5: 워크플로우 파일 복사

봇 레포의 `workflows/` 디렉토리를 게임 레포의 `.github/workflows/`에 복사합니다.

```bash
# 게임 레포에서
cp <bot-repo-path>/workflows/code-review.yml .github/workflows/
cp <bot-repo-path>/workflows/code-review-manual.yml .github/workflows/
git add .github/workflows/
git commit -m "Add UE5 code review bot workflows"
git push
```

### Step 6: 동작 확인

1. 게임 레포에서 테스트 PR을 생성합니다
2. Actions 탭에서 `UE5 Code Review` 워크플로우가 트리거되는지 확인
3. 각 Stage가 정상 실행되는지 로그 확인
4. PR에 리뷰 코멘트가 게시되는지 확인

---

## 4. GHES 유의사항

### 4.1 API URL 구성

이 봇은 GHES API를 다음과 같이 구성합니다:

```
GHES_URL 설정 시: {GHES_URL}/api/v3  (예: https://github.company.com/api/v3)
GHES_URL 미설정: https://api.github.com  (github.com 기본값)
```

코드 참조 (`scripts/post_review.py:702-708`):
```python
ghes_url = os.environ.get("GHES_URL")
if ghes_url:
    api_url = f"{ghes_url.rstrip('/')}/api/v3"
else:
    api_url = "https://api.github.com"
```

워크플로우에서도 동일한 로직을 사용합니다 (`workflows/code-review.yml:304-305`):
```bash
API_URL="${GHES_URL:+${GHES_URL}/api/v3}"
API_URL="${API_URL:-https://api.github.com}"
```

### 4.2 SSL/TLS 인증서 문제

**가장 흔한 GHES 이슈입니다.**

GHES가 사설 CA 또는 자체 서명 인증서를 사용하는 경우, Python의 `urllib`이 SSL 검증에 실패합니다:

```
urllib.error.URLError: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED]>
```

**해결 방법 (Runner 머신에서):**

```bash
# 방법 1: 사설 CA 인증서를 시스템 인증서 저장소에 추가
sudo cp company-ca.crt /usr/local/share/ca-certificates/
sudo update-ca-certificates

# 방법 2: Python이 사용하는 인증서 번들에 추가
cat company-ca.crt >> $(python3 -c "import certifi; print(certifi.where())")

# 방법 3: 환경변수로 인증서 번들 경로 지정
export SSL_CERT_FILE=/path/to/company-ca-bundle.crt
export REQUESTS_CA_BUNDLE=/path/to/company-ca-bundle.crt

# 방법 4 (Git 관련): Git의 SSL 인증서 설정
git config --global http.sslCAInfo /path/to/company-ca.crt
```

> `SSL_CERT_FILE`을 Runner의 환경변수에 영구적으로 설정하는 것을 권장합니다 (`.env` 파일 또는 systemd 서비스 설정).

**Anthropic API도 HTTPS를 사용하므로**, Runner에서 외부 인터넷(api.anthropic.com:443)으로 나가는 경로에 프록시가 있다면 프록시의 CA 인증서도 함께 추가해야 합니다.

### 4.3 네트워크 / 방화벽

Self-hosted Runner에서 다음 네트워크 접근이 필요합니다:

| 대상 | 포트 | 용도 | 필수 여부 |
|------|------|------|----------|
| GHES 인스턴스 (`github.company.com`) | 443 | API 호출, Git 체크아웃 | 필수 |
| `api.anthropic.com` | 443 | Stage 3 LLM 리뷰 | Stage 3 사용 시 |

**프록시 환경:**

```bash
# Runner 환경에 프록시 설정
export HTTPS_PROXY=http://proxy.company.com:8080
export NO_PROXY=github.company.com  # GHES는 프록시 제외 (내부망일 경우)
```

> `NO_PROXY`에 GHES 호스트를 추가하여 내부 통신은 프록시를 거치지 않도록 설정하세요.

### 4.4 `actions/checkout@v4` 크로스 레포 체크아웃

워크플로우에서 봇 레포를 체크아웃하는 부분:

```yaml
- uses: actions/checkout@v4
  with:
    repository: ${{ github.repository_owner }}/ue5-review-bot
    ref: main
    path: .review-bot
    token: ${{ secrets.BOT_REPO_TOKEN }}
```

**유의사항:**
- `repository`는 `owner/repo` 형식이며, 같은 GHES 인스턴스 내의 레포만 가능
- `BOT_REPO_TOKEN`은 봇 레포에 대한 읽기 권한이 있어야 함
- GHES에서는 `actions/checkout@v4`가 자동으로 GHES의 `GITHUB_SERVER_URL`을 사용하므로 별도 URL 설정 불필요
- 봇 레포가 다른 Organization에 있으면 `repository:` 값을 직접 변경 (예: `other-org/ue5-review-bot`)

### 4.5 `actions/github-script@v7` GHES 호환

수동 워크플로우(`code-review-manual.yml`)에서 `/review` 코멘트 리액션과 PR 정보 조회에 사용됩니다.

```yaml
- uses: actions/github-script@v7
  with:
    github-token: ${{ secrets.GHES_TOKEN || secrets.GITHUB_TOKEN }}
```

- `actions/github-script@v7`은 자동으로 `GITHUB_API_URL` 환경변수(GHES Runner가 자동 설정)를 사용하므로 별도 API URL 설정 불필요
- `GHES_TOKEN`을 우선 사용하고, 없으면 `GITHUB_TOKEN` fallback
- GHES 3.6 미만에서는 `@v6` 이하를 사용해야 할 수 있음

### 4.6 `gh` CLI 주의 (현재 미사용이나 참고)

코드에 `gh` CLI를 사용하는 함수(`scripts/utils/gh_api.py:get_pr_labels`)가 있지만, 현재 워크플로우에서는 직접 호출하지 않습니다 (대신 `jq`와 `toJSON()`을 사용).

만약 향후 `gh` CLI를 사용하게 된다면:

```bash
# GHES에서 gh CLI 사용 시 호스트 설정 필요
export GH_HOST=github.company.com
export GH_ENTERPRISE_TOKEN=<pat>

# 또는 gh auth login
gh auth login --hostname github.company.com
```

### 4.7 GHES 버전별 워크플로우 기능 호환성

| 워크플로우 기능 | 필요 GHES 버전 |
|----------------|----------------|
| `concurrency` 그룹 | 3.2+ |
| `permissions` 블록 | 3.4+ |
| Artifact v4 (`actions/*-artifact@v4`) | 3.9+ |
| `issue_comment` 트리거 | 3.0+ |
| `workflow_dispatch` 트리거 | 3.0+ |
| `actions/github-script@v7` | 3.6+ |
| `continue-on-error` | 3.0+ |

**GHES 3.9 미만에서 Artifact v4가 지원되지 않는 경우:**

```yaml
# @v4 → @v3으로 다운그레이드
- uses: actions/upload-artifact@v3
  with:
    name: findings-stage1
    path: ...
```

### 4.8 `GITHUB_TOKEN` vs `GHES_TOKEN` 권한 차이

| 항목 | `GITHUB_TOKEN` (자동 발급) | `GHES_TOKEN` (PAT) |
|------|---------------------------|---------------------|
| 발급 | 워크플로우 실행 시 자동 | 사용자가 직접 생성 |
| 범위 | 워크플로우가 실행되는 레포만 | PAT 설정에 따라 다름 |
| 크로스 레포 | 불가 | 가능 |
| PR 리뷰 작성 | 가능 (같은 레포 PR) | 가능 |
| 봇 레포 체크아웃 | 불가 (다른 레포) | `BOT_REPO_TOKEN`으로 가능 |

이 봇은 크로스 레포 체크아웃이 필요하므로 PAT 기반 토큰(`BOT_REPO_TOKEN`, `GHES_TOKEN`)을 사용합니다.

---

## 5. 검증 체크리스트

세팅 후 아래 항목을 순서대로 확인하세요:

### 인프라
- [ ] GHES에서 Actions가 활성화되어 있는가?
- [ ] 필요한 GitHub Actions (`checkout`, `upload-artifact`, `download-artifact`, `github-script`)가 GHES에서 접근 가능한가?
- [ ] Self-hosted Runner가 등록되어 있고 Online 상태인가?
- [ ] 워크플로우의 `runs-on:` 라벨이 사용할 Runner의 라벨과 일치하는가?
  - 기존 러너: `runs-on` 값을 기존 러너 라벨로 변경했는지 확인
  - 새 러너: `self-hosted,ue5-review` 라벨이 설정되어 있는지 확인

### 네트워크
- [ ] Runner에서 GHES 인스턴스로 HTTPS 통신이 가능한가?
- [ ] Runner에서 `api.anthropic.com:443`으로 HTTPS 통신이 가능한가? (Stage 3)
- [ ] SSL 인증서 문제가 없는가? (`python3 -c "import urllib.request; urllib.request.urlopen('https://github.company.com')"`)
- [ ] 프록시 설정이 필요한 경우 `HTTPS_PROXY`, `NO_PROXY`가 설정되어 있는가?

### Secrets
- [ ] `BOT_REPO_TOKEN`이 게임 레포 Secrets에 등록되어 있는가?
- [ ] `GHES_TOKEN`이 게임 레포 Secrets에 등록되어 있는가?
- [ ] `GHES_URL`이 게임 레포 Secrets에 등록되어 있는가? (예: `https://github.company.com`)
- [ ] `ANTHROPIC_API_KEY`가 게임 레포 Secrets에 등록되어 있는가? (Stage 3)
- [ ] `GHES_URL` 끝에 `/`가 없는가?

### 도구
- [ ] Runner에 Python 3.10+가 설치되어 있는가?
- [ ] `pyyaml` 패키지가 설치되어 있는가?
- [ ] `jq`가 설치되어 있는가?
- [ ] `clang-format` 16+가 설치되어 있는가? (Stage 1)
- [ ] `clang-tidy` 16+가 설치되어 있는가? (Stage 2, 선택)

### 워크플로우
- [ ] 게임 레포의 `.github/workflows/`에 `code-review.yml`이 있는가?
- [ ] 게임 레포의 `.github/workflows/`에 `code-review-manual.yml`이 있는가?
- [ ] 봇 레포 이름이 `ue5-review-bot`이며 같은 Organization에 있는가?
  - 다른 이름/Organization이면 워크플로우의 `repository:` 값 수정 필요

### 기능 테스트
- [ ] 테스트 PR 생성 → `UE5 Code Review` 워크플로우 자동 트리거 확인
- [ ] Gate → Stage 1 → Post Review 순서로 정상 실행 확인
- [ ] PR에 리뷰 코멘트가 게시되는지 확인
- [ ] `/review` 코멘트로 수동 리뷰 트리거 확인 (선택)

---

## 6. 문제 해결

### "Resource not accessible by integration"

- `GHES_TOKEN` 또는 `BOT_REPO_TOKEN`의 권한이 부족합니다
- PAT에 `repo` 스코프가 있는지 확인하세요
- PAT이 만료되지 않았는지 확인하세요

### "SSL: CERTIFICATE_VERIFY_FAILED"

- GHES가 사설 CA 인증서를 사용하는 경우 [4.2 SSL/TLS 인증서 문제](#42-ssltls-인증서-문제) 참고
- `SSL_CERT_FILE` 환경변수 설정 확인

### "Repository not found" (봇 레포 체크아웃 실패)

- `BOT_REPO_TOKEN`이 봇 레포에 대한 읽기 권한이 있는지 확인
- 워크플로우의 `repository:` 값이 GHES에 존재하는 레포 경로와 일치하는지 확인
- 봇 레포와 게임 레포가 같은 GHES 인스턴스에 있는지 확인

### "HttpError: Not Found" (actions/checkout@v4)

- GHES에서 해당 Action이 사용 가능한지 확인
- GitHub Connect가 활성화되어 있거나 수동 동기화가 되어 있어야 함
- GHES 버전과 Action 버전 호환성 확인 (필요 시 `@v3`으로 다운그레이드)

### GitHub API 422 에러 (PR Review 게시 실패)

- 리뷰 코멘트가 diff hunk 밖의 라인을 참조하는 경우 발생
- 이 봇은 `filter_findings_by_diff()`로 diff 밖의 finding을 필터링하지만, diff 파일이 누락되면 필터가 작동하지 않음
- `--diff` 인자에 올바른 diff 파일 경로가 전달되는지 확인

### Stage 3 (LLM) 실행 실패

- `ANTHROPIC_API_KEY`가 설정되어 있는지 확인
- Runner에서 `api.anthropic.com:443`으로 네트워크 접근이 가능한지 확인
- 프록시 환경이면 `HTTPS_PROXY` 설정 확인

### Runner가 Job을 받지 않음

- Runner가 Online 상태인지 확인 (Settings > Actions > Runners)
- 워크플로우의 `runs-on:` 라벨과 Runner의 라벨이 일치하는지 확인
- 기존 조직 러너를 사용하는 경우: `runs-on` 값을 기존 러너 라벨로 변경했는지 확인
- 새 전용 러너의 경우: `self-hosted,ue5-review` 라벨이 설정되어 있는지 확인
