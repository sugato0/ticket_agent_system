import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import Dict

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .auth import create_token, get_current_user, hash_password, require_admin, verify_password
from .database import Base, SessionLocal, engine, get_db
from .models import Agent, Route, Task, User
from .schemas import AdminPriorityIn, LoginIn, RouteIn, TaskIn, TaskUpdate, TokenOut
from .services import (
    date_load,
    expire_old_tasks,
    log,
    mark_offline_agents,
    queue_position,
    task_dates,
    task_priority_key,
)

app = FastAPI(title="Ticket Agent Task System")
templates = Jinja2Templates(directory="app/templates")

agent_connections: Dict[str, WebSocket] = {}


def seed_data() -> None:
    """Минимальные тестовые данные для демонстрации работы системы."""
    db = SessionLocal()

    try:
        if db.query(User).count() == 0:
            db.add_all(
                [
                    User(
                        username="admin",
                        password_hash=hash_password("admin"),
                        is_admin=True,
                    ),
                    User(
                        username="user",
                        password_hash=hash_password("user"),
                        is_admin=False,
                    ),
                ]
            )
            db.commit()

        if db.query(Route).count() == 0:
            db.add_all(
                [
                    Route(description="Москва - Сочи"),
                    Route(description="Москва - Санкт-Петербург"),
                    Route(description="Калининград - Москва"),
                ]
            )
            db.commit()

        need_demo_tasks = os.getenv("SEED_DEMO_DATA", "1") == "1"
        if need_demo_tasks and db.query(Task).count() == 0:
            user = db.query(User).filter(User.username == "user").first()
            routes = db.query(Route).order_by(Route.id).all()

            demo_tasks = [
                Task(
                    user_id=user.id,
                    route_id=routes[0].id,
                    dates=json.dumps(["2026-05-10", "2026-05-12"]),
                    last_name="Кукушка",
                    first_name="кукушонку",
                    email="kukushka@gmail.com",
                    phone="+71111111111",
                    user_priority=1,
                ),
                Task(
                    user_id=user.id,
                    route_id=routes[0].id,
                    dates=json.dumps(["2026-05-10"]),
                    last_name="Надела",
                    first_name="капюшоку",
                    email="kapusha@gmail.com",
                    phone="+71111111111",
                    user_priority=5,
                ),
                Task(
                    user_id=user.id,
                    route_id=routes[1].id,
                    dates=json.dumps(["2026-05-11", "2026-05-15"]),
                    last_name="Дальше",
                    first_name="не помню",
                    email="forgot2005@gmail.com",
                    phone="+71111111111",
                    user_priority=3,
                ),
            ]

            db.add_all(demo_tasks)
            db.commit()

    finally:
        db.close()


@app.on_event("startup")
async def startup() -> None:
    Base.metadata.create_all(bind=engine)
    seed_data()
    asyncio.create_task(scheduler_loop())


async def send_abort(agent_id: str, task_id: int) -> None:
    connection = agent_connections.get(agent_id)
    if connection is None:
        return

    await connection.send_text(
        json.dumps(
            {
                "type": "abort",
                "task_id": task_id,
            }
        )
    )


async def assign_task(db: Session, task: Task, agent: Agent) -> None:
    """Назначает заявку агенту и отправляет команду assign по WebSocket."""
    task.status = "assigned"
    task.assigned_agent_id = agent.id

    agent.status = "busy"
    agent.current_task_id = task.id

    db.commit()

    connection = agent_connections.get(agent.id)
    if connection is not None:
        await connection.send_text(
            json.dumps(
                {
                    "type": "assign",
                    "task_id": task.id,
                    "route_id": task.route_id,
                    "dates": task_dates(task),
                    "passenger": {
                        "last_name": task.last_name,
                        "first_name": task.first_name,
                        "email": task.email,
                        "phone": task.phone,
                    },
                },
                ensure_ascii=False,
            )
        )

    log(db, "task_assigned", task.id, agent.id, "Задача назначена агенту")


def get_queued_tasks(db: Session) -> list[Task]:
    return (
        db.query(Task)
        .filter(Task.status == "queued")
        .order_by(
            Task.admin_priority.desc(),
            Task.user_priority.desc(),
            Task.created_at.asc(),
        )
        .all()
    )


def get_free_agents(db: Session) -> list[Agent]:
    return (
        db.query(Agent)
        .filter(Agent.status == "online")
        .order_by(Agent.connected_at.asc())
        .all()
    )


async def try_preemption(db: Session) -> None:
    """Вытеснение: новая важная заявка может прервать самую слабую активную."""
    best_queued = (
        db.query(Task)
        .filter(Task.status == "queued")
        .order_by(
            Task.admin_priority.desc(),
            Task.user_priority.desc(),
            Task.created_at.asc(),
        )
        .first()
    )

    if best_queued is None:
        return

    active_tasks = db.query(Task).filter(Task.status == "assigned").all()
    if not active_tasks:
        return

    now = datetime.utcnow()
    allowed_to_preempt = [
        task
        for task in active_tasks
        if task.preempt_cooldown_until is None or task.preempt_cooldown_until <= now
    ]

    if not allowed_to_preempt:
        return

    lowest_task = sorted(allowed_to_preempt, key=task_priority_key)[0]
    if task_priority_key(best_queued) <= task_priority_key(lowest_task):
        return

    agent_id = lowest_task.assigned_agent_id
    if agent_id is None:
        return

    agent = db.get(Agent, agent_id)
    await send_abort(agent_id, lowest_task.id)

    lowest_task.status = "queued"
    lowest_task.assigned_agent_id = None
    lowest_task.preempt_cooldown_until = now + timedelta(seconds=30)

    if agent is not None:
        agent.status = "online"
        agent.current_task_id = None

    db.commit()
    log(db, "task_preempted", lowest_task.id, agent_id, "Задача вытеснена")

    if agent is not None:
        await assign_task(db, best_queued, agent)


async def run_scheduler_once() -> None:
    db = SessionLocal()

    try:
        expire_old_tasks(db)
        mark_offline_agents(db)

        free_agents = get_free_agents(db)
        queued_tasks = get_queued_tasks(db)

        while free_agents and queued_tasks:
            agent = free_agents.pop(0)
            task = queued_tasks.pop(0)
            await assign_task(db, task, agent)

        # Если свободных агентов нет, проверяем возможность вытеснения.
        if not free_agents:
            await try_preemption(db)

    finally:
        db.close()


async def scheduler_loop() -> None:
    interval = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "10"))

    while True:
        try:
            await run_scheduler_once()
        except Exception as error:
            print(f"scheduler error: {error}")

        await asyncio.sleep(interval)


@app.post("/api/auth/login", response_model=TokenOut)
def login(data: LoginIn, db: Session = Depends(get_db)) -> TokenOut:
    user = db.query(User).filter(User.username == data.username).first()

    if user is None or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Bad credentials")

    return TokenOut(
        access_token=create_token(user),
        is_admin=user.is_admin,
    )


@app.get("/api/routes")
def list_routes(db: Session = Depends(get_db)):
    return db.query(Route).order_by(Route.id).all()


@app.post("/api/tasks")
async def create_task(
    data: TaskIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = Task(
        user_id=user.id,
        route_id=data.route_id,
        dates=json.dumps(data.dates),
        last_name=data.last_name,
        first_name=data.first_name,
        email=data.email,
        phone=data.phone,
        user_priority=data.user_priority,
    )

    db.add(task)
    db.commit()
    db.refresh(task)

    log(db, "task_created", task.id, comment="Пользователь создал заявку")
    await run_scheduler_once()

    return task


@app.get("/api/tasks")
def my_tasks(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tasks = (
        db.query(Task)
        .filter(Task.user_id == user.id)
        .order_by(Task.created_at.desc())
        .all()
    )
    return [task_to_dict(db, task) for task in tasks]


@app.get("/api/tasks/{task_id}")
def get_task(
    task_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = db.get(Task, task_id)

    if task is None or (task.user_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="Not found")

    return task_to_dict(db, task)


@app.delete("/api/tasks/{task_id}")
async def cancel_task(
    task_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = db.get(Task, task_id)

    if task is None or (task.user_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="Not found")

    if task.assigned_agent_id:
        await send_abort(task.assigned_agent_id, task.id)

        agent = db.get(Agent, task.assigned_agent_id)
        if agent is not None:
            agent.status = "online"
            agent.current_task_id = None

    task.status = "cancelled_by_admin" if user.is_admin else "cancelled_by_user"

    db.commit()
    log(db, "task_cancelled", task.id, task.assigned_agent_id, "Заявка отменена")

    return {"ok": True}


@app.put("/api/tasks/{task_id}")
async def update_task(
    task_id: int,
    data: TaskUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = db.get(Task, task_id)

    if task is None or task.user_id != user.id or task.status != "queued":
        raise HTTPException(status_code=400, detail="Only own queued task can be updated")

    changes = data.model_dump(exclude_unset=True)
    for field, value in changes.items():
        if field == "dates":
            value = json.dumps(value)
        setattr(task, field, value)

    db.commit()
    log(db, "task_updated", task.id, comment="Заявка изменена пользователем")

    await run_scheduler_once()
    return task_to_dict(db, task)


@app.get("/api/tasks/{task_id}/queue-position")
def get_position(
    task_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = db.get(Task, task_id)

    if task is None or (task.user_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="Not found")

    return {
        "task_id": task_id,
        "position": queue_position(db, task_id),
    }


@app.get("/api/date-load")
def get_date_load(
    date: str,
    route_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return {
        "date": date,
        "route_id": route_id,
        "load": date_load(db, date, route_id),
    }


@app.get("/api/admin/tasks")
def admin_tasks(
    status: str | None = None,
    route_id: int | None = None,
    user_id: int | None = None,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = db.query(Task)

    if status:
        query = query.filter(Task.status == status)
    if route_id:
        query = query.filter(Task.route_id == route_id)
    if user_id:
        query = query.filter(Task.user_id == user_id)

    tasks = query.order_by(Task.created_at.desc()).all()
    return [task_to_dict(db, task) for task in tasks]


@app.post("/api/admin/routes")
def admin_create_route(
    data: RouteIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    route = Route(description=data.description)
    db.add(route)
    db.commit()
    db.refresh(route)
    return route


@app.put("/api/admin/routes/{route_id}")
def admin_update_route(
    route_id: int,
    data: RouteIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    route = db.get(Route, route_id)
    if route is None:
        raise HTTPException(status_code=404, detail="Not found")

    route.description = data.description
    db.commit()
    return route


@app.delete("/api/admin/routes/{route_id}")
def admin_delete_route(
    route_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    route = db.get(Route, route_id)
    if route is None:
        raise HTTPException(status_code=404, detail="Not found")

    db.delete(route)
    db.commit()
    return {"ok": True}


@app.put("/api/admin/tasks/{task_id}/priority")
async def admin_priority(
    task_id: int,
    data: AdminPriorityIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Not found")

    task.admin_priority = data.admin_priority
    db.commit()

    log(db, "admin_priority_changed", task.id, comment=str(data.admin_priority))
    await run_scheduler_once()

    return task_to_dict(db, task)


@app.get("/api/admin/agents")
def admin_agents(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return db.query(Agent).order_by(Agent.id).all()


@app.post("/api/admin/agents/{agent_id}/abort")
async def admin_abort(
    agent_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    agent = db.get(Agent, agent_id)

    if agent is None or agent.current_task_id is None:
        raise HTTPException(status_code=404, detail="No active task")

    task_id = agent.current_task_id
    await send_abort(agent_id, task_id)

    task = db.get(Task, task_id)
    if task is not None:
        task.status = "queued"
        task.assigned_agent_id = None
        task.preempt_cooldown_until = datetime.utcnow() + timedelta(seconds=30)

    agent.status = "online"
    agent.current_task_id = None

    db.commit()
    log(db, "admin_abort", task_id, agent_id, "Администратор прервал задачу")

    return {"ok": True}


@app.websocket("/ws/agent")
async def agent_ws(ws: WebSocket):
    await ws.accept()

    agent_id: str | None = None
    db = SessionLocal()

    try:
        while True:
            raw_message = await ws.receive_text()
            data = json.loads(raw_message)
            message_type = data.get("type")

            if message_type == "register":
                agent_id = data["agent_id"]
                agent_connections[agent_id] = ws

                agent = db.get(Agent, agent_id)
                if agent is None:
                    agent = Agent(id=agent_id)

                agent.status = "online"
                agent.current_task_id = None
                agent.last_heartbeat = datetime.utcnow()
                agent.connected_at = datetime.utcnow()

                db.merge(agent)
                db.commit()

                log(db, "agent_registered", agent_id=agent_id)

                await ws.send_text(
                    json.dumps(
                        {
                            "type": "registered",
                            "agent_id": agent_id,
                        }
                    )
                )
                await run_scheduler_once()
                continue

            if message_type == "heartbeat" and agent_id:
                agent = db.get(Agent, agent_id)
                if agent is not None:
                    agent.last_heartbeat = datetime.utcnow()
                    db.commit()
                continue

            if message_type == "task_result" and agent_id:
                await handle_task_result(db, agent_id, data)
                await run_scheduler_once()

    except WebSocketDisconnect:
        pass

    finally:
        if agent_id is not None:
            agent_connections.pop(agent_id, None)
        db.close()


async def handle_task_result(db: Session, agent_id: str, data: dict) -> None:
    task = db.get(Task, int(data["task_id"]))
    agent = db.get(Agent, agent_id)

    if task is None:
        return

    if task.status != "assigned" or task.assigned_agent_id != agent_id:
        return

    if data.get("success"):
        ticket = data.get("ticket", {})
        task.status = "completed"
        task.ticket_number = ticket.get("number")
        task.ticket_date = ticket.get("date")
        task.ticket_time = ticket.get("time")
        log(db, "task_completed", task.id, agent_id, f"Билет {task.ticket_number}")
    else:
        task.status = "failed"
        task.failure_reason = data.get("reason", "failed")
        log(db, "task_failed", task.id, agent_id, task.failure_reason)

    if agent is not None:
        agent.status = "online"
        agent.current_task_id = None

    db.commit()


def task_to_dict(db: Session, task: Task) -> dict:
    dates = task_dates(task)

    return {
        "id": task.id,
        "route_id": task.route_id,
        "route": task.route.description if task.route else None,
        "dates": dates,
        "passenger": f"{task.last_name} {task.first_name}",
        "email": task.email,
        "phone": task.phone,
        "user_priority": task.user_priority,
        "admin_priority": task.admin_priority,
        "status": task.status,
        "assigned_agent_id": task.assigned_agent_id,
        "ticket_number": task.ticket_number,
        "ticket_date": task.ticket_date,
        "ticket_time": task.ticket_time,
        "queue_position": queue_position(db, task.id),
        "date_load": {item: date_load(db, item, task.route_id) for item in dates},
        "created_at": task.created_at.isoformat(),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "routes": db.query(Route).order_by(Route.id).all(),
        },
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "routes": db.query(Route).order_by(Route.id).all(),
        },
    )
