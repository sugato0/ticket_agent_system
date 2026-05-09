import json
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from .models import Agent, Task, TaskLog

ACTIVE_TASK_STATUSES = ["queued", "assigned"]


def add_log(
    db: Session,
    action: str,
    task_id: int | None = None,
    agent_id: str | None = None,
    comment: str = "",
) -> None:
    """Записывает событие по заявке или агенту."""
    record = TaskLog(
        action=action,
        task_id=task_id,
        agent_id=agent_id,
        comment=comment,
    )
    db.add(record)
    db.commit()


log = add_log


def task_dates(task: Task) -> list[str]:
    """Даты хранятся в БД как JSON-строка, здесь возвращаем обычный список."""
    if not task.dates:
        return []
    return json.loads(task.dates)


def task_priority_key(task: Task) -> tuple[int, int, float]:
    """Ключ сравнения: сначала админский приоритет, потом пользовательский.

    Более старая заявка должна быть выше, поэтому timestamp берется со знаком минус.
    """
    return (
        task.admin_priority or 0,
        task.user_priority or 0,
        -task.created_at.timestamp(),
    )


def sorted_queue_query(db: Session):
    return (
        db.query(Task)
        .filter(Task.status.in_(ACTIVE_TASK_STATUSES))
        .order_by(
            Task.admin_priority.desc(),
            Task.user_priority.desc(),
            Task.created_at.asc(),
        )
    )


queue_query = sorted_queue_query


def queue_position(db: Session, task_id: int) -> int | None:
    for position, task in enumerate(sorted_queue_query(db).all(), start=1):
        if task.id == task_id:
            return position
    return None


def date_load(db: Session, date_value: str, route_id: int | None = None) -> int:
    query = db.query(Task).filter(Task.status.in_(ACTIVE_TASK_STATUSES))

    if route_id is not None:
        query = query.filter(Task.route_id == route_id)

    load = 0
    for task in query.all():
        if date_value in task_dates(task):
            load += 1

    return load


def expire_old_tasks(db: Session) -> None:
    """Переводит активные заявки в failed, если все даты уже прошли."""
    today = date.today().isoformat()
    active_tasks = db.query(Task).filter(Task.status.in_(ACTIVE_TASK_STATUSES)).all()

    for task in active_tasks:
        dates = task_dates(task)
        if dates and max(dates) < today:
            task.status = "failed"
            task.failure_reason = "all_dates_expired"
            add_log(
                db,
                action="task_expired",
                task_id=task.id,
                agent_id=task.assigned_agent_id,
                comment="Все даты заявки прошли",
            )

    db.commit()


def mark_offline_agents(db: Session) -> None:
    """Если агент молчит больше 2 минут, возвращаем его задачу в очередь."""
    heartbeat_deadline = datetime.utcnow() - timedelta(minutes=2)

    stale_agents = (
        db.query(Agent)
        .filter(
            Agent.status.in_(["online", "busy"]),
            Agent.last_heartbeat < heartbeat_deadline,
        )
        .all()
    )

    for agent in stale_agents:
        task_id = agent.current_task_id

        agent.status = "offline"
        agent.current_task_id = None

        if task_id is None:
            continue

        task = db.get(Task, task_id)
        if task and task.status == "assigned":
            task.status = "queued"
            task.assigned_agent_id = None
            add_log(
                db,
                action="agent_timeout_requeue",
                task_id=task.id,
                agent_id=agent.id,
                comment="Агент отключился, задача возвращена в очередь",
            )

    db.commit()
