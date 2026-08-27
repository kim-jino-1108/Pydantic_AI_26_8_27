"""아주 작은 SQLite 할 일 보드: Week 5의 모든 워커가 공유하는 기반.

워커는 목표 하나를 받는다. 그 목표에 도달하기 위해 워커는 목표 아래에 자신의
단계 할 일들을 직접 적고, 하나씩 끝낼 때마다 체크하며, 단계가 모두 끝나면
목표를 완료로 표시한다. Day 1부터 Day 4까지는 각자 이런 작은 보드를 하나씩
돌리고, Day 5에는 같은 아이디어가 팀 전체를 조율하는 공유 보드로 확장된다.
보드는 테이블 하나짜리 단일 파일이라 서버가 필요 없고 Mac과 Windows에서
동일하게 동작한다. WAL 모드와 busy timeout 덕분에 여러 에이전트가 서로
충돌하지 않고 동시에 읽고 쓸 수 있다.
"""

# 파이썬 3.7~3.9에서도 list[dict] 같은 내장 제네릭 타입 힌트를 쓸 수 있게 함
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from rich.console import Console

# 보드 파일 경로
# 환경 변수 BOARD_PATH가 있으면 그 값을 쓰고, 없으면 이 파일이 있는 디렉터리의 board.sqlite를 사용
# __file__.resolve()로 심볼릭 링크까지 풀어 절대 경로를 얻은 뒤 parent로 디렉터리를 구함
BOARD_PATH = Path(os.environ.get("BOARD_PATH", Path(__file__).resolve().parent / "board.sqlite"))


def _connect(path: Path = BOARD_PATH) -> sqlite3.Connection:
    # SQLite 파일에 연결
    conn = sqlite3.connect(path)
    # 조회 결과를 인덱스뿐 아니라 컬럼명으로도 접근할 수 있는 Row 객체로 반환
    conn.row_factory = sqlite3.Row
    # WAL(Write-Ahead Logging) 모드로 전환: 읽기와 쓰기가 서로를 막지 않음
    conn.execute("PRAGMA journal_mode=WAL")
    # 잠금이 걸려 있을 때 즉시 실패하지 않고 최대 5000ms 동안 재시도
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def reset_board(path: Path = BOARD_PATH) -> None:
    """이미 있는 내용을 모두 지우고 비어 있는 새 보드를 만든다."""
    # with 블록을 벗어날 때 트랜잭션이 커밋됨 (연결 자체가 닫히지는 않음)
    with _connect(path) as conn:
        # 기존 todos 테이블이 있으면 삭제
        conn.execute("DROP TABLE IF EXISTS todos")
        # todos 테이블을 새로 생성
        # id: 자동 증가 기본키
        # parent_id: 상위 목표의 id (NULL이면 최상위 목표)
        # task: 할 일 내용
        # status: 상태 값, 기본값은 'pending'
        # result: 완료 결과 텍스트, 기본값은 빈 문자열
        conn.execute(
            """CREATE TABLE todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id INTEGER,
                task TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                result TEXT NOT NULL DEFAULT ''
            )"""
        )


def add_goal(task: str, path: Path = BOARD_PATH) -> int:
    """보드에 최상위 목표를 추가하고 그 id를 반환한다."""
    with _connect(path) as conn:
        # parent_id를 지정하지 않으므로 NULL이 되어 최상위 목표로 저장됨
        cur = conn.execute("INSERT INTO todos (task) VALUES (?)", (task,))
        # 방금 삽입된 행의 id를 반환
        return cur.lastrowid


def add_step(goal_id: int, task: str, path: Path = BOARD_PATH) -> int:
    """목표 아래에 단계를 추가하고 그 id를 반환한다."""
    with _connect(path) as conn:
        # parent_id에 목표 id를 넣어 해당 목표의 하위 단계로 저장
        cur = conn.execute(
            "INSERT INTO todos (parent_id, task) VALUES (?, ?)", (goal_id, task)
        )
        return cur.lastrowid


def list_todos(path: Path = BOARD_PATH) -> list[dict]:
    """보드에 있는 모든 할 일을 목표와 단계 구분 없이 오래된 순으로 반환한다."""
    with _connect(path) as conn:
        # id 오름차순으로 모든 행을 조회 (삽입된 순서와 동일)
        rows = conn.execute(
            "SELECT id, parent_id, task, status, result FROM todos ORDER BY id"
        ).fetchall()
        # sqlite3.Row 객체를 일반 dict로 변환해 반환
        return [dict(row) for row in rows]


def claim_todo(task_id: int, path: Path = BOARD_PATH) -> None:
    """할 일을 진행 중으로 표시해, 워커가 그 일을 집어 들었음을 나타낸다."""
    with _connect(path) as conn:
        # 해당 id의 상태를 'in_progress'로 변경
        conn.execute("UPDATE todos SET status = 'in_progress' WHERE id = ?", (task_id,))


def complete_todo(task_id: int, result: str, path: Path = BOARD_PATH) -> None:
    """할 일을 완료로 표시하고 그 결과를 기록한다."""
    with _connect(path) as conn:
        # 상태를 'done'으로 바꾸고 결과 텍스트를 함께 기록
        conn.execute(
            "UPDATE todos SET status = 'done', result = ? WHERE id = ?",
            (result, task_id),
        )


def show_board(path: Path = BOARD_PATH) -> None:
    """사람이 보기 좋게 보드를 출력한다: 각 목표 아래에 단계들을 들여쓰기해서
    보여주고, 완료된 할 일은 초록색 취소선으로, 진행 중인 것은 노란색으로
    표시한다. 이것은 보기 좋게 꾸민 화면일 뿐이며, 에이전트의 show_todos 도구는
    여전히 평범한 dict를 받는다.
    """
    # 보드의 모든 항목을 가져옴
    todos = list_todos(path)
    lines = []
    # parent_id가 None인 항목(최상위 목표)만 골라 순회
    for goal in [t for t in todos if t["parent_id"] is None]:
        lines.append(_format(goal, "Goal", ""))
        # 해당 목표를 parent_id로 가지는 하위 단계들을 들여쓰기해서 추가
        for step in [t for t in todos if t["parent_id"] == goal["id"]]:
            lines.append(_format(step, "Step", "  "))
    if lines:
        # 보드 전체를 한 번에 출력한다. 한 줄씩 출력하면 Jupyter 커널이 줄마다
        # 별도 블록을 만들어 사이가 벌어진 채로 쌓인다. soft_wrap은 긴 목표 줄이
        # 자동으로 줄바꿈되는 것을 막아준다.
        Console().print("\n".join(lines), soft_wrap=True)


def _format(todo: dict, kind: str, indent: str) -> str:
    # "들여쓰기 + 종류 + #id: 할 일" 형태의 기본 라벨을 만듦
    label = f"{indent}{kind} #{todo['id']}: {todo['task']}"
    if todo["status"] == "done":
        # 완료 상태는 rich 마크업으로 초록색 + 취소선 처리
        line = f"[green][strike]{label}[/strike][/green]"
        if todo["result"]:
            # 결과 텍스트가 있으면 흐린 색으로 뒤에 덧붙임
            line += f"  [dim]{todo['result']}[/dim]"
        return line
    if todo["status"] == "in_progress":
        # 진행 중 상태는 노란색으로 표시
        return f"[yellow]{label}[/yellow]"
    # 그 외(pending)는 아무 색상 없이 그대로 반환
    return label