"""보드를 대상으로 Pydantic AI 워커를 단순 서브프로세스로 실행한다.

인자 없이 실행하면 자체 Day 2 목표를 시드하고 수행한다(notes.txt 읽기, 스페인어로
번역, spanish.txt 쓰기): 보드에 단계를 계획하고, 파일시스템 MCP 서버를 통해 파일을
읽고, 번역하고, 스페인어 결과를 다시 쓰고, 목표를 닫기 전에 각 단계를 완료 처리한다.

task id와 공유 보드 경로를 주면, 같은 워커가 대신 Day 5 에이전트 루프에 합류한다:
보드 도구와 파일 도구를 공유 보드와 공유 사이트로 향하게 하고, 해당 task 하나만
점유하여 요구된 작업을 만들고 종료하며, 보드의 나머지는 건드리지 않는다.

    uv run pydantic_worker.py                       # 단독 실행 Day 2 데모
    uv run pydantic_worker.py <taskId> <boardPath>  # Day 5: 공유 보드에서 task 하나 처리
"""

from __future__ import annotations

import asyncio          # 비동기 이벤트 루프 실행용
import os               # 환경 변수 접근 및 os.devnull 사용
import sys              # 명령행 인자(sys.argv) 접근
from pathlib import Path  # 경로 조작을 객체 방식으로 처리

# Day 5 모드는 "<taskId> <boardPath>" 형태다. board 모듈은 import 시점에 BOARD_PATH로
# 대상 파일을 결정하므로, board를 import하기 전에 인자를 먼저 읽어야 한다.
# 인자 없이 실행하면 아래 블록은 전혀 동작하지 않는다.
TASK_ID = int(sys.argv[1]) if len(sys.argv) > 2 else None  # 인자가 2개 초과일 때만 task id로 해석
if TASK_ID is not None:
    # 이미 환경 변수가 설정돼 있으면 유지하고, 없을 때만 두 번째 인자를 보드 경로로 등록
    os.environ.setdefault("BOARD_PATH", sys.argv[2])

from dotenv import load_dotenv  # noqa: E402  # .env 파일에서 환경 변수 로드
from pydantic_ai import Agent  # noqa: E402  # 에이전트 본체 클래스
from pydantic_ai.mcp import MCPToolset  # noqa: E402  # MCP 서버를 도구 모음으로 감싸는 래퍼
from fastmcp.client.transports import StdioTransport  # noqa: E402  # 표준입출력 기반 MCP 전송 방식

import board  # noqa: E402  # 할 일 보드 조작 모듈(BOARD_PATH 설정 이후에 import)

load_dotenv(override=True)  # override=True: .env 값이 기존 환경 변수를 덮어씀

# 사용할 모델 식별자. WORKER_MODEL 환경 변수가 없으면 기본값 사용
MODEL = f"openai-chat:{os.environ.get('WORKER_MODEL', 'gpt-5.4-mini')}"
# 이 스크립트 파일이 있는 디렉터리 아래의 workspace 폴더 절대 경로
WORKSPACE = Path(__file__).resolve().parent / "workspace"
# 단독 실행 시 에이전트가 수행할 목표 문구
GOAL = "notes.txt를 읽고, 그 내용을 자연스러운 스페인어로 번역한 뒤, 그 스페인어를 spanish.txt에 쓰세요."
# 파일 도구가 쓰기를 허용받는 위치: 단독 실행이면 이 워커 자신의 workspace,
# Day 5 task 처리 중이면 공유 사이트(보드 파일이 들어 있는 폴더).
WORK_DIR = WORKSPACE if TASK_ID is None else Path(sys.argv[2]).resolve().parent


def show_todos() -> list[dict]:
    """보드에 있는 모든 할 일을 나열합니다. 목표(goal)는 parent_id가 None이고, 단계(step)는 parent_id에 소속 목표의 id가 들어 있습니다."""
    return board.list_todos()  # 보드 모듈에 그대로 위임


def plan_steps(goal_id: int, steps: list[str]) -> dict:
    """목표를 순서가 있는 단계 체크리스트로 나누어 보드에 등록합니다. 목표의 id와 짧은 단계 설명 목록을 전달하세요."""
    # 각 단계 문자열을 보드에 추가하고, 생성된 단계 id들을 리스트로 모아 반환
    return {"goal_id": goal_id, "step_ids": [board.add_step(goal_id, step) for step in steps]}


def complete_task(task_id: int, result: str) -> dict:
    """이 id를 가진 할 일(단계 또는 목표)을 완료로 표시하고 짧은 결과 요약을 기록합니다."""
    board.complete_todo(task_id, result)  # 상태를 done으로 바꾸고 결과 문자열 저장
    return {"task_id": task_id, "status": "done"}  # 호출자에게 처리 결과 반환


# 에이전트에게 지속적으로 적용되는 시스템 지시문
INSTRUCTIONS = """
당신은 공유 할 일 보드와 파일 도구 모음을 갖춘 신중한 작업자입니다.

대기 중인 목표를 맡아 끝까지 완수하세요. 먼저 짧은 계획을 세우는 것으로 시작하세요: 그 작업이 실제로 나뉘는 몇 가지 구체적인 단계를 목표 아래 보드에 추가하세요. 그런 다음 파일 도구로 그 단계들을 수행하고, 각 단계를 끝낼 때마다 완료로 표시하세요. 단계가 모두 끝나면 목표를 닫으세요. 당신의 파일은 도구가 사용을 허가받은 단 하나의 폴더 안에 있습니다.
"""

# 파일시스템 MCP 서버를 도구 모음으로 구성
filesystem = MCPToolset(
    StdioTransport(
        command="npx",  # Node 패키지 실행기로 MCP 서버를 기동
        # -y: 설치 확인 프롬프트 생략, 마지막 인자로 접근 허용 디렉터리를 지정
        args=["-y", "@modelcontextprotocol/server-filesystem", str(WORK_DIR)],
        cwd=str(WORK_DIR),  # 상대 경로 파일명이 작업 폴더 기준으로 해석되도록 서버를 그 폴더에서 시작
        log_file=Path(os.devnull),  # 서버 로그를 버림(콘솔 출력 오염 방지)
    ),
    init_timeout=60  # MCP 서버 초기화 대기 시간(초)
)


def seed() -> int:
    """보드를 초기화하고, 이전 출력물을 지우고, 목표 하나를 추가합니다."""
    board.reset_board()  # 보드의 기존 항목 전부 제거
    WORKSPACE.mkdir(exist_ok=True)  # workspace 폴더가 없으면 생성
    (WORKSPACE / "spanish.txt").unlink(missing_ok=True)  # 이전 실행 결과 파일이 있으면 삭제
    goal_id = board.add_goal(GOAL)  # 목표를 보드에 등록하고 id를 받음
    board.claim_todo(goal_id)  # 워커가 목표를 점유: pending -> in_progress
    return goal_id


async def main() -> None:
    if TASK_ID is None:  # 단독 실행 모드
        goal_id = seed()  # 보드 초기화 및 목표 시드
        print(f"목표 {goal_id} 시드 완료: {GOAL}\n")
        message = "보드에 대기 중인 목표를 수행해 주세요."  # 에이전트에게 보낼 첫 입력
    else:  # Day 5 공유 보드 모드
        board.claim_todo(TASK_ID)  # 공유 보드에서 이 task 하나만 점유 상태로 전환
        message = (
            f"당신은 공유 보드에서 task #{TASK_ID}를 점유했습니다. 오직 그 task와 그 단계들만 수행하세요. "
            f"작업을 만들고 확인까지 끝냈으면, complete_task로 task #{TASK_ID} 자체를 완료 표시한 다음 멈추세요."
        )

    # 워커 에이전트 구성: 모델, 지시문, 로컬 파이썬 도구, MCP 도구 모음
    worker = Agent(
        MODEL,
        instructions=INSTRUCTIONS,
        tools=[show_todos, plan_steps, complete_task],  # 보드 조작용 도구 3종
        toolsets=[filesystem],  # 파일 읽기/쓰기용 MCP 도구 모음
    )
    async with worker:  # MCP 서버 기동과 종료를 컨텍스트로 관리
        result = await worker.run(message)  # 에이전트 실행(도구 호출 루프 포함)

    # 단독 실행일 때만 결과를 출력한다. Day 5에서는 오케스트레이터가 콘솔을 담당한다.
    if TASK_ID is None:
        print(result.output)  # 에이전트의 최종 응답 텍스트
        print("\n실행 후 보드 상태:")
        board.show_board()  # 보드 전체 현황 출력
        spanish = WORKSPACE / "spanish.txt"
        if spanish.exists():  # 결과 파일이 실제로 생성됐는지 확인
            print("\nspanish.txt:\n" + spanish.read_text(encoding="utf-8"))


if __name__ == "__main__":
    asyncio.run(main())  # 비동기 main을 이벤트 루프에서 실행