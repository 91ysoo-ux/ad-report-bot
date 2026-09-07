# 광고 리포트 봇

박연수 브랜드(금별맥주 등)의 Meta 광고 캠페인 성과를 주 2회 자동으로 가져와
[docs/index.html](docs/index.html)로 갱신하고 GitHub Pages에 올리는 무인 파이프라인.

```
목/월 09:00 KST → Meta Graph API 인사이트 pull → CPV 효율 계산 → docs/index.html 생성 → GitHub Pages 배포
```

사람이 대화창을 열어서 "갱신해줘"라고 말할 필요 없이, GitHub Actions가 대신 돈다.

## 왜 이렇게 만들었나

- 캠페인마다 광고금액·타겟 설정이 매번 달라서, 절대 조회수가 아니라
  **조회 1회당 비용(CPV)** 으로 비교해야 어떤 조합이 효율적이었는지 보인다.
- CPV 하위 1/3은 초록, 상위 1/3은 빨강으로 표에도 막대그래프에도 동일하게 표시한다
  (숫자만으로도 판단 가능하도록 텍스트 배지 형태 — 색만으로 정보 전달하지 않음).
- 3초조회(`actions.video_view`)와 완료조회(`video_p100_watched_actions`)는
  Meta가 주는 값 그대로 쓴다 — 플랫폼마다 정의가 달라서 임의로 통일하지 않는다.
- 캠페인 하나가 여러 주차의 세팅(광고그룹)을 재사용하는 경우가 있어서, 비교 단위는
  캠페인이 아니라 **광고그룹(ad set)**. 세팅마다 예산·타겟·최적화 목표를 그대로 보여줘야
  "이렇게 세팅하면 이런 조회수가 나오는구나"를 판단할 수 있다.
- 화면은 **표로 한눈에, 클릭하면 세팅 상세**가 펼쳐지는 구조다. 표 단위는 "기간"
  (같은 날 시작한 세팅 묶음) — 메타·틱톡은 항상 같은 날 시작·종료하기로 되어 있으므로,
  **날짜가 곧 두 플랫폼을 하나로 묶는 조인 키**다. `_parse_date()`로 이름 앞자리
  (YYMMDD/YYYYMMDD)에서 날짜를 뽑아 `row["date"]`에 저장해두면, 화면은 그 날짜로
  그룹핑만 하면 된다 — 플랫폼이 몇 개든 새로 짤 필요가 없다.
- 가장 먼저 봐야 하는 숫자는 "이번 달/이번 기간에 틱톡+메타 합쳐서 얼마 쓰고 얼마
  조회됐나"이므로, 월별 요약 표를 상세 표보다 위에 둔다.

## 화면 구조

1. **상단 KPI 타일** — 누적 통합 광고비·조회수·CPV (지금은 Meta만, 틱톡 붙으면 자동으로 합산됨)
2. **월별 통합 성과** — 월 단위 롤업, 펼치기 없음
3. **세팅별 CPV 막대그래프** — 광고그룹 단위 비교, 효율 상/하위 1/3 색상 표시
4. **캠페인 기간별 통합 성과** — 기간(날짜) 단위 표, 행 클릭 시 그 기간의 개별 세팅
   카드(예산·타겟·목표·CPV·100자 총평)가 펼쳐짐. 각 기간 행에 Meta/TikTok 열이 이미
   분리되어 있고, 틱톡 데이터가 없으면 "연동 전"으로 표시됨
5. **다음 세팅 가이드** — 목표(optimization_goal)별 평균 CPV를 비교해 최적 조합과
   추천 예산대를 자동 생성

## 로컬에서 돌려보기

```bash
pip install -r requirements.txt
python src/report.py
```

`.env`에 `META_ACCESS_TOKEN`, `META_AD_ACCOUNT_ID`를 넣어두면 로컬에서도 동일하게 동작한다
(GitHub Actions에서는 같은 이름의 저장소 시크릿을 읽는다).

## 남은 일

- **TikTok 연동 계획 (앱 심사 통과하면 바로 진행)**
  1. `src/report.py`에 `fetch_tiktok_rows()` 추가 — TikTok Marketing API의
     Reporting 엔드포인트에서 캠페인/광고그룹 인사이트를 가져온다.
  2. 각 TikTok row는 Meta row와 **완전히 같은 스키마**로 만든다: `platform: "tiktok"`,
     `date`(이름 앞자리 YYMMDD 파싱 — Meta와 동일 컨벤션), `spend`, `views`(TikTok의
     조회 지표 — 6초/2초 조회 중 정할 것), `budget`, `optimization_goal` 등.
     화면 쪽(`template.html`) 코드는 한 줄도 안 고쳐도 된다 — 이미 `platform` 필드로
     분기하고, `date`로 기간을 묶고, `views`로 통합 집계하도록 짜여 있다.
  3. `CAMPAIGNS` 배열에 Meta row와 TikTok row를 그냥 합쳐서 넣으면, 같은 날짜를 가진
     Meta+TikTok 세팅이 자동으로 같은 "기간" 행으로 묶여서 통합 광고비·조회수가 나온다
     (메타·틱톡은 항상 같은 날 시작·종료하기로 되어 있어서 날짜가 그대로 조인 키가 됨).
  4. PLATFORM_LABEL/GOAL_LABEL에 TikTok 쪽 값(예: `optimization_goal`이 다른 이름을
     쓸 수 있음) 매핑만 추가.
- 자연조회수(광고 아닌 순수 도달)는 API로 못 가져와서 계속 수동 입력 필요 —
  당장은 이 리포트에서 빠져 있음
- GitHub 기본 cron이 며칠씩 안 도는 사례가 morningpick에서 있었다 —
  이 리포지토리도 그러면 `workflow_dispatch`를 외부 크론(cron-job.org 등)으로
  두드리는 방식으로 바꾼다
