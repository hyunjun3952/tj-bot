# TJ Bot

[TJ 미디어](https://www.tjmedia.com/) 노래방 반주곡을 Discord에서 검색하는 봇이에요.

> 비공식 프로젝트예요. TJ 미디어와 제휴, 후원, 승인 관계가 없어요. 곡 정보와 관련 명칭·로고의 권리는 각 권리자에게 있어요.

## 명령어

| 명령어 | 설명 |
| --- | --- |
| `/검색 <검색어> [유형] [곡분류]` | 곡명, 가수명, 곡번호로 검색해요. 초성 검색과 일본곡의 한글 발음·로마자 검색도 돼요. 유형(가수명 등)과 곡분류(일본곡 등)는 처음부터 고르거나 결과에서 바꿀 수 있어요. |
| `/최신곡` | TJ 미디어의 이번 달 최신곡을 보여줘요. 이번 달 목록이 아직 비어 있으면 지난달 목록을 보여줘요. |

결과 메시지의 버튼은 명령어를 실행한 사람이 조작해요. 다른 사람이 누르면 그 사람에게만 보이는 복사본이 열려요.

## 실행하기

Python 3.12 이상과 [uv](https://docs.astral.sh/uv/)가 필요해요.

1. [Discord Developer Portal](https://discord.com/developers/applications)에서 봇을 만들고 토큰을 발급해요. Privileged Intent는 필요 없어요.
2. OAuth2 스코프 `bot`, `applications.commands`로 초대 링크를 만들어 서버에 초대해요. DM이나 봇이 없는 서버에서도 쓰려면 Installation 탭에서 **User Install**을 켜요.
3. 저장소 루트에 `.env`를 만들어요.

   ```env
   DISCORD_TOKEN=발급받은_봇_토큰
   # 선택: 개발용 서버 ID. 지정하면 명령어가 그 서버에 즉시 동기화돼요. 비우면 전역 동기화예요.
   GUILD_ID=
   ```

4. 설치하고 실행해요.

   ```sh
   uv sync
   uv run python main.py
   ```

`.env`와 토큰은 절대 커밋하지 마세요.

## 개발

```sh
uv run ruff check .
uv run ruff format --check .
uv run pyrefly check
uv run pytest
```

PR 전에 네 가지를 모두 통과시켜 주세요. CI도 같은 검사를 해요. 구조와 코드 스타일은 [CLAUDE.md](CLAUDE.md)에 있어요.

## 유의사항

- 공식 API 없이 TJ 미디어의 공개 페이지를 조회해서 보여줘요. 사이트 구조가 바뀌면 예고 없이 동작하지 않을 수 있어요.
- 직접 운영하기 전에 TJ 미디어와 [Discord](https://discord.com/terms)의 약관·정책을 확인하세요. 이용 가능 여부는 보증하지 않아요.
- 요청 수 제한, 캐시, 쿨다운은 TJ 서버를 보호하기 위한 장치예요. 풀거나 우회하지 말고, 대량 수집이나 재배포에 쓰지 마세요.
- 곡 데이터는 저장소에 없어요. 질의 시점에 TJ 사이트에서 가져와요.
- 사용자 데이터는 저장하지 않아요. 검색어는 TJ 미디어에 전달되고 잠시 메모리 캐시에만 남아요.

## 기여

이슈와 PR 환영해요. 커밋은 `fix(search): ...` 같은 Conventional Commit 스타일로 써 주세요.

## 라이선스

[Apache License 2.0](LICENSE.md)이에요. 소스 코드에만 적용되고, TJ 미디어의 서비스·상표·곡 정보의 권리는 포함하지 않아요.
