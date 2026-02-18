# STEP4_POST_REVIEW.md — PR 코멘트 게시

> Stage 1~3 결과를 통합하여 **단일 PR Review**로 게시.
> 워크플로우에서 `if: always()`로 실행되므로 앞 Stage 일부 실패/skip 시에도 동작해야 함.

## 산출물

| 파일 | 설명 |
|------|------|
| `scripts/post_review.py` | 결과 통합 + PR Review 게시 |
| `scripts/utils/gh_api.py` | 기존 파일 확장 — PR Review 게시 함수 추가 |
| `tests/test_post_review.py` | 통합/게시 로직 테스트 |

---

## 1. `scripts/post_review.py`

### CLI

```
python -m scripts.post_review \
  --stage1-patterns  findings-stage1.json \
  --stage1-format    suggestions-format.json \
  --stage2           findings-stage2.json \
  --stage3           findings-stage3.json \
  --pr-number        123 \
  --pr-diff          pr.diff \
  --repo             owner/repo \
  --output           review-result.json
```

| 인자 | 필수 | 설명 |
|------|------|------|
| `--stage1-patterns` | O | Stage 1 패턴 검사 결과 JSON |
| `--stage1-format` | X | Stage 1 clang-format suggestion JSON |
| `--stage2` | X | Stage 2 clang-tidy 결과 JSON |
| `--stage3` | X | Stage 3 LLM 리뷰 결과 JSON |
| `--pr-number` | O | PR 번호 |
| `--pr-diff` | O | PR diff 파일 (코멘트 위치 검증용) |
| `--repo` | O | `owner/repo` 형식 |
| `--output` | X | 게시 결과 JSON (디버깅/로깅용) |

환경변수:
- `GHES_URL` — GitHub Enterprise Server URL (예: `https://github.company.com`)
- `GHES_TOKEN` — PR Review 쓰기 권한 PAT

> `GHES_URL`이 없으면 github.com API 사용 (테스트/오픈소스 호환).

### 동작 흐름

```
1. 입력 JSON 로드 (없는 파일은 빈 배열로 처리)
2. 전체 결과 통합 + 정렬 (file → line 순)
3. diff 기반 코멘트 위치 검증 (유효 라인만 통과)
4. 중복 제거 (같은 file + line + rule_id → 우선순위 높은 것만)
5. GitHub PR Review API로 단일 리뷰 제출
6. 결과 JSON 출력 (성공/실패 카운트)
```

### 1-1. 입력 로드

```python
def load_findings(path: str) -> List[dict]:
    """JSON 파일 로드. 파일 없거나 빈 파일이면 빈 리스트 반환."""
```

- 파일 없음 → `[]` (경고 로그, 에러 아님)
- JSON 파싱 실패 → `[]` (경고 로그)
- 정상 → 리스트 반환

### 1-2. 통합 + 정렬

모든 Stage 결과를 하나의 리스트로 합치되, 출처(`stage`) 필드 부착:

```python
finding["stage"] = "stage1-pattern"  # or "stage1-format", "stage2", "stage3"
```

정렬 기준: `file` → `line` (ASC)

### 1-3. diff 기반 코멘트 위치 검증

PR diff를 파싱하여 **코멘트 가능한 라인 범위**를 확인한다.
GitHub PR Review API는 diff에 포함된 라인에만 코멘트를 달 수 있다.

```python
def get_commentable_ranges(diff_text: str) -> Dict[str, List[Tuple[int, int]]]:
    """diff에서 파일별 코멘트 가능 라인 범위 추출.

    Returns: {"Source/A.cpp": [(10, 25), (40, 60)], ...}
    """
```

- diff_parser.py의 `parse_diff()` 재사용하여 파일별 hunk 범위 추출
- finding의 `line`이 해당 파일의 어떤 hunk 범위에도 속하지 않으면 **제외** (경고 로그)
- `end_line`이 있으면 `line`~`end_line` 전체가 범위 내에 있어야 suggestion, 부분 포함이면 일반 코멘트로 전환

### 1-4. 중복 제거

여러 Stage에서 같은 위치를 지적할 수 있다. 우선순위:

```
Stage 1 (확정적) > Stage 2 (정적 분석) > Stage 3 (LLM)
```

- 같은 `file` + `line` + `rule_id` → 낮은 Stage 번호 우선
- 같은 `file` + `line`, 다른 `rule_id` → 모두 유지 (서로 다른 지적)

### 1-5. PR Review 코멘트 변환

각 finding을 GitHub Review Comment로 변환:

**suggestion이 있는 경우 (auto-fix):**

```markdown
**[rule_id]** message

```suggestion
suggestion 내용
```⁣
```

- `end_line` 있으면 `start_line` = `line`, `line` = `end_line` (GitHub multi-line 형식)
- `end_line` 없으면 single-line suggestion

**suggestion이 없는 경우 (일반 코멘트):**

```markdown
**[rule_id]** ⚠️ message
```

- severity가 `error`이면 앞에 severity 아이콘 추가: `🚫`
- severity가 `warning`이면: `⚠️`
- severity가 `info`이면: `ℹ️`

### 1-6. Review 제출

**단일 Review로 일괄 제출** (개별 코멘트가 아닌 `POST /repos/.../pulls/.../reviews`):

```python
{
  "event": "COMMENT",          # APPROVE / REQUEST_CHANGES 하지 않음
  "body": "## UE5 코드 리뷰 결과 요약\n\n...",
  "comments": [
    {
      "path": "Source/MyActor.cpp",
      "line": 42,               # single-line
      "body": "**[logtemp]** ⚠️ LogTemp 대신 적절한 로그 카테고리를 사용하세요."
    },
    {
      "path": "Source/MyActor.cpp",
      "start_line": 10,         # multi-line (GHES 3.4+)
      "line": 12,
      "body": "**[clang_format]** clang-format 자동 수정 제안\n\n```suggestion\n    if (bFlag)\n    {\n    }\n```"
    }
  ]
}
```

**Review body (요약):**

```markdown
## UE5 코드 리뷰 결과 요약

| Stage | 항목 수 |
|-------|--------|
| Stage 1 — 패턴 검사 | 3 |
| Stage 1 — 포맷 제안 | 2 |
| Stage 2 — 정적 분석 | 1 |
| Stage 3 — LLM 리뷰  | 0 (skip) |
| **합계** | **6** |

> 🔧 `suggestion` 블록은 GitHub UI에서 바로 적용할 수 있습니다.
```

- 0건이면: "리뷰 항목이 없습니다. ✅" 요약만 게시 (빈 Review)
- Stage가 skip되면 "(skip)" 표시

### 1-7. 코멘트 수 제한

- **최대 50개** 코멘트까지 게시 (GitHub API 한도 + 리뷰 가독성)
- 초과 시 severity 기준 정렬 (`error` > `warning` > `info` > `suggestion`) 후 상위 50개만
- 요약에 "N개 항목이 생략되었습니다" 표시

### 1-8. GHES 버전 감지 + Fallback

```python
def check_ghes_multiline_support(ghes_url: str, token: str) -> bool:
    """GHES 3.4+ 여부 확인. multi-line suggestion 지원 판단."""
```

- `GET /api/v3/meta` → `installed_version` 확인
- 3.4 미만이면 multi-line suggestion을 **단일 라인 코멘트 + 코드 블록**으로 fallback:

```markdown
**[clang_format]** clang-format 자동 수정 제안 (L10-L12)

```cpp
    if (bFlag)
    {
    }
```⁣

> ℹ️ GHES 버전이 multi-line suggestion을 지원하지 않아 코드 블록으로 표시합니다.
```

- 버전 확인 실패 시 multi-line 사용 시도 → API 오류 시 fallback 재시도

---

## 2. `scripts/utils/gh_api.py` 확장

기존 `get_pr_labels()` 유지. 다음 함수 추가:

### 추가 함수

```python
def submit_pr_review(
    repo: str,
    pr_number: int,
    body: str,
    comments: List[dict],
    ghes_url: Optional[str] = None,
    token: Optional[str] = None,
) -> dict:
    """PR Review를 단일 요청으로 제출.

    Args:
        repo: "owner/repo" 형식
        pr_number: PR 번호
        body: Review 요약 (body)
        comments: Review comment 리스트
        ghes_url: GHES URL (None이면 github.com)
        token: PAT (None이면 환경변수 GHES_TOKEN)

    Returns:
        API 응답 dict (review_id, html_url 등)

    Raises:
        RuntimeError: API 호출 실패 시
    """
```

```python
def get_ghes_version(
    ghes_url: str,
    token: str,
) -> Optional[str]:
    """GHES 버전 문자열 반환. 실패 시 None."""
```

### API 호출 방식

`requests` 라이브러리 사용 (Runner 환경에 설치 가정):

```python
# 엔드포인트
base_url = ghes_url or "https://api.github.com"
url = f"{base_url}/api/v3/repos/{repo}/pulls/{pr_number}/reviews"

# github.com일 때
url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"

headers = {
    "Authorization": f"token {token}",
    "Accept": "application/vnd.github.v3+json",
}
```

- GHES: `{ghes_url}/api/v3/...`
- github.com: `https://api.github.com/...`
- 타임아웃: 30초
- 재시도: HTTP 5xx에 대해 최대 3회 (exponential backoff 1s, 2s, 4s)

---

## 3. 출력 JSON (`--output`)

```json
{
  "review_id": 12345,
  "review_url": "https://github.company.com/owner/repo/pull/123#pullrequestreview-12345",
  "total_findings": 15,
  "posted_comments": 12,
  "skipped_out_of_range": 2,
  "skipped_duplicate": 1,
  "skipped_over_limit": 0,
  "by_stage": {
    "stage1-pattern": 5,
    "stage1-format": 3,
    "stage2": 2,
    "stage3": 2
  },
  "by_severity": {
    "error": 2,
    "warning": 6,
    "info": 1,
    "suggestion": 3
  }
}
```

---

## 4. 테스트

### `tests/test_post_review.py`

#### 입력 로드 (5개)

- 정상 JSON → 리스트 반환
- 파일 없음 → 빈 리스트 + 경고
- 빈 파일 → 빈 리스트
- JSON 파싱 실패 → 빈 리스트 + 경고
- 여러 Stage 파일 로드 → stage 필드 부착 확인

#### 통합 + 정렬 (3개)

- 여러 Stage 결과 합산 → file/line 정렬 확인
- 빈 입력 포함 → 정상 동작
- 전부 빈 입력 → 빈 리스트

#### diff 위치 검증 (5개)

- hunk 범위 내 라인 → 통과
- hunk 범위 밖 라인 → 제외
- end_line 범위가 부분적으로 hunk 밖 → suggestion 제거, 일반 코멘트 전환
- diff에 없는 파일 → 제외
- 빈 diff → 모든 finding 제외

#### 중복 제거 (3개)

- 같은 file/line/rule_id, 다른 Stage → 낮은 Stage 우선
- 같은 file/line, 다른 rule_id → 모두 유지
- 중복 없음 → 변화 없음

#### 코멘트 변환 (6개)

- suggestion 있음 → suggestion 블록 포함 body 생성
- suggestion 없음 + severity error → `🚫` 아이콘
- suggestion 없음 + severity warning → `⚠️` 아이콘
- suggestion 없음 + severity info → `ℹ️` 아이콘
- multi-line (end_line) → start_line / line 구분
- single-line → line만

#### 코멘트 수 제한 (3개)

- 50개 이하 → 전부 게시
- 51개 이상 → severity 우선순위로 50개 선택 + 요약에 생략 표시
- 0개 → 빈 Review (요약만)

#### Review 요약 (3개)

- 각 Stage별 카운트 정확성
- skip된 Stage → "(skip)" 표시
- 0건 → "리뷰 항목이 없습니다. ✅"

#### GHES 버전 + fallback (4개)

- GHES 3.4+ → multi-line suggestion 사용
- GHES 3.3 → fallback (코드 블록)
- 버전 확인 실패 → multi-line 시도
- github.com (GHES_URL 없음) → multi-line 사용

#### API 제출 (5개, mock)

- 정상 제출 → review_id 반환
- HTTP 5xx → 재시도 후 성공
- HTTP 4xx → 즉시 실패 (재시도 안 함)
- 토큰 없음 → RuntimeError
- GHES URL 유무에 따른 엔드포인트 분기

#### 통합 테스트 (3개)

- Stage 1 결과만 있을 때 → 정상 게시 (mock)
- Stage 1 + 2 + 3 전부 있을 때 → 통합 게시 (mock)
- 모든 입력 없음 → 빈 Review 게시 (mock)

**예상 테스트 수: 약 40개**

---

## 5. 의존성

| 패키지 | 용도 | 비고 |
|--------|------|------|
| `requests` | GitHub API 호출 | Runner에 `pip install requests` |
| `scripts/utils/diff_parser.py` | hunk 범위 추출 | 기존 모듈 재사용 |

> `gh` CLI 대신 `requests`를 사용하는 이유: Review API의 `comments` 배열을 단일 요청으로 보내려면 REST API 직접 호출이 필요. `gh` CLI는 개별 코멘트만 지원.

---

## 6. 에러 처리

| 상황 | 동작 |
|------|------|
| 입력 파일 없음 | 빈 배열로 처리, 경고 로그 |
| 모든 입력 빈 배열 | 빈 Review 게시 (요약만) |
| GHES_TOKEN 없음 | 에러 종료 (exit 1) |
| API 호출 실패 (5xx) | 최대 3회 재시도 후 에러 종료 |
| API 호출 실패 (4xx) | 에러 메시지 출력 후 종료 |
| multi-line 미지원 | fallback 코드 블록으로 재시도 |
| 코멘트 50개 초과 | severity 우선순위로 상위 50개만 게시 |
