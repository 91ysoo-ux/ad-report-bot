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

## 로컬에서 돌려보기

```bash
pip install -r requirements.txt
python src/report.py
```

`.env`에 `META_ACCESS_TOKEN`, `META_AD_ACCOUNT_ID`를 넣어두면 로컬에서도 동일하게 동작한다
(GitHub Actions에서는 같은 이름의 저장소 시크릿을 읽는다).

## 남은 일

- TikTok Marketing API 앱 심사 통과하면 `src/report.py`에 TikTok fetch 추가,
  `platform` 필드로 Meta/TikTok 구분
- 자연조회수(광고 아닌 순수 도달)는 API로 못 가져와서 계속 수동 입력 필요 —
  당장은 이 리포트에서 빠져 있음
- GitHub 기본 cron이 며칠씩 안 도는 사례가 morningpick에서 있었다 —
  이 리포지토리도 그러면 `workflow_dispatch`를 외부 크론(cron-job.org 등)으로
  두드리는 방식으로 바꾼다
